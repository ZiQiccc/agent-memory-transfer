"""Claude Code 探测（安装 / CLI / 会话目录）。

三个状态必须分开表达，不要混为一谈：

    installed        装了 claude CLI
    cli_usable       CLI 真的能发起对话（已登录且有额度）——``--version`` 成功
                     并不代表可用：未登录时 ``claude -p`` 会返回
                     ``Not logged in · Please run /login``
    source_available 本地有可解析的会话数据（``~/.claude/projects/**/*.jsonl``）

另外：**即使本机没装 claude CLI，我们依然可以生成 Claude Code 认得的上下文文件**
（``CLAUDE.md`` + ``.agent-transfer/memory.md``），用户之后手动启动即可自动加载。
因此 Claude 作为 Target 始终 supported，只是「自动启动」会降级。
"""

from __future__ import annotations

import json
from pathlib import Path

from amt.context import AppContext
from amt.core.models import AgentInstallation, DetectionResult
from amt.services.process import executable_version


class ClaudeDetector:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    def executable(self) -> str | None:
        cfg = self.ctx.config.claude.executable
        return (
            self.ctx.process.find_executable(cfg) if cfg else self.ctx.process.find_executable("claude")
        )

    def projects_dir(self) -> Path:
        return self.ctx.config.claude.resolved_projects_dir()

    def session_files(self) -> list[Path]:
        projects = self.projects_dir()
        if not projects.is_dir():
            return []
        return [p for p in projects.rglob("*.jsonl") if p.is_file()]

    def cli_status(self) -> tuple[bool, str]:
        """判断 CLI 是否**真的能发起对话**，且**不产生任何副作用**。

        为什么不直接 ``claude -p ping``：那会在用户的 ``~/.claude/projects``
        里**留下一条真实会话记录**（本机实测确认）。探测工具不该污染用户的会话历史。

        因此改为**证据式判断**：读取最近一条会话，看它是否以鉴权类错误收尾。
        未登录时 Claude 会把 ``isApiErrorMessage`` 写进记录，这就是现成的证据。
        """
        exe = self.executable()
        if exe is None:
            return False, "未找到 claude 可执行文件"

        files = self.session_files()
        if not files:
            return True, "CLI 已安装；暂无会话记录可判断登录状态"

        newest = max(files, key=lambda p: p.stat().st_mtime)
        try:
            from amt.adapters.claude.parser import read_records

            records, _ = read_records(newest)
        except Exception:
            return True, "CLI 已安装；最近会话无法读取"

        for record in reversed(records):
            if record.get("type") != "assistant":
                continue
            if not record.get("isApiErrorMessage"):
                return True, "CLI 已安装，最近一次调用未见鉴权错误"
            text = json.dumps(record.get("message") or {}, ensure_ascii=False)
            if "not logged in" in text.lower() or "login" in text.lower():
                return False, "最近一次调用因未登录失败（依据最近会话记录判断，未额外发起请求）"
            if "credit" in text.lower() or "quota" in text.lower():
                return False, "最近一次调用因额度不足失败"
            return False, "最近一次调用返回 API 错误"
        return True, "CLI 已安装，最近会话未见异常"

    # ------------------------------------------------------------------
    def detect_target(self) -> DetectionResult:
        exe = self.executable()
        if exe is None:
            return DetectionResult(
                agent="claude",
                installed=False,
                runtime_available=False,
                detail="未检测到 Claude Code CLI；上下文文件仍会生成，需手动启动 Claude Code",
                evidence=["未找到 claude 可执行文件"],
            )
        usable, note = self.cli_status()
        return DetectionResult(
            agent="claude",
            installed=True,
            runtime_available=usable,
            detail=f"已检测到 Claude Code CLI；{note}",
            evidence=[f"可执行文件：{exe}"],
        )

    # 兼容旧调用名
    def detect(self) -> DetectionResult:
        return self.detect_target()

    def detect_source(self) -> DetectionResult:
        files = self.session_files()
        projects = self.projects_dir()
        evidence: list[str] = []
        if projects.is_dir():
            evidence.append(f"会话目录：{projects}")
        if files:
            evidence.append(f"本地会话文件：{len(files)} 个")

        installed = projects.is_dir() or self.executable() is not None
        runtime_available = bool(files)
        if not projects.is_dir():
            detail = f"未找到 Claude Code 会话目录：{projects}"
        elif not files:
            detail = "会话目录存在但没有任何会话文件（尚未在项目里使用过 Claude Code）"
        else:
            detail = f"就绪：可读取 {len(files)} 个 Claude Code 会话文件"

        return DetectionResult(
            agent="claude",
            installed=installed,
            runtime_available=runtime_available,
            detail=detail,
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    def installation(self) -> AgentInstallation:
        exe = self.executable()
        target = self.detect_target()
        source = self.detect_source()
        files = self.session_files()

        notes = [
            "注入方式：CLAUDE.md 中通过 @.agent-transfer/memory.md 引入记忆文件（Claude Code 原生支持 @ 导入）",
        ]
        if exe is None:
            notes.append("未找到 claude CLI：上下文文件照常生成，需手动启动")
        else:
            usable, note = self.cli_status()
            notes.append(f"CLI 状态：{note}")
            if not usable:
                notes.append("CLI 不可用时仍会完整生成上下文文件，可手动启动后自动加载")
        notes.append(
            f"来源支持：{'可读取 ' + str(len(files)) + ' 个会话文件' if files else '暂无可读会话文件'}"
        )

        return AgentInstallation(
            agent="claude",
            display_name="Claude Code",
            installed=target.installed or source.installed,
            runtime_available=target.runtime_available or source.runtime_available,
            version=executable_version(self.ctx.process, exe, ("--version",)) if exe else None,
            executable=exe,
            home_dir=str(self.ctx.config.claude.resolved_home()),
            data_dir=str(self.projects_dir()),
            source_supported=True,
            target_supported=True,
            notes=notes,
        )
