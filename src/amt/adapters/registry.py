"""Agent 注册表与能力目录。

设计要点：**没实现的 Agent 也如实列出**（``source_supported=False``），
这样 `amt agents` 的输出不会让用户误以为工具支持所有 Agent；
能力不足的地方（例如 Cursor 缺少可靠的注入入口）同样如实标注。

已实现（Phase 1–4）：

    Source   codex / claude / cursor / workbuddy
    Target   claude / codex

后续 Agent 只需新增 Adapter 并在本文件注册，Core 无需改动。
"""

from __future__ import annotations

from typing import Type

from amt.adapters.base import (
    AdapterNotImplementedError,
    SourceAdapter,
    TargetAdapter,
)
from amt.context import AppContext
from amt.core.models import AgentInstallation

#: 路线图上尚未实现的 Agent（需求文档 §4.1）
PLANNED_AGENTS: dict[str, str] = {
    "mimo": "Mimo Code",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}


class AgentRegistry:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._sources: dict[str, Type[SourceAdapter]] = {}
        self._targets: dict[str, Type[TargetAdapter]] = {}

    # ------------------------------------------------------------------
    def register_source(self, agent: str, adapter: Type[SourceAdapter]) -> None:
        self._sources[agent] = adapter

    def register_target(self, agent: str, adapter: Type[TargetAdapter]) -> None:
        self._targets[agent] = adapter

    # ------------------------------------------------------------------
    def source(self, agent: str) -> SourceAdapter:
        adapter = self._sources.get(agent)
        if adapter is None:
            if agent in PLANNED_AGENTS:
                raise AdapterNotImplementedError(
                    f"{PLANNED_AGENTS[agent]} 的 Source Adapter 尚未实现（当前支持："
                    f"{', '.join(sorted(self._sources))}）"
                )
            raise AdapterNotImplementedError(f"未知的 Agent：{agent}")
        return adapter(self.ctx)

    def target(self, agent: str) -> TargetAdapter:
        adapter = self._targets.get(agent)
        if adapter is None:
            if agent in PLANNED_AGENTS:
                raise AdapterNotImplementedError(
                    f"{PLANNED_AGENTS[agent]} 的 Target Adapter 尚未实现（当前支持："
                    f"{', '.join(sorted(self._targets))}）"
                )
            raise AdapterNotImplementedError(
                f"{agent} 不支持作为迁移目标（缺少可靠的上下文注入入口）"
            )
        return adapter(self.ctx)

    # ------------------------------------------------------------------
    def has_source(self, agent: str) -> bool:
        return agent in self._sources

    def has_target(self, agent: str) -> bool:
        return agent in self._targets

    @property
    def source_agents(self) -> list[str]:
        return sorted(self._sources)

    @property
    def target_agents(self) -> list[str]:
        return sorted(self._targets)

    def migration_pairs(self) -> list[tuple[str, str]]:
        """枚举所有可用的 (source, target) 组合。

        因为采用 Canonical Memory 中间协议，组合数是
        ``len(sources) × len(targets)``，而不是 N×(N-1) 条手工映射
        —— 这正是「不做 N×N 转换」的直接收益。
        """
        return [(s, t) for s in self.source_agents for t in self.target_agents if s != t]

    # ------------------------------------------------------------------
    def installations(self) -> list[AgentInstallation]:
        """列出全部 Agent 的可用状态（含未实现的路线图 Agent）。

        探测**每个 Agent 只做一次**：既避免重复探测（部分探测有成本），
        也避免同一个 Adapter 的说明被追加两遍。
        """
        items: dict[str, AgentInstallation] = {}

        for agent in sorted(set(self._sources) | set(self._targets)):
            if self.has_source(agent):
                item = self._installation_of(agent, source=True)
                item.target_supported = self.has_target(agent)
            else:
                item = self._installation_of(agent, source=False)
                item.source_supported = False
                item.target_supported = True
            items[agent] = item

        for agent, display in PLANNED_AGENTS.items():
            if agent in items:
                continue
            items[agent] = AgentInstallation(
                agent=agent,
                display_name=display,
                source_supported=False,
                target_supported=False,
                notes=["尚未实现（架构已预留 Adapter 接口，新增适配器即可接入）"],
            )
        return [items[k] for k in sorted(items)]

    def _installation_of(self, agent: str, *, source: bool) -> AgentInstallation:
        try:
            adapter = self.source(agent) if source else self.target(agent)
        except Exception as exc:
            return AgentInstallation(agent=agent, display_name=agent, notes=[f"探测失败：{exc}"])

        detector = getattr(adapter, "detector", None)
        if detector is not None and hasattr(detector, "installation"):
            installation = detector.installation()
        else:
            detection = adapter.detect()
            installation = AgentInstallation(
                agent=agent,
                display_name=getattr(adapter, "display_name", agent),
                installed=detection.installed,
                runtime_available=detection.runtime_available,
                notes=[detection.detail],
            )
        if source:
            installation.source_supported = True
        else:
            installation.target_supported = True
        return installation


def default_registry(ctx: AppContext) -> AgentRegistry:
    """装配全部已实现的 Adapter。"""
    from amt.adapters.claude import ClaudeCodeSourceAdapter, ClaudeTargetAdapter
    from amt.adapters.codex import CodexSourceAdapter, CodexTargetAdapter
    from amt.adapters.cursor import CursorSourceAdapter
    from amt.adapters.workbuddy import WorkBuddySourceAdapter

    registry = AgentRegistry(ctx)
    registry.register_source("codex", CodexSourceAdapter)
    registry.register_source("claude", ClaudeCodeSourceAdapter)
    registry.register_source("cursor", CursorSourceAdapter)
    registry.register_source("workbuddy", WorkBuddySourceAdapter)
    registry.register_target("claude", ClaudeTargetAdapter)
    registry.register_target("codex", CodexTargetAdapter)
    return registry
