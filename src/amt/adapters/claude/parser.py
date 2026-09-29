"""Claude Code 会话解析器。

Claude Code 的会话落在::

    ~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl

每行一条 JSON。**行式解析在这里是安全的**（不同于 Codex 的 rollout：Claude
把长内容转义在同一行内，不会出现跨物理行的记录），但为了与 Codex 侧保持一致的
健壮性，仍然复用容错的 raw_decode 读取器。

记录形态（按公开结构与 `claude --version 2.1.x` 的落盘约定实现）：

    {"type":"summary", "summary": "...", "leafUuid": "..."}                    → 会话摘要
    {"type":"user", "message":{"role":"user","content":"..."}, "uuid":...}     → 用户消息
    {"type":"user", "message":{"role":"user","content":[{"type":"tool_result",
        "tool_use_id":"toolu_...","content":"...","is_error":true}]}}          → 工具输出
    {"type":"assistant", "message":{"role":"assistant","content":[
        {"type":"text","text":"..."},
        {"type":"thinking","thinking":"..."},
        {"type":"tool_use","id":"toolu_...","name":"Bash","input":{...}}]}}     → 助手输出
    {"type":"system", "subtype": "...", ...}                                   → harness 记录

架构边界：本模块只做**结构转换**，输出 AgentEvent；不做语义归纳、不生成 Memory。

诚实说明：本机当前**没有 Claude Code 会话数据**（`~/.claude/projects` 不存在），
因此本解析器由合成夹具验证（复刻上述结构），未在本机真实数据上验证。
首次拉到真实会话后应复核 `read_records` 的字段假设。
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
from amt.utils import parse_iso, truncate

#: 由 harness 注入的用户侧文本（不是用户真实需求）
_INJECTED_MARKERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "Caveat: The messages below",
    "<user_instructions>",
    "<environment_context>",
    "This session is being continued from a previous conversation",
)

#: Claude 工具名 → 语义类别（跨 Agent 工具表的 Claude 侧补充）
_CLAUDE_TOOL_CATEGORY: dict[str, str] = {
    "bash": "terminal",
    "bashoutput": "terminal",
    "killshell": "terminal",
    "edit": "file_edit",
    "multiedit": "file_edit",
    "write": "file_edit",
    "notebookedit": "file_edit",
    "read": "file_read",
    "notebookread": "file_read",
    "glob": "file_read",
    "grep": "file_read",
    "ls": "file_read",
    "todowrite": "plan",
    "exitplanmode": "plan",
    "webfetch": "tool_call",
    "websearch": "tool_call",
    "task": "tool_call",
}

#: 从工具入参里取「命令 / 路径」的键
_COMMAND_KEYS = ("command", "cmd")
_PATH_KEYS = ("file_path", "notebook_path", "path", "filePath")

_ERROR_HINT = re.compile(
    r"^(?:Error|error|EACCES|ENOENT|fatal|Traceback|Exception)\b|"
    r"<tool_use_error>|command not found|No such file",
    re.MULTILINE,
)

#: 纯 harness 记账记录：不是任务状态，必须丢弃
#: （本机真实会话里 ``attachment`` / ``queue-operation`` / ``cost-state`` 等
#: 占了大半，若当成事件会严重污染 Memory）
_BOOKKEEPING_TYPES = frozenset(
    {"queue-operation", "last-prompt", "cost-state", "atis-latch", "file-history-snapshot"}
)

#: CLI 未登录 / 无额度时助手消息的标记（本机实测：``isApiErrorMessage`` 为真，
#: 正文是 "Not logged in · Please run /login"）
_API_ERROR_HINT = re.compile(r"(?i)not logged in|please run /login|invalid api key|credit balance")


class ClaudeParseStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_records: int = 0
    used_records: int = 0
    parse_errors: int = 0
    skipped_by_type: dict[str, int] = Field(default_factory=dict)
    injected_user_texts: int = 0
    sidechain_skipped: int = 0


@dataclass
class ClaudeParseResult:
    events: list[AgentEvent] = field(default_factory=list)
    metadata: SessionMetadata | None = None
    stats: ClaudeParseStats = field(default_factory=ClaudeParseStats)


class ClaudeCodeParser:
    """Claude Code JSONL → AgentEvent[]。"""

    format_name = "claude-code-jsonl"

    def parse(self, raw: RawSession) -> ClaudeParseResult:
        result = ClaudeParseResult()
        stats = result.stats
        stats.total_records = raw.total_records or len(raw.records)
        stats.parse_errors = raw.parse_errors

        metadata = SessionMetadata(agent="claude", session_id=raw.session_id, cwd=raw.cwd)
        result.metadata = metadata

        events: list[AgentEvent] = []
        call_index: dict[str, AgentEvent] = {}

        for index, record in enumerate(raw.records):
            record_type = record.get("type")
            if not isinstance(record_type, str):
                stats.skipped_by_type["<no-type>"] = stats.skipped_by_type.get("<no-type>", 0) + 1
                continue

            # 侧链（subagent / Task）默认跳过：它是子任务的执行记录，
            # 混进主任务状态会误导目标 Agent。
            if record.get("isSidechain"):
                stats.sidechain_skipped += 1
                continue

            self._absorb_metadata(record, metadata)
            timestamp = parse_iso(record.get("timestamp"))
            uuid = record.get("uuid") or index

            produced: list[AgentEvent] = []
            if record_type == "summary":
                produced = [
                    self._event(
                        EventType.SESSION_META,
                        uuid,
                        timestamp,
                        content=str(record.get("summary") or "")[:2000],
                        metadata={"kind": "claude_summary"},
                        significance=3,
                    )
                ]
            elif record_type == "user":
                produced = self._parse_user(record, uuid, timestamp, stats, call_index)
            elif record_type == "assistant":
                produced = self._parse_assistant(record, uuid, timestamp, stats, call_index)
            elif record_type in _BOOKKEEPING_TYPES:
                # 队列操作 / 最后提示词 / 成本状态 / 内部闩锁：都是 harness 记账，
                # 不是任务状态（真实数据里这些占了相当比例）。
                stats.skipped_by_type[record_type] = stats.skipped_by_type.get(record_type, 0) + 1
                continue
            elif record_type == "attachment":
                # 环境快照 / 文件附件：harness 注入的上下文，不是用户说的话
                stats.skipped_by_type["attachment"] = stats.skipped_by_type.get("attachment", 0) + 1
                continue
            elif record_type == "system":
                stats.skipped_by_type[f"system/{record.get('subtype')}"] = (
                    stats.skipped_by_type.get(f"system/{record.get('subtype')}", 0) + 1
                )
            else:
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
            ("gitBranch", "__git_branch"),
            ("version", "cli_version"),
            ("entrypoint", "__entrypoint"),
        ):
            value = record.get(source_key)
            if not value:
                continue
            if target.startswith("__"):
                metadata.extra[target.strip("_")] = value
            else:
                setattr(metadata, target, value)

        # cost-state 记录了整份会话的统计（行数变化 / 成本 / 模型用量），
        # 这是**真实数据**，可以进 Memory 的 stats 供报告展示。
        if record.get("type") == "cost-state":
            usage = record.get("modelUsage")
            if isinstance(usage, dict) and usage:
                metadata.extra["model_usage"] = usage
            for key in ("totalLinesAdded", "totalLinesRemoved", "totalCostUSD"):
                value = record.get(key)
                if isinstance(value, (int, float)):
                    metadata.extra[key] = value
        if record.get("type") == "attachment" and not metadata.cwd:
            snapshot = (record.get("attachment") or {}).get("snapshot")
            if isinstance(snapshot, dict) and snapshot.get("workingDirectory"):
                metadata.cwd = str(snapshot["workingDirectory"])

    def _parse_user(
        self,
        record: dict[str, Any],
        uuid: Any,
        timestamp: datetime | None,
        stats: ClaudeParseStats,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")

        # 工具输出以 user 记录回传（Claude Code 的约定）
        if isinstance(content, list):
            has_result = any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
            if has_result:
                return self._parse_tool_results(record, content, uuid, timestamp, call_index)
            text = _join_text_blocks(content)
        else:
            text = content if isinstance(content, str) else ""

        text = (text or "").strip()
        if not text:
            return []

        if record.get("isMeta") or _is_injected(text):
            stats.injected_user_texts += 1
            return []

        return [
            self._event(
                EventType.USER_MESSAGE,
                uuid,
                timestamp,
                role="user",
                content=text,
                metadata={"kind": "user_message"},
                significance=3,
            )
        ]

    def _parse_tool_results(
        self,
        record: dict[str, Any],
        content: list[Any],
        uuid: Any,
        timestamp: datetime | None,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        tool_use_result = record.get("toolUseResult")

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            output = _join_tool_result(block.get("content"))
            is_error = bool(block.get("is_error"))
            exit_code = _extract_exit_code(is_error, tool_use_result, output)
            if not is_error and exit_code is None and output and _ERROR_HINT.search(output[:600]):
                is_error = True

            call_event = call_index.get(call_id)
            if call_event is not None:
                call_event.metadata["matched"] = True

            metadata: dict[str, Any] = {
                "kind": "tool_output",
                "call_id": call_id,
                "exit_code": exit_code,
                "is_failure": is_error,
                "matched": call_event is not None,
            }
            if call_event is not None:
                metadata["tool_name"] = call_event.tool_name
                if call_event.command:
                    metadata["command"] = call_event.command
                if call_event.metadata.get("paths"):
                    metadata["paths"] = call_event.metadata.get("paths")

            events.append(
                self._event(
                    EventType.TOOL_RESULT,
                    f"{uuid}:{call_id[-8:]}",
                    timestamp,
                    role="tool",
                    content=truncate(output, 6000),
                    result=truncate(output, 6000),
                    tool_name=(call_event.tool_name if call_event else None),
                    command=(call_event.command if call_event else None),
                    metadata=metadata,
                    significance=2 if is_error else 1,
                )
            )
        return events

    def _parse_assistant(
        self,
        record: dict[str, Any],
        uuid: Any,
        timestamp: datetime | None,
        stats: ClaudeParseStats,
        call_index: dict[str, AgentEvent],
    ) -> list[AgentEvent]:
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else []
        )

        events: list[AgentEvent] = []
        texts: list[str] = []

        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
                value = (block.get("text") or "").strip()
                if value:
                    texts.append(value)
            elif block_type == "thinking":
                value = (block.get("thinking") or "").strip()
                if value:
                    events.append(
                        self._event(
                            EventType.REASONING,
                            f"{uuid}:thinking",
                            timestamp,
                            role="assistant",
                            content=truncate(value, 4000),
                            metadata={"kind": "thinking"},
                            significance=1,
                        )
                    )
            elif block_type == "tool_use":
                event = self._tool_call_event(block, uuid, timestamp)
                call_id = str(block.get("id") or "")
                if call_id:
                    call_index[call_id] = event
                events.append(event)
            elif block_type == "tool_result":
                # 少数版本会把 tool_result 混在 assistant 里
                stats.skipped_by_type["assistant/tool_result"] = (
                    stats.skipped_by_type.get("assistant/tool_result", 0) + 1
                )

        # API 错误（未登录 / 无额度）：这是**任务中断的直接原因**，
        # 必须作为 ERROR 保留，目标 Agent 需要知道上一次为什么停了。
        if record.get("isApiErrorMessage") or (
            texts and _API_ERROR_HINT.search(texts[0][:200])
        ):
            body = "\n\n".join(texts) or str(record.get("error") or "API 调用失败")
            return [
                self._event(
                    EventType.ERROR,
                    uuid,
                    timestamp,
                    role="assistant",
                    content=truncate(body, 2000),
                    metadata={
                        "kind": "api_error",
                        "error": record.get("error"),
                        "model": (message.get("model") if isinstance(message, dict) else None),
                    },
                    significance=3,
                )
            ]

        if texts:
            # 一条 assistant 记录里的多个 text 块属于同一轮回复，合并为一条
            events.insert(
                0,
                self._event(
                    EventType.ASSISTANT_MESSAGE,
                    uuid,
                    timestamp,
                    role="assistant",
                    content="\n\n".join(texts),
                    metadata={"kind": "assistant_message"},
                    significance=2,
                ),
            )
        return events

    def _tool_call_event(
        self, block: dict[str, Any], uuid: Any, timestamp: datetime | None
    ) -> AgentEvent:
        name = str(block.get("name") or "unknown_tool")
        tool_input = block.get("input")
        arguments = tool_input if isinstance(tool_input, dict) else {}
        call_id = str(block.get("id") or "")

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

        category_hint = _CLAUDE_TOOL_CATEGORY.get(name.lower())
        metadata: dict[str, Any] = {
            "kind": "tool_call",
            "call_id": call_id,
            "arguments": arguments,
            "paths": paths,
        }
        if category_hint:
            metadata["category_hint"] = category_hint

        return self._event(
            EventType.TOOL_CALL,
            f"{uuid}:{call_id[-8:]}" if call_id else uuid,
            timestamp,
            role="assistant",
            content=truncate(json.dumps(arguments, ensure_ascii=False), 4000),
            tool_name=name,
            command=command,
            file_path=paths[0] if paths else None,
            metadata=metadata,
            significance=2,
        )

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
        meta.setdefault("source_agent", "claude")
        return AgentEvent(
            id=f"claude:{ordinal}:{event_type.value}",
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


def _join_text_blocks(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _join_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _is_injected(text: str) -> bool:
    head = text.lstrip()[:300]
    return any(marker in head for marker in _INJECTED_MARKERS)


def _extract_exit_code(is_error: bool, tool_use_result: Any, output: str) -> int | None:
    """从 toolUseResult 或输出里推断退出码。"""
    if isinstance(tool_use_result, dict):
        for key in ("exitCode", "exit_code", "code"):
            value = tool_use_result.get(key)
            if isinstance(value, int):
                return value
        if tool_use_result.get("interrupted"):
            return 130
        stderr = tool_use_result.get("stderr")
        if isinstance(stderr, str) and stderr.strip() and is_error:
            return 1
    match = re.search(r"(?:exited with code|Exit code:?)\s*(-?\d+)", output or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    if is_error:
        return 1
    return None


def read_records(path: str | Path, max_bytes: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """读取 JSONL 记录（复用 Codex 侧的容错读取器）。"""
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
