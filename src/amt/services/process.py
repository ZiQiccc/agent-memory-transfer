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
        new_console: bool = True,
        alive_check_seconds: float = 0.0,
    ) -> tuple[subprocess.Popen | None, str]:
        """启动长驻的**交互式**进程（如 Codex / Claude Code 的 TUI）。

        关键：Windows 上必须用 ``CREATE_NEW_CONSOLE``。

        早期实现只用了 ``CREATE_NEW_PROCESS_GROUP`` —— 那只是新建进程组，
        子进程仍然**继承父进程的控制台**。于是启动出来的 Codex TUI 会直接接管
        运行 ``amt`` 的那个终端窗口，用户既看不到新窗口也无法切换进去。
        实测证据（``GetConsoleWindow`` 句柄）：

            CREATE_NEW_PROCESS_GROUP → 子进程句柄 0（无自己的控制台）
            CREATE_NEW_CONSOLE       → 子进程句柄 330838（独立控制台）

        ``CREATE_BREAKAWAY_FROM_JOB`` 一并尝试，是为了让窗口在父进程（或它所在的
        Job 对象）退出后依然存活 —— 否则某些宿主（IDE 终端、带 kill-on-close 的
        Job）会连带杀掉刚打开的 Agent 窗口。Job 不允许 breakaway 时该标志会导致
        CreateProcess 失败，因此按阶梯回退。

        交互式进程**绝不重定向标准流**：一旦重定向就失去控制台，
        TUI 会退化成不可交互的管道进程。反过来说，独立窗口里的输出我们也拿不到 ——
        所以 ``alive_check_seconds > 0`` 时会在启动后短暂观察：若进程已经退出，
        说明目标 CLI 启动即崩，此时必须**报失败**并提示手动执行，
        否则用户只会看到「窗口闪了一下」而无从排查。
        """
        # (creationflags, 说明, 是否降级)
        #
        # 「降级」的判据是**用户是否失去独立窗口**，而不是「用了几次尝试」：
        # 前两种方式都拿到了新控制台，只是第一种额外尝试脱离 Job，
        # 因此第一种失败退到第二种**不算降级**（否则会误导用户以为窗口有问题）。
        attempts: list[tuple[int, str, bool]] = []
        if new_console and os.name == "nt":
            new_console_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            attempts = [
                (new_console_flag | breakaway, "新控制台 + 脱离 Job", False),
                (new_console_flag, "新控制台", False),
                (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    "新进程组（会占用当前窗口）",
                    True,
                ),
            ]
        else:
            # POSIX：新建会话，脱离父进程的终端组
            attempts = [(0, "默认", False)]

        errors: list[str] = []
        for creationflags, label, degraded in attempts:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd) if cwd else None,
                    env={**os.environ, **(env or {})} if env else None,
                    creationflags=creationflags,
                    close_fds=True,
                )
            except FileNotFoundError:
                return None, f"找不到可执行文件：{argv[0] if argv else ''}"
            except OSError as exc:
                errors.append(f"{label}: {exc}")
                continue

            if alive_check_seconds > 0:
                time.sleep(alive_check_seconds)
                code = proc.poll()
                if code is not None:
                    return None, (
                        f"目标进程启动后立即退出（退出码 {code}）。"
                        "它运行在独立窗口里，输出无法被本工具捕获 —— "
                        "请手动执行上面打印的启动命令以查看具体报错"
                    )

            return proc, (f"已降级为「{label}」" if degraded else "ok")
        return None, describe_start_failure(errors)

    def terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass


def describe_start_failure(errors: list[str]) -> str:
    """把多次尝试的失败原因压成一句可诊断的话。

    阶梯重试会产生多条**内容相同**的错误（例如环境策略一律拒绝创建进程），
    原样拼接只会让用户更难读，因此去重后再补充针对性的排查提示。
    """
    if not errors:
        return "启动失败：原因未知"
    _, _, message = errors[0].partition(": ")
    reasons = {e.partition(": ")[2] for e in errors}
    if len(reasons) == 1:
        text = f"启动失败：{message}（{len(errors)} 种创建方式均被拒绝）"
    else:
        text = "启动失败：" + "；".join(errors)

    if "WinError 5" in text or "拒绝访问" in text:
        text += (
            "。这类错误通常不是参数问题，而是执行环境禁止创建进程："
            "可尝试在普通终端（而非受限沙箱 / 受管 IDE 终端）中重跑，"
            "或用 --no-launch 后手动执行上面打印的启动命令"
        )
    return text


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
