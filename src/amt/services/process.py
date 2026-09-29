"""进程执行与启动。

所有外部命令（git / codex / claude / shell 探测）统一走这里，
便于集中处理超时、编码与「命令不存在」的降级。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from amt.services.shell import ShellResolver, resolve_executable

_DECODE_CHAIN = ("utf-8", "gbk", "cp936", "latin-1")


def decode_bytes(raw: bytes | None) -> str:
    """Windows 下命令输出可能是 UTF-8 / GBK 混合，逐个尝试。"""
    if not raw:
        return ""
    for enc in _DECODE_CHAIN:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass
class CommandResult:
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def output_excerpt(self) -> str:
        text = (self.stdout or "").strip() or (self.stderr or "").strip()
        return text[:800]

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "error": self.error,
        }


class ProcessManager:
    """进程管理：查找可执行文件、执行命令、启动长驻进程。

    原则：**任何外部依赖缺失都降级，不抛异常**——迁移流程必须能在
    缺少 claude CLI 的机器上走完并产出 Memory。
    """

    def __init__(self, shell: ShellResolver | None = None, default_timeout: float = 60.0) -> None:
        self.shell = shell or ShellResolver()
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------
    def find_executable(self, name: str, extra_dirs: list[Path] | None = None) -> str | None:
        return resolve_executable(name, extra_dirs)

    def is_available(self, name: str) -> bool:
        return self.find_executable(name) is not None

    # ------------------------------------------------------------------
    def run(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> CommandResult:
        """执行命令，永不抛异常。"""
        effective_timeout = timeout if timeout is not None else self.default_timeout
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                input=stdin_text.encode("utf-8") if stdin_text is not None else None,
                capture_output=True,
                timeout=effective_timeout,
                env={**os.environ, **(env or {})} if env else None,
            )
            return CommandResult(
                command=argv,
                returncode=proc.returncode,
                stdout=decode_bytes(proc.stdout),
                stderr=decode_bytes(proc.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=argv,
                returncode=None,
                stdout=decode_bytes(exc.stdout),
                stderr=decode_bytes(exc.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                error=f"命令超时（>{effective_timeout}s）",
            )
        except FileNotFoundError:
            return CommandResult(
                command=argv,
                returncode=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"找不到可执行文件：{argv[0] if argv else ''}",
            )
        except OSError as exc:
            return CommandResult(
                command=argv,
                returncode=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"执行失败：{exc}",
            )

    def run_cli(
        self,
        executable: str,
        args: list[str] | None = None,
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """执行一个 CLI 程序，自动处理 .cmd/.bat 包装。"""
        argv = self.shell.build_command(executable, args or [])
        return self.run(argv, cwd=cwd, timeout=timeout)

    def run_shell(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        argv = self.shell.shell_argv(command)
        return self.run(argv, cwd=cwd, timeout=timeout)

    # ------------------------------------------------------------------
    def start(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.Popen | None, str]:
        """启动长驻进程（如交互式 CLI）。返回 (进程, 说明)。"""
        try:
            creationflags = 0
            if os.name == "nt":
                # 新进程组：让目标 Agent 在独立控制台/窗口运行。
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env={**os.environ, **(env or {})} if env else None,
                creationflags=creationflags,
            )
            return proc, "ok"
        except FileNotFoundError:
            return None, f"找不到可执行文件：{argv[0] if argv else ''}"
        except OSError as exc:
            return None, f"启动失败：{exc}"

    def terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass


def executable_version(
    manager: ProcessManager,
    executable: str,
    args: tuple[str, ...] = ("--version",),
    timeout: float = 15.0,
) -> str | None:
    """取版本号，失败返回 None。"""
    result = manager.run_cli(executable, list(args), timeout=timeout)
    if not result.ok:
        return None
    text = (result.stdout or result.stderr).strip()
    if not text:
        return None
    return text.splitlines()[0].strip()[:200]
