"""Cursor Source 测试（Phase 3）。

Cursor 的数据在 SQLite 里，且**气泡 key 是 UUID、与时间顺序无关**——
这是最容易踩的坑，因此单独立用例锁定。
"""

from __future__ import annotations

from amt.adapters.cursor import CursorDetector, CursorParser, CursorSourceAdapter
from amt.core.models import EventType, RawSession

from conftest import CURSOR_COMPOSER_ID, build_cursor_db


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def test_bubbles_are_ordered_by_time_not_by_key(cursor_ctx):
    """气泡 key 是 UUID，必须按 createdAt 排序才能还原对话顺序。"""
    source = CursorSourceAdapter(cursor_ctx)
    raw, parsed = source.load_result(CURSOR_COMPOSER_ID)
    contents = [e.content for e in parsed.events if e.type is EventType.USER_MESSAGE]
    assert contents[0].startswith("登录接口")
    assert contents[1].startswith("顺便看下日志")


def test_thinking_bubble_becomes_reasoning(cursor_ctx):
    source = CursorSourceAdapter(cursor_ctx)
    _, parsed = source.load_result(CURSOR_COMPOSER_ID)
    reasoning = [e for e in parsed.events if e.type is EventType.REASONING]
    assert reasoning
    assert "Session 刷新" in reasoning[0].content


def test_empty_text_bubble_does_not_produce_empty_message(cursor_ctx):
    source = CursorSourceAdapter(cursor_ctx)
    _, parsed = source.load_result(CURSOR_COMPOSER_ID)
    assert all((e.content or "").strip() for e in parsed.events if e.type is EventType.ASSISTANT_MESSAGE)


def test_suggested_diffs_become_file_edits(cursor_ctx):
    """编辑以 diff 形式存在；该分支在真实数据里没有样本，标记为未验证。"""
    source = CursorSourceAdapter(cursor_ctx)
    _, parsed = source.load_result(CURSOR_COMPOSER_ID)
    edits = [e for e in parsed.events if e.metadata.get("category_hint") == "file_edit"]
    assert edits
    event = edits[0]
    assert event.metadata.get("paths") == ["src/service/LoginService.java"]
    assert event.metadata.get("unverified") is True
    stats = event.metadata.get("patch_file_stats") or {}
    assert stats["src/service/LoginService.java"]["added"] == 2


def test_composer_metadata_is_captured(cursor_ctx):
    source = CursorSourceAdapter(cursor_ctx)
    _, parsed = source.load_result(CURSOR_COMPOSER_ID)
    assert parsed.metadata is not None
    assert parsed.metadata.model == "claude-sonnet-4-5"
    assert parsed.metadata.extra.get("composer_name") == "Login fix session"


# ----------------------------------------------------------------------
# 发现 / 探测
# ----------------------------------------------------------------------
def test_empty_draft_composers_are_filtered(cursor_ctx):
    """Cursor 会留下大量空窗口/草稿 composer，列出来只会干扰选择。"""
    source = CursorSourceAdapter(cursor_ctx)
    sessions = source.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == CURSOR_COMPOSER_ID


def test_cwd_is_honestly_unknown(cursor_ctx):
    """Cursor 不记录 cwd，必须如实为 None，而不是编一个路径出来。"""
    sessions = CursorSourceAdapter(cursor_ctx).list_sessions()
    assert sessions[0].cwd is None
    assert sessions[0].resumable is False


def test_detector_reports_source_only(cursor_ctx):
    """Cursor 没有可靠的注入入口，因此 Target 必须如实标注为不支持。"""
    installation = CursorDetector(cursor_ctx).installation()
    assert installation.source_supported is True
    assert installation.target_supported is False
    assert any("不提供 Target" in note for note in installation.notes)


def test_detection_reports_missing_database(tmp_path):
    from amt.config import AMTConfig, CursorConfig
    from amt.context import AppContext

    ctx = AppContext(
        config=AMTConfig(
            home_dir=tmp_path / "amt",
            cursor=CursorConfig(global_storage=tmp_path / "gone", workspace_storage=tmp_path / "gone2"),
        )
    )
    detection = CursorDetector(ctx).detect()
    assert detection.runtime_available is False
    assert "未找到" in detection.detail or "未检测到" in detection.detail


def test_single_broken_database_does_not_break_scan(cursor_ctx, tmp_path):
    """一个库损坏不应让整体扫描失败。"""
    (cursor_ctx.config.cursor.resolved_workspace_storage()).mkdir(parents=True, exist_ok=True)
    broken = cursor_ctx.config.cursor.resolved_workspace_storage() / "w1"
    broken.mkdir(exist_ok=True)
    (broken / "state.vscdb").write_bytes(b"this is not a sqlite database")
    sessions = CursorSourceAdapter(cursor_ctx).list_sessions()
    assert any(s.session_id == CURSOR_COMPOSER_ID for s in sessions)


def test_read_composers_returns_structured_data(tmp_path):
    db = build_cursor_db(tmp_path / "state.vscdb")
    from amt.adapters.cursor import read_composers

    composers = read_composers(db)
    real = [c for c in composers if c["composer_id"] == CURSOR_COMPOSER_ID]
    assert len(real) == 1
    assert len(real[0]["bubbles"]) == 5
    assert real[0]["archived"] is False
