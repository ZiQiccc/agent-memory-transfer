"""WorkBuddy 探测。

会话位置（本机实测）::

    ~/.workbuddy/projects/<escaped-workspace>/<session-uuid>.jsonl

**关于 Target 的诚实说明**：WorkBuddy 的工作区记忆文件
（``<workspace>/.workbuddy/memory/MEMORY.md``）会被注入到会话上下文，
因此「把任务上下文写进去」在原理上可行；但本实现**无法确认**该文件是否
在所有场景下都被自动加载，也无法确认追加内容是否会影响用户自己的记忆笔记。

按本项目的原则（宁可不支持，也不提供假的能力），WorkBuddy 目前
**只标注为 Source**；Target 留待确认注入机制后再实现。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext
from amt.core.models import AgentInstallation, DetectionResult


class WorkBuddyDetector:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    def projects_dir(self) -> Path:
        return self.ctx.config.workbuddy.resolved_projects_dir()

    def session_files(self) -> list[Path]:
        projects = self.projects_dir()
        if not projects.is_dir():
            return []
        return [p for p in projects.rglob("*.jsonl") if p.is_file()]

    def workspaces(self) -> list[Path]:
        projects = self.projects_dir()
        if not projects.is_dir():
            return []
        return [p for p in projects.iterdir() if p.is_dir()]

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        projects = self.projects_dir()
        files = self.session_files()
        evidence: list[str] = []
        if projects.is_dir():
            evidence.append(f"会话目录：{projects}")
            evidence.append(f"工作区：{len(self.workspaces())} 个")
        if files:
            evidence.append(f"会话文件：{len(files)} 个")

        installed = projects.is_dir()
        runtime_available = bool(files)
        if not installed:
            detail = f"未找到 WorkBuddy 会话目录：{projects}"
        elif not runtime_available:
            detail = "会话目录存在但没有任何 .jsonl 会话文件"
        else:
            detail = f"就绪：可读取 {len(files)} 个 WorkBuddy 会话文件"

        return DetectionResult(
            agent="workbuddy",
            installed=installed,
            runtime_available=runtime_available,
            detail=detail,
            evidence=evidence,
        )

    def installation(self) -> AgentInstallation:
        detection = self.detect()
        notes = [
            "来源支持：读取 ~/.workbuddy/projects/**/*.jsonl（message / reasoning / "
            "function_call / function_call_result）",
            "用户需求位于 <user_query> 标签内；同一条消息里还混有 harness 注入的 "
            "<system-reminder>，因此按标签抽取而不是整条丢弃",
            "**暂不支持作为迁移目标**：工作区记忆文件是否被自动加载尚未验证，"
            "不提供未经确认的注入能力",
        ]
        if not detection.runtime_available:
            notes.append("当前没有可读会话")
        return AgentInstallation(
            agent="workbuddy",
            display_name="WorkBuddy",
            installed=detection.installed,
            runtime_available=detection.runtime_available,
            home_dir=str(self.projects_dir().parent),
            data_dir=str(self.projects_dir()),
            source_supported=True,
            target_supported=False,
            notes=notes,
        )
