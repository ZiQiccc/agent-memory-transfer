"""Git 状态采集（技术架构 §14 / 实现plan §十四）。

原则：
1. Git Diff **不**完整进入 Canonical Memory，只保留 ``diff_summary``。
2. 采集失败一律降级为「未知」，不阻断迁移。
3. 统一使用 ``git -C <cwd>``，避免依赖进程工作目录。
"""

from __future__ import annotations

from pathlib import Path

from amt.core.models import GitContext
from amt.services.process import ProcessManager


class GitService:
    def __init__(self, manager: ProcessManager | None = None, executable: str | None = None,
                 timeout: float = 20.0) -> None:
        self.manager = manager or ProcessManager()
        self.timeout = timeout
        self.executable = executable or self.manager.find_executable("git")

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.executable)

    def _git(self, cwd: str | Path, *args: str):
        if not self.executable:
            return None
        argv = [self.executable, "-C", str(cwd), *args]
        return self.manager.run(argv, timeout=self.timeout)

    # ------------------------------------------------------------------
    def is_repository(self, cwd: str | Path) -> bool:
        result = self._git(cwd, "rev-parse", "--is-inside-work-tree")
        return bool(result and result.ok and "true" in result.stdout.strip().lower())

    def top_level(self, cwd: str | Path) -> str | None:
        result = self._git(cwd, "rev-parse", "--show-toplevel")
        if result and result.ok and result.stdout.strip():
            return result.stdout.strip().splitlines()[0].strip()
        return None

    def branch(self, cwd: str | Path) -> str | None:
        result = self._git(cwd, "branch", "--show-current")
        if result and result.ok:
            name = result.stdout.strip()
            if name:
                return name
        # detached HEAD 时回退到 describe
        result = self._git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if result and result.ok and result.stdout.strip() != "HEAD":
            return result.stdout.strip()
        return None

    def head_commit(self, cwd: str | Path) -> str | None:
        result = self._git(cwd, "rev-parse", "--short", "HEAD")
        if result and result.ok and result.stdout.strip():
            return result.stdout.strip()
        return None

    def remote_url(self, cwd: str | Path) -> str | None:
        result = self._git(cwd, "remote", "get-url", "origin")
        if result and result.ok and result.stdout.strip():
            return result.stdout.strip()
        return None

    def status_lines(self, cwd: str | Path) -> list[str]:
        result = self._git(cwd, "status", "--short")
        if not result or not result.ok:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def diff_stat(self, cwd: str | Path) -> str | None:
        result = self._git(cwd, "diff", "--stat")
        if not result or not result.ok:
            return None
        text = result.stdout.strip()
        return text or None

    @staticmethod
    def parse_status(lines: list[str]) -> tuple[list[str], list[str]]:
        """把 ``git status --short`` 拆成 (已跟踪改动, 未跟踪)。"""
        tracked: list[str] = []
        untracked: list[str] = []
        for raw in lines:
            if not raw.strip():
                continue
            code = raw[:2]
            path = raw[3:].strip()
            if " -> " in path:  # rename
                path = path.split(" -> ", 1)[1].strip()
            path = path.strip('"')
            if code.strip() == "??":
                untracked.append(path)
            else:
                tracked.append(path)
        return tracked, untracked

    # ------------------------------------------------------------------
    def collect(self, cwd: str | Path) -> GitContext:
        """采集 Git 上下文。非 Git 目录返回空上下文（不抛异常）。"""
        cwd = str(cwd)
        if not self.available:
            return GitContext(status_summary="git 不可用（未找到 git 可执行文件）")
        if not Path(cwd).is_dir():
            return GitContext(status_summary=f"目录不存在：{cwd}")
        if not self.is_repository(cwd):
            return GitContext(status_summary="当前目录不是 Git 仓库")

        lines = self.status_lines(cwd)
        tracked, untracked = self.parse_status(lines)
        status_summary_parts = []
        if tracked:
            status_summary_parts.append(f"{len(tracked)} 个已跟踪文件被修改")
        if untracked:
            status_summary_parts.append(f"{len(untracked)} 个未跟踪文件")
        if not status_summary_parts:
            status_summary_parts.append("工作区干净")

        return GitContext(
            repository=self.remote_url(cwd) or self.top_level(cwd),
            branch=self.branch(cwd),
            commit=self.head_commit(cwd),
            status_summary="，".join(status_summary_parts),
            changed_files=tracked,
            diff_summary=self.diff_stat(cwd),
            has_uncommitted_changes=bool(tracked or untracked),
        )
