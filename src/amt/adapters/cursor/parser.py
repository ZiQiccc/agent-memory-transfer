"""Cursor 会话解析器。

Cursor 是 VS Code 系产品，会话**不存在 JSONL 里**，而是落在 SQLite::

    <User>/globalStorage/state.vscdb
        ├── composerHeaders                        会话头（composerId / 时间 / 是否归档）
        └── cursorDiskKV
              ├── composerData:<composerId>        会话元数据（name / status / 模型 / 统计）
              └── bubbleId:<composerId>:<bubbleId> 消息气泡

气泡结构（本机实测 Cursor 的真实数据）::

    {"type": 1, "text": "你是什么模型", "richText": ..., "modelInfo": {"modelName": "grok-4.6"},
     "createdAt": "2026-09-11T01:46:04.962Z"}                         ← 用户
    {"type": 2, "text": "我是 **Cursor Grok 4.6** ...", "createdAt": ...}  ← 助手
    {"type": 2, "text": "", "thinking": {"text": "...", "signature": ""}}  ← 仅思考的助手气泡

要点：
1. 气泡的 key 是 UUID，**排序必须按 ``createdAt``**，不能按 key。
2. ``text`` 为空但 ``thinking`` 非空的气泡是「思考块」，映射为 REASONING 而不是空消息。
3. Cursor 没有工具调用记录（编辑以 diff 形式存在），因此该 Agent 的
   ``modified_files`` 依赖 ``assistantSuggestedDiffs``；本机数据中没有 diff，
   该分支**未在真实数据上验证**（已在 README 标注）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amt.core.models import AgentEvent, EventType, RawSession, SessionMetadata
from amt.utils import parse_iso, truncate


class CursorParseStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_records: int = 0
    used_records: int = 0
    parse_errors: int = 0
    skipped_by_type: dict[str, int] = Field(default_factory=dict)


@dataclass
class CursorParseResult:
    events: list[AgentEvent] = field(default_factory=list)
    metadata: SessionMetadata | None = None
    stats: CursorParseStats = field(default_factory=CursorParseStats)


class CursorParser:
    """Cursor composer（会话）→ AgentEvent[]。"""

    format_name = "cursor-composer"

    def parse(self, raw: RawSession) -> CursorParseResult:
        result = CursorParseResult()
        stats = result.stats
        stats.total_records = raw.total_records or len(raw.records)

        metadata = SessionMetadata(agent="cursor", session_id=raw.session_id, cwd=raw.cwd)
        result.metadata = metadata

        events: list[AgentEvent] = []
        for record in raw.records:
            kind = record.get("type")
            if kind == "__composer__":
                # composerData：补充会话元信息
                if record.get("name") and not metadata.extra.get("composer_name"):
                    metadata.extra["composer_name"] = record["name"]
                if record.get("model"):
                    metadata.model = record["model"]
                metadata.extra.setdefault("composer_status", record.get("status"))
                stats.used_records += 1
                continue

            if kind == "__bubble__":
                bubble = record.get("bubble") or {}
                if not isinstance(bubble, dict):
                    stats.parse_errors += 1
                    continue
                produced = self._parse_bubble(bubble, record.get("index"))
                if produced:
                    stats.used_records += 1
                    events.extend(produced)
                else:
                    stats.skipped_by_type["bubble/empty"] = (
                        stats.skipped_by_type.get("bubble/empty", 0) + 1
                    )
                if not metadata.model:
                    model = (bubble.get("modelInfo") or {}).get("modelName")
                    if isinstance(model, str):
                        metadata.model = model
                continue

            stats.skipped_by_type[str(kind)] = stats.skipped_by_type.get(str(kind), 0) + 1

        result.events = events
        return result

    # ------------------------------------------------------------------
    def _parse_bubble(self, bubble: dict[str, Any], index: Any) -> list[AgentEvent]:
        bubble_type = bubble.get("type")
        created = bubble.get("createdAt") or bubble.get("createdAtMs")
        timestamp = parse_iso(created) if isinstance(created, str) else _from_millis(created)
        bubble_id = str(bubble.get("bubbleId") or index)

        events: list[AgentEvent] = []
        text = _bubble_text(bubble)
        thinking = _thinking_text(bubble)

        if bubble_type == 1:
            if text:
                events.append(
                    self._event(
                        EventType.USER_MESSAGE,
                        bubble_id,
                        timestamp,
                        role="user",
                        content=text,
                        significance=3,
                    )
                )
            return events

        if bubble_type == 2:
            if thinking:
                events.append(
                    self._event(
                        EventType.REASONING,
                        f"{bubble_id}:thinking",
                        timestamp,
                        role="assistant",
                        content=thinking,
                        metadata={"kind": "thinking"},
                        significance=1,
                    )
                )
            if text:
                events.append(
                    self._event(
                        EventType.ASSISTANT_MESSAGE,
                        bubble_id,
                        timestamp,
                        role="assistant",
                        content=text,
                        significance=2,
                    )
                )
            events.extend(self._parse_edits(bubble, bubble_id, timestamp))
            events.extend(self._parse_tool_results(bubble, bubble_id, timestamp))
            return events

        return events

    def _parse_edits(self, bubble: dict[str, Any], bubble_id: str, timestamp) -> list[AgentEvent]:
        """Cursor 的代码改动以 diff 存在（本机数据无样本，按结构防御式实现）。"""
        events: list[AgentEvent] = []
        for key in ("assistantSuggestedDiffs", "diffsSinceLastApply", "humanChanges"):
            diffs = bubble.get(key)
            if not isinstance(diffs, list) or not diffs:
                continue
            paths: list[str] = []
            added = removed = hunks = 0
            for diff in diffs:
                if not isinstance(diff, dict):
                    continue
                path = (
                    diff.get("uri", {}).get("path")
                    if isinstance(diff.get("uri"), dict)
                    else diff.get("path") or diff.get("filePath") or diff.get("relativeWorkspacePath")
                )
                if isinstance(path, str) and path and path not in paths:
                    paths.append(path)
                for line in str(diff.get("diff") or diff.get("text") or "").splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        removed += 1
                    elif line.startswith("@@"):
                        hunks += 1
            if not paths:
                continue
            stats = {p: {"hunks": hunks, "added": added, "removed": removed} for p in paths}
            events.append(
                self._event(
                    EventType.TOOL_CALL,
                    f"{bubble_id}:{key}",
                    timestamp,
                    role="assistant",
                    tool_name="cursor_apply_diff",
                    file_path=paths[0],
                    content=truncate(json.dumps(diffs, ensure_ascii=False), 3000),
                    metadata={
                        "kind": "tool_call",
                        "category_hint": "file_edit",
                        "paths": paths,
                        "patch_file_stats": stats,
                        "patch_statuses": {p: "modified" for p in paths},
                        "unverified": True,
                    },
                    significance=2,
                )
            )
        return events

    def _parse_tool_results(self, bubble: dict[str, Any], bubble_id: str, timestamp) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        results = bubble.get("toolResults")
        if not isinstance(results, list):
            return events
        for position, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            output = item.get("result") or item.get("output") or item.get("content") or ""
            if isinstance(output, (dict, list)):
                output = json.dumps(output, ensure_ascii=False)
            events.append(
                self._event(
                    EventType.TOOL_RESULT,
                    f"{bubble_id}:tool{position}",
                    timestamp,
                    role="tool",
                    tool_name=str(item.get("name") or item.get("toolName") or "cursor_tool"),
                    content=truncate(str(output), 4000),
                    result=truncate(str(output), 4000),
                    metadata={
                        "kind": "tool_output",
                        "is_failure": bool(item.get("isError") or item.get("is_error")),
                        "exit_code": None,
                        "unverified": True,
                    },
                    significance=1,
                )
            )
        return events

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
        file_path: str | None = None,
        result: str | None = None,
        metadata: dict[str, Any] | None = None,
        significance: int = 1,
    ) -> AgentEvent:
        meta = dict(metadata or {})
        meta["ordinal"] = ordinal
        meta["significance"] = significance
        meta.setdefault("source_agent", "cursor")
        return AgentEvent(
            id=f"cursor:{ordinal}:{event_type.value}",
            timestamp=timestamp,
            type=event_type,
            role=role,
            content=content,
            tool_name=tool_name,
            file_path=file_path,
            command=None,
            result=result,
            metadata=meta,
        )


# ----------------------------------------------------------------------
# Cursor 数据库读取
# ----------------------------------------------------------------------


def open_readonly(db_path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    """以只读方式打开 Cursor 的 SQLite。

    Cursor 运行时会持有锁；优先 ``mode=ro``，失败则退到 ``immutable=1``
    （可能读到略旧的快照，但不会因加锁失败而让整个迁移中断）。
    """
    db_path = Path(db_path)
    attempts = (
        f"file:{db_path.as_posix()}?mode=ro",
        f"file:{db_path.as_posix()}?mode=ro&immutable=1",
    )
    last_error: Exception | None = None
    for uri in attempts:
        try:
            return sqlite3.connect(uri, uri=True, timeout=timeout)
        except sqlite3.Error as exc:  # pragma: no cover - 依赖本机状态
            last_error = exc
    raise sqlite3.Error(f"无法以只读方式打开 {db_path}：{last_error}")


def read_composers(db_path: Path) -> list[dict[str, Any]]:
    """读取全部 composer 的元数据（含气泡）。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    con = open_readonly(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent FROM composerHeaders")
        headers = cur.fetchall()
        for composer_id, workspace_id, created, updated, archived, subagent in headers:
            if not composer_id or composer_id in ("empty-state-draft",):
                continue
            data: dict[str, Any] = {}
            try:
                cur.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"composerData:{composer_id}",))
                row = cur.fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
            except (sqlite3.Error, ValueError):
                data = {}

            bubbles: list[dict[str, Any]] = []
            try:
                cur.execute(
                    "SELECT value FROM cursorDiskKV WHERE key LIKE ?", (f"bubbleId:{composer_id}:%",)
                )
                for (value,) in cur.fetchall():
                    try:
                        bubble = json.loads(value)
                    except ValueError:
                        continue
                    if isinstance(bubble, dict):
                        bubbles.append(bubble)
            except sqlite3.Error:
                bubbles = []

            # 气泡的 key 是 UUID，必须按时间排序才能还原对话顺序
            bubbles.sort(key=lambda b: str(b.get("createdAt") or b.get("createdAtMs") or ""))

            out.append(
                {
                    "composer_id": composer_id,
                    "workspace_id": workspace_id,
                    "created_at": created,
                    "updated_at": updated,
                    "archived": bool(archived),
                    "subagent": bool(subagent),
                    "data": data,
                    "bubbles": bubbles,
                }
            )
    finally:
        con.close()
    return out


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _from_millis(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 1_000_000_000_000:
        try:
            return datetime.fromtimestamp(value / 1000).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _bubble_text(bubble: dict[str, Any]) -> str:
    text = bubble.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    # 部分版本把用户输入放在 richText 的 ProseMirror 文档里
    rich = bubble.get("richText")
    if isinstance(rich, str):
        collapsed = rich.strip()
        if collapsed.startswith("{"):
            try:
                return _prosemirror_text(json.loads(collapsed))
            except ValueError:
                return ""
        return collapsed
    if isinstance(rich, dict):
        return _prosemirror_text(rich)
    return ""


def _prosemirror_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []
    if node.get("type") == "text" and isinstance(node.get("text"), str):
        parts.append(node["text"])
    for child in node.get("content") or []:
        parts.append(_prosemirror_text(child))
    text = "".join(parts)
    if node.get("type") == "paragraph" and text:
        return text + "\n"
    return text


def _thinking_text(bubble: dict[str, Any]) -> str:
    thinking = bubble.get("thinking")
    if isinstance(thinking, dict):
        text = thinking.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()

    blocks = bubble.get("allThinkingBlocks")
    if isinstance(blocks, list):
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict):
                value = block.get("text") or block.get("thinking")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        if parts:
            return "\n\n".join(parts)
    return ""
