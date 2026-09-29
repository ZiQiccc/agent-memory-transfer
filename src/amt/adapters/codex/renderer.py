"""Codex 上下文渲染。

**与 Claude 最关键的差异**：Claude Code 支持 ``@path`` 导入，因此可以在
``CLAUDE.md`` 里只放一行引用；而 **Codex 不解析 ``@`` 导入**，
``AGENTS.md`` 是纯文本指令。

所以 Codex 侧必须：

1. 把**核心上下文内联**进 ``AGENTS.md``（受长度约束，只放最高价值字段）；
2. 同时把完整记忆写到 ``.agent-transfer/memory.md``，并在内联段落里**明确告知路径**，
   让 Codex 需要细节时自己去读。

如果把上下文写成「请读取 xxx.md」了事，Codex 很可能不读——这就是为什么要内联。
内联长度必须有上限：``AGENTS.md`` 会在**每个会话**被加载，膨胀会持续消耗预算。
"""

from __future__ import annotations

from pathlib import Path

from amt.core.memory.renderer import render_initial_prompt, render_markdown
from amt.core.models import CanonicalMemory, MigrationOptions, TargetContext
from amt.services.filesystem import ProjectState

AGENTS_MD_BEGIN = "<!-- agent-memory-transfer:begin -->"
AGENTS_MD_END = "<!-- agent-memory-transfer:end -->"

#: 内联段落的最大字符数。超出时按优先级裁剪（先砍已完成，再砍决策）。
INLINE_BUDGET = 2600


class CodexContextRenderer:
    def __init__(self, memory_dir: str = ".agent-transfer", context_file: str = "AGENTS.md") -> None:
        self.memory_dir = memory_dir.strip("/\\")
        self.context_file = context_file

    # ------------------------------------------------------------------
    def memory_relative_path(self) -> str:
        return f"{self.memory_dir}/memory.md"

    def render_memory_markdown(self, memory: CanonicalMemory) -> str:
        return render_markdown(memory)

    def render_memory_json(self, memory: CanonicalMemory) -> str:
        return memory.model_dump_json(indent=2)

    def render_initial_prompt(self, memory: CanonicalMemory) -> str:
        return render_initial_prompt(memory, memory_ref=self.memory_relative_path())

    # ------------------------------------------------------------------
    def render_inline_section(self, memory: CanonicalMemory) -> str:
        """生成写入 AGENTS.md 的内联上下文段落。

        优先级：任务 → 失败方案 → 未解决 → 下一步 → 已完成 → 决策。
        失败方案优先级高于已完成工作 —— 重复已完成的活只是浪费，
        而重走已失败的路会直接把任务带偏。
        """
        blocks: list[tuple[str, list[str]]] = []

        task_lines = [
            f"- 目标：{_clip(memory.task.goal, 300)}",
            f"- 状态：{memory.task.status}",
        ]
        if memory.project.path:
            task_lines.append(f"- 项目：{memory.project.path}")
        blocks.append(("任务", task_lines))

        failed = memory.failed_attempts()
        if failed:
            lines = []
            for attempt in failed[:5]:
                detail = _clip(attempt.error or attempt.result, 140)
                lines.append(f"- ❌ {_clip(attempt.action, 130)}" + (f" → {detail}" if detail else ""))
            blocks.append(("已尝试且失败的方案（不要重复）", lines))

        if memory.unresolved:
            lines = [f"- {_clip(i.description, 160)}" for i in memory.unresolved[:4]]
            blocks.append(("当前未解决的问题", lines))

        actions = memory.open_actions()
        if actions:
            lines = [f"- {_clip(a.action, 170)}" for a in actions[:4]]
            blocks.append(("下一步", lines))

        if memory.implementation.completed:
            lines = [f"- {_clip(item, 150)}" for item in memory.implementation.completed[:4]]
            blocks.append(("已完成", lines))

        if memory.decisions:
            lines = [f"- {_clip(d.decision, 150)}" for d in memory.decisions[:3]]
            blocks.append(("已确定的方案（不要重新讨论）", lines))

        # 按优先级装配，超预算就丢掉优先级最低的块
        header = [
            AGENTS_MD_BEGIN,
            "# 接续任务上下文（由 Agent Memory Transfer 生成）",
            "",
            "本项目存在一个**进行中的开发任务**。以下是从上一个 Agent 的会话恢复出的状态，",
            "**不要重新分析已经完成的工作，也不要重复已经失败的方案**。",
            "",
        ]
        footer = [
            "",
            f"完整记忆（含失败明细、验证结果、Git 状态）见 `{self.memory_relative_path()}`，需要细节时请自行读取。",
            "",
            "如本段描述与当前代码或 Git 状态冲突，**以真实文件与 Git 为准**。",
            AGENTS_MD_END,
        ]

        assembled = list(header)
        used = len("\n".join(header + footer))
        for title, lines in blocks:
            chunk = [f"## {title}", "", *lines, ""]
            size = len("\n".join(chunk))
            if used + size > INLINE_BUDGET:
                continue
            assembled.extend(chunk)
            used += size
        assembled.extend(footer)
        return "\n".join(assembled)

    # ------------------------------------------------------------------
    def render_agents_md(self, existing: str | None, section: str) -> str:
        """幂等地把内联段落写入 AGENTS.md。"""
        if existing and AGENTS_MD_BEGIN in existing and AGENTS_MD_END in existing:
            head, _, rest = existing.partition(AGENTS_MD_BEGIN)
            _, _, tail = rest.partition(AGENTS_MD_END)
            return f"{head}{section}{tail.lstrip(chr(10))}"
        if existing and existing.strip():
            return existing.rstrip() + "\n\n" + section + "\n"
        return section + "\n"

    # ------------------------------------------------------------------
    def render_project_context(self, project: ProjectState) -> str:
        parts = [
            f"项目：{project.project_name or 'unknown'}",
            f"路径：{project.cwd or 'unknown'}",
            f"语言：{', '.join(project.languages) or 'unknown'}",
        ]
        if project.git.branch:
            parts.append(f"Git 分支：{project.git.branch}")
        return "\n".join(parts)

    def build(
        self,
        memory: CanonicalMemory,
        project: ProjectState,
        options: MigrationOptions,
        project_root: Path,
    ) -> TargetContext:
        inline = self.render_inline_section(memory)
        artifacts = [
            f"{self.memory_dir}/manifest.json",
            f"{self.memory_dir}/memory.json",
            f"{self.memory_dir}/memory.md",
            f"{self.memory_dir}/source.json",
            self.context_file,
        ]
        injection_plan = [
            f"写入 {self.memory_dir}/memory.md（完整 Canonical Memory）",
            f"写入 {self.memory_dir}/memory.json（协议原样，机器读取）",
            f"写入 {self.memory_dir}/manifest.json 与 source.json（元数据与来源）",
            f"把核心上下文**内联**写入 {self.context_file} 的标记段（Codex 不支持 @ 导入，必须内联）",
            "以初始 Prompt 启动 Codex CLI（auto_launch=True 时）",
        ]
        return TargetContext(
            agent="codex",
            system_context=inline,
            project_context=self.render_project_context(project),
            task_context=self.render_initial_prompt(memory),
            memory_file=str(project_root / self.memory_relative_path()),
            launch_command=None,
            working_directory=str(project_root),
            artifacts=artifacts,
            injection_plan=injection_plan,
        )


def _clip(text: str | None, limit: int) -> str:
    value = (text or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"
