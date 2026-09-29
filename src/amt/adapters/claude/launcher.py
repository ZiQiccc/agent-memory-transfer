"""Claude Code 启动器（实现plan §二十一）。

Windows 注意：Claude Code 官方支持 WSL 与 Git for Windows/Git Bash，
且 CLI 常以 ``claude.cmd`` 形式安装。平台差异全部交给 ShellResolver，
本模块不出现 ``if windows:``。

降级原则：找不到 CLI 时**不报错、不中断**——
Memory 已经生成并落盘，用户可以手动启动 Claude Code，
它会通过 CLAUDE.md 自动加载上下文（需求文档 §24）。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext
from amt.core.models import LaunchResult, TargetContext
from amt.services.process import executable_version


class ClaudeLauncher:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.config = ctx.config.claude

    # ------------------------------------------------------------------
    def executable(self) -> str | None:
        cfg = self.config.executable
        return self.ctx.process.find_executable(cfg) if cfg else self.ctx.process.find_executable("claude")

    def version(self) -> str | None:
        exe = self.executable()
        return executable_version(self.ctx.process, exe, ("--version",)) if exe else None

    # ------------------------------------------------------------------
    def build_command(self, cwd: str | Path, prompt: str) -> list[str] | None:
        """构造启动命令：``claude "<initial prompt>"``。"""
        exe = self.executable()
        if exe is None:
            return None
        return self.ctx.shell.build_command(exe, [prompt])

    # ------------------------------------------------------------------
    def launch(self, context: TargetContext, *, auto_launch: bool = True) -> LaunchResult:
        if not auto_launch:
            return LaunchResult(
                attempted=False,
                success=False,
                message="已跳过自动启动（auto_launch=false）；上下文文件已生成",
                degraded=True,
            )

        exe = self.executable()
        if exe is None:
            return LaunchResult(
                attempted=False,
                success=False,
                message=(
                    "未检测到 Claude Code CLI，无法自动启动。"
                    "Memory 与上下文文件已生成，请手动在本项目目录启动 Claude Code，"
                    "它会通过 CLAUDE.md 自动加载任务上下文。"
                ),
                degraded=True,
            )

        cwd = context.working_directory or str(Path.cwd())
        argv = self.ctx.shell.build_command(exe, [context.task_context])
        proc, note = self.ctx.process.start(argv, cwd=cwd)
        if proc is None:
            return LaunchResult(
                attempted=True,
                success=False,
                command=argv,
                message=f"启动 Claude Code 失败：{note}",
                degraded=True,
            )
        return LaunchResult(
            attempted=True,
            success=True,
            command=argv,
            pid=proc.pid,
            message=f"已启动 Claude Code（pid={proc.pid}），初始 Prompt 已注入",
            degraded=False,
        )
