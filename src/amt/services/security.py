"""敏感信息检测与脱敏（需求文档 §25 / 技术架构 §41）。

前提：Coding Agent 的 Session 里天然包含源码、API Key、Token、数据库密码、
内部 URL。因此**原始会话在落盘与注入前必须扫描**。

三档策略：
- ``strict``   所有命中项一律脱敏（含低置信度的泛化赋值）
- ``balanced`` 脱敏高/中置信度命中项（默认）——结构化密钥 + 明确的 key=value
- ``off``      完全关闭扫描与脱敏

掩码规则（两条不同的安全语义）：
- **结构化密钥**：公开的类型前缀不是秘密（``sk-`` / ``AKIA`` / ``ghp_``），
  保留前缀便于人工辨认密钥类型，其余打码 → ``sk-****``。
- **键值赋值**：密码/密钥的**值本身**没有任何可公开部分，**全掩码** → ``password=****``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from amt.core.models import SecretFinding

MASK = "****"

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class _Rule:
    """一条检测规则。

    group  : 需要打码的捕获组编号；None 表示整个匹配
    keep   : 打码时保留的前导字符数（仅用于公开类型前缀）
    """

    kind: str
    pattern: re.Pattern[str]
    severity: str
    group: int | None = None
    keep: int = 0


_RULES: list[_Rule] = [
    # ---- 结构化密钥：保留公开类型前缀 ----
    _Rule("openai_api_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{16,}"), "high", keep=3),
    _Rule("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), "high", keep=7),
    _Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high", keep=4),
    _Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high", keep=4),
    _Rule("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "high", keep=4),
    _Rule("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "high", keep=4),
    # ---- 无公开前缀：整体打码 ----
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), "high"),
    _Rule("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "high"),
    _Rule("bearer_token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"), "high", group=1),
    # ---- 内嵌凭据：只掩盖值，保留结构可读性 ----
    _Rule(
        "jdbc_url_with_password",
        re.compile(r"(?i)(jdbc:[a-z0-9]+://[^\s\"'<>]*?password=)([^\s&\"'<>]+)"),
        "high",
        group=2,
    ),
    _Rule(
        "credential_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token"
            r"|auth[_-]?token|refresh[_-]?token|private[_-]?key|password|passwd|pwd"
            r"|database[_-]?url|db[_-]?password|secret|token)\b"
            r"(\s*[:=]\s*[\"']?)([^\s\"',;\[\]{}()]{6,})"
        ),
        "medium",
        group=3,
    ),
    _Rule(
        "env_secret_var",
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:API_KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY)[A-Z0-9_]*"
            r"\b(\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"
        ),
        "medium",
        group=2,
    ),
]


def _truncate(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


#: 明显不是凭据的字面量（如 Java 常量 `TOKEN_TYPE = "Bearer"`）
_NON_SECRET_LITERALS = frozenset(
    {
        "bearer", "token", "password", "passwd", "secret", "pwd", "none", "null",
        "true", "false", "changeme", "placeholder", "example", "your_token_here",
        "xxxxxxxx", "********", "todo", "unknown", "test", "demo", "sample",
    }
)

_MIN_MEDIUM_VALUE_LEN = 12


def _plausible_secret_value(value: str) -> bool:
    """中置信度规则的**值可信度**判断。

    没有这道闸门时，源码里的常量声明会被大面积误伤，例如::

        private static final String TOKEN_TYPE = "Bearer";

    会被 ``env_secret_var`` 判成凭据并脱敏。代价是漏掉「短且无数字」的密码
    （如 6 位纯字母），但这类值本身置信度就低，balanced 模式不处理是合理的；
    需要更激进时把 ``strict`` 打开即可。
    """
    cleaned = value.strip().strip("\"'")
    if not cleaned or cleaned.lower() in _NON_SECRET_LITERALS:
        return False
    if len(cleaned) >= _MIN_MEDIUM_VALUE_LEN:
        return True
    return any(ch.isdigit() for ch in cleaned)


class _Match:
    __slots__ = ("rule", "start", "end", "replacement", "raw")

    def __init__(self, rule: _Rule, start: int, end: int, replacement: str, raw: str) -> None:
        self.rule = rule
        self.start = start
        self.end = end
        self.replacement = replacement
        self.raw = raw


def _build_replacement(rule: _Rule, match: re.Match[str]) -> tuple[int, int, str] | None:
    """计算 (start, end, replacement)，只替换需要打码的那一段。

    返回 None 表示「命中但值不可信」，应丢弃该命中（中置信度规则的误报抑制）。
    """
    if rule.group is None:
        return match.start(), match.end(), f"{match.group(0)[: rule.keep]}{MASK}"
    value = match.group(rule.group) or ""
    if rule.severity != "high" and not _plausible_secret_value(value):
        return None
    start, end = match.span(rule.group)
    return start, end, f"{value[: rule.keep]}{MASK}"


class SecretScanner:
    """敏感信息扫描器。扫描与脱敏分离，便于「只报告不修改」。"""

    def __init__(self, mode: str = "balanced") -> None:
        if mode not in ("strict", "balanced", "off"):
            mode = "balanced"
        self.mode = mode

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _severities_to_redact(self) -> set[str]:
        if self.mode == "strict":
            return {"high", "medium", "low"}
        if self.mode == "off":
            return set()
        return {"high", "medium"}

    def _collect_matches(self, text: str) -> list[_Match]:
        """收集全部命中并**去重叠**。

        同一片段常被多条规则命中（如 ``password=xxx`` 同时命中
        credential_assignment 与 env_secret_var）。按（起点升序、严重度降序）
        排序后贪心接受，重叠者丢弃，避免同一条敏感信息被重复上报。
        """
        candidates: list[_Match] = []
        for rule in _RULES:
            for match in rule.pattern.finditer(text):
                built = _build_replacement(rule, match)
                if built is None:
                    continue  # 命中但值不可信 → 丢弃
                start, end, replacement = built
                if start >= end:
                    continue
                candidates.append(
                    _Match(rule, start, end, replacement, text[start:end])
                )

        candidates.sort(key=lambda m: (m.start, _SEVERITY_RANK.get(m.rule.severity, 9), -(m.end - m.start)))

        accepted: list[_Match] = []
        occupied_end = -1
        for cand in candidates:
            if cand.start < occupied_end:
                continue  # 与已接受区间重叠 → 丢弃
            accepted.append(cand)
            occupied_end = cand.end
        return accepted

    # ------------------------------------------------------------------
    def scan(self, text: str | None, source: str | None = None) -> list[SecretFinding]:
        """只扫描不改写。mode=off 时返回空列表。"""
        if not text or not self.enabled:
            return []
        return [
            SecretFinding(
                kind=m.rule.kind,
                severity=m.rule.severity,  # type: ignore[arg-type]
                preview=_truncate(m.replacement),
                source=source,
                line=text.count("\n", 0, m.start) + 1,
            )
            for m in self._collect_matches(text)
        ]

    def redact(self, text: str | None, source: str | None = None) -> tuple[str, list[SecretFinding]]:
        """脱敏。返回 (脱敏后文本, 命中列表)。"""
        if not text:
            return "", []
        if not self.enabled:
            return text, []

        matches = self._collect_matches(text)
        if not matches:
            return text, []

        redact_levels = self._severities_to_redact()
        findings: list[SecretFinding] = []
        out: list[str] = []
        cursor = 0
        for m in matches:
            findings.append(
                SecretFinding(
                    kind=m.rule.kind,
                    severity=m.rule.severity,  # type: ignore[arg-type]
                    preview=_truncate(m.replacement),
                    source=source,
                    line=text.count("\n", 0, m.start) + 1,
                )
            )
            out.append(text[cursor : m.start])
            if m.rule.severity in redact_levels:
                out.append(m.replacement)
            else:
                out.append(text[m.start : m.end])  # 已报告但不改写
            cursor = m.end
        out.append(text[cursor:])
        return "".join(out), findings

    # ------------------------------------------------------------------
    def redact_structure(
        self, obj: Any, source: str | None = None, _depth: int = 0
    ) -> tuple[Any, list[SecretFinding]]:
        """递归脱敏任意 JSON 结构（用于 Raw Session 落盘前处理）。"""
        if _depth > 40:
            return obj, []
        findings: list[SecretFinding] = []

        if isinstance(obj, str):
            return self.redact(obj, source=source)
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                new_v, found = self.redact_structure(v, source=source, _depth=_depth + 1)
                findings.extend(found)
                out[k] = new_v
            return out, findings
        if isinstance(obj, list):
            out_list = []
            for item in obj:
                new_item, found = self.redact_structure(item, source=source, _depth=_depth + 1)
                findings.extend(found)
                out_list.append(new_item)
            return out_list, findings
        return obj, findings

    # ------------------------------------------------------------------
    @staticmethod
    def summarize(findings: list[SecretFinding]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.kind] = counts.get(f.kind, 0) + 1
        return counts
