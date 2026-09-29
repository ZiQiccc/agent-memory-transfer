"""Codex Source Adapter 测试（POC-01 ~ POC-03）。"""

from __future__ import annotations

from amt.adapters.codex.parser import (
    CodexParser,
    _analyze_patch,
    _clean_codex_output,
    _looks_like_failure,
    iter_json_records,
)
from amt.core.models import EventType, RawSession

from conftest import PATCHED_FILE, SESSION_ID, build_rollout, raw_session_from


# ----------------------------------------------------------------------
# 容错读取
# ----------------------------------------------------------------------
def test_iter_json_records_handles_cross_line_records():
    """跨多个物理行的 JSON 记录必须被还原，而不是被当成解析失败丢掉。

    这是本机实测的真实缺陷：行式 json.loads 在 72 个会话上会误报 35 处失败。
    """
    text = '{"a":1}\n{\n  "b": "值",\n  "c": [1, 2]\n}\n{"d":3}\n'
    records = [r for r in iter_json_records(text)]
    assert all(r is not None for r in records), "不应出现无法解析的片段"
    assert [sorted(r.keys()) for r in records if r] == [["a"], ["b", "c"], ["d"]]
    assert records[1]["c"] == [1, 2]


def test_line_based_parsing_would_lose_cross_line_records():
    """反证：行式解析无法处理跨行记录——这正是需要容错读取器的原因。"""
    import json

    text = '{"a":1}\n{\n  "b": "值"\n}\n{"d":3}\n'
    line_results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            line_results.append(json.loads(line))
        except ValueError:
            line_results.append(None)
    assert None in line_results
    assert all(r is not None for r in iter_json_records(text))


def test_iter_json_records_reports_unparsable_fragment():
    text = '{"a":1}\n<<<坏数据>>>\n{"c":3}\n'
    records = list(iter_json_records(text))
    assert sum(1 for r in records if r is None) == 1
    assert sum(1 for r in records if r is not None) == 2


# ----------------------------------------------------------------------
# 输出信封与失败判定
# ----------------------------------------------------------------------
def test_clean_codex_output_strips_envelope():
    raw = (
        "Chunk ID: 016a84\nWall time: 6.0399 seconds\n"
        "Process exited with code 1\nOriginal token count: 102\nOutput:\n真实输出内容\n"
    )
    assert _clean_codex_output(raw) == "真实输出内容"


def test_clean_codex_output_handles_missing_envelope_lines():
    """信封行不总是齐全，也不总是在开头。"""
    raw = "Original token count: 82\nOutput:\nCompiling 1 file\n"
    assert _clean_codex_output(raw) == "Compiling 1 file"


def test_clean_codex_output_with_only_envelope_returns_empty():
    """命令成功但没有输出时（Original token count: 0），结果应是空串。

    回归用例：早期实现用 ``cleaned or output`` 兜底，会把信封原样还回去，
    导致 Memory 里出现「输出摘要 = Chunk ID: xxx」这种无意义内容。
    """
    raw = (
        "Chunk ID: ba10a0\nWall time: 7.8250 seconds\n"
        "Process exited with code 0\nOriginal token count: 0\nOutput:"
    )
    assert _clean_codex_output(raw) == ""


def test_clean_codex_output_leaves_plain_text_untouched():
    assert _clean_codex_output("普通输出，没有信封") == "普通输出，没有信封"


def test_failure_detection_prefers_exit_code():
    assert _looks_like_failure("Process exited with code 1\nOutput:\nboom", 1) is True
    assert _looks_like_failure("Process exited with code 0\nOutput:\nok", 0) is False


def test_failure_detection_does_not_flag_log_content_as_error():
    """读取一个含 error 字样的日志文件，不应被判成失败。"""
    log_content = "\n".join(f"line {i} filler" for i in range(30)) + "\n[ERROR] old problem from 3 days ago\n"
    assert _looks_like_failure(log_content, None) is False


def test_failure_detection_catches_strong_marker_without_exit_code():
    assert _looks_like_failure("apply_patch verification failed: no match", None) is True


def test_analyze_patch_counts_changes():
    patch = (
        "*** Begin Patch\n*** Update File: a.java\n@@\n"
        "-old line\n+new line\n+another line\n*** End Patch"
    )
    stats = _analyze_patch(patch)
    assert stats["a.java"] == {"hunks": 1, "added": 2, "removed": 1}


# ----------------------------------------------------------------------
# 事件解析
# ----------------------------------------------------------------------
def test_parser_maps_records_to_events():
    result = CodexParser().parse(raw_session_from(build_rollout("/tmp/proj")))
    types = [e.type for e in result.events]

    # 真实用户消息只应有一条（注入的 developer / environment_context 被过滤）
    assert types.count(EventType.USER_MESSAGE) == 1
    assert result.stats.injected_user_texts >= 1
    # 推理摘要被保留（扩展事件）
    assert EventType.REASONING in types
    # 工具调用与输出都被捕获
    assert types.count(EventType.TOOL_CALL) >= 4
    assert types.count(EventType.TOOL_RESULT) >= 4
    # 中断信号被识别为 ERROR
    assert any(e.type is EventType.ERROR for e in result.events)


def test_parser_marks_shell_heredoc_patch_as_file_edit():
    """apply_patch 经 shell heredoc 调用时，必须仍被识别为文件编辑。"""
    result = CodexParser().parse(raw_session_from(build_rollout("/tmp/proj")))
    hints = [
        e.metadata.get("category_hint")
        for e in result.events
        if e.type is EventType.TOOL_CALL and e.metadata.get("paths")
    ]
    assert "file_edit" in hints
    patched = [e for e in result.events if PATCHED_FILE in (e.metadata.get("paths") or [])]
    assert patched, "应解析出被 patch 的文件路径"


def test_parser_records_session_metadata_and_cwd():
    result = CodexParser().parse(raw_session_from(build_rollout("D:/proj/demo")))
    assert result.metadata is not None
    assert result.metadata.cwd == "D:/proj/demo"
    assert result.metadata.cli_version == "0.149.1"


def test_cross_line_record_is_recovered_from_fixture():
    """fixture 中的跨行记录必须变成一条助手消息，而不是解析失败。"""
    raw = raw_session_from(build_rollout("/tmp/proj"))
    assert raw.parse_errors == 0
    result = CodexParser().parse(raw)
    assert any("跨行记录的助手消息" in (e.content or "") for e in result.events)


# ----------------------------------------------------------------------
# 会话发现
# ----------------------------------------------------------------------
def test_discovery_lists_and_finds_sessions(codex_source):
    sessions = codex_source.list_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == SESSION_ID
    assert session.cwd is not None
    # 非 deep 模式不得谎报统计值为 0
    assert session.user_message_count is None
    assert session.title and "保养日期" in session.title


def test_discovery_deep_mode_reports_counts(codex_source):
    session = codex_source.list_sessions(deep=True)[0]
    assert session.user_message_count == 1
    assert session.record_count and session.record_count > 10


def test_load_session_roundtrip(codex_source):
    raw = codex_source.load_session(SESSION_ID)
    assert raw.session_id == SESSION_ID
    assert raw.total_records > 10
    assert raw.cwd
    events = codex_source.parse_events(raw)
    assert events


def test_load_session_raises_for_unknown_id(codex_source):
    import pytest

    from amt.adapters.base import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        codex_source.load_session("does-not-exist")
