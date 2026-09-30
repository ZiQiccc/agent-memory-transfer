"""Codex 启动器。

与 Claude 侧同构：找不到 CLI 时**不报错、不中断**，Memory 与上下文文件照常生成，
用户手动启动 Codex 后由 AGENTS.md 自动加载。

Codex CLI 的启动形态（``codex --help`` 实测）::

    codex                       交互式启动
    codex "prompt"              带初始 prompt 启动
    codex exec "prompt"         非交互执行

本模块使用 ``codex "<prompt>"``：交接场景需要的是**能继续对话**的会话，
而不是一次性执行。Windows 下 codex 是 ``codex.cmd``，批处理包装由 ShellResolver 处理。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext
from amt.core.models import LaunchResult, TargetContext
from amt.services.launch import build_launch_command
from amt.services.process import executable_version


class CodexLauncher:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.config = ctx.config.codex

    # ------------------------------------------------------------------
    def executable(self) -> str | None:
        cfg = self.config.executable
        return self.ctx.process.find_executable(cfg) if cfg else self.ctx.process.find_executable("codex")

    def version(self) -> str | None:
        exe = self.executable()
        return executable_version(self.ctx.process, exe, ("--version",)) if exe else None

    # ------------------------------------------------------------------
    def build_command(self, cwd: str | Path, prompt: str) -> list[str] | None:
        exe = self.executable()
        if exe is None:
            return None
        argv, _, _ = build_launch_command(
            self.ctx, executable=exe, prompt=prompt, project_root=Path(cwd)
        )
        return argv

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
                    "未检测到 Codex CLI，无法自动启动。"
                    "Memory 与 AGENTS.md 已生成，请手动在本项目目录启动 Codex，"
                    "它会自动加载 AGENTS.md 中的接续上下文。"
                ),
                degraded=True,
            )

        cwd = context.working_directory or str(Path.cwd())
        argv, prompt_file, notes = build_launch_command(
            self.ctx, executable=exe, prompt=context.task_context, project_root=Path(cwd)
        )
        # 交互式 TUI 必须在**独立控制台**里启动，否则会接管运行 amt 的那个窗口。
        # alive_check_seconds：独立窗口捕获不到输出，若目标 CLI 启动即崩，
        # 用户只会看到「窗口闪一下」，这里短暂观察以便给出可诊断的失败。
        proc, note = self.ctx.process.start(
            argv, cwd=cwd, new_console=True, alive_check_seconds=1.2
        )
        if proc is None:
            return LaunchResult(
                attempted=True,
                success=False,
                command=argv,
                message=f"启动 Codex 失败：{note}",
                warnings=list(notes),
                degraded=True,
            )

        suffix = f"；完整初始 Prompt 见 {prompt_file}" if prompt_file else ""
        return LaunchResult(
            attempted=True,
            success=True,
            command=argv,
            pid=proc.pid,
            message=(
                f"已在新窗口中启动 Codex（pid={proc.pid}），初始 Prompt 已注入{suffix}"
                + (f"（{note}）" if note != "ok" else "")
            ),
            warnings=list(notes),
            degraded=note != "ok",
        )
