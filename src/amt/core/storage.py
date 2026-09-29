"""本地存储（技术架构 §18 / §40）。

MVP 不使用数据库，全部落盘为文件，便于人工查看与恢复::

    ~/.agent-memory-transfer/
    ├── config/config.yaml
    ├── sessions/{agent}/{session_id}/raw.json + metadata.json
    ├── memories/{memory_id}.json + {memory_id}.md
    ├── migrations/{migration_id}.json
    ├── logs/
    └── cache/

安全：原始会话落盘前必须已脱敏（由调用方保证），这是唯一可能包含
源码与凭据的持久化数据。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from amt.config import AMTConfig
from amt.core.models import CanonicalMemory, MemoryPackage, MigrationRecord
from amt.utils import now_local, parse_iso


class Storage:
    def __init__(self, config: AMTConfig) -> None:
        self.config = config
        self.config.ensure_dirs()

    # ------------------------------------------------------------------
    # ID 生成
    # ------------------------------------------------------------------
    def new_memory_id(self) -> str:
        today = now_local().strftime("%Y%m%d")
        existing = list(self.config.memories_dir.glob(f"mem_{today}_*.json"))
        return f"mem_{today}_{len(existing) + 1:03d}"

    def new_migration_id(self) -> str:
        return "mig_" + now_local().strftime("%Y%m%d_%H%M%S_%f")[:-3]

    # ------------------------------------------------------------------
    # 原始会话
    # ------------------------------------------------------------------
    def session_dir(self, agent: str, session_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)[:120]
        path = self.config.sessions_dir / agent / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_raw_session(
        self,
        agent: str,
        session_id: str,
        *,
        records: list[dict[str, Any]],
        metadata: dict[str, Any],
        source_path: str | None = None,
    ) -> Path:
        target = self.session_dir(agent, session_id)
        (target / "raw.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        payload = {
            **metadata,
            "agent": agent,
            "session_id": session_id,
            "source_path": source_path,
            "saved_at": now_local().isoformat(),
            "record_count": len(records),
        }
        (target / "metadata.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    # ------------------------------------------------------------------
    # Canonical Memory
    # ------------------------------------------------------------------
    def memory_path(self, memory_id: str) -> Path:
        return self.config.memories_dir / f"{memory_id}.json"

    def save_memory(self, memory: CanonicalMemory, markdown: str | None = None) -> list[Path]:
        path = self.memory_path(memory.metadata.memory_id)
        path.write_text(memory.model_dump_json(indent=2), encoding="utf-8")
        written = [path]
        if markdown is not None:
            md_path = path.with_suffix(".md")
            md_path.write_text(markdown, encoding="utf-8")
            written.append(md_path)
        return written

    def load_memory(self, memory_id: str) -> CanonicalMemory:
        path = self.memory_path(memory_id)
        if not path.is_file():
            raise FileNotFoundError(f"未找到 Memory：{memory_id}")
        return CanonicalMemory.model_validate_json(path.read_text(encoding="utf-8"))

    def load_memory_markdown(self, memory_id: str) -> str | None:
        path = self.memory_path(memory_id).with_suffix(".md")
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def list_memories(self, limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(
            self.config.memories_dir.glob("mem_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append(
                {
                    "memory_id": data.get("metadata", {}).get("memory_id", path.stem),
                    "source_agent": data.get("metadata", {}).get("source_agent"),
                    "session_id": data.get("metadata", {}).get("source_session_id"),
                    "title": data.get("task", {}).get("title"),
                    "status": data.get("task", {}).get("status"),
                    "path": str(path),
                    "created_at": data.get("metadata", {}).get("created_at"),
                    "size_bytes": path.stat().st_size,
                }
            )
        return items[:limit] if limit else items

    # ------------------------------------------------------------------
    # 任务包（便于跨 Agent 携带）
    # ------------------------------------------------------------------
    def save_package(self, package: MemoryPackage, target_dir: Path) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        files = {
            "manifest.json": package.manifest.model_dump(mode="json"),
            "memory.json": package.memory.model_dump(mode="json"),
            "source.json": package.source.model_dump(mode="json"),
        }
        for name, payload in files.items():
            path = target_dir / name
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(path)
        return written

    # ------------------------------------------------------------------
    # 迁移记录
    # ------------------------------------------------------------------
    def save_migration(self, record: MigrationRecord) -> Path:
        path = self.config.migrations_dir / f"{record.migration_id}.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return path

    def list_migrations(self, limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(
            self.config.migrations_dir.glob("mig_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append(
                {
                    "migration_id": data.get("migration_id", path.stem),
                    "source_agent": data.get("source_agent"),
                    "target_agent": data.get("target_agent"),
                    "status": data.get("status"),
                    "dry_run": data.get("dry_run"),
                    "memory_id": data.get("memory_id"),
                    "project_path": data.get("project_path"),
                    "timestamp": data.get("timestamp"),
                    "path": str(path),
                }
            )
        return items[:limit] if limit else items

    def load_migration(self, migration_id: str) -> MigrationRecord:
        path = self.config.migrations_dir / f"{migration_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"未找到迁移记录：{migration_id}")
        return MigrationRecord.model_validate_json(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    def write_log(self, name: str, text: str) -> Path:
        path = self.config.logs_dir / f"{name}-{now_local().strftime('%Y%m%d')}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {now_local().isoformat()} =====\n{text}\n")
        return path


def timestamp_label(value: str | datetime | None) -> str:
    """把时间格式化为**本地时区**的可读串。

    记录里的时间戳按 UTC 存储；直接格式化会显示成早 8 小时的时间（例如
    08:51 的迁移显示为 00:51），因此这里统一转换到本地时区再展示。
    """
    if isinstance(value, datetime):
        return _to_local(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, str):
        parsed = parse_iso(value)
        if parsed is not None:
            return _to_local(parsed).strftime("%Y-%m-%d %H:%M")
        return value.replace("T", " ")[:16]
    return "-"


def _to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone()
