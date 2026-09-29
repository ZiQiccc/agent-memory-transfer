"""Codex 会话发现（实现plan §五 / §六）。

落盘形式（本机实测）::

    ~/.codex/sessions/2026/09/08/rollout-2026-09-08T15-49-34-<uuid>.jsonl

原则：**不单纯依赖文件名**。``session_id`` / ``cwd`` / 时间戳优先从
``session_meta`` 记录内容读取，文件名只作为索引线索与快速匹配手段。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from amt.adapters.codex.parser import CodexParser, read_records
from amt.core.models import RawSession, SessionInfo
from amt.utils import collapse, parse_iso, truncate

_FILE_RE = re.compile(
    r"^rollout-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<sid>[0-9a-fA-F-]{36})\.jsonl$"
)

#: 读取会话头部用于列表展示的字节上限（避免为了列表把 5MB 会话全读一遍）
_HEADER_BYTES = 262_144

#: 用户消息里常见的模板噪音行——不适合做标题
#: 注意：比较时标题行的 ``#`` 已被剥掉，因此这里不加 ``#`` 前缀。
_TITLE_NOISE = (
    "Files mentioned by the user",
    "Files pasted by the user",
    "AGENTS.md instructions",
    "environment_context",
    "app-context",
)


def _derive_title(text: str, limit: int = 80) -> str | None:
    """从用户首条消息里提炼一个可读标题。

    跳过 harness 模板噪音行与代码围栏，取第一条有信息量的行；
    若整段都不可用，退化为压缩后的整段文本。
    """
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if not line or line.startswith("```"):
            continue
        if any(noise in line for noise in _TITLE_NOISE):
            continue
        if len(line) < 2:
            continue
        return truncate(line, limit)
    collapsed = truncate(collapse(text), limit)
    return collapsed or None


class CodexSessionDiscovery:
    def __init__(self, sessions_dir: Path, include_archived: bool = True) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.include_archived = include_archived
        self._parser = CodexParser()

    # ------------------------------------------------------------------
    def session_files(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        files = [p for p in self.sessions_dir.rglob("rollout-*.jsonl") if p.is_file()]
        if self.include_archived:
            archive = self.sessions_dir.parent / "archived_sessions"
            if archive.is_dir():
                files.extend(p for p in archive.rglob("*.jsonl") if p.is_file())
        return files

    @staticmethod
    def parse_filename(path: Path) -> tuple[str | None, datetime | None]:
        """从文件名提取 (session_id, 时间戳)，失败返回 (None, None)。"""
        match = _FILE_RE.match(path.name)
        if not match:
            return None, None
        return match.group("sid"), parse_iso(match.group("stamp"))

    # ------------------------------------------------------------------
    def scan(self, limit: int | None = None, deep: bool = False) -> list[SessionInfo]:
        """扫描会话列表。

        ``deep=False``（默认）只读文件头，速度快；此时 ``user_message_count`` /
        ``record_count`` 为 None——**不谎报**为 0。
        """
        infos: list[SessionInfo] = []
        for path in self.session_files():
            infos.append(self._describe(path, deep=deep))

        infos.sort(key=lambda s: s.updated_at or s.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if limit is not None:
            infos = infos[:limit]
        return infos

    def _describe(self, path: Path, deep: bool = False) -> SessionInfo:
        stat = path.stat()
        file_sid, file_stamp = self.parse_filename(path)

        records, errors = read_records(path, max_bytes=None if deep else _HEADER_BYTES)
        raw = RawSession(
            agent="codex",
            session_id=file_sid or path.stem,
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
                title = _derive_title(event.content)
                if title:
                    break

        user_messages = sum(1 for e in parsed.events if e.type.value == "user_message")

        return SessionInfo(
            agent="codex",
            session_id=(metadata.session_id if metadata else None) or file_sid or path.stem,
            path=str(path),
            cwd=(metadata.cwd if metadata else None),
            created_at=(metadata.created_at if metadata else None) or file_stamp,
            updated_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
            title=title,
            size_bytes=stat.st_size,
            resumable=True,
            model=metadata.model if metadata else None,
            record_count=(len(records) + errors) if deep else None,
            user_message_count=user_messages if deep else None,
            parse_errors=errors,
            archived="archived_sessions" in path.parts,
        )

    # ------------------------------------------------------------------
    def find(self, session_id: str) -> Path | None:
        """按 session_id 定位会话文件。

        先用文件名（含 uuid）快速匹配；未命中再读文件头比对 ``session_meta``，
        以应对「文件名与索引不一致」的情况。
        """
        if not session_id:
            return None
        needle = session_id.strip()

        for path in self.session_files():
            file_sid, _ = self.parse_filename(path)
            if file_sid and file_sid == needle:
                return path

        # 退化匹配：文件名里包含该 id
        for path in self.session_files():
            if needle in path.name:
                return path

        # 最后读文件头（只读前 32KB，足够覆盖 session_meta）
        for path in self.session_files():
            records, _ = read_records(path, max_bytes=32_768)
            for record in records:
                payload = record.get("payload")
                if record.get("type") == "session_meta" and isinstance(payload, dict):
                    if payload.get("session_id") == needle or payload.get("id") == needle:
                        return path
        return None
