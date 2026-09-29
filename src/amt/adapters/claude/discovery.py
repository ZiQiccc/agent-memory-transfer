"""Claude Code 会话发现。

落盘约定::

    ~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl

``<escaped-cwd>`` 是把工作目录里的非字母数字字符替换成 ``-`` 得到的，
例如 ``D:\\Java_project\\CYKJ\\cykj-mes`` → ``D--Java-project-CYKJ-cykj-mes``。

原则与 Codex 侧一致：**不单纯依赖目录名**。真实的 ``cwd`` / ``sessionId``
优先从记录内容读取，目录名只用于「没有记录时的兜底展示」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from amt.adapters.claude.parser import ClaudeCodeParser, read_records
from amt.core.models import RawSession, SessionInfo
from amt.utils import collapse, truncate

_HEADER_BYTES = 262_144

_TITLE_NOISE = (
    "Caveat: The messages below",
    "<command-name>",
    "<local-command-stdout>",
    "<system-reminder>",
)


def decode_project_dir(name: str) -> str | None:
    """把转义后的目录名还原成一个可读路径（仅用于展示，不保证可逆）。

    转义是有损的（原始目录名里的 ``-`` 与分隔符无法区分），因此这里只做
    「看起来像 Windows 盘符」的还原，其余保持原样。
    """
    if not name:
        return None
    text = name
    if len(text) > 2 and text[0].isalpha() and text[1:3] == "--":
        text = f"{text[0]}:\\" + text[3:].replace("-", "\\")
    return text or None


def _derive_title(text: str, limit: int = 80) -> str | None:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if not line:
            continue
        if any(noise in line for noise in _TITLE_NOISE):
            continue
        if len(line) < 2:
            continue
        return truncate(line, limit)
    collapsed = truncate(collapse(text), limit)
    return collapsed or None


class ClaudeSessionDiscovery:
    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = Path(projects_dir)
        self._parser = ClaudeCodeParser()

    # ------------------------------------------------------------------
    def session_files(self) -> list[Path]:
        if not self.projects_dir.is_dir():
            return []
        return [p for p in self.projects_dir.rglob("*.jsonl") if p.is_file()]

    # ------------------------------------------------------------------
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
            agent="claude",
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
                title = _derive_title(event.content)
                break
        if not title:
            for event in parsed.events:
                if event.metadata.get("kind") == "claude_summary" and event.content:
                    title = truncate(collapse(event.content), 80)
                    break

        user_messages = sum(1 for e in parsed.events if e.type.value == "user_message")
        cwd = (metadata.cwd if metadata else None) or decode_project_dir(path.parent.name)

        return SessionInfo(
            agent="claude",
            session_id=(metadata.session_id if metadata else None) or path.stem,
            path=str(path),
            cwd=cwd,
            created_at=(metadata.created_at if metadata else None),
            updated_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
            title=title,
            size_bytes=stat.st_size,
            resumable=True,
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

        # 兜底：读文件内容比对 sessionId 字段
        for path in self.session_files():
            records, _ = read_records(path, max_bytes=65_536)
            for record in records:
                if record.get("sessionId") == needle:
                    return path
        return None
