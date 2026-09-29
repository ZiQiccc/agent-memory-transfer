"""Codex 安装探测（实现plan §4.1）。

必须区分两个状态：
    installed          「装了 Codex」
    runtime_available  「本地会话数据可读」——POC 实际依赖的东西

两者不可混为一谈。曾出现过 ``Codex app-server process is not available``，
因此本 POC **不把 app-server 作为依赖**：``CodexFileSource`` 是 MVP 主路径，
app-server 留待 V2（见 CodexAppServerSource 占位说明）。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext
from amt.core.models import AgentInstallation, DetectionResult
from amt.services.process import executable_version


class CodexDetector:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.home = ctx.config.codex.resolved_home()
        self.sessions_dir = ctx.config.codex.sessions_dir()

    # ------------------------------------------------------------------
    def executable(self) -> str | None:
        cfg = self.ctx.config.codex.executable
        return self.ctx.process.find_executable(cfg) if cfg else self.ctx.process.find_executable("codex")

    def session_files(self) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return []
        return sorted(self.sessions_dir.rglob("rollout-*.jsonl"))

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        exe = self.executable()
        home_exists = self.home.is_dir()
        files = self.session_files()

        evidence: list[str] = []
        if exe:
            evidence.append(f"可执行文件：{exe}")
        if home_exists:
            evidence.append(f"数据目录：{self.home}")
        if files:
            evidence.append(f"本地会话文件：{len(files)} 个")

        installed = bool(exe) or home_exists
        runtime_available = bool(files)

        if not installed:
            detail = "未检测到 Codex（既无 codex 可执行文件，也无 ~/.codex 数据目录）"
        elif not runtime_available:
            detail = "已安装 Codex，但未找到本地会话文件（~/.codex/sessions 下无 rollout-*.jsonl）"
        else:
            detail = f"就绪：可读取 {len(files)} 个本地会话文件"

        return DetectionResult(
            agent="codex",
            installed=installed,
            runtime_available=runtime_available,
            detail=detail,
            evidence=evidence,
        )

    def installation(self) -> AgentInstallation:
        detection = self.detect()
        exe = self.executable()
        version = executable_version(self.ctx.process, exe, ("--version",)) if exe else None
        notes: list[str] = []
        if not exe:
            notes.append("未找到 codex 可执行文件，但可直接读取本地会话文件")
        notes.append("POC 使用 CodexFileSource（本地会话文件），不依赖 Codex app-server")

        return AgentInstallation(
            agent="codex",
            display_name="Codex",
            installed=detection.installed,
            runtime_available=detection.runtime_available,
            version=version,
            executable=exe,
            home_dir=str(self.home),
            data_dir=str(self.sessions_dir),
            source_supported=True,
            target_supported=False,
            notes=notes,
        )
