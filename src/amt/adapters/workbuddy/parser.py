"""WorkBuddy 会话解析器。

落盘形式（本机实测）::

    ~/.workbuddy/projects/<escaped-workspace>/<session-uuid>.jsonl

记录类型与关键字段：

    message                  role=user/assistant，content=[{type: input_text|output_text, text}]
    reasoning                rawContent=[{type: reasoning_text, text}]，providerData.model
    function_call            name / arguments / callId
    function_call_result     name / callId / status / output=[{type: input_text, text}]
    file-history-snapshot    快照记账 → 丢弃

格式族系：**混血**。工具调用用 Codex 风格的 ``function_call`` / ``function_call_result``，
但内容块（``input_text`` / ``output_text``）与 ``parentId`` 链是 Claude 风格的。
因此不能套用任何一个既有 Parser。

两个必须按真实数据处理的细节：

1. ``timestamp`` 是**毫秒整数**，不是 ISO 字符串；
2. **用户消息里 harness 注入与真实提问同处一个文本块**：整块以
   ``<system-reminder data-role="user-context">`` 开头（可能上万字符），
   真实需求在结尾的 ``<user_query>…</user_query>`` 里。
   因此**不能整条丢弃** —— 那会把用户需求一起丢掉（这是与 Claude 侧相反的陷阱）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amt.adapters.codex.parser import iter_json_records
from amt.core.models import AgentEvent, EventType, RawSession, SessionMetadata
from amt.utils import truncate

#: 真实用户需求所在的标签（harness 用它把用户原话与注入上下文分开）
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)

#: harness 注入标记：命中且**没有** <user_query> 时，整条消息是注入内容
_INJECTED_MARKERS = (
    "<system-reminder",
    "<identity_context>",
    "<user_info>",
    "<additional_data>",
    "# BOOTSTRAP.md",
)

#: WorkBuddy 工具 → 语义类别（Bash/Write/Read 已在跨 Agent 通用表里，
#: 这里只补它特有的工具）
_WORKBUDDY_TOOL_CATEGORY: dict[str, str] = {
    "bash": "terminal",
    "write": "file_edit",
    "edit": "file_edit",
    "read": "file_read",
    "glob": "file_read",
    "grep": "file_read",
}

_COMMAND_KEYS = ("command", "cmd")
_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

_ERROR_HINT = re.compile(
    # 构建工具常用 [ERROR] / [FATAL] 这类带方括号的标记（Maven / Gradle 等），
    # 只匹配行首标记，避免把正文里出现的单词也当成失败。
    r"(?m)^\s*\[(?:ERROR|FATAL)\]|"
    r"^\s*(?:Error|error|EACCES|ENOENT|fatal|Traceback|Exception)\b|"
    r"command not found|No such file|Permission denied"
)

#: 这些状态之外的 function_call_result 视为失败
_OK_STATUSES = frozenset({"completed", "success", "ok"})


class WorkBuddyParseStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_records: int = 0
    used_records: int = 0
    parse_errors: int = 0
    skipped_by_type: dict[str, int] = Field(default_factory=dict)
    injected_only_messages: int = 0
    recovered_user_queries: int = 0


@dataclass
class WorkBuddyParseResult:
    events: list[AgentEvent] = field(default_factory=list)
    metadata: SessionMetadata | None = None
    stats: WorkBuddyParseStats = field(default_factory=WorkBuddyParseStats)


class WorkBuddyParser:
    """WorkBuddy JSONL → AgentEvent[]。"""

    format_name = "workbuddy-jsonl"

    def parse(self, raw: RawSession) -> WorkBuddyParseResult:
        result = WorkBuddyParseResult()
        stats = result.stats
        stats.total_records = raw.total_records or len(raw.records)
        stats.parse_errors = raw.parse_errors

        metadata = SessionMetadata(agent="workbuddy", session_id=raw.session_id, cwd=raw.cwd)
        result.metadata = metadata

        events: list[AgentEvent] = []
        call_index: dict[str, AgentEvent] = {}

        for index, record in enumerate(raw.records):
            record_type = record.get("type")
            if not isinstance(record_type, str):
                stats.skipped_by_type["<no-type>"] = stats.skipped_by_type.get("<no-type>", 0) + 1
                continue

            self._absorb_metadata(record, metadata)
            timestamp = _to_datetime(record.get("timestamp"))
            record_id = str(record.get("id") or index)

            produced: list[AgentEvent] = []
            if record_type == "message":
                produced = self._parse_message(record, record_id, timestamp, stats)
            elif record_type == "reasoning":
                produced = self._parse_reasoning(record, record_id, timestamp)
            elif record_type == "function_call":
                produced = self._parse_function_call(record, record_id, timestamp, call_index)
            elif record_type == "function_call_result":
                produced = self._parse_function_result(record, record_id, timestamp, call_index)
            else:
                # file-history-snapshot 等纯粹是 harness 记账
                stats.skipped_by_type[record_type] = stats.skipped_by_type.get(record_type, 0) + 1
                continue

            stats.used_records += 1
            events.extend(produced)

        result.events = events
        return result

    # ------------------------------------------------------------------
    def _absorb_metadata(self, record: dict[str, Any], metadata: SessionMetadata) -> None:
        for source_key, target in (
            ("cwd", "cwd"),
            ("sessionId", "session_id"),
        ):
            value = record.get(source_key)
            if value and not getattr(metadata, target):
                setattr(metadata, target, value)
        provider = record.get("providerData")
        if isinstance(provider, dict):
            model = provider.get("model")
            if isinstance(model, str) and model and not metadata.model:
                metadata.model = model

    # ------------------------------------------------------------------
    def _parse_message(
        self,
        record: dict[str, Any],
        record_id: str,
        timestamp: datetime | None,
        stats: WorkBuddyParseStats,
    ) -> list[AgentEvent]:
        role = (record.get("role") or "").lower()
        text = _join_blocks(record.get("content"))
        if not text:
            return []

        if role == "user":
            query = _extract_user_query(text)
            if query:
                stats.recovered_user_queries += 1
            else:
                # 没有 <user_query> 的用户消息：要么是纯注入，要么是用户直接输入
                if _looks_injected(text):
                    stats.injected_only_messages += 1
                    return []
                query = text
            return [
                self._event(
                    EventType.USER_MESSAGE,
                    record_id,
                    timestamp,
                    role="user",
                    content=query,
                    metadata={"kind": "user_message", "unwrapped": True},
                    significance=3,
                )
            ]

        if role == "assistant":
            return [
                self._event(
                    EventType.ASSISTANT_MESSAGE,
                    record_id,
                    timestamp,
                    role="assistant",
                    content=text,
                    metadata={"kind": "assistant_message"},
                    significance=2,
                )
            ]
        return []

    def _parse_reasoning(
        self, record: dict[str, Any], record_id: str, timestamp: datetime | None
    ) -> list[AgentEvent]:
        parts: list[str] = []
        raw_content = record.get("rawContent")
        if isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
        if not parts:
            fallback = _join_blocks(record.get("content"))
            if fallback:
                parts.append(fallback)
        if not parts:
            return []
        return [
            self._event(
                EventType.REASONING,
                record_id,
                timestamp,
                role="assistant",
                content=truncate("\n".join(parts), 4000),
                metadata={"kind": "reasoning"},
                significance=1,
            )
        ]

    def _parse_function_call(
        self,
        record: dict[str, Any],
        record_id: str,
        timestamp: datetime | None,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        name = str(record.get("name") or "unknown_tool")
        call_id = str(record.get("callId") or record_id)

        arguments: dict[str, Any] = {}
        raw_args = record.get("arguments")
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

        command = None
        for key in _COMMAND_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                command = value.strip()
                break

        paths: list[str] = []
        for key in _PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())

        metadata: dict[str, Any] = {
            "kind": "tool_call",
            "call_id": call_id,
            "arguments": arguments,
            "paths": paths,
        }
        category = _WORKBUDDY_TOOL_CATEGORY.get(name.lower())
        if category:
            metadata["category_hint"] = category

        event = self._event(
            EventType.TOOL_CALL,
            record_id,
            timestamp,
            role="assistant",
            content=truncate(raw_text, 4000),
            tool_name=name,
            command=command,
            file_path=paths[0] if paths else None,
            metadata=metadata,
            significance=2,
        )
        if call_id:
            call_index[call_id] = event
        return [event]

    def _parse_function_result(
        self,
        record: dict[str, Any],
        record_id: str,
        timestamp: datetime | None,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        call_id = str(record.get("callId") or "")
        status = str(record.get("status") or "").lower()
        output = _join_blocks(record.get("output"))
        is_failure = bool(status and status not in _OK_STATUSES)
        if not is_failure and output and _ERROR_HINT.search(output[:600]):
            is_failure = True

        call_event = call_index.get(call_id)
        if call_event is not None:
            call_event.metadata["matched"] = True

        metadata: dict[str, Any] = {
            "kind": "tool_output",
            "call_id": call_id,
            "status": status,
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
                record_id,
                timestamp,
                role="tool",
                content=truncate(output, 6000),
                result=truncate(output, 6000),
                tool_name=(call_event.tool_name if call_event else None) or record.get("name"),
                command=(call_event.command if call_event else None),
                metadata=metadata,
                significance=2 if is_failure else 1,
            )
        ]

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
        meta.setdefault("source_agent", "workbuddy")
        return AgentEvent(
            id=f"workbuddy:{ordinal}:{event_type.value}",
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


def read_records(path: str | Path, max_bytes: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """读取 JSONL 记录（复用容错读取器）。"""
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


def _join_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_user_query(text: str) -> str | None:
    """从含 harness 注入的文本里取出 ``<user_query>`` 的真实内容。"""
    match = _USER_QUERY_RE.search(text)
    if not match:
        return None
    query = match.group(1).strip()
    return query or None


def _looks_injected(text: str) -> bool:
    head = text.lstrip()[:400]
    return any(marker in head for marker in _INJECTED_MARKERS)


def _to_datetime(value: Any) -> datetime | None:
    """WorkBuddy 的时间戳是**毫秒整数**（也可能是秒或 ISO 字符串，做兼容）。"""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1_000_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        from amt.utils import parse_iso

        return parse_iso(value)
    return None
