"""Codex rollout JSONL 解析器。

架构边界（实现plan §三 / §七）：
    Codex JSONL 属于 Codex Source Adapter，**不得进入 Core**。

    ❌ Core 直接依赖 Codex JSONL
    ✅ CodexAdapter → AgentEvent[]

未来 Codex 改动 Session 格式，只需改本文件。

真实格式实测（本机 72 个 rollout-*.jsonl，17139 条记录）：
    外层 type      : session_meta / event_msg / response_item / turn_context
                     / world_state / token_usage_record / compacted
    response_item  : function_call / function_call_output / custom_tool_call
                     / custom_tool_call_output / message / reasoning
    event_msg      : item_completed / token_count / task_started / task_complete
                     / turn_aborted / thread_settings_applied

**容错要点**：部分记录会**跨多个物理行**（JSON 允许 token 之间换行），
因此行式 ``json.loads`` 在本机实测的 72 个会话上会误报 35 处解析失败。
本模块改用缓冲区 ``JSONDecoder.raw_decode`` 逐值推进，实测
17109 条记录 0 失败（行式解析为 17104 条），并且能跳过真正损坏的片段
而不中断整份会话的解析。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from json import JSONDecoder
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from amt.core.models import AgentEvent, EventType, RawSession, SessionMetadata
from amt.utils import parse_iso, truncate

# ----------------------------------------------------------------------
# 容错读取
# ----------------------------------------------------------------------

_DECODER = JSONDecoder()


def iter_json_records(text: str) -> Iterator[dict[str, Any] | None]:
    """从文本中逐个取出 JSON 值；无法解析的片段产出 None 并跳到下一行。"""
    n = len(text)
    i = 0
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            return
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except ValueError:
            newline = text.find("\n", i)
            yield None
            if newline == -1:
                return
            i = newline + 1
            continue
        if isinstance(obj, dict):
            yield obj
        else:
            yield None
        i = end


def read_records(path: str | Path, max_bytes: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """读取 JSONL 记录。返回 (记录列表, 解析失败片段数)。"""
    p = Path(path)
    if not p.is_file():
        return [], 0
    raw = p.read_bytes()
    if max_bytes is not None and len(raw) > max_bytes:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    records: list[dict[str, Any]] = []
    errors = 0
    for obj in iter_json_records(text):
        if obj is None:
            errors += 1
        else:
            records.append(obj)
    return records, errors


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 由 harness 注入的用户侧文本（不是用户真实需求），必须排除在需求之外
_INJECTED_USER_MARKERS = (
    "<environment_context>",
    "<app-context>",
    "<user_instructions>",
    "<INSTRUCTIONS>",
    "# AGENTS.md instructions",
    "<turn_context>",
    "<skills_instructions>",
    "<plugin_instructions>",
    "<personality_spec>",
    "<collaboration_mode>",
    # harness 在用户回合写入的状态标记，不是用户说的话
    "<turn_aborted>",
    "<turn_complete>",
)

_EXIT_CODE_PATTERNS = (
    re.compile(r"Process exited with code (-?\d+)"),
    re.compile(r"Exit code: (-?\d+)"),
    re.compile(r"[Ee]xited with code (-?\d+)"),
)

_ERROR_HINT = re.compile(
    r"(?i)\b(error|exception|traceback|failed|failure|cannot find|not found"
    r"|no such file|fatal|permission denied)\b|错误|失败|异常|不存在|无法"
)

#: 强失败标志——即使没有 exit code 也可确认为失败
_STRONG_FAILURE = re.compile(
    r"(?i)(apply_patch verification failed|command not found|is not recognized"
    r"|no such file or directory|traceback \(most recent call last\)"
    r"|cannot find module|could not find|fatal:|permission denied"
    r"|无法将|不是内部或外部命令|拒绝访问)"
)

#: 失败判定的输出窗口（只看开头，避免把「读到含 error 的日志文件」误判为失败）
_FAILURE_WINDOW = 600

_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(Update|Add|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE
)
_PATCH_MOVE_RE = re.compile(r"^\*\*\*\s+Move to:\s*(.+?)\s*$", re.MULTILINE)

#: 形如 shell 命令的工具参数键（Codex 的参数 schema，属格式层知识）
_COMMAND_ARG_KEYS = ("cmd", "command", "script", "shell_command")

_PATCH_STATUS = {"Update": "modified", "Add": "added", "Delete": "deleted"}

#: Codex 工具输出的信封格式：
#:     Chunk ID: xxx
#:     Wall time: 6.0399 seconds
#:     Process exited with code 1
#:     Original token count: 102
#:     Output:
#:     <真实输出>
#: 实测发现信封行并非总是出现在开头（也可能只有其中几行），
#: 因此按「行」匹配并允许出现在任意位置，而不是只匹配开头的前缀块。
_ENVELOPE_LINE_RE = re.compile(
    r"^(?:Chunk ID:|Wall time:|Process exited with code|Exit code:|Process exit code:"
    r"|Original token count:)\s*.*$",
    re.MULTILINE | re.IGNORECASE,
)
_OUTPUT_HEADER_RE = re.compile(r"^Output:\s*$", re.MULTILINE)

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_REMOVED_LINE_RE = re.compile(r"^-(?!--)", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@", re.MULTILINE)


class ParseStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_records: int = 0
    used_records: int = 0
    parse_errors: int = 0
    skipped_by_type: dict[str, int] = Field(default_factory=dict)
    record_type_counts: dict[str, int] = Field(default_factory=dict)
    injected_user_texts: int = 0
    unmatched_tool_outputs: int = 0


@dataclass
class CodexParseResult:
    events: list[AgentEvent] = field(default_factory=list)
    metadata: SessionMetadata | None = None
    stats: ParseStats = field(default_factory=ParseStats)


# ----------------------------------------------------------------------
# 解析器
# ----------------------------------------------------------------------


class CodexParser:
    """Codex 私有格式 → AgentEvent[]。"""

    format_name = "codex-rollout-jsonl"

    def parse(self, raw: RawSession) -> CodexParseResult:
        result = CodexParseResult()
        stats = result.stats
        stats.total_records = raw.total_records or len(raw.records)
        stats.parse_errors = raw.parse_errors

        metadata = SessionMetadata(agent="codex", session_id=raw.session_id, cwd=raw.cwd)
        result.metadata = metadata

        events: list[AgentEvent] = []
        call_index: dict[str, AgentEvent] = {}

        for record in raw.records:
            outer_type = record.get("type")
            if not isinstance(outer_type, str):
                stats.skipped_by_type["<no-type>"] = stats.skipped_by_type.get("<no-type>", 0) + 1
                continue
            stats.record_type_counts[outer_type] = stats.record_type_counts.get(outer_type, 0) + 1

            ordinal = record.get("ordinal")
            timestamp = parse_iso(record.get("timestamp"))
            payload = record.get("payload")
            if not isinstance(payload, dict):
                stats.skipped_by_type[outer_type] = stats.skipped_by_type.get(outer_type, 0) + 1
                continue

            produced: list[AgentEvent] = []

            if outer_type == "session_meta":
                produced = self._parse_session_meta(payload, metadata, ordinal, timestamp)
            elif outer_type == "response_item":
                produced = self._parse_response_item(
                    payload, metadata, ordinal, timestamp, stats, call_index
                )
            elif outer_type == "event_msg":
                produced = self._parse_event_msg(payload, ordinal, timestamp, stats)
            elif outer_type == "compacted":
                produced = [
                    self._event(
                        EventType.SESSION_META,
                        ordinal,
                        timestamp,
                        content="会话历史已被 Codex 压缩（compacted）",
                        metadata={"kind": "compacted"},
                        significance=1,
                    )
                ]
            else:
                # turn_context / world_state / token_usage_record：
                # 仅用于补充元信息或纯噪音，不产出事件。
                self._absorb_context(outer_type, payload, metadata)
                stats.skipped_by_type[outer_type] = stats.skipped_by_type.get(outer_type, 0) + 1
                continue

            stats.used_records += 1
            events.extend(produced)

        # 未配对的 tool 输出：计一次，避免静默丢失
        for event in events:
            if event.type is EventType.TOOL_RESULT and not event.metadata.get("matched"):
                stats.unmatched_tool_outputs += 1

        result.events = events
        return result

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def _parse_session_meta(
        self,
        payload: dict[str, Any],
        metadata: SessionMetadata,
        ordinal: Any,
        timestamp: datetime | None,
    ) -> list[AgentEvent]:
        metadata.session_id = payload.get("session_id") or payload.get("id") or metadata.session_id
        metadata.cwd = payload.get("cwd") or metadata.cwd
        metadata.created_at = parse_iso(payload.get("timestamp")) or timestamp
        metadata.cli_version = payload.get("cli_version")
        metadata.originator = payload.get("originator")
        metadata.source = payload.get("source")
        if payload.get("model"):
            metadata.model = payload.get("model")
        for key in ("model_provider", "thread_source"):
            if payload.get(key):
                metadata.extra[key] = payload[key]

        location = metadata.cwd or "未知目录"
        return [
            self._event(
                EventType.SESSION_META,
                ordinal,
                timestamp,
                content=f"Codex 会话开始（{location}）",
                metadata={"kind": "session_start", "cwd": metadata.cwd},
                significance=1,
            )
        ]

    def _parse_response_item(
        self,
        payload: dict[str, Any],
        metadata: SessionMetadata,
        ordinal: Any,
        timestamp: datetime | None,
        stats: ParseStats,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        item_type = payload.get("type")

        if item_type == "message":
            return self._parse_message(payload, metadata, ordinal, timestamp, stats)

        if item_type == "reasoning":
            summary = payload.get("summary") or []
            text = "\n".join(
                part.get("text", "")
                for part in summary
                if isinstance(part, dict) and part.get("text")
            ).strip()
            if not text:
                return []
            return [
                self._event(
                    EventType.REASONING,
                    ordinal,
                    timestamp,
                    role="assistant",
                    content=truncate(text, 4000),
                    metadata={"kind": "reasoning_summary"},
                    significance=1,
                )
            ]

        if item_type in ("function_call", "custom_tool_call"):
            return self._parse_tool_call(payload, ordinal, timestamp, call_index)

        if item_type in ("function_call_output", "custom_tool_call_output"):
            return self._parse_tool_output(payload, ordinal, timestamp, call_index)

        stats.skipped_by_type[f"response_item/{item_type}"] = (
            stats.skipped_by_type.get(f"response_item/{item_type}", 0) + 1
        )
        return []

    def _parse_message(
        self,
        payload: dict[str, Any],
        metadata: SessionMetadata,
        ordinal: Any,
        timestamp: datetime | None,
        stats: ParseStats,
    ) -> list[AgentEvent]:
        role = (payload.get("role") or "").lower()
        text = _extract_content_text(payload.get("content"))
        if not text:
            return []

        if role in ("developer", "system"):
            # harness 注入的指令，不是会话内容
            stats.skipped_by_type[f"message/{role}"] = stats.skipped_by_type.get(f"message/{role}", 0) + 1
            return []

        if role == "user":
            if _is_injected_user_text(text):
                stats.injected_user_texts += 1
                if "# AGENTS.md instructions" in text:
                    metadata.extra.setdefault("agents_md", truncate(text, 2000))
                return []
            return [
                self._event(
                    EventType.USER_MESSAGE,
                    ordinal,
                    timestamp,
                    role="user",
                    content=text,
                    significance=3,
                )
            ]

        if role == "assistant":
            return [
                self._event(
                    EventType.ASSISTANT_MESSAGE,
                    ordinal,
                    timestamp,
                    role="assistant",
                    content=text,
                    metadata={"kind": "assistant_message"},
                    significance=2,
                )
            ]

        stats.skipped_by_type[f"message/{role or 'unknown'}"] = (
            stats.skipped_by_type.get(f"message/{role or 'unknown'}", 0) + 1
        )
        return []

    def _parse_tool_call(
        self,
        payload: dict[str, Any],
        ordinal: Any,
        timestamp: datetime | None,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        tool_name = payload.get("name") or "unknown_tool"
        call_id = payload.get("call_id") or payload.get("id") or ""
        raw_args = payload.get("arguments")
        if raw_args is None:
            raw_args = payload.get("input")

        arguments: dict[str, Any] = {}
        raw_text = ""
        if isinstance(raw_args, str):
            raw_text = raw_args
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    arguments = parsed
            except Exception:
                arguments = {}
        elif isinstance(raw_args, dict):
            arguments = raw_args
            raw_text = json.dumps(raw_args, ensure_ascii=False)

        # 命令提取：Codex 参数 schema 的通用键
        command = None
        for key in _COMMAND_ARG_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                command = value.strip()
                break

        # patch 路径提取：apply_patch 有两种调用路径——
        #   ① 直接作为工具调用（custom_tool_call，input 即 patch 文本）
        #   ② 通过 shell 以 heredoc 方式调用（exec_command，patch 文本在命令行里）
        # 实测 ② 很常见，若只处理 ① 会漏掉大量真实文件改动。
        paths: list[str] = []
        patch_statuses: dict[str, str] = {}
        body = raw_text
        if isinstance(arguments.get("input"), str):
            body = arguments["input"]
        patch_sources = [body]
        if command:
            patch_sources.append(command)

        for source in patch_sources:
            if "*** Begin Patch" not in source and "*** Update File:" not in source:
                continue
            for action, path in _PATCH_FILE_RE.findall(source):
                cleaned = path.strip()
                if cleaned:
                    paths.append(cleaned)
                    patch_statuses[cleaned] = _PATCH_STATUS.get(action, "modified")
            for moved in _PATCH_MOVE_RE.findall(source):
                moved = moved.strip()
                if moved:
                    paths.append(moved)
                    patch_statuses[moved] = "modified"

        metadata: dict[str, Any] = {
            "kind": "tool_call",
            "call_id": call_id,
            "arguments": arguments,
        }
        if patch_statuses:
            metadata["patch_statuses"] = patch_statuses
            metadata["patch_file_stats"] = _analyze_patch(
                "\n".join(s for s in patch_sources if "*** " in s)
            )
            # 明确告知 Core：这是一次文件编辑，而不是普通终端命令
            metadata["category_hint"] = "file_edit"

        event = self._event(
            EventType.TOOL_CALL,
            ordinal,
            timestamp,
            role="assistant",
            content=truncate(raw_text, 4000),
            tool_name=tool_name,
            command=command,
            file_path=paths[0] if paths else None,
            metadata=metadata,
            significance=2,
        )
        event.metadata["paths"] = paths
        if call_id:
            call_index[call_id] = event
        return [event]

    def _parse_tool_output(
        self,
        payload: dict[str, Any],
        ordinal: Any,
        timestamp: datetime | None,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        call_id = payload.get("call_id") or payload.get("id") or ""
        output = payload.get("output")
        if output is None:
            output = payload.get("content")
        if isinstance(output, (dict, list)):
            output = json.dumps(output, ensure_ascii=False)
        output = str(output or "")

        exit_code = _extract_exit_code(output)
        is_failure = _looks_like_failure(output, exit_code)

        call_event = call_index.get(call_id)
        if call_event is not None:
            call_event.metadata["matched"] = True

        metadata: dict[str, Any] = {
            "kind": "tool_output",
            "call_id": call_id,
            "exit_code": exit_code,
            "is_failure": is_failure,
            "matched": call_event is not None,
        }
        if call_event is not None:
            metadata["tool_name"] = call_event.tool_name
            if call_event.command:
                metadata["command"] = call_event.command
            if call_event.metadata.get("paths"):
                metadata["paths"] = call_event.metadata.get("paths")

        return [
            self._event(
                EventType.TOOL_RESULT,
                ordinal,
                timestamp,
                role="tool",
                content=truncate(output, 6000),
                result=truncate(_clean_codex_output(output), 6000),
                tool_name=(call_event.tool_name if call_event else None) or payload.get("name"),
                command=(call_event.command if call_event else None),
                metadata=metadata,
                significance=2 if is_failure else 1,
            )
        ]

    def _parse_event_msg(
        self,
        payload: dict[str, Any],
        ordinal: Any,
        timestamp: datetime | None,
        stats: ParseStats,
    ) -> list[AgentEvent]:
        item_type = payload.get("type")

        if item_type == "task_complete":
            text = payload.get("last_agent_message")
            if not isinstance(text, str) or not text.strip():
                return []
            return [
                self._event(
                    EventType.ASSISTANT_MESSAGE,
                    ordinal,
                    timestamp,
                    role="assistant",
                    content=text,
                    metadata={"kind": "task_complete"},
                    significance=2,
                )
            ]

        if item_type == "turn_aborted":
            reason = payload.get("reason") or "unknown"
            return [
                self._event(
                    EventType.ERROR,
                    ordinal,
                    timestamp,
                    content=f"本轮任务被中断（reason={reason}）",
                    metadata={
                        "kind": "turn_aborted",
                        "reason": reason,
                        "duration_ms": payload.get("duration_ms"),
                    },
                    significance=2,
                )
            ]

        if item_type in ("item_completed", "token_count", "task_started", "thread_settings_applied"):
            # 与 response_item 内容重复，或纯用量统计：
            # POC 阶段不产出事件（V2 可用 item_completed 做交叉校验）。
            stats.skipped_by_type[f"event_msg/{item_type}"] = (
                stats.skipped_by_type.get(f"event_msg/{item_type}", 0) + 1
            )
            return []

        stats.skipped_by_type[f"event_msg/{item_type}"] = (
            stats.skipped_by_type.get(f"event_msg/{item_type}", 0) + 1
        )
        return []

    # ------------------------------------------------------------------
    def _absorb_context(self, outer_type: str, payload: dict[str, Any], metadata: SessionMetadata) -> None:
        """从 turn_context / world_state 里补充元信息（不产出事件）。"""
        if outer_type == "turn_context":
            if payload.get("model"):
                metadata.model = payload["model"]
            if payload.get("cwd") and not metadata.cwd:
                metadata.cwd = payload["cwd"]
            if payload.get("workspace_roots"):
                metadata.extra["workspace_roots"] = payload["workspace_roots"]
        elif outer_type == "world_state":
            state = payload.get("state")
            if isinstance(state, dict):
                envs = state.get("environments")
                if isinstance(envs, dict):
                    local = (envs.get("environments") or {}).get("local")
                    if isinstance(local, dict):
                        if local.get("cwd") and not metadata.cwd:
                            metadata.cwd = local["cwd"]
                        if local.get("shell"):
                            metadata.extra["shell"] = local["shell"]
                if state.get("current_date"):
                    metadata.extra["current_date"] = state["current_date"]
                if state.get("timezone"):
                    metadata.extra["timezone"] = state["timezone"]

    # ------------------------------------------------------------------
    @staticmethod
    def _event(
        event_type: EventType,
        ordinal: Any,
        timestamp: datetime | None,
        *,
        role: str | None = None,
        content: str | None = None,
        tool_name: str | None = None,
        command: str | None = None,
        file_path: str | None = None,
        result: str | None = None,
        metadata: dict[str, Any] | None = None,
        significance: int = 1,
    ) -> AgentEvent:
        meta = dict(metadata or {})
        meta["ordinal"] = ordinal
        meta["significance"] = significance
        meta.setdefault("source_agent", "codex")
        return AgentEvent(
            id=f"codex:{ordinal}:{event_type.value}",
            timestamp=timestamp,
            type=event_type,
            role=role,
            content=content,
            tool_name=tool_name,
            file_path=file_path,
            command=command,
            result=result,
            metadata=meta,
        )


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _extract_content_text(content: Any) -> str:
    """从 Codex message.content 提取文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def _is_injected_user_text(text: str) -> bool:
    head = text.lstrip()[:400]
    return any(marker in head for marker in _INJECTED_USER_MARKERS)


def _extract_exit_code(output: str) -> int | None:
    for pattern in _EXIT_CODE_PATTERNS:
        match = pattern.search(output)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _looks_like_failure(output: str, exit_code: int | None) -> bool:
    """判定工具输出是否代表失败。

    优先级：有 exit code 就以它为准（这是最可靠的真实信号）；
    没有 exit code 时才退回文本启发式，且**只检查输出开头**——
    否则「读取一个含 error 字样的日志文件」会被误判成失败。
    """
    if exit_code is not None:
        return exit_code != 0

    head = (output or "")[:_FAILURE_WINDOW]
    if _STRONG_FAILURE.search(head):
        return True
    # 弱标志只在开头几行内才算失败
    first_lines = "\n".join(head.splitlines()[:3])
    return bool(_ERROR_HINT.search(first_lines))


def _clean_codex_output(output: str) -> str:
    """剥离 Codex 工具输出的信封，只留真实输出。

    信封形如::

        Chunk ID: 016a84
        Wall time: 6.0399 seconds
        Process exited with code 1
        Original token count: 102
        Output:
        <真实输出>
    """
    if not output:
        return ""
    had_envelope = bool(_ENVELOPE_LINE_RE.search(output) or _OUTPUT_HEADER_RE.search(output))
    cleaned = _ENVELOPE_LINE_RE.sub("", output)
    cleaned = _OUTPUT_HEADER_RE.sub("", cleaned, count=1)
    cleaned = cleaned.strip()
    if cleaned:
        return cleaned
    # 清洗后为空有两种完全不同的含义，必须区分：
    #   ① 原文只有信封（命令成功但没有任何输出，如 Original token count: 0）
    #      → 真实输出就是空串，绝不能把信封原样还回去
    #   ② 原文根本没有信封 → 说明这段文本不能被当作信封处理，保留原文
    return "" if had_envelope else output.strip()


def _analyze_patch(patch_text: str) -> dict[str, dict[str, int]]:
    """统计 apply_patch 每个文件的改动量。

    返回 ``{path: {"hunks": n, "added": n, "removed": n}}``，
    供 Memory 生成可验证的 ``modified_files.summary``（如「修改 3 处（+12/-4）」）。
    """
    if not patch_text:
        return {}

    stats: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in patch_text.splitlines():
        match = _PATCH_FILE_RE.match(line)
        if match:
            current = match.group(2).strip()
            stats.setdefault(current, {"hunks": 0, "added": 0, "removed": 0})
            continue
        if current is None:
            continue
        if _HUNK_RE.match(line):
            stats[current]["hunks"] += 1
        elif _ADDED_LINE_RE.match(line):
            stats[current]["added"] += 1
        elif _REMOVED_LINE_RE.match(line):
            stats[current]["removed"] += 1
    return stats
