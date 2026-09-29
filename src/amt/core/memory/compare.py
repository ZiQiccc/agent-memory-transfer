"""记忆质量对比：确定性重建 vs LLM 归纳。

回答的问题很具体：**花 token 调 LLM，到底换来了什么？**

对比分两部分：

1. **语义字段差异** —— 目标 / 决策 / 失败尝试 / 未解决问题 / 下一步 等
   LLM 有权改写的字段，逐项列出两侧的产出与差异。
2. **事实字段不变量** —— 项目、Git、运行时、测试结果这些由程序读取的事实，
   在两条通道下必须**完全一致**。这一项是「LLM 不得覆盖事实」原则的
   可执行验证：一旦不一致，说明合并逻辑有 bug，而不是「模型发挥」。

注意：若使用的是 ``tools/mock_llm_server.py``，差异只反映**规则模拟器**与
启发式的差别，不代表真实模型的能力。报告中会显式标注这一点。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amt.core.memory.extractor import BuildOutcome
from amt.core.models import CanonicalMemory


class FieldDiff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    heuristic: str = ""
    llm: str = ""
    delta: str = ""
    note: str = ""


class MemoryComparison(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_agent: str = ""
    session_id: str | None = None
    project_path: str = ""

    heuristic: CanonicalMemory
    llm: CanonicalMemory

    semantic_fields: list[FieldDiff] = Field(default_factory=list)
    fact_fields: list[FieldDiff] = Field(default_factory=list)

    llm_available: bool = False
    llm_meta: dict[str, Any] = Field(default_factory=dict)
    heuristic_meta: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    provider_is_mock: bool = False

    @property
    def facts_are_invariant(self) -> bool:
        return all(d.delta == "一致" for d in self.fact_fields)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ----------------------------------------------------------------------
# 事实字段：必须两侧一致
# ----------------------------------------------------------------------


def _fact_snapshot(memory: CanonicalMemory) -> dict[str, str]:
    """事实字段快照。

    只取 ``source="program"``（程序确定性解析）的验证结果：LLM 补充的验证线索
    属于语义推断，若把它算进事实，这个自检就会与实现自相矛盾。
    """
    git = memory.git
    rt = memory.runtime
    program_tests = [t for t in memory.validation.tests if t.source == "program"]
    return {
        "project.path": memory.project.path,
        "project.language": "、".join(memory.project.language),
        "project.framework": "、".join(memory.project.framework),
        "git.branch": git.branch or "",
        "git.commit": git.commit or "",
        "git.changed_files": "、".join(sorted(git.changed_files)),
        "git.has_uncommitted_changes": str(git.has_uncommitted_changes),
        "runtime.working_directory": rt.working_directory,
        "runtime.recent_commands": "、".join(rt.recent_commands),
        "validation.tests（程序解析）": "、".join(
            f"{t.command}={t.status}" for t in program_tests
        ),
    }


# ----------------------------------------------------------------------
# 语义字段：允许不同
# ----------------------------------------------------------------------


def _semantic_snapshot(memory: CanonicalMemory) -> dict[str, str]:
    failed = memory.failed_attempts()
    return {
        "task.title": memory.task.title,
        "task.goal": memory.task.goal,
        "task.status": memory.task.status,
        "task.background": memory.task.background or "",
        "requirements（条）": str(len(memory.task.requirements)),
        "constraints（条）": str(len(memory.task.constraints)),
        "decisions（条）": str(len(memory.decisions)),
        "attempts（条）": str(len(memory.attempts)),
        "failed_attempts（条）": str(len(failed)),
        "attempts 带 lesson（条）": str(sum(1 for a in failed if a.lesson)),
        "implementation.completed（条）": str(len(memory.implementation.completed)),
        "modified_files（条）": str(len(memory.implementation.modified_files)),
        "unresolved（条）": str(len(memory.unresolved)),
        "next_actions（条）": str(len(memory.next_actions)),
        "risks（条）": str(len(memory.risks)),
        "validation.tests LLM 补充线索（条）": str(
            sum(1 for t in memory.validation.tests if t.source == "llm")
        ),
        "conversation.key_points（条）": str(len(memory.conversation.key_points)),
        "conversation.summary 字数": str(len(memory.conversation.summary)),
        "confidence": memory.task.confidence,
        "reconstructed_by": memory.task.reconstructed_by or "",
    }


def _describe(field: str, heuristic: str, llm: str) -> FieldDiff:
    if heuristic == llm:
        delta = "一致"
    elif field.endswith("（条）"):
        try:
            delta = f"{int(llm) - int(heuristic):+d}"
        except ValueError:
            delta = "不同"
    else:
        delta = "不同"
    return FieldDiff(field=field, heuristic=heuristic, llm=llm, delta=delta)


class MemoryComparer:
    """对同一会话的两份 BuildOutcome 做结构化对比。"""

    def compare(self, heuristic: BuildOutcome, llm: BuildOutcome) -> MemoryComparison:
        h_memory = heuristic.memory
        l_memory = llm.memory

        semantic: list[FieldDiff] = []
        h_snap = _semantic_snapshot(h_memory)
        l_snap = _semantic_snapshot(l_memory)
        for field in h_snap:
            diff = _describe(field, h_snap[field], l_snap.get(field, ""))
            if field == "task.goal" and diff.delta != "一致":
                diff.note = "LLM 通常能把目标写得比规则提取更凝练"
            if field.startswith("attempts 带 lesson") and diff.delta != "一致":
                diff.note = "失败经验越多，目标 Agent 越不容易重走老路"
            if field == "decisions（条）" and h_snap[field] == "0" and l_snap.get(field) != "0":
                diff.note = "启发式对决策的召回很低，这通常正是 LLM 的主要增益点"
            semantic.append(diff)

        facts: list[FieldDiff] = []
        h_facts = _fact_snapshot(h_memory)
        l_facts = _fact_snapshot(l_memory)
        for field in h_facts:
            diff = _describe(field, h_facts[field], l_facts.get(field, ""))
            if diff.delta != "一致":
                diff.note = "⚠ 事实字段不应被 LLM 改动——请检查合并逻辑"
            facts.append(diff)

        warnings = list(heuristic.warnings) + list(llm.warnings)
        llm_meta = dict(llm.llm_meta or {})
        provider_is_mock = "mock" in str(llm_meta.get("model", "")).lower()

        return MemoryComparison(
            source_agent=h_memory.metadata.source_agent,
            session_id=h_memory.metadata.source_session_id,
            project_path=h_memory.project.path,
            heuristic=h_memory,
            llm=l_memory,
            semantic_fields=semantic,
            fact_fields=facts,
            llm_available=bool(llm.llm_used),
            llm_meta=llm_meta,
            heuristic_meta={
                "events": h_memory.stats.get("total_events"),
                "digest_tokens_estimate": h_memory.stats.get("digest_tokens_estimate"),
                "compression": h_memory.stats.get("compression"),
            },
            warnings=warnings,
            provider_is_mock=provider_is_mock,
        )


# ----------------------------------------------------------------------
# 文本渲染（CLI / 报告共用）
# ----------------------------------------------------------------------


def render_comparison_markdown(comparison: MemoryComparison) -> str:
    lines: list[str] = ["# 记忆质量对比：启发式 vs LLM", ""]

    if comparison.provider_is_mock:
        lines += [
            "> ⚠ **本次使用的是 mock LLM（规则模拟器）**，差异只反映规则模拟与启发式的区别，",
            "> 不能代表真实模型的能力。要评估真实模型，请指向真实 OpenAI 兼容端点。",
            "",
        ]
    if not comparison.llm_available:
        lines += [
            "> ⚠ LLM 未生效，右侧数据实际仍是确定性重建结果。",
            f"> 原因：{comparison.llm_meta.get('reason', 'unknown')}",
            "",
        ]

    lines += [
        f"- 来源 Agent：{comparison.source_agent}",
        f"- 会话：`{comparison.session_id}`",
        f"- 项目：`{comparison.project_path}`",
        f"- LLM：{comparison.llm_meta.get('model', '-')}"
        f"（策略 {comparison.llm_meta.get('strategy', '-')}，"
        f"{comparison.llm_meta.get('duration_ms', '-')} ms，"
        f"{comparison.llm_meta.get('total_tokens', '-')} tokens）",
        "",
        "## 1. 语义字段对比",
        "",
        "| 字段 | 启发式 | LLM | 差异 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for diff in comparison.semantic_fields:
        lines.append(
            f"| {diff.field} | {_cell(diff.heuristic)} | {_cell(diff.llm)} "
            f"| {diff.delta} | {diff.note} |"
        )

    lines += [
        "",
        "## 2. 事实字段不变量校验（必须全部一致）",
        "",
        f"结论：**{'✅ 全部一致' if comparison.facts_are_invariant else '❌ 存在不一致，需排查合并逻辑'}**",
        "",
        "| 字段 | 启发式 | LLM | 判定 |",
        "| --- | --- | --- | --- |",
    ]
    for diff in comparison.fact_fields:
        lines.append(
            f"| {diff.field} | {_cell(diff.heuristic)} | {_cell(diff.llm)} | {diff.delta} |"
        )

    if comparison.warnings:
        lines += ["", "## 3. 警告", ""]
        for warning in comparison.warnings[:12]:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _cell(value: str, limit: int = 120) -> str:
    text = (value or "").replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."
