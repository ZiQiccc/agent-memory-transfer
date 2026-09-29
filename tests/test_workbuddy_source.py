"""WorkBuddy Source 测试。

夹具复刻的是本机实测的真实格式（由用户提供的会话样本确认）：

- ``timestamp`` 是毫秒整数；
- 用户消息把 harness 注入与真实需求放在**同一个文本块**里 ——
  这是与 Claude 侧**相反**的陷阱：Claude 要整条丢弃注入消息，
  而 WorkBuddy 必须从标签里**抽取**，否则会丢掉整个需求。
"""

from __future__ import annotations

from amt.adapters.workbuddy import (
    WorkBuddyDetector,
    WorkBuddyParser,
    WorkBuddySourceAdapter,
    decode_workspace_dir,
)
from amt.core.models import EventType, RawSession

from conftest import WORKBUDDY_SESSION_ID, WORKBUDDY_WORKSPACE, build_workbuddy_session


def _raw(cwd: str = "C:/proj/demo"):
    from conftest import records_from

    records = records_from(build_workbuddy_session(cwd))
    return RawSession(
        agent="workbuddy",
        session_id=WORKBUDDY_SESSION_ID,
        path="memory://workbuddy",
        cwd=cwd,
        records=records,
        total_records=len(records),
    )


# ----------------------------------------------------------------------
# 消息解析：注入与需求同块
# ----------------------------------------------------------------------
def test_user_query_is_extracted_from_injected_block():
    """真实需求必须从 <user_query> 里抽出来，不能被注入文本淹没。"""
    result = WorkBuddyParser().parse(_raw())
    users = [e for e in result.events if e.type is EventType.USER_MESSAGE]
    assert len(users) == 2
    assert users[0].content == "登录接口偶发 401，帮我排查并修复"
    assert users[1].content == "再确认一下 Redis 的 TTL 配置"
    assert result.stats.recovered_user_queries == 2


def test_injected_text_does_not_leak_into_events():
    result = WorkBuddyParser().parse(_raw())
    blob = " ".join((e.content or "") for e in result.events)
    assert "<system-reminder" not in blob
    assert "<identity_context>" not in blob


def test_pure_injection_message_is_skipped():
    """没有 <user_query> 的纯注入消息必须丢弃。"""
    raw = _raw()
    raw.records.insert(
        0,
        {
            "id": "inject-only",
            "timestamp": 1789108260000,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<system-reminder>\n<user_info>\nOS: win32\n</user_info>\n</system-reminder>"}],
        },
    )
    result = WorkBuddyParser().parse(raw)
    assert result.stats.injected_only_messages == 1
    assert sum(1 for e in result.events if e.type is EventType.USER_MESSAGE) == 2


def test_assistant_message_and_reasoning():
    result = WorkBuddyParser().parse(_raw())
    assistants = [e for e in result.events if e.type is EventType.ASSISTANT_MESSAGE]
    assert len(assistants) == 1
    assert "Session 刷新逻辑" in assistants[0].content

    reasoning = [e for e in result.events if e.type is EventType.REASONING]
    assert reasoning
    assert "TTL" in reasoning[0].content


# ----------------------------------------------------------------------
# 工具调用
# ----------------------------------------------------------------------
def test_tool_calls_and_results_are_paired():
    result = WorkBuddyParser().parse(_raw())
    calls = {e.metadata.get("call_id"): e for e in result.events if e.type is EventType.TOOL_CALL}
    assert {"call-1", "call-2", "call-3"} <= set(calls)

    assert calls["call-1"].command == "mvn -q test"
    assert calls["call-1"].metadata.get("category_hint") == "terminal"
    assert calls["call-2"].metadata.get("paths") == ["src/service/LoginService.java"]
    assert calls["call-2"].metadata.get("category_hint") == "file_edit"

    outputs = [e for e in result.events if e.type is EventType.TOOL_RESULT]
    assert len(outputs) == 3
    assert all(e.metadata.get("matched") for e in outputs)


def test_failed_status_and_error_output_are_detected():
    result = WorkBuddyParser().parse(_raw())
    outputs = {e.metadata.get("call_id"): e for e in result.events if e.type is EventType.TOOL_RESULT}
    # status=failed → 失败
    assert outputs["call-3"].metadata["is_failure"] is True
    # status=completed 但输出里有 [ERROR] → 也算失败
    assert outputs["call-1"].metadata["is_failure"] is True
    # 正常完成
    assert outputs["call-2"].metadata["is_failure"] is False


def test_bookkeeping_records_are_skipped():
    result = WorkBuddyParser().parse(_raw())
    assert result.stats.skipped_by_type.get("file-history-snapshot") == 1


def test_timestamp_is_milliseconds():
    result = WorkBuddyParser().parse(_raw())
    users = [e for e in result.events if e.type is EventType.USER_MESSAGE]
    year = users[0].timestamp.year
    assert 2020 <= year <= 2100, f"毫秒时间戳未正确转换：{users[0].timestamp}"


def test_model_is_captured():
    result = WorkBuddyParser().parse(_raw())
    assert result.metadata is not None
    assert result.metadata.model == "hy4-preview-f"


# ----------------------------------------------------------------------
# 发现 / 能力
# ----------------------------------------------------------------------
def test_sessions_are_discovered(workbuddy_ctx):
    adapter = WorkBuddySourceAdapter(workbuddy_ctx)
    detection = adapter.detect()
    assert detection.installed and detection.runtime_available

    sessions = adapter.list_sessions(deep=True)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == WORKBUDDY_SESSION_ID
    assert session.title and "401" in session.title, "标题应是真实提问"
    assert session.user_message_count == 2
    assert session.model == "hy4-preview-f"
    # WorkBuddy 没有 CLI 恢复入口
    assert session.resumable is False


def test_empty_workspace_does_not_break_scan(workbuddy_ctx):
    sessions = WorkBuddySourceAdapter(workbuddy_ctx).list_sessions()
    assert len(sessions) == 1


def test_unknown_session_raises(workbuddy_ctx):
    import pytest

    from amt.adapters.base import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        WorkBuddySourceAdapter(workbuddy_ctx).load_session("nope")


def test_detector_is_source_only(workbuddy_ctx):
    """注入机制未经验证，因此 Target 必须如实标注为不支持。"""
    installation = WorkBuddyDetector(workbuddy_ctx).installation()
    assert installation.source_supported is True
    assert installation.target_supported is False
    assert any("暂不支持作为迁移目标" in n for n in installation.notes)


def test_missing_directory_reported(tmp_path):
    from amt.config import AMTConfig, WorkBuddyConfig
    from amt.context import AppContext

    ctx = AppContext(
        config=AMTConfig(home_dir=tmp_path / "amt", workbuddy=WorkBuddyConfig(projects_dir=tmp_path / "gone"))
    )
    detection = WorkBuddyDetector(ctx).detect()
    assert detection.installed is False
    assert "未找到" in detection.detail


def test_decode_workspace_dir():
    decoded = decode_workspace_dir(WORKBUDDY_WORKSPACE)
    assert decoded is not None
    assert decoded.startswith("C:\\Users\\HUAWEI\\WorkBuddy")


def test_normalizer_maps_workbuddy_tools(workbuddy_ctx):
    """端到端：WorkBuddy 工具名必须与其它 Agent 归一到同一语义。"""
    from amt.core.memory.normalizer import Normalizer

    adapter = WorkBuddySourceAdapter(workbuddy_ctx)
    raw = adapter.load_session(WORKBUDDY_SESSION_ID)
    normalized = Normalizer().normalize(adapter.parse_events(raw))

    categories = [e.category for e in normalized]
    # `mvn -q test` 归 test、`mvn -q package` 归 build，都属验证类命令
    assert "test" in categories
    assert "build" in categories
    assert "file_edit" in categories
    assert any(e.is_failure for e in normalized), "失败必须被识别"

    test_run = next(e for e in normalized if e.category == "test")
    assert test_run.command == "mvn -q test"
