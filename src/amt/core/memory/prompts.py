"""Memory Extraction Prompt（实现plan §十一 / 技术架构 §43）。

关键定位：**这不是 Conversation Summary，而是 Task State Reconstruction。**
提示词必须让模型恢复「任务当前处于什么状态」，而不是复述聊天内容。
"""

from __future__ import annotations

import json

from amt.services.filesystem import ProjectState

SYSTEM_PROMPT = """你是一个 Agent Task State Reconstruction Engine。

你的任务不是总结对话，而是从 Coding Agent 的执行记录中恢复「当前任务状态」，
以便另一个 Coding Agent 读取后能直接继续执行，而不必重新做已完成的工作。

必须识别并输出：
1. 用户最终目标（goal）
2. 已完成工作（completed）
3. 已修改文件（modified_files）
4. 已做出的关键技术决策（decisions）
5. 已尝试且失败的方案（attempts，success=false 的项）
6. 测试与验证结果（validation）
7. 当前未解决问题（unresolved）
8. 下一步应该执行什么（next_actions）

严格规则：
- 不要猜测不存在的信息；无法确定的字段用空数组或 null，不要编造。
- 失败方案必须保留，且要写清「为什么失败」——这是目标 Agent 最需要的信息，
  它决定了目标 Agent 不会重复尝试同样的方案。
- 已完成的工作不得再出现在 next_actions 中。
- 文件路径必须使用执行记录中出现的真实路径。
- 优先采信「项目实际状态与 Git 状态」，它们优先于对话中的描述。
- validation.tests 只填**执行记录里确实出现过的验证命令**（测试/构建/校验脚本），
  并如实标注其结果。这一栏会被标记为「LLM 归纳、未经程序校验」，
  所以宁可留空也不要为了填满而推测。
- 输出必须严格符合给定 JSON Schema，不要输出任何解释性文字或 Markdown 代码围栏。
"""

_OUTPUT_CONTRACT = {
    "task": {
        "title": "string，任务标题，不超过 40 字",
        "goal": "string，用户最终想要达成的目标",
        "status": "pending | in_progress | blocked | completed",
        "requirements": ["string，用户明确提出的要求"],
        "constraints": ["string，明确的技术约束"],
        "confidence": "unknown | low | medium | high",
    },
    "conversation": {
        "summary": "string，任务层面的摘要（不是聊天流水账）",
        "key_points": ["string，关键结论"],
        "user_preferences": ["string，用户表现出的偏好"],
    },
    "decisions": [
        {"decision": "string", "reason": "string|null", "alternatives": ["string"]}
    ],
    "implementation": {
        "completed": ["string，已完成的动作"],
        "modified_files": [{"path": "string", "summary": "string", "status": "modified|added|deleted"}],
    },
    "attempts": [
        {
            "action": "string",
            "purpose": "string|null",
            "result": "string，实际结果",
            "success": "boolean",
            "error": "string|null，报错信息",
            "lesson": "string|null，从失败中学到的东西",
        }
    ],
    "validation": {
        "tests": [{"command": "string", "status": "passed|failed|skipped", "output_summary": "string|null"}]
    },
    "unresolved": [
        {"description": "string", "priority": "low|medium|high|critical", "context": "string|null", "suspected_cause": "string|null"}
    ],
    "next_actions": [
        {"action": "string", "reason": "string|null", "priority": "integer 从 1 开始"}
    ],
    "risks": [{"description": "string", "impact": "string|null", "mitigation": "string|null"}],
}


def build_user_prompt(
    *,
    digest: str,
    project: ProjectState,
    source_agent: str,
    session_id: str | None,
    extra_facts: str | None = None,
) -> str:
    git_lines = [
        f"- 分支：{project.git.branch or 'unknown'}",
        f"- HEAD：{project.git.commit or 'unknown'}",
        f"- 状态：{project.git.status_summary or 'unknown'}",
    ]
    if project.git.diff_summary:
        git_lines.append("- diff --stat：\n```\n" + project.git.diff_summary[:2000] + "\n```")

    facts = [
        f"## 来源\n- Agent：{source_agent}\n- Session：{session_id or 'unknown'}",
        "## 项目真实状态（优先于对话描述）\n"
        f"- 路径：{project.cwd or 'unknown'}\n"
        f"- 名称：{project.project_name or 'unknown'}\n"
        f"- 语言：{', '.join(project.languages) or 'unknown'}\n"
        f"- 框架：{', '.join(project.frameworks) or 'unknown'}\n"
        f"- 顶层结构：{project.structure_summary or 'unknown'}",
        "## Git 状态\n" + "\n".join(git_lines),
        "## 执行记录时间线\n```\n" + digest[:60_000] + "\n```",
    ]
    if extra_facts:
        facts.append("## 附加事实\n" + extra_facts)

    facts.append(
        "## 输出契约\n"
        "严格按以下 JSON Schema 输出（只输出 JSON）：\n```json\n"
        + json.dumps(_OUTPUT_CONTRACT, ensure_ascii=False, indent=2)
        + "\n```"
    )
    return "\n\n".join(facts)
