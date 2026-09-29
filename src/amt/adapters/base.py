"""Adapter 抽象接口（技术架构 §24–25）。

关键设计：**Source 与 Target 分离**。
一个 Agent 可以只有 Source（只能导出）、只有 Target（只能接收），或两者皆有。
未来支持「只允许导出的 Agent」不会破坏架构。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from amt.context import AppContext
from amt.core.models import (
    AgentEvent,
    CanonicalMemory,
    DetectionResult,
    InjectionResult,
    LaunchResult,
    MigrationOptions,
    RawSession,
    RuntimeContext,
    SessionInfo,
    TargetContext,
)
from amt.services.filesystem import ProjectState


class SourceAdapter(ABC):
    """从某个 Agent 获取真实上下文。"""

    agent: ClassVar[str] = ""
    display_name: ClassVar[str] = ""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ---- 能力探测 ----
    @abstractmethod
    def detect(self) -> DetectionResult:
        """该 Agent 是否可作数据源。"""

    # ---- 会话 ----
    @abstractmethod
    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        ...

    @abstractmethod
    def load_session(self, session_id: str) -> RawSession:
        """按 session_id 载入原始会话。找不到时必须抛 SessionNotFoundError。"""

    # ---- 事件 ----
    @abstractmethod
    def parse_events(self, raw: RawSession) -> list[AgentEvent]:
        """Agent 私有格式 → 统一 AgentEvent[]。

        边界：此方法**只做结构转换**，不做语义归纳、不生成 Memory。
        """

    # ---- 状态采集 ----
    def collect_project_state(self, cwd: str | None) -> ProjectState:
        return self.ctx.collector.collect(cwd)

    def collect_runtime_state(self, cwd: str | None, recent_commands: list[str] | None = None) -> RuntimeContext:
        import os
        import platform

        return RuntimeContext(
            working_directory=str(cwd or ""),
            operating_system=f"{platform.system()} {platform.release()}",
            shell=self.ctx.shell.detect().type.value,
            environment_summary={
                "python": platform.python_version(),
                "home": os.path.expanduser("~"),
            },
            recent_commands=list(recent_commands or [])[:20],
        )


class TargetAdapter(ABC):
    """让目标 Agent 接收到 Canonical Memory。"""

    agent: ClassVar[str] = ""
    display_name: ClassVar[str] = ""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def detect(self) -> DetectionResult:
        ...

    @abstractmethod
    def prepare_context(
        self,
        memory: CanonicalMemory,
        project: ProjectState,
        options: MigrationOptions,
    ) -> TargetContext:
        """生成 Canonical Memory → 目标 Agent 的中间上下文对象。"""

    @abstractmethod
    def inject(self, context: TargetContext, memory: CanonicalMemory) -> InjectionResult:
        """写入目标 Agent 认得的上下文文件。

        ``memory`` 显式传入而不挂在 ``context`` 上，是为了让 TargetContext
        保持「纯渲染产物」的语义（只含字符串与路径，可安全序列化）。
        """

    @abstractmethod
    def launch(self, context: TargetContext) -> LaunchResult:
        """启动目标 Agent。未安装时须降级返回，不得抛异常。"""


class SessionNotFoundError(LookupError):
    """会话不存在。"""


class AdapterNotImplementedError(NotImplementedError):
    """该 Agent 尚未实现对应 Adapter。"""
