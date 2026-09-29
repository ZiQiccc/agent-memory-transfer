"""Claude Code Source 测试（Phase 2）。

夹具复刻的是本机实测的 2.1.284 真实格式 —— 包括那些会污染 Memory 的
harness 记账记录。
"""

from __future__ import annotations

from amt.adapters.claude import ClaudeCodeParser, ClaudeCodeSourceAdapter
from amt.adapters.claude.discovery import decode_project_dir
from amt.core.models import EventType, RawSession

from conftest import CLAUDE_SESSION_ID, build_claude_session


def _raw(cwd: str = "D:/proj/demo") -> RawSession:
    from conftest import records_from

    records = records_from(build_claude_session(cwd))
    return RawSession(
        agent="claude",
        session_id=CLAUDE_SESSION_ID,
        path="memory://claude",
        cwd=cwd,
        records=records,
        total_records=len(records),
    )


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def test_parser_extracts_user_and_assistant_messages():
    result = ClaudeCodeParser().parse(_raw())
    types = [e.type for e in result.events]
    assert types.count(EventType.USER_MESSAGE) == 2
    # 只有带 text 块的助手指令才算「助手消息」；纯 tool_use 记录是工具调用
    assert types.count(EventType.ASSISTANT_MESSAGE) == 1
    assert types.count(EventType.TOOL_CALL) == 2
    assert EventType.REASONING in types


def test_harness_bookkeeping_records_are_dropped():
    """attachment / queue-operation / cost-state / atis-latch / last-prompt
    都是 harness 记账，混进事件流会严重污染 Memory。"""
    result = ClaudeCodeParser().parse(_raw())
    contents = " ".join((e.content or "") for e in result.events)
    assert "system-reminder" not in contents
    assert "Not logged in" in contents  # 这是 API 错误，应当保留
    stats = result.stats.skipped_by_type
    for key in ("attachment", "queue-operation", "cost-state", "atis-latch", "last-prompt"):
        assert stats.get(key, 0) >= 1, f"{key} 未被识别为记账记录"


def test_api_error_becomes_error_event():
    """未登录 / 无额度是通过 assistant 上的 isApiErrorMessage 表达的。"""
    result = ClaudeCodeParser().parse(_raw())
    errors = [e for e in result.events if e.type is EventType.ERROR]
    assert errors
    assert errors[0].metadata.get("kind") == "api_error"
    assert "login" in (errors[0].content or "").lower()


def test_sidechain_is_skipped():
    result = ClaudeCodeParser().parse(_raw())
    assert result.stats.sidechain_skipped == 1
    assert not any("侧链内容" in (e.content or "") for e in result.events)


def test_tool_call_and_result_are_paired():
    result = ClaudeCodeParser().parse(_raw())
    calls = {e.metadata.get("call_id"): e for e in result.events if e.type is EventType.TOOL_CALL}
    assert "toolu_1" in calls and "toolu_2" in calls
    assert calls["toolu_1"].command == "mvn -q test"
    assert calls["toolu_1"].metadata.get("category_hint") == "terminal"
    assert calls["toolu_2"].metadata.get("category_hint") == "file_edit"
    assert calls["toolu_2"].metadata.get("paths") == ["src/service/LoginService.java"]

    outputs = [e for e in result.events if e.type is EventType.TOOL_RESULT]
    assert outputs
    assert any(e.metadata.get("is_failure") for e in outputs)
    assert all(e.metadata.get("matched") for e in outputs)


def test_cost_state_metadata_is_captured():
    result = ClaudeCodeParser().parse(_raw())
    assert result.metadata is not None
    assert result.metadata.extra.get("totalLinesAdded") == 4
    assert result.metadata.extra.get("totalLinesRemoved") == 1
    assert result.metadata.extra.get("git_branch") == "feature/login"


def test_cwd_from_records_wins():
    result = ClaudeCodeParser().parse(_raw("D:/from-records"))
    assert result.metadata is not None
    assert result.metadata.cwd == "D:/from-records"


# ----------------------------------------------------------------------
# 发现 / 适配器
# ----------------------------------------------------------------------
def test_sessions_are_discovered(claude_ctx):
    source = ClaudeCodeSourceAdapter(claude_ctx)
    detection = source.detect()
    assert detection.installed and detection.runtime_available

    sessions = source.list_sessions(deep=True)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == CLAUDE_SESSION_ID
    assert session.title and "401" in session.title
    assert session.user_message_count == 2


def test_load_and_parse_roundtrip(claude_ctx):
    source = ClaudeCodeSourceAdapter(claude_ctx)
    raw = source.load_session(CLAUDE_SESSION_ID)
    assert raw.session_id == CLAUDE_SESSION_ID
    events = source.parse_events(raw)
    assert events


def test_unknown_session_raises(claude_ctx):
    import pytest

    from amt.adapters.base import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        ClaudeCodeSourceAdapter(claude_ctx).load_session("nope")


def test_detector_reports_login_state_without_side_effects(claude_ctx):
    """登录状态必须**从既有会话记录推断**，而不是再发一次请求。

    真实教训：早期实现用 `claude -p ping` 探测，会在用户的
    ~/.claude/projects 里留下垃圾会话。
    """
    import sys

    source = ClaudeCodeSourceAdapter(claude_ctx)

    # ① CLI 不存在 → 明确报「未找到」
    usable, note = source.detector.cli_status()
    assert usable is False
    assert "未找到" in note

    # ② CLI 存在时，登录状态应来自**既有会话记录**（夹具最后一条 assistant
    #    记录带 isApiErrorMessage），而不是再发一次请求
    claude_ctx.config.claude.executable = sys.executable
    usable, note = source.detector.cli_status()
    assert usable is False
    assert "未登录" in note or "API 错误" in note

    # ③ 关键：探测不得新增会话（早期实现会污染用户的会话历史）
    sessions_before = len(source.detector.session_files())
    source.detector.cli_status()
    source.detector.detect_target()
    assert len(source.detector.session_files()) == sessions_before


def test_decode_project_dir():
    assert decode_project_dir("D--proj-demo") == "D:\\proj\\demo"
    assert decode_project_dir("") is None


def test_normalizer_reduces_claude_session_to_semantics(claude_ctx):
    """端到端：真实格式 → 事件 → 归一化语义（验证跨 Agent 一致性）。"""
    from amt.core.memory.normalizer import Normalizer

    source = ClaudeCodeSourceAdapter(claude_ctx)
    raw = source.load_session(CLAUDE_SESSION_ID)
    normalized = Normalizer().normalize(source.parse_events(raw))

    categories = {e.category for e in normalized}
    # `mvn -q test` 属于验证类命令，因此归到 test 而不是 terminal
    assert "test" in categories
    assert "file_edit" in categories
    assert any(e.is_failure for e in normalized), "测试失败必须被识别"
    test_run = next(e for e in normalized if e.category == "test")
    assert test_run.exit_code == 1
    assert test_run.is_failure is True
