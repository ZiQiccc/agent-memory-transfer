"""Normalizer（技术架构 §21 / 实现plan §九）。

职责：把不同 Agent 的同类动作**表现一致**。

    Codex  exec_command / apply_patch
    Claude Bash / Edit
    Cursor edit_file
    → 统一为 terminal / file_edit / file_read / test / error

工具名 → 语义的映射表是**跨 Agent 第一层协议**的一部分，因此放在 Core；
新增 Agent 时若其工具名未收录，Adapter 可通过
``metadata["category_hint"]`` 提供提示，或在此表补充。

本模块不做任何 LLM 调用，也不生成 Memory。
"""

from __future__ import annotations

import re
from collections import OrderedDict

from amt.core.memory.renderer import unwrap_injected_prompt
from amt.core.models import AgentEvent, EventType, NormalizedEvent
from amt.utils import collapse, first_line, truncate

# ----------------------------------------------------------------------
# 跨 Agent 工具语义表
# ----------------------------------------------------------------------

TOOL_SEMANTICS: dict[str, str] = {
    # --- 终端 ---
    "exec_command": "terminal",
    "execute_command": "terminal",
    "run_command": "terminal",
    "shell": "terminal",
    "bash": "terminal",
    "terminal": "terminal",
    "sh": "terminal",
    "cmd": "terminal",
    "powershell": "terminal",
    # --- 文件编辑 ---
    "apply_patch": "file_edit",
    "patch": "file_edit",
    "edit_file": "file_edit",
    "edit": "file_edit",
    "write_file": "file_edit",
    "write": "file_edit",
    "create_file": "file_edit",
    "str_replace_editor": "file_edit",
    "str_replace": "file_edit",
    "notebook_edit": "file_edit",
    # --- 文件读取 / 检索 ---
    "read_file": "file_read",
    "read": "file_read",
    "cat": "file_read",
    "view": "file_read",
    "get_file": "file_read",
    "list_dir": "file_read",
    "ls": "file_read",
    "glob": "file_read",
    "grep": "file_read",
    "grep_search": "file_read",
    "search": "file_read",
    "codebase_search": "file_read",
    # --- 计划 ---
    "update_plan": "plan",
    "todo_write": "plan",
    "plan": "plan",
    # --- 联网等其它 ---
    "web_search": "tool_call",
    "fetch": "tool_call",
    "webfetch": "tool_call",
}

_CATEGORY_TO_EVENT: dict[str, EventType] = {
    "terminal": EventType.TERMINAL,
    "file_edit": EventType.FILE_EDIT,
    "file_read": EventType.FILE_READ,
    "plan": EventType.PLAN,
    "tool_call": EventType.TOOL_CALL,
    "tool_result": EventType.TOOL_RESULT,
}

# ----------------------------------------------------------------------
# 测试 / 构建 / 静态检查识别
# ----------------------------------------------------------------------

_TEST_PATTERNS = (
    re.compile(r"(?i)(^|[\s&|;(])pytest(\s|$)"),
    re.compile(r"(?i)python[0-9.]*\s+-m\s+(pytest|unittest|nose2|tox)"),
    re.compile(r"(?i)(^|[\s&|;(])tox(\s|$)"),
    re.compile(r"(?i)\bmvn\w*\b[^\n]*?\btest\b"),
    re.compile(r"(?i)\bgradlew?\b[^\n]*?\btest\b"),
    re.compile(r"(?i)\b(npm|yarn|pnpm|bun)\b[^\n]*?\btest\b"),
    re.compile(r"(?i)\bgo\s+test\b"),
    re.compile(r"(?i)\bcargo\s+test\b"),
    re.compile(r"(?i)\bdotnet\s+test\b"),
    re.compile(r"(?i)(^|[\s&|;(])(jest|vitest|mocha|phpunit|ctest|rspec)(\s|$)"),
)

_BUILD_PATTERNS = (
    re.compile(r"(?i)\bmvn\w*\b[^\n]*?\b(package|install|compile|verify)\b"),
    re.compile(r"(?i)\bgradlew?\b[^\n]*?\b(build|assemble)\b"),
    re.compile(r"(?i)\b(npm|yarn|pnpm|bun)\b[^\n]*?\b(run\s+)?build\b"),
    re.compile(r"(?i)\bgo\s+build\b"),
    re.compile(r"(?i)\bcargo\s+build\b"),
    re.compile(r"(?i)\bdotnet\s+build\b"),
    re.compile(r"(?i)\btsc\b"),
    re.compile(r"(?i)\bvite\s+build\b"),
    re.compile(r"(?i)\bwebpack\b"),
)

_LINT_PATTERNS = (
    re.compile(r"(?i)(^|[\s&|;(])(ruff|flake8|pylint|eslint|prettier|stylelint|checkstyle|sonar\w*)(\s|$)"),
    re.compile(r"(?i)\b(npm|yarn|pnpm)\b[^\n]*?\blint\b"),
    re.compile(r"(?i)\bgolangci-lint\b"),
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# ----------------------------------------------------------------------
# 「退出码 1 属于正常返回」的命令
#
# 这是跨 Agent 的**命令语义知识**，不是某个 Agent 的格式细节：
# 检索类工具未匹配到结果时返回 1，是正常行为而非失败。
# 若不处理，`rg` 这类高频命令会把「没搜到」全部误报成「执行失败」，
# 污染 attempts / unresolved / next_actions 三个核心字段。
# ----------------------------------------------------------------------
_NONZERO_IS_NORMAL: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)^\s*(?:rg|grep|egrep|fgrep|ag|ack|findstr)\b"),
    re.compile(r"(?i)^\s*git\s+grep\b"),
    re.compile(r"(?i)^\s*git\s+diff\s+--quiet\b"),
    re.compile(r"(?i)\bSelect-String\b"),
    re.compile(r"(?i)^\s*(?:where\.exe|which|command\s+-v)\b"),
    re.compile(r"(?i)^\s*(?:npm|yarn|pnpm)\s+ls\b"),
    re.compile(r"(?i)^\s*test\s+-"),
)


def is_expected_nonzero(command: str | None, exit_code: int | None) -> bool:
    """判断「非零退出码」是否属于该命令的正常语义。

    仅在 ``exit_code == 1`` 时成立：退出码 2 通常是真实错误（如 rg 的参数错误）。
    """
    if exit_code != 1 or not command:
        return False
    return any(pattern.search(command) for pattern in _NONZERO_IS_NORMAL)


def _looks_like_test(command: str | None) -> bool:
    if not command:
        return False
    return any(p.search(command) for p in _TEST_PATTERNS)


def _looks_like_build(command: str | None) -> bool:
    if not command:
        return False
    if _looks_like_test(command):
        return False
    return any(p.search(command) for p in _BUILD_PATTERNS)


def _looks_like_lint(command: str | None) -> bool:
    if not command:
        return False
    return any(p.search(command) for p in _LINT_PATTERNS)


def clean_output(text: str | None) -> str:
    if not text:
        return ""
    return _ANSI_RE.sub("", text).strip()


def event_output(event: AgentEvent | NormalizedEvent) -> str:
    """取事件的工具输出。

    注意**不能**写 ``event.result or event.content``：清洗后的输出合法地可能是
    空串（例如命令成功但没有任何输出），而空串是假值，会让 ``or`` 回退到
    未经清洗的原始文本，把工具信封又带进 Memory。因此这里显式区分
    「未提供 result」与「result 为空」。
    """
    return event.result if event.result is not None else (event.content or "")


#: 一级失败关键词——几乎可以确定是报错
_ERROR_KEYWORDS = (
    "error", "exception", "failed", "cannot", "not found", "fatal",
    "traceback", "错误", "失败", "异常", "无法", "不存在",
)

#: 二级失败关键词——编译/构建类报错常见措辞
_ERROR_KEYWORDS_WEAK = (
    "undefined", "unknown", "unrecognized", "invalid", "missing", "no matching",
    "unsupported", "not recognized", "mismatch", "declared", "警告", "不能", "缺少",
)


#: 只描述进程/信封状态、对目标 Agent 没有信息量的行
_NOISE_LINE = re.compile(
    r"(?i)^(?:process\s+)?(?:exited\s+with\s+code|exit\s+code:?)\s*-?\d+\s*$"
    r"|^\s*original token count:\s*\d+\s*$"
    r"|^\s*chunk id:.*$"
    r"|^\s*wall time:.*$"
)


def first_error_line(text: str | None, limit: int = 200) -> str:
    """从输出里找出最能代表失败原因的一行。

    会跳过「Process exited with code 1」「Original token count: 207」这类
    只描述进程状态的信封行——它们对目标 Agent 没有信息量。

    **找不到可读报错时返回空串**，而不是随便挑一行：挑到表头（如 ``FullName``）
    或列表首项会产生误导性信息，比「没有信息」更糟。调用方会用退出码兜底。
    """
    if not text:
        return ""
    for line in text.splitlines():
        line = clean_output(line)
        if not line or _NOISE_LINE.match(line):
            continue
        lowered = line.lower()
        if any(k in lowered or k in line for k in _ERROR_KEYWORDS):
            return truncate(line, limit)
    for line in text.splitlines():
        line = clean_output(line)
        if not line or _NOISE_LINE.match(line):
            continue
        lowered = line.lower()
        if any(k in lowered or k in line for k in _ERROR_KEYWORDS_WEAK):
            return truncate(line, limit)
    return ""


class Normalizer:
    """AgentEvent[] → NormalizedEvent[]。"""

    def normalize(self, events: list[AgentEvent]) -> list[NormalizedEvent]:
        results: list[NormalizedEvent] = []
        pending_calls: dict[str, NormalizedEvent] = {}

        for event in events:
            if event.type is EventType.SESSION_META:
                results.append(self._meta(event))
            elif event.type is EventType.USER_MESSAGE:
                results.append(self._message(event, "user_message", significance=3))
            elif event.type is EventType.ASSISTANT_MESSAGE:
                results.append(self._message(event, "assistant_message", significance=2))
            elif event.type is EventType.REASONING:
                results.append(self._reasoning(event))
            elif event.type is EventType.TOOL_CALL:
                normalized = self._tool_call(event)
                call_id = event.metadata.get("call_id")
                if call_id:
                    pending_calls[call_id] = normalized
                results.append(normalized)
            elif event.type is EventType.TOOL_RESULT:
                self._attach_tool_result(event, pending_calls, results)
            elif event.type is EventType.ERROR:
                results.append(self._error(event))
            else:
                results.append(self._passthrough(event))

        return self._post_process(results)

    # ------------------------------------------------------------------
    # 单事件转换
    # ------------------------------------------------------------------

    def _base(self, event: AgentEvent, event_type: EventType, category: str, summary: str) -> NormalizedEvent:
        return NormalizedEvent(
            id=event.id,
            timestamp=event.timestamp,
            type=event_type,
            category=category,
            role=event.role,
            summary=summary,
            content=clean_output(event.content),
            tool_name=event.tool_name,
            paths=list(event.metadata.get("paths") or ([event.file_path] if event.file_path else [])),
            command=event.command,
            result=clean_output(event.result),
            is_failure=bool(event.metadata.get("is_failure")),
            exit_code=event.metadata.get("exit_code"),
            significance=int(event.metadata.get("significance", 1)),
            metadata=dict(event.metadata),
        )

    def _meta(self, event: AgentEvent) -> NormalizedEvent:
        return self._base(event, EventType.SESSION_META, "meta", collapse(event.content or ""))

    def _message(self, event: AgentEvent, category: str, significance: int) -> NormalizedEvent:
        normalized = self._base(event, event.type, category, first_line(event.content, 160))
        normalized.significance = significance

        # 回环防护：目标 Agent 的会话被再次迁移时，它的第一条「用户消息」
        # 其实是我们注入的初始 Prompt。这里还原出其中的真实目标，
        # 否则新一轮记忆会把自己的包装文案当成用户需求（实测会发生）。
        if event.type is EventType.USER_MESSAGE:
            inner = unwrap_injected_prompt(event.content)
            if inner:
                normalized.content = inner
                normalized.metadata["injected_prompt"] = True
                normalized.summary = first_line(inner, 160)
        return normalized

    def _reasoning(self, event: AgentEvent) -> NormalizedEvent:
        normalized = self._base(event, EventType.REASONING, "reasoning", first_line(event.content, 160))
        normalized.significance = 1
        return normalized

    def _error(self, event: AgentEvent) -> NormalizedEvent:
        normalized = self._base(event, EventType.ERROR, "error", collapse(event.content or ""))
        normalized.is_failure = True
        normalized.significance = max(normalized.significance, 2)
        return normalized

    def _passthrough(self, event: AgentEvent) -> NormalizedEvent:
        category = event.metadata.get("category_hint") or "tool_call"
        return self._base(event, event.type, category, first_line(event.content, 160))

    def _tool_call(self, event: AgentEvent) -> NormalizedEvent:
        tool = (event.tool_name or "").lower()
        # 优先级：Adapter 的显式提示 > 跨 Agent 工具名映射表 > 兜底。
        # Adapter 掌握 Agent 私有调用细节（例如「以 heredoc 方式调用 apply_patch」），
        # 它的判断优于仅凭工具名的猜测。
        hint = event.metadata.get("category_hint")
        category = hint or TOOL_SEMANTICS.get(tool) or "tool_call"

        event_type = _CATEGORY_TO_EVENT.get(category, EventType.TOOL_CALL)
        # 终端命令进一步细分为 test / build：二者同属「验证类」事件类型，
        # 但 category 必须区分开，否则下游的测试结果统计会漏掉测试命令
        # （`VALIDATION.tests` 与 `analysis.tests` 都按 category 过滤）。
        if category == "terminal":
            if _looks_like_test(event.command):
                event_type = EventType.TEST
                category = "test"
            elif _looks_like_build(event.command):
                event_type = EventType.TEST
                category = "build"

        summary = self._summarize_tool_call(event, category)
        normalized = self._base(event, event_type, category, summary)
        return normalized

    @staticmethod
    def _summarize_tool_call(event: AgentEvent, category: str) -> str:
        paths = event.metadata.get("paths") or ([event.file_path] if event.file_path else [])
        patch_stats = event.metadata.get("patch_file_stats") or {}
        statuses = event.metadata.get("patch_statuses") or {}

        if category == "terminal":
            return f"执行命令：{truncate(event.command or '', 160)}"
        if category == "build":
            return f"执行构建：{truncate(event.command or '', 160)}"
        if category == "file_edit":
            if not paths:
                return "修改文件（未解析出路径）"
            parts = []
            for path in paths:
                stat = patch_stats.get(path)
                if stat:
                    parts.append(f"{path}（{stat['hunks']} 处，+{stat['added']}/-{stat['removed']}）")
                else:
                    parts.append(f"{path}（{statuses.get(path, 'modified')}）")
            return "修改文件：" + "；".join(parts)
        if category == "file_read":
            if paths:
                return "读取文件：" + "、".join(paths[:6])
            return f"检索代码：{truncate(event.command or event.tool_name or '', 120)}"
        if category == "plan":
            return "更新执行计划"
        return f"调用工具：{event.tool_name or 'unknown'}"

    def _attach_tool_result(
        self,
        event: AgentEvent,
        pending_calls: dict[str, NormalizedEvent],
        results: list[NormalizedEvent],
    ) -> None:
        """把工具输出合并回对应的调用事件，避免同一动作产生两条记忆。"""
        call_id = event.metadata.get("call_id")
        target = pending_calls.get(call_id) if call_id else None
        if target is None:
            # 未配对的输出独立成事件
            normalized = self._base(event, EventType.TOOL_RESULT, "tool_result", "工具输出（未配对）")
            results.append(normalized)
            return

        target.result = clean_output(event_output(event))
        exit_code = event.metadata.get("exit_code")
        if exit_code is not None:
            target.exit_code = exit_code
        if event.metadata.get("is_failure") is not None:
            target.is_failure = bool(event.metadata.get("is_failure"))
        target.metadata["result_attached"] = True

        # 校正「检索类命令未匹配到结果」被误判为失败的情况
        if target.is_failure and is_expected_nonzero(target.command, target.exit_code):
            target.is_failure = False
            target.significance = 1
            target.metadata["nonzero_is_normal"] = True

        # 失败是最高价值信号 → 提升显著性
        if target.is_failure:
            target.significance = max(target.significance, 3)

    # ------------------------------------------------------------------
    # 后处理
    # ------------------------------------------------------------------

    def _post_process(self, events: list[NormalizedEvent]) -> list[NormalizedEvent]:
        events = self._dedupe_messages(events)
        for event in events:
            if event.type is EventType.TEST:
                base = "测试" if event.category != "build" else "构建"
                status = "失败" if event.is_failure else "通过"
                event.summary = f"{base}{status}：{truncate(event.command or '', 140)}"
        return events

    @staticmethod
    def _dedupe_messages(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
        """去掉内容完全重复的消息。

        Codex 的 ``event_msg/task_complete`` 与其 assistant message 会同时出现，
        内容一致；这里按 (type, role, content) 去重，保留首次出现的位置。
        """
        seen: set[tuple[str, str | None, str]] = set()
        out: list[NormalizedEvent] = []
        for event in events:
            if event.type in (EventType.USER_MESSAGE, EventType.ASSISTANT_MESSAGE):
                key = (event.type.value, event.role, (event.content or "").strip())
                if len(key[2]) > 40 and key in seen:
                    continue
                seen.add(key)
            out.append(event)
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def recent_commands(events: list[NormalizedEvent], limit: int = 20) -> list[str]:
        """收集最近执行过的命令（供 RuntimeContext.recent_commands）。"""
        commands: OrderedDict[str, None] = OrderedDict()
        for event in events:
            if event.command and event.category in ("terminal", "build", "test"):
                commands[truncate(event.command, 200)] = None
        values = list(commands.keys())
        return values[-limit:]

    @staticmethod
    def lint_commands(events: list[NormalizedEvent]) -> list[str]:
        return [
            truncate(e.command or "", 200)
            for e in events
            if e.command and _looks_like_lint(e.command)
        ]
