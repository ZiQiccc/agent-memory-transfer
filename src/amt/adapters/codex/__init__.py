"""Codex Adapter。

本包是**唯一**允许理解 Codex rollout JSONL 的地方。

Phase 2 起，Codex 同时具备 Source 与 Target 能力：

    Source  解析 ~/.codex/sessions/**/rollout-*.jsonl
    Target  写入 .agent-transfer/ 任务包 + 把核心上下文**内联**进 AGENTS.md
            （Codex 不解析 @ 导入，这是与 Claude 的关键差异）
"""

from amt.adapters.codex.detector import CodexDetector
from amt.adapters.codex.discovery import CodexSessionDiscovery
from amt.adapters.codex.injector import CodexInjector
from amt.adapters.codex.launcher import CodexLauncher
from amt.adapters.codex.parser import CodexParseResult, CodexParser, ParseStats, read_records
from amt.adapters.codex.renderer import AGENTS_MD_BEGIN, AGENTS_MD_END, CodexContextRenderer
from amt.adapters.codex.source import CodexSourceAdapter
from amt.adapters.codex.target import CodexTargetAdapter

__all__ = [
    "CodexDetector",
    "CodexSessionDiscovery",
    "CodexParser",
    "ParseStats",
    "CodexParseResult",
    "read_records",
    "CodexSourceAdapter",
    "CodexContextRenderer",
    "CodexInjector",
    "CodexLauncher",
    "CodexTargetAdapter",
    "AGENTS_MD_BEGIN",
    "AGENTS_MD_END",
]
