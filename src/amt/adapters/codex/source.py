"""Codex Source Adapter —— 聚合探测器 / 发现器 / 解析器。

    CodexAdapter
    ├── CodexDetector            检测安装
    ├── CodexSessionDiscovery    找回话
    ├── CodexFileSource          ← MVP：~/.codex/sessions/**/*.jsonl
    └── CodexParser              JSONL → AgentEvent[]

V2 将增加 ``CodexAppServerSource``（thread/list、thread/read、thread/resume），
与 FileSource 并存——这样第一阶段不依赖 app-server 是否正常。
"""

from __future__ import annotations

from pathlib import Path

from amt.adapters.base import SessionNotFoundError, SourceAdapter
from amt.adapters.codex.detector import CodexDetector
from amt.adapters.codex.discovery import CodexSessionDiscovery
from amt.adapters.codex.parser import CodexParseResult, CodexParser, read_records
from amt.context import AppContext
from amt.core.models import AgentEvent, DetectionResult, RawSession, SessionInfo
from amt.utils import parse_iso


class CodexSourceAdapter(SourceAdapter):
    agent = "codex"
    display_name = "Codex"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.detector = CodexDetector(ctx)
        self.discovery = CodexSessionDiscovery(ctx.config.codex.sessions_dir())
        self.parser = CodexParser()

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        return self.detector.detect()

    def list_sessions(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        return self.discovery.scan(limit=limit, deep=deep)

    def latest_session(self) -> SessionInfo | None:
        sessions = self.discovery.scan(limit=1)
        return sessions[0] if sessions else None

    # ------------------------------------------------------------------
    def load_session(self, session_id: str) -> RawSession:
        path = self.discovery.find(session_id)
        if path is None:
            raise SessionNotFoundError(
                f"未找到 Codex 会话：{session_id}（搜索目录 {self.discovery.sessions_dir}）"
            )

        records, errors = read_records(path)
        file_sid, file_stamp = self.discovery.parse_filename(path)

        resolved_sid = file_sid or session_id
        cwd: str | None = None
        created = file_stamp
        for record in records:
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                resolved_sid = payload.get("session_id") or payload.get("id") or resolved_sid
                cwd = payload.get("cwd") or cwd
                created = parse_iso(payload.get("timestamp")) or created
            break

        return RawSession(
            agent="codex",
            session_id=resolved_sid,
            path=str(path),
            cwd=cwd,
            records=records,
            parse_errors=errors,
            total_records=len(records) + errors,
            format_version="codex-rollout-jsonl",
        )

    def parse_events(self, raw: RawSession) -> list[AgentEvent]:
        return self.parser.parse(raw).events

    # ------------------------------------------------------------------
    def load_result(self, session_id: str) -> tuple[RawSession, CodexParseResult]:
        """一次拿到原始会话与解析结果（含元信息与统计）。"""
        raw = self.load_session(session_id)
        return raw, self.parser.parse(raw)

    def session_path(self, session_id: str) -> Path | None:
        return self.discovery.find(session_id)
