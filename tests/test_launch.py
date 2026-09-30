"""交互式启动的回归测试。

锁定两个真实缺陷（用户实际遇到并报告）：

1. **没有创建独立控制台** —— 早期实现只用了 ``CREATE_NEW_PROCESS_GROUP``，
   子进程继承父进程的控制台，于是启动出来的 Codex TUI 直接接管了运行 ``amt``
   的那个终端窗口，用户看不到新窗口也无法切换进去。
   实测证据：``GetConsoleWindow`` 句柄在 ``NEW_PROCESS_GROUP`` 下为 0，
   在 ``NEW_CONSOLE`` 下为 330838。

2. **初始 Prompt 被 cmd.exe 破坏** —— Codex 常以 ``.cmd`` 形式安装，
   必须经 ``cmd.exe /c`` 启动，而 shell 会再解析一次参数。
   实测：多行文本只送到第一行；``%PATH%`` 被展开（26 字 → 1473 字）；
   ``&``/``|`` 切断命令；``<``/``>`` 让整行消失。

测试**绝不真的启动外部进程**：``subprocess.Popen`` 被替换为记录器。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from amt.config import AMTConfig, ClaudeConfig, CodexConfig, LLMConfig, WorkBuddyConfig
from amt.context import AppContext
from amt.core.models import TargetContext
from amt.services.launch import build_launch_command
from amt.services.process import ProcessManager, describe_start_failure
from amt.services.shell import ShellResolver


@pytest.fixture
def launch_ctx(tmp_path: Path) -> AppContext:
    return AppContext(
        config=AMTConfig(
            home_dir=tmp_path / "amt",
            llm=LLMConfig(enabled=False),
            codex=CodexConfig(home=tmp_path / "no-codex", executable=str(tmp_path / "none")),
            claude=ClaudeConfig(projects_dir=tmp_path / "no-claude", executable=str(tmp_path / "none")),
            workbuddy=WorkBuddyConfig(projects_dir=tmp_path / "no-wb"),
        )
    )


PROMPT = """你正在继续一个已经进行中的开发任务。

请先读取 .agent-transfer/memory.md，理解任务当前状态，然后继续执行。

## 任务目标

覆盖 60% 的场景 & 边界（a|b），处理 <文件> 与 "引号"。

## 执行要求

1. 不要重新分析或重做已经完成的工作。
"""


# ----------------------------------------------------------------------
# 缺陷 1：独立控制台
# ----------------------------------------------------------------------
class _FakePopen:
    """记录 creationflags 的假 Popen；可按需抛错或立即"退出"。"""

    captured: list[int] = []
    failures: dict[int, OSError] = {}
    exit_codes: dict[int, int] = {}
    next_pid = 1000

    def __init__(self, argv, **kwargs):
        flags = int(kwargs.get("creationflags") or 0)
        _FakePopen.captured.append(flags)
        if flags in _FakePopen.failures:
            raise _FakePopen.failures[flags]
        self.pid = _FakePopen.next_pid
        _FakePopen.next_pid += 1
        self.argv = argv
        self._flags = flags

    def poll(self):
        """只在 exit_codes 里登记过的标志位下假装已退出，用于验证存活探测。"""
        return _FakePopen.exit_codes.get(self._flags)

    def terminate(self) -> None:  # pragma: no cover - 仅满足接口
        pass


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.captured = []
    _FakePopen.failures = {}
    _FakePopen.exit_codes = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_windows_launch_requests_a_new_console(fake_popen, monkeypatch):
    """必须请求 CREATE_NEW_CONSOLE，而不是只开一个进程组。"""
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    manager = ProcessManager()

    proc, note = manager.start(["codex"], cwd="C:/x")

    assert proc is not None
    assert note == "ok"
    flags = fake_popen.captured[0]
    assert flags & subprocess.CREATE_NEW_CONSOLE, "缺少 CREATE_NEW_CONSOLE 会占用当前窗口"
    assert flags & 0x01000000, "应同时尝试脱离 Job，避免父进程退出后窗口被连带关闭"


def test_launch_falls_back_when_breakaway_is_denied(fake_popen, monkeypatch):
    """Job 不允许 breakaway 时 CreateProcess 返回 ERROR_ACCESS_DENIED，必须回退。"""
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    new_console = subprocess.CREATE_NEW_CONSOLE
    _FakePopen.failures = {
        new_console | 0x01000000: PermissionError(5, "拒绝访问。"),
    }
    manager = ProcessManager()

    proc, note = manager.start(["codex"])

    assert proc is not None
    assert fake_popen.captured == [new_console | 0x01000000, new_console]
    assert note == "ok", "第二次就是首选的新控制台，不算降级"


def test_launch_reports_degradation_when_only_process_group_works(fake_popen, monkeypatch):
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    new_console = subprocess.CREATE_NEW_CONSOLE
    group = subprocess.CREATE_NEW_PROCESS_GROUP
    _FakePopen.failures = {
        new_console | 0x01000000: OSError(5, "拒绝访问。"),
        new_console: OSError(5, "拒绝访问。"),
    }
    manager = ProcessManager()

    proc, note = manager.start(["codex"])

    assert proc is not None
    assert fake_popen.captured[-1] == group
    assert "降级" in note, "退化为「占用当前窗口」时必须如实上报，不能假装正常"


def test_failure_message_is_deduplicated_and_actionable(fake_popen, monkeypatch):
    """三次尝试若原因相同，不应把同一句话重复三遍；且要给出可执行的建议。"""
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    for flags in (
        subprocess.CREATE_NEW_CONSOLE | 0x01000000,
        subprocess.CREATE_NEW_CONSOLE,
        subprocess.CREATE_NEW_PROCESS_GROUP,
    ):
        _FakePopen.failures[flags] = PermissionError(5, "拒绝访问。")
    manager = ProcessManager()

    proc, note = manager.start(["codex"])

    assert proc is None
    assert note.count("拒绝访问") == 1, f"重复报错影响可读性：{note}"
    assert "均被拒绝" in note
    assert "新控制台" not in note, "不必把每次尝试的标签都堆出来"
    assert "--no-launch" in note, "应告诉用户可退化为手动启动"


def test_describe_start_failure_keeps_distinct_reasons():
    text = describe_start_failure(["a: err1", "b: err2"])
    assert "err1" in text and "err2" in text


def test_immediate_exit_is_reported_as_failure(fake_popen, monkeypatch):
    """独立窗口捕获不到输出：目标 CLI 启动即崩时必须报失败，而不是「已启动」。

    否则用户只看到「窗口闪了一下」，完全无从排查。
    """
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    monkeypatch.setattr("amt.services.process.time.sleep", lambda _s: None)
    _FakePopen.exit_codes = {
        subprocess.CREATE_NEW_CONSOLE | 0x01000000: 1,
        subprocess.CREATE_NEW_CONSOLE: 1,
        subprocess.CREATE_NEW_PROCESS_GROUP: 1,
    }
    manager = ProcessManager()

    proc, note = manager.start(["codex"], alive_check_seconds=1.2)

    assert proc is None
    assert "立即退出" in note and "退出码 1" in note
    assert "手动执行" in note, "应引导用户手动执行以看到报错"


def test_alive_check_is_skipped_by_default(fake_popen, monkeypatch):
    """默认不做存活观察：不该为普通启动引入固定延迟。"""
    monkeypatch.setattr("amt.services.process.os.name", "nt")
    calls: list[float] = []
    monkeypatch.setattr("amt.services.process.time.sleep", lambda s: calls.append(s))
    manager = ProcessManager()

    proc, note = manager.start(["codex"])

    assert proc is not None and note == "ok"
    assert calls == [], "未要求存活探测时不应 sleep"


# ----------------------------------------------------------------------
# 缺陷 2：参数投递
# ----------------------------------------------------------------------
def test_shell_wrapper_is_needed_only_for_batch_files(monkeypatch):
    monkeypatch.setattr("amt.services.shell.IS_WINDOWS", True)
    resolver = ShellResolver()
    assert resolver.needs_shell_wrapper(r"C:\x\codex.CMD") is True
    assert resolver.needs_shell_wrapper(r"C:\x\thing.bat") is True
    assert resolver.needs_shell_wrapper(r"C:\Users\x\.local\bin\claude.exe") is False


def test_sanitize_argument_neutralises_cmd_metacharacters():
    resolver = ShellResolver()
    raw = 'line1\nline2 60% & a|b <f> ^ "q"'
    safe = resolver.sanitize_argument(raw)

    assert "\n" not in safe, "cmd 只保留第一行，必须折叠为单行"
    for ch in '%&|<>^"':
        assert ch not in safe, f"cmd 元字符 {ch!r} 必须被替换"
    assert "％" in safe and "＆" in safe and "｜" in safe, "应换成同形全角字符以保留可读性"


def test_sanitize_argument_truncates_long_text():
    resolver = ShellResolver()
    safe = resolver.sanitize_argument("字" * 5000, limit=200)
    assert len(safe) <= 200


def test_batch_target_gets_single_line_summary_plus_prompt_file(launch_ctx, tmp_path):
    """`.cmd` 目标：命令行只放安全摘要，完整 Prompt 必须落盘。"""
    argv, prompt_file, notes = build_launch_command(
        launch_ctx,
        executable=r"C:\tools\codex.CMD",
        prompt=PROMPT,
        project_root=tmp_path,
    )

    assert argv[0].lower().endswith("cmd.exe")
    assert argv[1] == "/c"
    arg = argv[-1]
    assert "\n" not in arg
    assert not any(ch in arg for ch in '%&|<>^"'), f"摘要未安全化：{arg}"
    assert "initial-prompt.md" in arg, "摘要必须指路到完整文件"
    assert "60" in arg and "％" in arg, "目标应出现在摘要里，且已安全化"

    assert prompt_file is not None and prompt_file.is_file()
    assert prompt_file.read_text(encoding="utf-8") == PROMPT, "落盘的必须是完整原文"
    assert notes and "cmd" in notes[0]


def test_real_executable_gets_full_prompt_without_shell(launch_ctx, tmp_path):
    """真实 .exe：不经任何 shell，完整 Prompt 可以原样传递（含多行）。"""
    argv, prompt_file, notes = build_launch_command(
        launch_ctx,
        executable=r"C:\Users\x\.local\bin\claude.exe",
        prompt=PROMPT,
        project_root=tmp_path,
    )

    assert argv == [r"C:\Users\x\.local\bin\claude.exe", PROMPT]
    assert "\n" in argv[-1], "不经过 shell 时不应做任何降级处理"
    assert prompt_file is None
    assert notes == []


def test_prompt_file_lives_in_memory_dir(launch_ctx, tmp_path):
    _, prompt_file, _ = build_launch_command(
        launch_ctx, executable=r"C:\tools\codex.CMD", prompt=PROMPT, project_root=tmp_path
    )
    assert prompt_file is not None
    assert prompt_file.parent == tmp_path / launch_ctx.config.memory_dir


# ----------------------------------------------------------------------
# 启动器 → 模型
# ----------------------------------------------------------------------
def test_launcher_surfaces_notes_as_warnings(launch_ctx, tmp_path, monkeypatch):
    """启动说明必须能传到记录里，否则用户看不懂为什么命令行只有一句摘要。"""
    from amt.adapters.codex import CodexTargetAdapter
    from amt.core.models import MigrationRecord

    shim = tmp_path / "codex.CMD"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    launch_ctx.config.codex.executable = str(shim)

    target = CodexTargetAdapter(launch_ctx)
    monkeypatch.setattr(
        type(target.launcher), "executable", lambda self: str(shim), raising=False
    )

    captured: dict = {}

    def fake_start(argv, *, cwd=None, env=None, new_console=True, alive_check_seconds=0.0):
        captured["argv"] = argv
        captured["new_console"] = new_console
        captured["cwd"] = cwd
        captured["alive_check_seconds"] = alive_check_seconds

        class P:
            pid = 4321

        return P(), "ok"

    monkeypatch.setattr(launch_ctx.process, "start", fake_start)

    context = TargetContext(
        agent="codex",
        working_directory=str(tmp_path),
        memory_file=str(tmp_path / ".agent-transfer" / "memory.md"),
        task_context=PROMPT,
    )
    launch = target.launcher.launch(context)

    assert captured["new_console"] is True, "启动器必须显式要求新控制台"
    assert captured["alive_check_seconds"] > 0, "独立窗口拿不到输出，必须做存活探测"
    assert launch.success is True
    assert launch.warnings, "必须把「命令行只传摘要」这一说明带出来"
    assert "新窗口" in launch.message
    assert MigrationRecord(migration_id="x", source_agent="a", target_agent="b").warnings is not None
