"""Shell 解析。

实现plan §二十一/§二十二 的硬性要求：**不允许在 Adapter 内写 ``if windows:``**。
所有「这个平台该用哪个 shell、怎么拼命令行」的知识集中在本模块。

本机实测约束（Windows）：Codex CLI 与 Claude Code CLI 常以 ``.cmd`` 形式安装，
而 ``CreateProcess`` 无法直接执行 ``.cmd``/``.bat``，必须经 ``cmd.exe /c``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

IS_WINDOWS = os.name == "nt"

#: 常见安装位置的兜底候选（PATH 异常时使用）
_WINDOWS_FALLBACK_DIRS = [
    Path(r"E:\software\node24\node_global"),
    Path(r"C:\Program Files\nodejs"),
    Path(r"C:\Program Files\Git\cmd"),
    Path(r"E:\software\Git\cmd"),
    Path(os.environ.get("APPDATA", "")) / "npm",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    Path.home() / ".local" / "bin",
    Path.home() / ".codex" / "bin",
]


class ShellType(str, Enum):
    POWERSHELL = "powershell"
    PWSH = "pwsh"
    CMD = "cmd"
    GIT_BASH = "gitbash"
    WSL = "wsl"
    POSIX = "posix"
    UNKNOWN = "unknown"


class ShellInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ShellType
    executable: str | None = None
    notes: list[str] = []


_BATCH_SUFFIXES = {".cmd", ".bat"}


def resolve_executable(name: str, extra_dirs: list[Path] | None = None) -> str | None:
    """解析可执行文件绝对路径。

    顺序：``shutil.which`` → ``where.exe`` → 常见安装目录兜底。
    全部失败返回 None（调用方负责降级，不抛异常）。
    """
    if not name:
        return None

    candidate = Path(name)
    if candidate.is_file():
        return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    if IS_WINDOWS:
        try:
            proc = subprocess.run(
                ["where.exe", name],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0:
                for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and Path(line).is_file():
                        return line
        except Exception:
            pass

    dirs = list(extra_dirs or []) + _WINDOWS_FALLBACK_DIRS
    suffixes = [""] + (list(os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";")) if IS_WINDOWS else [])
    for d in dirs:
        if not d or not str(d):
            continue
        for suffix in suffixes:
            suffix = suffix.strip()
            p = d / (name + suffix.lower())
            if p.is_file():
                return str(p)
            p2 = d / (name + suffix.upper())
            if p2.is_file():
                return str(p2)
    return None


class ShellResolver:
    """决定命令行如何构造。Adapter 只调用它，不做平台判断。"""

    def __init__(self, preferred: ShellType | None = None) -> None:
        self._preferred = preferred
        self._cached: ShellInfo | None = None

    # ------------------------------------------------------------------
    def detect(self) -> ShellInfo:
        if self._cached is not None:
            return self._cached

        if not IS_WINDOWS:
            self._cached = ShellInfo(
                type=ShellType.POSIX,
                executable=os.environ.get("SHELL") or "/bin/sh",
            )
            return self._cached

        if self._preferred is not None:
            exe = self._executable_for(self._preferred)
            self._cached = ShellInfo(type=self._preferred, executable=exe)
            return self._cached

        for candidate in (ShellType.PWSH, ShellType.POWERSHELL, ShellType.GIT_BASH, ShellType.CMD):
            exe = self._executable_for(candidate)
            if exe:
                notes = []
                if candidate is ShellType.POWERSHELL:
                    notes.append("使用 Windows PowerShell 5.1")
                if candidate is ShellType.GIT_BASH:
                    notes.append("使用 Git Bash（Claude Code 官方支持的 Windows 运行方式之一）")
                self._cached = ShellInfo(type=candidate, executable=exe, notes=notes)
                return self._cached

        self._cached = ShellInfo(type=ShellType.UNKNOWN)
        return self._cached

    def _executable_for(self, shell: ShellType) -> str | None:
        mapping = {
            ShellType.PWSH: "pwsh",
            ShellType.POWERSHELL: "powershell",
            ShellType.CMD: "cmd",
            ShellType.WSL: "wsl",
        }
        if shell is ShellType.GIT_BASH:
            return (
                resolve_executable("bash")
                or resolve_executable("git")
                and str(Path(resolve_executable("git") or "").parent.parent / "bin" / "bash.exe")
            )
        name = mapping.get(shell)
        return resolve_executable(name) if name else None

    # ------------------------------------------------------------------
    def build_command(
        self,
        executable: str,
        args: list[str] | None = None,
    ) -> list[str]:
        """构造可直接交给 ``subprocess`` 的 argv。

        对 ``.cmd`` / ``.bat`` 自动补 ``cmd.exe /c``。
        """
        args = list(args or [])
        if IS_WINDOWS and Path(executable).suffix.lower() in _BATCH_SUFFIXES:
            cmd_exe = resolve_executable("cmd") or "cmd.exe"
            return [cmd_exe, "/c", executable, *args]
        return [executable, *args]

    # ------------------------------------------------------------------
    def needs_shell_wrapper(self, executable: str) -> bool:
        """该可执行文件是否必须经 shell 包装（``.cmd`` / ``.bat``）。

        这个判断很重要：**只要经过 shell，参数就会被 shell 再解析一次**，
        任意文本（换行、``%``、``&``、``|``、``<``、``>``、引号）都可能被破坏。
        实测：``cmd.exe /c shim.cmd "<多行 Prompt>"`` 只会把**第一行**送到目标程序，
        ``%PATH%`` 会被展开成上千字符，``<``/``>`` 甚至会让整行消失。
        """
        if not IS_WINDOWS:
            return False
        return Path(executable).suffix.lower() in _BATCH_SUFFIXES

    @staticmethod
    def sanitize_argument(text: str, *, limit: int = 600) -> str:
        """把任意文本压成**能安全穿过 cmd.exe 的单行参数**。

        做法是把 cmd 的元字符换成同形全角字符（视觉几乎不变，但不再有语义），
        并把所有空白折叠成单个空格。这样得到的字符串可以安全地作为
        ``cmd.exe /c`` 的参数传递，不需要依赖脆弱的转义规则
        （cmd 的引号/``^`` 转义规则在命令行与批处理中并不一致）。

        注意：**这必然改变原文**，所以只用于「命令行摘要」；
        完整原文必须另存为文件交付。
        """
        collapsed = " ".join(text.split())
        table = {
            "%": "％",   # 变量展开：实测 %PATH% 会被替换成上千字符
            "&": "＆",   # 命令分隔：实测会把参数截断
            "|": "｜",   # 管道
            "<": "＜",   # 重定向：实测会让整行消失
            ">": "＞",
            "^": "＾",   # 转义符
            '"': "'",    # 双引号会破坏参数边界
            "`": "｀",
        }
        out = "".join(table.get(ch, ch) for ch in collapsed)
        if len(out) > limit:
            out = out[: limit - 1] + "…"
        return out


    def shell_argv(self, command: str, *, shell: ShellType | None = None) -> list[str]:
        """把一段 shell 命令字符串包成 argv（用于确实需要 shell 语义的场景）。"""
        chosen = shell or self.detect().type
        if IS_WINDOWS:
            if chosen in (ShellType.POWERSHELL, ShellType.PWSH):
                exe = self._executable_for(chosen) or "powershell"
                return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
            if chosen is ShellType.GIT_BASH:
                exe = self._executable_for(ShellType.GIT_BASH) or "bash"
                return [exe, "-lc", command]
            exe = resolve_executable("cmd") or "cmd.exe"
            return [exe, "/c", command]
        return ["/bin/sh", "-c", command]

    def describe(self) -> str:
        info = self.detect()
        return f"{info.type.value} ({info.executable or 'n/a'})"


def default_pathext() -> list[str]:
    if IS_WINDOWS:
        return [s for s in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if s]
    return [""]


def python_executable() -> str:
    return sys.executable
