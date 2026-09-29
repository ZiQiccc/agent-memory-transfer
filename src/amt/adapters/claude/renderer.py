"""Claude 上下文渲染（技术架构 §34 / §44 / 实现plan §十六–§二十）。

策略 A（CLAUDE.md + @import）与策略 B（Initial Prompt）**同时使用**：
只依赖 Prompt 会把 20K tokens 的记忆塞进命令行，既浪费 token 又不利于人工查看；
只依赖文件则在某些场景下模型可能忽略它。二者结合最稳。

    Canonical Memory
        ↓
    .agent-transfer/memory.md      （人 + Agent 可读）
    .agent-transfer/memory.json    （机器可读）
    CLAUDE.md @import              （Claude Code 启动时自动加载）
        +
    Initial Prompt                 （明确的操作要求）
"""

from __future__ import annotations

from pathlib import Path

from amt.core.memory.renderer import render_initial_prompt, render_markdown
from amt.core.models import CanonicalMemory, MigrationOptions, TargetContext
from amt.services.filesystem import ProjectState

CLAUDE_MD_SECTION_TEMPLATE = """<!-- agent-memory-transfer:begin -->
# Agent Transfer Context

本项目存在一个**进行中的开发任务**，其上下文由 Agent Memory Transfer 维护。
开工前请先读取以下文件，再继续执行：

{memory_ref}

（该文件为机器生成，请勿手工编辑；若与实际代码状态冲突，以实际代码与 Git 状态为准。）
<!-- agent-memory-transfer:end -->
"""

#: 默认路径下的段落文本，供外部直接引用 / 断言
CLAUDE_MD_SECTION = CLAUDE_MD_SECTION_TEMPLATE.format(memory_ref="@.agent-transfer/memory.md")


class ClaudeContextRenderer:
    """Canonical Memory → Claude 可用的上下文对象（不写盘）。"""

    def __init__(self, memory_dir: str = ".agent-transfer", context_file: str = "CLAUDE.md") -> None:
        self.memory_dir = memory_dir.strip("/\\")
        self.context_file = context_file

    # ------------------------------------------------------------------
    def memory_relative_path(self) -> str:
        return f"{self.memory_dir}/memory.md"

    def memory_import_ref(self) -> str:
        return f"@{self.memory_relative_path()}"

    def render_memory_markdown(self, memory: CanonicalMemory) -> str:
        return render_markdown(memory)

    def render_memory_json(self, memory: CanonicalMemory) -> str:
        return memory.model_dump_json(indent=2)

    def render_initial_prompt(self, memory: CanonicalMemory) -> str:
        return render_initial_prompt(memory, memory_ref=self.memory_import_ref())

    def render_project_context(self, project: ProjectState) -> str:
        parts = [
            f"项目：{project.project_name or 'unknown'}",
            f"路径：{project.cwd or 'unknown'}",
            f"语言：{', '.join(project.languages) or 'unknown'}",
            f"框架：{', '.join(project.frameworks) or 'unknown'}",
        ]
        if project.git.branch:
            parts.append(f"Git 分支：{project.git.branch}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def render_claude_md(self, existing: str | None) -> str:
        """把 @import 段落幂等地写入 CLAUDE.md。

        - 已有标记段 → 原地替换（重复迁移不会累积）
        - 有 CLAUDE.md 但无标记 → 追加
        - 无 CLAUDE.md → 新建
        """
        begin = "<!-- agent-memory-transfer:begin -->"
        end = "<!-- agent-memory-transfer:end -->"
        # 段落文本必须按当前配置的 memory_dir 生成，否则改路径配置会失效
        section = CLAUDE_MD_SECTION_TEMPLATE.format(memory_ref=self.memory_import_ref())

        if existing and begin in existing and end in existing:
            head, _, rest = existing.partition(begin)
            _, _, tail = rest.partition(end)
            return f"{head}{section}{tail.lstrip(chr(10))}"

        if existing and existing.strip():
            return existing.rstrip() + "\n\n" + section

        return "# 项目说明\n\n" + section

    # ------------------------------------------------------------------
    def build(
        self,
        memory: CanonicalMemory,
        project: ProjectState,
        options: MigrationOptions,
        project_root: Path,
    ) -> TargetContext:
        memory_md = self.render_memory_markdown(memory)
        prompt = self.render_initial_prompt(memory)

        artifacts = [
            f"{self.memory_dir}/manifest.json",
            f"{self.memory_dir}/memory.json",
            f"{self.memory_dir}/memory.md",
            f"{self.memory_dir}/source.json",
            self.context_file,
        ]

        injection_plan = [
            f"写入 {self.memory_dir}/memory.md（Canonical Memory 的可读渲染）",
            f"写入 {self.memory_dir}/memory.json（协议原样，机器读取）",
            f"写入 {self.memory_dir}/manifest.json 与 source.json（元数据与来源，保证可追溯）",
            f"在 {self.context_file} 中写入 {self.memory_import_ref()} 引入段（幂等）",
            "以初始 Prompt 启动 Claude Code（auto_launch=True 时）",
        ]
        if options.redact_secrets:
            injection_plan.insert(0, "注入前完成敏感信息脱敏")

        return TargetContext(
            agent="claude",
            system_context=memory_md,
            project_context=self.render_project_context(project),
            task_context=prompt,
            memory_file=str(project_root / self.memory_relative_path()),
            launch_command=None,  # 由 Launcher 依 ShellResolver 构造
            working_directory=str(project_root),
            artifacts=artifacts,
            injection_plan=injection_plan,
        )
