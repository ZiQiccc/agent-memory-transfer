"""迁移状态机（技术架构 §27 / 需求文档 §22）。

正常链路（只能向前推进）::

    INIT → SOURCE_DETECTED → SESSION_LOADED → STATE_COLLECTED
         → MEMORY_EXTRACTED → MEMORY_VALIDATED → TARGET_PREPARED
         → CONTEXT_INJECTED → AGENT_STARTED → VERIFIED → COMPLETED

任意阶段可进入异常分支::

    ERROR → RETRY → （成功则继续 / 失败则 RECOVER）
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from amt.core.models import MigrationRecord, MigrationState, StepRecord

FORWARD_ORDER: tuple[MigrationState, ...] = (
    MigrationState.INIT,
    MigrationState.SOURCE_DETECTED,
    MigrationState.SESSION_LOADED,
    MigrationState.STATE_COLLECTED,
    MigrationState.MEMORY_EXTRACTED,
    MigrationState.MEMORY_VALIDATED,
    MigrationState.TARGET_PREPARED,
    MigrationState.CONTEXT_INJECTED,
    MigrationState.AGENT_STARTED,
    MigrationState.VERIFIED,
    MigrationState.COMPLETED,
)

_INDEX = {state: index for index, state in enumerate(FORWARD_ORDER)}
_EXCEPTION_STATES = {MigrationState.ERROR, MigrationState.RETRY, MigrationState.RECOVER}


class InvalidTransition(RuntimeError):
    pass


class MigrationStateMachine:
    """记录并约束迁移状态流转。"""

    def __init__(self, record: MigrationRecord) -> None:
        self.record = record
        self._entered: list[MigrationState] = [MigrationState.INIT]

    # ------------------------------------------------------------------
    @property
    def state(self) -> MigrationState:
        return self.record.state

    @property
    def history(self) -> list[MigrationState]:
        return list(self._entered)

    def can(self, target: MigrationState) -> bool:
        if target in _EXCEPTION_STATES:
            return True
        if self.state in _EXCEPTION_STATES:
            return True
        current = _INDEX.get(self.state, -1)
        wanted = _INDEX.get(target, -1)
        return wanted > current

    def to(self, target: MigrationState, detail: str = "") -> None:
        if not self.can(target):
            raise InvalidTransition(f"非法状态流转：{self.state.value} → {target.value}")
        self.record.state = target
        self._entered.append(target)
        if detail:
            step = self.record.step(target.value)
            if step is None:
                self.record.steps.append(
                    StepRecord(name=target.value, state=target, status="success", detail=detail)
                )

    # ------------------------------------------------------------------
    def fail(self, error: str) -> None:
        self.record.state = MigrationState.ERROR
        self._entered.append(MigrationState.ERROR)
        self.record.errors.append(error)

    def retry(self) -> None:
        self.record.state = MigrationState.RETRY
        self._entered.append(MigrationState.RETRY)

    def recover(self, note: str) -> None:
        self.record.state = MigrationState.RECOVER
        self._entered.append(MigrationState.RECOVER)
        self.record.warnings.append(note)

    # ------------------------------------------------------------------
    @contextmanager
    def step(self, name: str, state: MigrationState | None = None) -> Iterator[StepRecord]:
        """执行一个步骤并记录耗时；抛异常时标记失败并切到 ERROR。"""
        step = StepRecord(name=name, state=state, status="running")
        self.record.steps.append(step)
        started = time.monotonic()
        try:
            yield step
        except Exception as exc:
            step.status = "failed"
            step.error = str(exc)
            step.duration_ms = int((time.monotonic() - started) * 1000)
            self.fail(f"{name} 失败：{exc}")
            raise
        else:
            # 只在本步骤未被显式标记时判成功——步骤内部可以主动标为 skipped
            # （例如 --no-redact 跳过脱敏、用户跳过启动）。
            if step.status == "running":
                step.status = "success"
            step.duration_ms = int((time.monotonic() - started) * 1000)
            if state is not None and self.can(state):
                self.to(state)

    def finish(self, checked_at: datetime | None = None) -> None:
        self.record.finished_at = checked_at
        created = self.record.timestamp
        finished = self.record.finished_at
        if created and finished:
            self.record.duration_ms = int((finished - created).total_seconds() * 1000)
