"""Codex 安装探测（实现plan §4.1）。

必须区分两个状态：
    installed          「装了 Codex」
    runtime_available  「本地会话数据可读」——POC 实际依赖的东西

两者不可混为一谈。曾出现过 ``Codex app-server process is not available``，
因此本 POC **不把 app-server 作为依赖**：``CodexFileSource`` 是 MVP 主路径，
app-server 留待 V2（见 CodexAppServerSource 占位说明）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from amt.context import AppContext
from amt.core.models import AgentInstallation, DetectionResult
from amt.services.process import executable_version

#: 会话记录里能反映「鉴权失败」的标记。用于**证据式**判断登录状态。
_AUTH_ERROR_RE = re.compile(
    r"not logged in|please run codex login|401 unauthorized|missing bearer|"
    r"invalid api key|incorrect api key",
    re.IGNORECASE,
)


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
    def cli_status(self) -> tuple[bool, str]:
        """判断 Codex 是否**真的能发起对话**（而不只是装了）。

        采用**证据式**判断，绝不主动发请求：早期版本用 ``claude -p ping`` 探活，
        在用户的 ``~/.claude/projects`` 里留下了一堆垃圾会话。这里只看两样东西：

        1. ``~/.codex/auth.json`` 是否存在且含有凭据字段；
        2. 最近的会话文件里是否出现鉴权失败标记。

        结论只能说「看起来可用 / 看起来未登录」，不会伪装成「已验证可用」——
        真正的验证只有跑一次迁移才知道。
        """
        exe = self.executable()
        if exe is None:
            return False, "未找到 codex 可执行文件"

        auth = self.home / "auth.json"
        if not auth.is_file():
            return False, "已安装 codex，但缺少凭据文件（~/.codex/auth.json），需要先 codex login"

        has_credential = False
        try:
            data = json.loads(auth.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                has_credential = any(
                    isinstance(data.get(k), str) and data.get(k)
                    for k in ("OPENAI_API_KEY", "tokens", "access_token")
                ) or bool(data.get("tokens"))
        except Exception as exc:
            return False, f"凭据文件无法解析（{exc}）"
        if not has_credential:
            return False, "凭据文件存在但未包含可用凭据，需要重新 codex login"

        failures = 0
        checked = 0
        files = sorted(self.session_files(), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        for path in files:
            checked += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if _AUTH_ERROR_RE.search(text):
                failures += 1
        if checked and failures == checked:
            return False, f"凭据存在，但最近 {checked} 个会话均以鉴权失败告终（可能已过期）"
        if failures:
            return True, f"凭据存在；最近 {checked} 个会话中 {failures} 个出现鉴权失败，供参考"
        return True, "凭据文件存在且最近会话未见鉴权失败（未主动发请求验证）"

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
        if exe:
            usable, status = self.cli_status()
            notes.append(("可自动启动： " if usable else "⚠ 无法自动启动： ") + status)

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
            # Codex 同时是已实现的**注入目标**（写 AGENTS.md），
            # 不能像 Cursor / WorkBuddy 那样标 False —— 否则直接调用本方法的一方会读到错的结论。
            target_supported=True,
            notes=notes,
        )
