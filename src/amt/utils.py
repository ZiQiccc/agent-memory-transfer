"""通用小工具：时间、ID、文本截断。

刻意保持零依赖，避免在 models / services / adapters 之间形成环。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

_WHITESPACE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def now_utc() -> datetime:
    """带时区的当前时间（UTC）。"""
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """带时区的当前本地时间。"""
    return datetime.now().astimezone()


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def parse_iso(value: str | None) -> datetime | None:
    """尽量宽松地解析 ISO 时间串，失败返回 None（绝不抛异常）。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def truncate(text: str | None, limit: int = 200, suffix: str = "...") -> str:
    """按字符数截断（中文场景按字符比按 token 估计更可控）。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def collapse(text: str | None) -> str:
    """压缩空白，用于标题 / 单行摘要。"""
    if not text:
        return ""
    return _MULTI_NEWLINE.sub("\n\n", _WHITESPACE.sub(" ", text)).strip()


def first_line(text: str | None, limit: int = 120) -> str:
    """取首个非空行，常用于把长回复压成标题。"""
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip().strip("#").strip()
        if line:
            return truncate(line, limit)
    return ""


def short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:length]


def estimate_tokens(text: str | None) -> int:
    """粗略 token 估算：CJK 约 1 字 ≈ 1 token，ASCII 约 4 字符 ≈ 1 token。

    仅用于「是否超预算」的判断，不用于计费。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def slugify(text: str, limit: int = 60, fallback: str = "item") -> str:
    """生成文件名安全 slug，保留中文。"""
    if not text:
        return fallback
    keep = []
    for ch in text.strip():
        if ch.isalnum() or ch in "-_" or "\u4e00" <= ch <= "\u9fff":
            keep.append(ch)
        elif ch in " /\\:*?\"<>|.\n\t":
            keep.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(keep)).strip("-")
    return (slug[:limit] or fallback)
