"""Compressor（技术架构 §19 的 Compress 环节 / 需求文档 §8.3）。

原则：**保留对任务执行有价值的信息，而不是机械复制历史对话。**

原始会话动辄 100K tokens（本机最大会话 5.3MB），不能整体塞给目标 Agent 或 LLM。
本模块做三件事：
1. 按类型裁剪与聚合（文件读取这类高频低信息量事件聚合成一条）
2. 失败事件**永不裁剪**（Negative Knowledge 是核心价值）
3. 生成紧凑时间线 digest，并做 token 预算控制
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from amt.core.memory.normalizer import NormalizedEvent, first_error_line
from amt.core.models import EventType
from amt.utils import estimate_tokens, first_line, truncate

DEFAULT_MAX_TOKENS = 12_000

#: 各类型保留上限（失败事件不受此限制）
_KEEP_LIMITS: dict[str, int] = {
    "user_message": 20,
    "assistant_message": 25,
    "reasoning": 6,
    "meta": 1,
}
_HIGH_VOLUME = {"terminal", "build", "test", "tool_call", "tool_result", "file_edit"}
_HIGH_VOLUME_LIMIT = 150
_ERROR_LIMIT = 20


class CompressedBundle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[NormalizedEvent] = Field(default_factory=list)
    digest: str = ""
    stats: dict[str, int] = Field(default_factory=dict)
    estimated_tokens: int = 0
    truncated: bool = False


class Compressor:
    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    def compress(self, events: list[NormalizedEvent], max_tokens: int | None = None) -> CompressedBundle:
        budget = max_tokens or self.max_tokens
        stats: dict[str, int] = {"input_events": len(events)}
        dropped: dict[str, int] = {}

        # 1) 聚合高频低信息量事件
        events, aggregated = self._aggregate_file_reads(events)
        stats["_file_read_aggregated"] = aggregated

        # 2) 按类型裁剪（失败事件无条件保留）
        kept: list[NormalizedEvent] = []
        per_type: dict[str, int] = {}
        for event in events:
            if event.is_failure or event.type is EventType.ERROR:
                kept.append(event)
                continue
            key = event.category or event.type.value
            limit = _KEEP_LIMITS.get(key)
            if limit is None and key in _HIGH_VOLUME:
                limit = _HIGH_VOLUME_LIMIT
            if limit is not None:
                per_type[key] = per_type.get(key, 0) + 1
                if per_type[key] > limit:
                    dropped[key] = dropped.get(key, 0) + 1
                    continue
            kept.append(event)

        # 3) 构建 digest 并做 token 预算控制
        digest = self._render_digest(kept)
        truncated = False
        if estimate_tokens(digest) > budget:
            kept = self._shrink_until_fits(kept, budget, dropped)
            digest = self._render_digest(kept)
            truncated = True

        stats["kept_events"] = len(kept)
        stats["dropped"] = sum(dropped.values())
        for key, value in dropped.items():
            stats[f"dropped_{key}"] = value

        return CompressedBundle(
            events=kept,
            digest=digest,
            stats=stats,
            estimated_tokens=estimate_tokens(digest),
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate_file_reads(events: list[NormalizedEvent]) -> tuple[list[NormalizedEvent], int]:
        """把大量 file_read 聚合成一条摘要，避免淹没真正有价值的操作。"""
        reads = [e for e in events if e.category == "file_read"]
        if len(reads) <= 3:
            return events, 0

        paths: list[str] = []
        for event in reads:
            for path in event.paths:
                if path not in paths:
                    paths.append(path)

        summary_event = normalize_copy(
            reads[-1],
            summary=(
                f"共读取/检索 {len(reads)} 次"
                + (f"，涉及文件：{'、'.join(paths[:8])}" if paths else "")
                + (f" 等 {len(paths)} 个" if len(paths) > 8 else "")
            ),
            content="",
            result="",
            significance=1,
        )
        out = [e for e in events if e.category != "file_read"]
        insert_at = 0
        for idx, event in enumerate(out):
            if event.timestamp <= reads[-1].timestamp:
                insert_at = idx + 1
        out.insert(insert_at, summary_event)
        return out, len(reads)

    # ------------------------------------------------------------------
    def _shrink_until_fits(
        self,
        events: list[NormalizedEvent],
        budget: int,
        dropped: dict[str, int],
    ) -> list[NormalizedEvent]:
        """超预算时按「显著性升序 + 时间顺序」丢弃可丢事件。"""
        protected = {
            EventType.USER_MESSAGE,
            EventType.ERROR,
        }
        droppable = [
            (idx, e)
            for idx, e in enumerate(events)
            if not e.is_failure and e.type not in protected and e.significance <= 1
        ]
        drop_indices = {idx for idx, _ in droppable}
        kept = [e for idx, e in enumerate(events) if idx not in drop_indices]
        dropped["low_significance"] = dropped.get("low_significance", 0) + len(drop_indices)

        # 仍超预算：再丢显著性 2 的非失败事件
        if estimate_tokens(self._render_digest(kept)) > budget:
            droppable2 = {
                idx
                for idx, e in enumerate(kept)
                if not e.is_failure and e.significance <= 2 and e.type is not EventType.USER_MESSAGE
            }
            kept = [e for idx, e in enumerate(kept) if idx not in droppable2]
            dropped["medium_significance"] = dropped.get("medium_significance", 0) + len(droppable2)
        return kept

    # ------------------------------------------------------------------
    def _render_digest(self, events: list[NormalizedEvent]) -> str:
        """渲染为紧凑事件时间线（供 LLM 提取与人工核对）。"""
        if not events:
            return "（无事件）"

        lines: list[str] = []
        for event in events:
            stamp = event.timestamp.strftime("%m-%d %H:%M") if event.timestamp else "--"
            tag = (event.category or event.type.value).upper()
            body = event.summary or first_line(event.content, 120) or ""
            mark = "  ⚠失败" if event.is_failure else ""
            lines.append(f"[{stamp}] {tag:<14} {truncate(body, 200)}{mark}")

            # 失败事件附带真正的报错行——这是目标 Agent 最需要的信息
            if event.is_failure:
                err = first_error_line(event.result or event.content, 240)
                if err:
                    lines.append(f"           └─ {err}")
            elif event.type is EventType.TEST and event.result:
                excerpt = first_line(event.result, 160)
                if excerpt:
                    lines.append(f"           └─ {excerpt}")
            elif event.type is EventType.USER_MESSAGE and event.content:
                lines.append(f"           └─ {truncate(event.content, 500)}")

        return "\n".join(lines)


def normalize_copy(event: NormalizedEvent, **overrides) -> NormalizedEvent:
    """基于已有事件复制一份并覆盖字段（保持 id 唯一）。"""
    data = event.model_dump()
    data.update(overrides)
    data["id"] = f"{event.id}#agg"
    return NormalizedEvent.model_validate(data)
