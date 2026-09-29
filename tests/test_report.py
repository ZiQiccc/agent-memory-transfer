"""GUI 报告测试（Phase 4）。"""

from __future__ import annotations

import json
import re

from amt.adapters.registry import default_registry
from amt.core.storage import Storage
from amt.gui import build_report_data, render_html, write_report


def test_report_data_includes_agents_and_pairs(ctx):
    storage = Storage(ctx.config)
    data = build_report_data(ctx.config, storage, default_registry(ctx))
    agents = {a["agent"] for a in data["agents"]}
    assert {"codex", "claude", "cursor", "workbuddy"} <= agents
    assert data["migration_pairs"]
    assert data["generated_at"]


def test_report_is_self_contained(ctx, tmp_path):
    """报告必须可离线打开：不引任何外部资源（CSS/JS/字体）。"""
    storage = Storage(ctx.config)
    path = write_report(ctx.config, storage, default_registry(ctx), out_path=tmp_path / "r.html")
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    # 数据里可能含用户内容中的 URL（正常），关键是**资源引用**不得指向外部
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html), "不得引用外部资源"
    assert "<script id=\"amt-data\"" in html
    assert "Agent Memory Transfer" in html


def test_report_embeds_valid_json(ctx, tmp_path):
    storage = Storage(ctx.config)
    path = write_report(ctx.config, storage, default_registry(ctx), out_path=tmp_path / "r.html")
    html = path.read_text(encoding="utf-8")
    match = re.search(r'<script id="amt-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "未找到内嵌数据块"
    payload = json.loads(match.group(1).replace("<\\/", "</"))
    for key in ("agents", "sessions", "memories", "migrations", "conversations", "migration_pairs"):
        assert key in payload


def test_report_renders_memory_and_conversation_after_migration(ctx, codex_source, project_root):
    """迁移后报告应包含记忆与（可脱敏的）会话快照。"""
    from amt.core.migration import MigrationOrchestrator
    from amt.core.models import MigrationOptions

    orchestrator = MigrationOrchestrator(
        ctx, storage=Storage(ctx.config), registry=default_registry(ctx)
    )
    orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id="01a08943-75e9-7953-bcc1-141f7ad3cc3d",
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    storage = Storage(ctx.config)
    data = build_report_data(ctx.config, storage, default_registry(ctx), include_live_sessions=False)

    assert data["memories"], "应至少有一份记忆"
    memory = data["memories"][0]
    assert memory["task"]["title"]
    assert "attempts" in memory

    assert data["conversations"], "应至少有一份会话快照"
    conversation = data["conversations"][0]
    assert conversation["agent"] == "codex"
    assert conversation["events"], "会话快照应能还原出事件"

    assert data["migrations"]
    assert data["migrations"][0]["steps"]


def test_conversation_snapshot_contains_no_raw_secrets(ctx, codex_source, project_root):
    """报告读的是已脱敏快照，因此报告里也不能出现原始凭据。"""
    from amt.core.migration import MigrationOrchestrator
    from amt.core.models import MigrationOptions

    orchestrator = MigrationOrchestrator(
        ctx, storage=Storage(ctx.config), registry=default_registry(ctx)
    )
    orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id="01a08943-75e9-7953-bcc1-141f7ad3cc3d",
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    data = build_report_data(ctx.config, Storage(ctx.config), None, include_live_sessions=False)
    blob = json.dumps(data["conversations"], ensure_ascii=False)
    assert "sk-proj-FAKE" not in blob
    assert "FakePassword987654" not in blob


def test_render_html_escapes_script_terminator():
    """数据里若含 </script> 必须转义，否则会截断页面。"""
    html = render_html({"generated_at": "now", "closing": "</script>", "agents": [],
                        "sessions": [], "memories": [], "migrations": [],
                        "conversations": [], "migration_pairs": [], "notes": [], "home_dir": ""})
    payload_start = html.index('<script id="amt-data"')
    payload_end = html.index("</script>", payload_start)
    assert "</script>" not in html[payload_start:payload_end]
