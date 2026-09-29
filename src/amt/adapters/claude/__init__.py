"""Claude Code Adapter。

Phase 2 起，Claude 同时具备 Source 与 Target 能力：

    Source  解析 ~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl
    Target  写入 .agent-transfer/ 任务包 + CLAUDE.md 中 @ 引入（Claude 原生支持）
"""

from amt.adapters.claude.detector import ClaudeDetector
from amt.adapters.claude.discovery import ClaudeSessionDiscovery, decode_project_dir
from amt.adapters.claude.injector import ClaudeInjector
from amt.adapters.claude.launcher import ClaudeLauncher
from amt.adapters.claude.parser import ClaudeCodeParser, ClaudeParseResult, read_records
from amt.adapters.claude.renderer import CLAUDE_MD_SECTION, ClaudeContextRenderer
from amt.adapters.claude.source import ClaudeCodeSourceAdapter
from amt.adapters.claude.target import ClaudeTargetAdapter

__all__ = [
    "ClaudeDetector",
    "ClaudeSessionDiscovery",
    "decode_project_dir",
    "ClaudeCodeParser",
    "ClaudeParseResult",
    "read_records",
    "ClaudeCodeSourceAdapter",
    "ClaudeContextRenderer",
    "ClaudeInjector",
    "ClaudeLauncher",
    "ClaudeTargetAdapter",
    "CLAUDE_MD_SECTION",
]
