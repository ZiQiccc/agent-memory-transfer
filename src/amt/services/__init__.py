"""Runtime Services：文件系统 / Git / 进程 / Shell / 安全。

共享一个 ProcessManager 实例即可复用可执行文件解析结果。
"""

from __future__ import annotations

from amt.services.filesystem import (
    ProjectState,
    ProjectStateCollector,
    read_text_safe,
)
from amt.services.git import GitService
from amt.services.process import CommandResult, ProcessManager, decode_bytes, executable_version
from amt.services.security import SecretScanner
from amt.services.shell import ShellInfo, ShellResolver, ShellType, resolve_executable

__all__ = [
    "ProcessManager",
    "CommandResult",
    "decode_bytes",
    "executable_version",
    "ShellResolver",
    "ShellType",
    "ShellInfo",
    "resolve_executable",
    "GitService",
    "SecretScanner",
    "ProjectStateCollector",
    "ProjectState",
    "read_text_safe",
]
