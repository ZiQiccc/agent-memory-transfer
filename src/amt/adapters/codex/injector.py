"""Codex 上下文注入。

产出与 Claude 侧同构（可携带任务包），差异只在 Context 文件：

    <project>/
    ├── .agent-transfer/          manifest / memory.json / memory.md / source.json
    └── AGENTS.md                 内联核心上下文（Codex 不解析 @ 导入）

幂等性：重复迁移不会让 AGENTS.md 累积多个标记段。
"""

from __future__ import annotations

from pathlib import Path

from amt.adapters.codex.renderer import CodexContextRenderer
from amt.core.models import (
    CanonicalMemory,
    InjectionResult,
    PackageManifest,
    SourceRef,
)
from amt.services.filesystem import read_text_safe
from amt.utils import now_local


class CodexInjector:
    def __init__(self, memory_dir: str = ".agent-transfer", context_file: str = "AGENTS.md") -> None:
        self.renderer = CodexContextRenderer(memory_dir=memory_dir, context_file=context_file)
        self.memory_dir = self.renderer.memory_dir
        self.context_file = self.renderer.context_file

    # ------------------------------------------------------------------
    def inject(
        self,
        *,
        memory: CanonicalMemory,
        project_root: Path,
        source: SourceRef,
        target_agent: str = "codex",
    ) -> InjectionResult:
        artifacts: list[str] = []
        warnings: list[str] = []
        target_dir = project_root / self.memory_dir

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return InjectionResult(
                success=False,
                message=f"无法创建目录 {target_dir}：{exc}",
                warnings=["Memory 已保存在本地存储中，可手动复制到项目目录"],
            )

        try:
            memory_md_path = target_dir / "memory.md"
            memory_md_path.write_text(self.renderer.render_memory_markdown(memory), encoding="utf-8")
            artifacts.append(str(memory_md_path))

            memory_json_path = target_dir / "memory.json"
            memory_json_path.write_text(self.renderer.render_memory_json(memory), encoding="utf-8")
            artifacts.append(str(memory_json_path))

            manifest = PackageManifest(
                memory_id=memory.metadata.memory_id,
                source_agent=memory.metadata.source_agent,
                target_agent=target_agent,
                project=memory.project.name,
                project_path=memory.project.path,
                created_at=now_local(),
            )
            manifest_path = target_dir / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            artifacts.append(str(manifest_path))

            source_path = target_dir / "source.json"
            source_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")
            artifacts.append(str(source_path))
        except OSError as exc:
            return InjectionResult(
                success=False, artifacts=artifacts, message=f"写入记忆文件失败：{exc}", warnings=warnings
            )

        context_path = project_root / self.context_file
        existing = read_text_safe(context_path, limit=200_000)
        if context_path.exists() and existing is None:
            warnings.append(f"{self.context_file} 存在但无法以文本读取，已跳过修改（记忆文件已生成）")
        else:
            try:
                section = self.renderer.render_inline_section(memory)
                context_path.write_text(
                    self.renderer.render_agents_md(existing, section), encoding="utf-8"
                )
                artifacts.append(str(context_path))
            except OSError as exc:
                warnings.append(f"写入 {self.context_file} 失败：{exc}")

        return InjectionResult(
            success=True,
            artifacts=artifacts,
            message=f"已生成 {len(artifacts)} 个上下文文件；AGENTS.md 已内联核心上下文",
            warnings=warnings,
        )
