"""Context Rendering（技术架构 §34 / §44）。

Canonical Memory 是唯一的协议对象；不同目标 Agent 可以有不同 Render Strategy。
本模块提供通用渲染器：

    Canonical Memory → memory.json   机器读取（协议原样）
    Canonical Memory → memory.md     人与 Agent 阅读
    Canonical Memory → Initial Prompt 启动目标 Agent 的第一句话
"""

from __future__ import annotations

from amt.core.models import CanonicalMemory
from amt.utils import now_local, truncate

_PRIORITY_LABEL = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
_STATUS_LABEL = {
    "pending": "待开始",
    "in_progress": "进行中",
    "blocked": "受阻",
    "completed": "已完成",
}
_CONFIDENCE_LABEL = {"unknown": "未知", "low": "低", "medium": "中", "high": "高"}


def render_json(memory: CanonicalMemory) -> str:
    return memory.model_dump_json(indent=2)


def render_markdown(memory: CanonicalMemory) -> str:
    """渲染 memory.md。

    设计目标：目标 Agent 读完这一份就能明白「任务是什么、做到哪了、
    哪些路走不通、下一步做什么」，无需重新分析已完成的工作。
    """
    lines: list[str] = []
    add = lines.append

    # 章节编号必须**动态生成**：某些章节在无内容时会被整节省略
    # （例如无风险时不输出「风险提示」），硬编码编号会导致断号。
    counter = {"n": 0}

    def section(title: str) -> str:
        counter["n"] += 1
        return f"## {counter['n']}. {title}"

    add("# 任务记忆（Canonical Memory）")
    add("")
    add(
        "> 本文件由 **Agent Memory Transfer** 生成，用于让接续的 Agent 直接恢复任务状态，"
        "不必重新分析已完成的工作。"
    )
    add("")
    add("## 0. 元信息")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add(f"| memory_id | `{memory.metadata.memory_id}` |")
    add(f"| 来源 Agent | {memory.metadata.source_agent} |")
    add(f"| 来源会话 | `{memory.metadata.source_session_id or 'unknown'}` |")
    add(f"| 协议版本 | {memory.metadata.schema_version} |")
    add(f"| 生成时间 | {now_local().strftime('%Y-%m-%d %H:%M:%S %z')} |")
    add(f"| 重建方式 | {memory.task.reconstructed_by or 'unknown'} |")
    add(f"| 置信度 | {_CONFIDENCE_LABEL.get(memory.task.confidence, memory.task.confidence)} |")
    add("")

    # ---- 任务 ----
    add(section("任务"))
    add("")
    add(f"- **标题**：{memory.task.title or 'unknown'}")
    add(f"- **状态**：{_STATUS_LABEL.get(memory.task.status, memory.task.status)}")
    add(f"- **目标**：{memory.task.goal or 'unknown'}")
    if memory.task.background:
        add(f"- **背景**：{memory.task.background}")
    add("")
    _bullet_section(add, "需求", memory.task.requirements)
    _bullet_section(add, "约束", memory.task.constraints)

    # ---- 已完成 ----
    add(section("已完成的工作"))
    add("")
    _bullet_section(add, None, memory.implementation.completed, empty="（未从执行记录中识别到已完成动作）")

    if memory.implementation.modified_files:
        add("### 已修改文件")
        add("")
        add("| 文件 | 状态 | 说明 |")
        add("| --- | --- | --- |")
        for file in memory.implementation.modified_files:
            status = {"modified": "修改", "added": "新增", "deleted": "删除"}.get(file.status, file.status)
            add(f"| `{file.path}` | {status} | {file.summary or '-'} |")
        add("")
    if memory.implementation.created_files:
        _bullet_section(add, "新增文件", memory.implementation.created_files)
    if memory.implementation.deleted_files:
        _bullet_section(add, "删除文件", memory.implementation.deleted_files)

    # ---- 决策 ----
    add(section("已做出的关键决策"))
    add("")
    if memory.decisions:
        add("> 以下方案已经确定，**不要重新讨论**。")
        add("")
        for decision in memory.decisions:
            add(f"- **{decision.decision}**")
            if decision.reason:
                add(f"  - 原因：{decision.reason}")
            if decision.alternatives:
                add(f"  - 已排除的备选：{'、'.join(decision.alternatives)}")
    else:
        add("（会话中未提取到明确的技术决策）")
    add("")

    # ---- 失败尝试（核心） ----
    add(section("已尝试但失败的方案 ⚠"))
    add("")
    failed = memory.failed_attempts()
    if failed:
        add("> **这些方案已经试过并且失败，不要重复执行。**")
        add("")
        add("| # | 动作 | 结果 | 报错 | 经验 |")
        add("| --- | --- | --- | --- | --- |")
        for index, attempt in enumerate(failed, start=1):
            error = (attempt.error or "-").replace("\n", " ")
            lesson = (attempt.lesson or "-").replace("\n", " ")
            add(
                f"| {index} | {_cell(attempt.action)} | {_cell(attempt.result)} "
                f"| {_cell(error)} | {_cell(lesson)} |"
            )
    else:
        add("（未检测到失败尝试）")
    add("")

    # ---- 验证 ----
    add(section("验证结果"))
    add("")
    if memory.validation.tests:
        add("| 命令 | 结果 | 来源 | 输出摘要 |")
        add("| --- | --- | --- | --- |")
        for test in memory.validation.tests:
            label = {"passed": "✅ 通过", "failed": "❌ 失败", "skipped": "⏭ 跳过"}.get(
                test.status, test.status
            )
            # 来源必须显式标注：程序解析出来的是事实，LLM 归纳的只是线索。
            origin = "程序解析" if test.source == "program" else "⚠ LLM 归纳（未经程序校验）"
            add(
                f"| `{_cell(test.command)}` | {label} | {origin} "
                f"| {_cell(test.output_summary or '-')} |"
            )
        add("")
    else:
        add("- 测试：未检测到测试命令执行记录")
    if memory.validation.build:
        add(f"- 构建：{memory.validation.build.status}（`{memory.validation.build.command or 'unknown'}`）")
    else:
        add("- 构建：未检测到构建命令")
    if memory.validation.lint:
        add(f"- 静态检查：{'、'.join(f'`{c}`' for c in memory.validation.lint[:5])}")
    add("")

    # ---- 未解决 ----
    add(section("当前未解决的问题"))
    add("")
    if memory.unresolved:
        for issue in memory.unresolved:
            add(f"- **[{_PRIORITY_LABEL.get(issue.priority, issue.priority)}] {issue.description}**")
            if issue.suspected_cause:
                add(f"  - 推测原因：{issue.suspected_cause}")
            if issue.context:
                add(f"  - 上下文：`{issue.context}`")
    else:
        add("（无已知未解决问题）")
    add("")

    # ---- 下一步 ----
    add(section("下一步行动"))
    add("")
    open_actions = memory.open_actions()
    if open_actions:
        for action in open_actions:
            add(f"{action.priority}. {action.action}")
            if action.reason:
                add(f"   - 依据：{action.reason}")
    else:
        add("（无待办，请先与用户确认任务是否已闭环）")
    add("")

    # ---- 风险 ----
    if memory.risks:
        add(section("风险提示"))
        add("")
        for risk in memory.risks:
            add(f"- **{risk.description}**")
            if risk.impact:
                add(f"  - 影响：{risk.impact}")
            if risk.mitigation:
                add(f"  - 建议：{risk.mitigation}")
        add("")

    # ---- Git ----
    add(section("Git 状态"))
    add("")
    git = memory.git
    if git.branch or git.status_summary:
        add(f"- 分支：`{git.branch or 'unknown'}`　HEAD：`{git.commit or 'unknown'}`")
        add(f"- 状态：{git.status_summary or 'unknown'}")
        if git.changed_files:
            add(f"- 改动文件（{len(git.changed_files)}）：" + "、".join(f"`{p}`" for p in git.changed_files[:12]))
        if git.diff_summary:
            add("")
            add("<details><summary>git diff --stat</summary>")
            add("")
            add("```")
            add(truncate(git.diff_summary, 3000))
            add("```")
            add("")
            add("</details>")
    else:
        add("（无 Git 信息）")
    add("")
    add("> 说明：Memory 只描述状态，**真实代码以文件系统为准，真实变更以 Git 为准**。")
    add("> 本工具默认不自动 commit，Working Tree 保持原样。")
    add("")

    # ---- 运行环境 ----
    rt = memory.runtime
    if rt.working_directory:
        add(section("运行环境"))
        add("")
        add(f"- 工作目录：`{rt.working_directory}`")
        add(f"- 操作系统：{rt.operating_system or 'unknown'}")
        add(f"- Shell：{rt.shell or 'unknown'}")
        if rt.recent_commands:
            add("- 最近执行的命令：")
            for command in rt.recent_commands[-8:]:
                add(f"  - `{truncate(command, 160)}`")
        add("")

    # ---- 冲突处理 ----
    add(section("冲突处理原则"))
    add("")
    add("如本文件描述与当前项目实际状态冲突，**以真实状态为准**，优先级为：")
    add("")
    add("```text")
    add("真实文件系统 > Git 状态 > 运行时状态 > 本 Memory > 会话摘要")
    add("```")
    add("")

    return "\n".join(lines)


PROMPT_OPENING = "你正在继续一个已经进行中的开发任务。"
PROMPT_GOAL_HEADER = "## 任务目标"
PROMPT_REQUIREMENT_HEADER = "## 执行要求"


def render_initial_prompt(memory: CanonicalMemory, memory_ref: str = "@.agent-transfer/memory.md") -> str:
    """技术架构 §44 —— 目标 Agent 的第一条 Prompt。

    只给指令，不塞记忆本体：Memory 是文件，Prompt 是操作要求。
    """
    goal = truncate(memory.task.goal, 600)
    return f"""{PROMPT_OPENING}

请先读取 {memory_ref}，理解任务当前状态，然后继续执行。

{PROMPT_GOAL_HEADER}

{goal}

{PROMPT_REQUIREMENT_HEADER}

1. **不要重新分析或重做已经完成的工作**——已完成内容见 memory.md 第 2 节。
2. **不要重复已经失败的方案**——失败方案及原因见 memory.md 第 4 节。
3. 先核对真实代码与 Git 状态，再动手：Memory 只描述状态，真实状态以文件和 Git 为准。
4. 从 memory.md 第 7 节的「下一步行动」开始推进；若发现 Memory 与实际状态冲突，以实际状态为准，并说明冲突点。
5. 动手前先用一两句话复述你理解的任务现状与本次要做的事，然后开始执行。
"""


def unwrap_injected_prompt(text: str | None) -> str | None:
    """识别「本工具注入的初始 Prompt」，并取回其中的原始任务目标。

    为什么需要它：当目标 Agent 的会话**再次被迁移**时（Codex → Claude → Codex），
    它的第一条「用户消息」其实是我们注入的 Prompt。若不处理，新一轮的记忆
    会把我们自己的包装文案当成用户目标，目标字段就废了。

    返回 None 表示这段文本不是本工具的 Prompt。
    """
    if not text:
        return None
    stripped = text.strip()
    if PROMPT_OPENING not in stripped[:80]:
        return None

    body = stripped
    if PROMPT_GOAL_HEADER in body:
        body = body.split(PROMPT_GOAL_HEADER, 1)[1]
    if PROMPT_REQUIREMENT_HEADER in body:
        body = body.split(PROMPT_REQUIREMENT_HEADER, 1)[0]
    goal = body.strip().strip("：:").strip()
    return goal or None


# ----------------------------------------------------------------------


def _bullet_section(add, title: str | None, items: list[str], empty: str = "（无）") -> None:
    if title:
        add(f"### {title}")
        add("")
    if not items:
        add(empty)
        add("")
        return
    for item in items:
        add(f"- {item}")
    add("")


def _cell(text: str, limit: int = 160) -> str:
    """表格单元格：转义竖线与换行。"""
    cleaned = (text or "").replace("\n", " ").replace("|", "\\|").strip()
    return truncate(cleaned, limit) if cleaned else "-"
