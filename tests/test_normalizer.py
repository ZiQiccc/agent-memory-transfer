"""Normalizer 测试：跨 Agent 语义归一（POC-04）。"""

from __future__ import annotations

from amt.core.memory.normalizer import (
    Normalizer,
    first_error_line,
    is_expected_nonzero,
)
from amt.core.models import AgentEvent, EventType


def _call(ordinal: int, name: str, args: dict, call_id: str = "c1") -> AgentEvent:
    import json

    return AgentEvent(
        id=f"codex:{ordinal}:tool_call",
        type=EventType.TOOL_CALL,
        role="assistant",
        tool_name=name,
        command=args.get("cmd"),
        content=json.dumps(args, ensure_ascii=False),
        metadata={"call_id": call_id, "arguments": args, "ordinal": ordinal, "significance": 2},
    )


def _output(ordinal: int, call_id: str, output: str, exit_code: int | None = None) -> AgentEvent:
    return AgentEvent(
        id=f"codex:{ordinal}:tool_result",
        type=EventType.TOOL_RESULT,
        role="tool",
        content=output,
        result=output,
        metadata={
            "call_id": call_id,
            "exit_code": exit_code,
            "is_failure": bool(exit_code not in (None, 0)),
            "ordinal": ordinal,
            "significance": 1,
        },
    )


def test_shell_command_is_normalized_to_terminal():
    events = [
        _call(1, "exec_command", {"cmd": "git status --short"}),
        _output(2, "c1", " M src/A.java", 0),
    ]
    result = Normalizer().normalize(events)
    assert len(result) == 1
    assert result[0].type is EventType.TERMINAL
    assert result[0].category == "terminal"
    assert result[0].exit_code == 0
    assert result[0].is_failure is False


def test_build_command_gets_its_own_category():
    """构建命令归入 build 类别（而非继续留在 terminal），否则验证类统计会漏项。"""
    events = [_call(1, "exec_command", {"cmd": "mvn -q -DskipTests compile"}), _output(2, "c1", "ok", 0)]
    result = Normalizer().normalize(events)
    assert result[0].type is EventType.TEST
    assert result[0].category == "build"


def test_claude_tool_names_map_to_same_semantics():
    """跨 Agent 一致性：Claude 的 Bash / Edit 必须与 Codex 归一到同一语义。"""
    events = [
        _call(1, "Bash", {"cmd": "ls -la"}),  # 普通命令 → terminal
        _call(2, "Bash", {"cmd": "npm test"}, call_id="c2"),  # 测试命令 → test
        _call(3, "Edit", {"file_path": "a.ts"}, call_id="c3"),  # 编辑 → file_edit
    ]
    result = Normalizer().normalize(events)
    assert result[0].category == "terminal"
    assert result[1].category == "test"
    assert result[2].category == "file_edit"


def test_search_command_exit_code_one_is_not_a_failure():
    """rg/grep 未匹配到结果时退出码为 1，属于正常返回。"""
    events = [
        _call(1, "exec_command", {"cmd": 'rg -n "NoSuchThing" src -S'}),
        _output(2, "c1", "", 1),
    ]
    result = Normalizer().normalize(events)
    assert result[0].is_failure is False
    assert result[0].metadata.get("nonzero_is_normal") is True


def test_expected_nonzero_helper_scope():
    assert is_expected_nonzero("rg foo .", 1) is True
    assert is_expected_nonzero("grep -r foo .", 1) is True
    assert is_expected_nonzero("rg foo .", 2) is False, "退出码 2 是真实错误"
    assert is_expected_nonzero("mvn test", 1) is False


def test_test_command_is_classified_as_test():
    events = [
        _call(1, "exec_command", {"cmd": "mvn -q test"}),
        _output(2, "c1", "[ERROR] Tests run: 1, Failures: 1", 1),
    ]
    result = Normalizer().normalize(events)
    assert result[0].type is EventType.TEST
    assert result[0].is_failure is True
    assert "测试失败" in result[0].summary


def test_build_command_classified_separately_from_test():
    events = [_call(1, "exec_command", {"cmd": "npm run build"}), _output(2, "c1", "ok", 0)]
    result = Normalizer().normalize(events)
    assert result[0].category == "build"


def test_patch_paths_and_summary_are_attached():
    event = AgentEvent(
        id="codex:1:tool_call",
        type=EventType.TOOL_CALL,
        tool_name="exec_command",
        command="apply_patch <<'PATCH' ...",
        metadata={
            "call_id": "c1",
            "paths": ["src/A.java"],
            "patch_file_stats": {"src/A.java": {"hunks": 2, "added": 5, "removed": 1}},
            "patch_statuses": {"src/A.java": "modified"},
            "category_hint": "file_edit",
        },
    )
    result = Normalizer().normalize([event])
    assert result[0].category == "file_edit"
    assert "A.java" in result[0].summary
    assert "+5/-1" in result[0].summary


def test_duplicate_messages_are_deduplicated():
    """Codex 的 task_complete 与其 assistant message 内容重复，只保留一条。"""
    content = "这是一段足够长的重复内容，用于验证去重逻辑是否生效，谢谢。" * 2
    events = [
        AgentEvent(id="a", type=EventType.ASSISTANT_MESSAGE, role="assistant", content=content),
        AgentEvent(id="b", type=EventType.ASSISTANT_MESSAGE, role="assistant", content=content),
    ]
    result = Normalizer().normalize(events)
    assert len(result) == 1


def test_interrupt_is_kept_as_error_event():
    events = [
        AgentEvent(
            id="x",
            type=EventType.ERROR,
            content="本轮任务被中断（reason=interrupted）",
            metadata={"kind": "turn_aborted", "reason": "interrupted", "significance": 2},
        )
    ]
    result = Normalizer().normalize(events)
    assert result[0].type is EventType.ERROR
    assert result[0].is_failure is True


def test_empty_cleaned_output_is_not_replaced_by_raw_envelope():
    """回归用例：清洗后为空的输出不得回退到未清洗的原始信封。

    早期实现用 ``event.result or event.content``，空串是假值，会把
    「Chunk ID: xxx / Wall time: ...」这类信封当成工具输出带进 Memory。
    """
    call = _call(1, "exec_command", {"cmd": "mvn -q compile"})
    result = AgentEvent(
        id="codex:2:tool_result",
        type=EventType.TOOL_RESULT,
        role="tool",
        content="Chunk ID: ba10a0\nWall time: 7.8 seconds\nProcess exited with code 0\nOriginal token count: 0\nOutput:",
        result="",  # 清洗后为空
        metadata={"call_id": "c1", "exit_code": 0, "is_failure": False, "significance": 1},
    )
    normalized = Normalizer().normalize([call, result])
    assert len(normalized) == 1
    assert normalized[0].result == ""
    assert "Chunk ID" not in normalized[0].result


def test_test_command_category_is_test_not_terminal():
    """测试命令必须带 category=test（否则 validation.tests 会漏掉它）。"""
    events = [_call(1, "exec_command", {"cmd": "pytest -q"}), _output(2, "c1", "1 passed", 0)]
    result = Normalizer().normalize(events)
    assert result[0].type is EventType.TEST
    assert result[0].category == "test"


def test_first_error_line_skips_envelope_noise():
    text = "Process exited with code 1\nOriginal token count: 207\nOutput:\nerror: 找不到符号\n"
    assert first_error_line(text) == "error: 找不到符号"


def test_first_error_line_returns_empty_instead_of_misleading_header():
    """找不到可读报错时应返回空串，而不是随便挑一行当报错。"""
    text = "FullName\n----\nC:/a/b.jar\nC:/c/d.jar\n"
    assert first_error_line(text) == ""


def test_recent_commands_collects_in_order():
    events = [
        _call(1, "exec_command", {"cmd": "git status"}, call_id="c1"),
        _call(2, "exec_command", {"cmd": "mvn test"}, call_id="c2"),
    ]
    commands = Normalizer.recent_commands(Normalizer().normalize(events))
    assert commands == ["git status", "mvn test"]
