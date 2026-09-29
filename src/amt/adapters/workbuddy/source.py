"""WorkBuddy 会话发现与 Source Adapter。

隐私边界（刻意设计）：本 Adapter 只在**用户显式调用** ``amt`` 且指定 workbuddy
作为来源时读取 ``~/.workbuddy/projects``。工具自身不会主动扫描该目录，
也不会把会话内容发往任何外部服务（除用户自行开启的 LLM 通道外）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from amt.adapters.base import SessionNotFoundError, SourceAdapter
from amt.adapters.workbuddy.detector import WorkBuddyDetector
from amt.adapters.workbuddy.parser import (
    WorkBuddyParseResult,
    WorkBuddyParser,
    read_records,
)
from amt.context import AppContext
from amt.core.models import AgentEvent, DetectionResult, RawSession, SessionInfo
from amt.utils import collapse, truncate

_HEADER_BYTES = 262_144


def decode_workspace_dir(name: str) -> str | None:
    """把转义后的工作区目录名还原成可读路径（仅用于展示）。

    例：``c-Users-HUAWEI-WorkBuddy-2026-09-11-14-31-00`` → ``C:\\Users\\HUAWEI\\WorkBuddy\\2026-09-11-14-31-00``
    转义是有损的（无法区分原始 ``-`` 与路径分隔符），因此只在没有记录内 cwd 时兜底。
    """
    if not name:
        return None
    if len(name) > 2 and name[0].isalpha() and name[1] == "-":
        rest = name[2:].replace("-", "\\")
        return f"{name[0].upper()}:\\{rest}"
    return name.replace("-", "\\")


class WorkBuddySessionDiscovery:
    def __init__(self, detector: WorkBuddyDetector) -> None:
        self.detector = detector
        self._parser = WorkBuddyParser()

    # ------------------------------------------------------------------
    def session_files(self) -> list[Path]:
        return self.detector.session_files()

    def scan(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        infos = [self._describe(path, deep=deep) for path in self.session_files()]
        infos.sort(
            key=lambda s: s.updated_at or s.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return infos[:limit] if limit is not None else infos

    def _describe(self, path: Path, deep: bool = False) -> SessionInfo:
        stat = path.stat()
        records, errors = read_records(path, max_bytes=None if deep else _HEADER_BYTES)
        raw = RawSession(
            agent="workbuddy",
            session_id=path.stem,
            path=str(path),
            records=records,
            parse_errors=errors,
            total_records=len(records) + errors,
        )
        parsed = self._parser.parse(raw)
        metadata = parsed.metadata

        title = None
        for event in parsed.events:
            if event.type.value == "user_message" and event.content:
                title = truncate(collapse(event.content), 80)
                break

        user_messages = sum(1 for e in parsed.events if e.type.value == "user_message")
        workspace = path.parent.name
        cwd = (metadata.cwd if metadata else None) or decode_workspace_dir(workspace)

        return SessionInfo(
            agent="workbuddy",
            session_id=(metadata.session_id if metadata else None) or path.stem,
            path=str(path),
            cwd=cwd,
            created_at=(metadata.created_at if metadata else None),
            updated_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
            title=title,
            size_bytes=stat.st_size,
            resumable=False,  # WorkBuddy 无 CLI 恢复入口
            model=metadata.model if metadata else None,
            record_count=(len(records) + errors) if deep else None,
            user_message_count=user_messages if deep else None,
            parse_errors=errors,
        )

    # ------------------------------------------------------------------
    def find(self, session_id: str) -> Path | None:
        if not session_id:
            return None
        needle = session_id.strip()
        for path in self.session_files():
            if path.stem == needle:
                return path
        for path in self.session_files():
            if needle in path.name:
                return path
        return None


class WorkBuddySourceAdapter(SourceAdapter):
    agent = "workbuddy"
    display_name = "WorkBuddy"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.detector = WorkBuddyDetector(ctx)
        self.discovery = WorkBuddySessionDiscovery(self.detector)
        self.parser = WorkBuddyParser()

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        return self.detector.detect()

    def list_sessions(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        return self.discovery.scan(limit=limit, deep=deep)

    def latest_session(self) -> SessionInfo | None:
        sessions = self.discovery.scan(limit=1)
        return sessions[0] if sessions else None

    def load_session(self, session_id: str) -> RawSession:
        path = self.discovery.find(session_id)
        if path is None:
            raise SessionNotFoundError(
                f"未找到 WorkBuddy 会话：{session_id}（搜索目录：{self.detector.projects_dir()}）"
            )
        records, errors = read_records(path)
        cwd = None
        real_id = path.stem
        for record in records:
            if record.get("cwd") and not cwd:
                cwd = str(record["cwd"])
            if record.get("sessionId"):
                real_id = str(record["sessionId"])
        return RawSession(
            agent="workbuddy",
            session_id=real_id,
            path=str(path),
            cwd=cwd,
            records=records,
            parse_errors=errors,
            total_records=len(records) + errors,
            format_version="workbuddy-jsonl",
        )

    def parse_events(self, raw: RawSession) -> list[AgentEvent]:
        return self.parser.parse(raw).events

    def load_result(self, session_id: str) -> tuple[RawSession, WorkBuddyParseResult]:
        raw = self.load_session(session_id)
        return raw, self.parser.parse(raw)
