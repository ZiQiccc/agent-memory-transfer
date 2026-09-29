"""ClaudeTargetAdapter —— 把 Canonical Memory 变成 Claude Code 的上下文。"""

from __future__ import annotations

from pathlib import Path

from amt.adapters.base import TargetAdapter
from amt.adapters.claude.detector import ClaudeDetector
from amt.adapters.claude.injector import ClaudeInjector
from amt.adapters.claude.launcher import ClaudeLauncher
from amt.adapters.claude.renderer import ClaudeContextRenderer
from amt.context import AppContext
from amt.core.models import (
    CanonicalMemory,
    DetectionResult,
    InjectionResult,
    LaunchResult,
    MigrationOptions,
    SourceRef,
    TargetContext,
)
from amt.services.filesystem import ProjectState


class ClaudeTargetAdapter(TargetAdapter):
    agent = "claude"
    display_name = "Claude Code"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        config = ctx.config
        self.detector = ClaudeDetector(ctx)
        # 与 Codex 侧共用同一个任务包目录（AMTConfig.memory_dir）
        self.renderer = ClaudeContextRenderer(config.memory_dir, config.claude.context_file)
        self.injector = ClaudeInjector(config.memory_dir, config.claude.context_file)
        self.launcher = ClaudeLauncher(ctx)
        self._source_ref: SourceRef | None = None

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        return self.detector.detect()

    def prepare_context(
        self,
        memory: CanonicalMemory,
        project: ProjectState,
        options: MigrationOptions,
    ) -> TargetContext:
        root = Path(memory.project.path or project.cwd or Path.cwd())
        context = self.renderer.build(memory, project, options, root)
        context.launch_command = self.launcher.build_command(root, context.task_context)
        return context

    def bind_source(self, source: SourceRef) -> None:
        """记录来源，注入时写入 source.json（保证记忆可追溯）。"""
        self._source_ref = source

    def inject(self, context: TargetContext, memory: CanonicalMemory) -> InjectionResult:
        source = self._source_ref or SourceRef(
            agent=memory.metadata.source_agent,
            session_id=memory.metadata.source_session_id,
        )
        return self.injector.inject(
            memory=memory,
            project_root=Path(context.working_directory),
            source=source,
            target_agent=self.agent,
        )

    def launch(self, context: TargetContext, *, auto_launch: bool = True) -> LaunchResult:
        return self.launcher.launch(context, auto_launch=auto_launch)

    # ------------------------------------------------------------------
    @property
    def cli_available(self) -> bool:
        return self.launcher.executable() is not None

    def version(self) -> str | None:
        return self.launcher.version()
