"""Cursor 会话发现与 Source Adapter。

Cursor 没有「会话文件」，只有 SQLite 表。因此这里的 Discovery 比 JSONL 系
Agent 简单（一条 SQL 就是一次扫描），但需要处理两件事：

1. **数据库可能被 Cursor 占用** —— 只读打开 + immutable 兜底；
2. **会话缺少 cwd** —— Cursor 在 ``empty-window`` 里也能开对话，
   此时工作目录未知，`project.path` 会为空。这会让 Memory 无法核对真实代码，
   因此 SessionInfo.cwd 必须如实为 ``None``，而不是编一个路径出来。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from amt.adapters.base import SessionNotFoundError, SourceAdapter
from amt.adapters.cursor.detector import CursorDetector
from amt.adapters.cursor.parser import CursorParseResult, CursorParser, read_composers
from amt.context import AppContext
from amt.core.models import AgentEvent, DetectionResult, RawSession, SessionInfo
from amt.utils import collapse, truncate


def _ms_to_dt(value) -> datetime | None:
    if isinstance(value, (int, float)) and value > 1_000_000_000_000:
        try:
            return datetime.fromtimestamp(value / 1000).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    return None


class CursorSessionDiscovery:
    def __init__(self, detector: CursorDetector) -> None:
        self.detector = detector

    def databases(self) -> list[Path]:
        return self.detector.databases()

    def composers(self, include_archived: bool = True) -> list[dict]:
        out: list[dict] = []
        for db in self.databases():
            try:
                for composer in read_composers(db):
                    composer["_db"] = str(db)
                    if not include_archived and composer.get("archived"):
                        continue
                    # Cursor 会留下大量「空窗口 / 草稿」composer（没有气泡也没有名字），
                    # 它们不是真实会话，列出来只会干扰选择。
                    if not composer.get("bubbles"):
                        continue
                    out.append(composer)
            except Exception:
                # 单个库不可读不应影响整体扫描
                continue
        out.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or 0, reverse=True)
        return out

    def scan(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        infos: list[SessionInfo] = []
        for composer in self.composers():
            bubbles = composer.get("bubbles") or []
            data = composer.get("data") or {}
            title = None
            for bubble in bubbles:
                if bubble.get("type") == 1:
                    from amt.adapters.cursor.parser import _bubble_text

                    text = _bubble_text(bubble)
                    if text:
                        title = truncate(collapse(text), 80)
                        break
            if not title and data.get("name"):
                title = truncate(str(data["name"]), 80)

            user_count = sum(1 for b in bubbles if b.get("type") == 1)
            # 用气泡的原始 JSON 长度近似会话体积（Cursor 没有会话文件可量大小）
            size_bytes = sum(len(json.dumps(b, ensure_ascii=False)) for b in bubbles)
            model_config = data.get("modelConfig")
            infos.append(
                SessionInfo(
                    agent="cursor",
                    session_id=str(composer.get("composer_id")),
                    path=str(composer.get("_db") or ""),
                    cwd=None,  # Cursor 不记录 cwd，如实为 None
                    created_at=_ms_to_dt(composer.get("created_at")),
                    updated_at=_ms_to_dt(composer.get("updated_at")) or _ms_to_dt(composer.get("created_at")),
                    title=title,
                    size_bytes=size_bytes,
                    resumable=False,  # Cursor 不支持以 CLI 方式恢复会话
                    model=model_config.get("modelName") if isinstance(model_config, dict) else None,
                    record_count=len(bubbles) if deep else None,
                    user_message_count=user_count if deep else None,
                    archived=bool(composer.get("archived")),
                )
            )
        return infos[:limit] if limit is not None else infos


class CursorSourceAdapter(SourceAdapter):
    agent = "cursor"
    display_name = "Cursor"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.detector = CursorDetector(ctx)
        self.discovery = CursorSessionDiscovery(self.detector)
        self.parser = CursorParser()

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        return self.detector.detect()

    def list_sessions(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        return self.discovery.scan(limit=limit, deep=deep)

    def latest_session(self) -> SessionInfo | None:
        sessions = self.discovery.scan(limit=1)
        return sessions[0] if sessions else None

    def load_session(self, session_id: str) -> RawSession:
        for composer in self.discovery.composers():
            if str(composer.get("composer_id")) == session_id:
                data = composer.get("data") or {}
                records: list[dict] = [
                    {
                        "type": "__composer__",
                        "name": data.get("name") or composer.get("composer_id"),
                        "status": data.get("status"),
                        "model": (data.get("modelConfig") or {}).get("modelName")
                        if isinstance(data.get("modelConfig"), dict)
                        else None,
                    }
                ]
                for index, bubble in enumerate(composer.get("bubbles") or []):
                    records.append({"type": "__bubble__", "index": index, "bubble": bubble})
                return RawSession(
                    agent="cursor",
                    session_id=session_id,
                    path=str(composer.get("_db") or ""),
                    cwd=None,
                    records=records,
                    parse_errors=0,
                    total_records=len(records),
                    format_version="cursor-composer",
                )
        raise SessionNotFoundError(
            f"未找到 Cursor 会话：{session_id}（数据库：{[str(d) for d in self.discovery.databases()]}）"
        )

    def parse_events(self, raw: RawSession) -> list[AgentEvent]:
        return self.parser.parse(raw).events

    def load_result(self, session_id: str) -> tuple[RawSession, CursorParseResult]:
        raw = self.load_session(session_id)
        return raw, self.parser.parse(raw)
