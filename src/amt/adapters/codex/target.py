"""CodexTargetAdapter —— 把 Canonical Memory 变成 Codex 的上下文。

Phase 2 的另一半：让 Codex 既能当来源（Phase 1）也能当目标，
与 Claude 形成双向闭环，验证 Canonical Memory 的对称性。
"""

from __future__ import annotations

from pathlib import Path

from amt.adapters.base import TargetAdapter
from amt.adapters.codex.detector import CodexDetector
from amt.adapters.codex.injector import CodexInjector
from amt.adapters.codex.launcher import CodexLauncher
from amt.adapters.codex.renderer import CodexContextRenderer
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


class CodexTargetAdapter(TargetAdapter):
    agent = "codex"
    display_name = "Codex"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        config = ctx.config
        self.detector = CodexDetector(ctx)
        # 与 Claude 侧共用同一个任务包目录（AMTConfig.memory_dir），
        # 这样在两个 Agent 之间来回迁移时复用的是同一份 memory package。
        memory_dir = config.memory_dir
        self.renderer = CodexContextRenderer(memory_dir, config.codex.agents_file)
        self.injector = CodexInjector(memory_dir, config.codex.agents_file)
        self.launcher = CodexLauncher(ctx)
        self._source_ref: SourceRef | None = None

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        detection = self.detector.detect()
        exe = self.detector.executable()
        if exe is None:
            return DetectionResult(
                agent="codex",
                installed=detection.installed,
                runtime_available=False,
                detail="未检测到 Codex CLI；AGENTS.md 与记忆文件仍会生成，需手动启动 Codex",
                evidence=detection.evidence,
            )
        return DetectionResult(
            agent="codex",
            installed=True,
            runtime_available=True,
            detail="已检测到 Codex CLI（AGENTS.md 会自动加载内联上下文）",
            evidence=detection.evidence + [f"可执行文件：{exe}"],
        )

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
