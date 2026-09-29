"""ClaudeCodeSourceAdapter —— 从 Claude Code 会话恢复任务状态。

这是 Phase 2 的一半：让 Canonical Memory 的**反向**通路成立（Claude → Memory），
从而与 Codex Source 形成对称，验证协议与 Agent 无关。
"""

from __future__ import annotations

from pathlib import Path

from amt.adapters.base import SessionNotFoundError, SourceAdapter
from amt.adapters.claude.detector import ClaudeDetector
from amt.adapters.claude.discovery import ClaudeSessionDiscovery
from amt.adapters.claude.parser import ClaudeCodeParser, ClaudeParseResult, read_records
from amt.context import AppContext
from amt.core.models import AgentEvent, DetectionResult, RawSession, SessionInfo


class ClaudeCodeSourceAdapter(SourceAdapter):
    agent = "claude"
    display_name = "Claude Code"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.detector = ClaudeDetector(ctx)
        self.discovery = ClaudeSessionDiscovery(self.detector.projects_dir())
        self.parser = ClaudeCodeParser()

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        return self.detector.detect_source()

    def list_sessions(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        return self.discovery.scan(limit=limit, deep=deep)

    def latest_session(self) -> SessionInfo | None:
        sessions = self.discovery.scan(limit=1)
        return sessions[0] if sessions else None

    def load_session(self, session_id: str) -> RawSession:
        path = self.discovery.find(session_id)
        if path is None:
            raise SessionNotFoundError(
                f"未找到 Claude Code 会话：{session_id}（搜索目录：{self.discovery.projects_dir}）"
            )
        records, errors = read_records(path)
        cwd = None
        real_id = path.stem
        for record in records:
            if record.get("cwd"):
                cwd = str(record["cwd"])
            if record.get("sessionId"):
                real_id = str(record["sessionId"])
            if cwd and real_id != path.stem:
                break
        return RawSession(
            agent="claude",
            session_id=real_id,
            path=str(path),
            cwd=cwd,
            records=records,
            parse_errors=errors,
            total_records=len(records) + errors,
            format_version="claude-code-jsonl",
        )

    def parse_events(self, raw: RawSession) -> list[AgentEvent]:
        return self.parser.parse(raw).events

    def load_result(self, session_id: str) -> tuple[RawSession, ClaudeParseResult]:
        raw = self.load_session(session_id)
        return raw, self.parser.parse(raw)

    # ------------------------------------------------------------------
    def collect_project_state(self, cwd: str | None):
        """Claude 会话里记录了 gitBranch，但没有 Git 状态；统一按真实仓库采集。"""
        return self.ctx.collector.collect(cwd)

    def projects_dir(self) -> Path:
        return self.detector.projects_dir()
