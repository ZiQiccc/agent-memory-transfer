"""迁移编排与状态机测试（POC-10 ~ POC-13）。"""

from __future__ import annotations

import pytest

from amt.adapters.base import AdapterNotImplementedError
from amt.adapters.claude import ClaudeContextRenderer
from amt.adapters.registry import AgentRegistry, default_registry
from amt.core.migration import MigrationOrchestrator
from amt.core.migration.state_machine import InvalidTransition, MigrationStateMachine
from amt.core.models import MigrationOptions, MigrationRecord, MigrationState
from amt.core.storage import Storage

from conftest import OPENAI_KEY_SAMPLE, PASSWORD_SAMPLE, SESSION_ID


# ----------------------------------------------------------------------
# 状态机
# ----------------------------------------------------------------------
def test_state_machine_allows_forward_transitions():
    record = MigrationRecord(migration_id="mig_x", source_agent="codex", target_agent="claude")
    machine = MigrationStateMachine(record)
    machine.to(MigrationState.SOURCE_DETECTED)
    machine.to(MigrationState.SESSION_LOADED)
    assert record.state is MigrationState.SESSION_LOADED
    assert machine.history[-1] is MigrationState.SESSION_LOADED


def test_state_machine_rejects_backward_transition():
    record = MigrationRecord(migration_id="mig_x", source_agent="codex", target_agent="claude")
    machine = MigrationStateMachine(record)
    machine.to(MigrationState.MEMORY_EXTRACTED)
    with pytest.raises(InvalidTransition):
        machine.to(MigrationState.SOURCE_DETECTED)


def test_state_machine_exception_states_reachable_from_anywhere():
    record = MigrationRecord(migration_id="mig_x", source_agent="codex", target_agent="claude")
    machine = MigrationStateMachine(record)
    machine.to(MigrationState.MEMORY_VALIDATED)
    machine.fail("boom")
    assert record.state is MigrationState.ERROR
    machine.retry()
    assert record.state is MigrationState.RETRY
    # 进入异常分支后允许重新推进
    machine.to(MigrationState.TARGET_PREPARED)
    assert record.state is MigrationState.TARGET_PREPARED


def test_step_context_marks_failure_and_records_error():
    record = MigrationRecord(migration_id="mig_x", source_agent="codex", target_agent="claude")
    machine = MigrationStateMachine(record)
    with pytest.raises(RuntimeError):
        with machine.step("boom_step"):
            raise RuntimeError("炸了")
    step = record.step("boom_step")
    assert step is not None and step.status == "failed"
    assert record.state is MigrationState.ERROR
    assert any("炸了" in e for e in record.errors)


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------
def test_registry_reports_implemented_and_planned_agents(ctx):
    registry = default_registry(ctx)
    assert "codex" in registry.source_agents
    assert "claude" in registry.target_agents

    installations = {i.agent: i for i in registry.installations()}
    assert installations["codex"].source_supported is True
    assert installations["claude"].target_supported is True
    # 未实现的 Agent 必须如实列出，不能伪装成已支持
    assert installations["opencode"].source_supported is False
    assert installations["mimo"].target_supported is False
    # 已实现为 Source 但没有注入入口的 Agent，必须如实标注 Target 不支持
    assert installations["cursor"].source_supported is True
    assert installations["cursor"].target_supported is False
    assert installations["workbuddy"].source_supported is True
    assert installations["workbuddy"].target_supported is False


def test_registry_raises_clear_error_for_planned_agent(ctx):
    registry = default_registry(ctx)
    with pytest.raises(AdapterNotImplementedError) as excinfo:
        registry.source("opencode")
    assert "OpenCode" in str(excinfo.value)


def test_unknown_agent_is_rejected(ctx):
    registry: AgentRegistry = default_registry(ctx)
    with pytest.raises(AdapterNotImplementedError):
        registry.source("not-an-agent")


# ----------------------------------------------------------------------
# 端到端：dry-run
# ----------------------------------------------------------------------
def _orchestrator(ctx):
    storage = Storage(ctx.config)
    return MigrationOrchestrator(ctx, storage=storage, registry=default_registry(ctx)), storage


def test_dry_run_stops_before_injection(ctx, project_root):
    orchestrator, storage = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False),
        dry_run=True,
    )
    assert outcome.record.status == "ready"
    assert outcome.record.state is MigrationState.TARGET_PREPARED
    assert outcome.memory is not None
    assert outcome.target_context is not None
    assert outcome.injection is None and outcome.launch is None
    # dry-run 绝不写入项目目录
    assert not (project_root / ".agent-transfer").exists()
    assert not (project_root / "CLAUDE.md").exists()
    # 但 Canonical Memory 必须落盘
    assert storage.memory_path(outcome.record.memory_id).is_file()


def test_dry_run_records_all_steps(ctx):
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False),
        dry_run=True,
    )
    names = [s.name for s in outcome.record.steps]
    for expected in (
        "detect_source",
        "load_session",
        "collect_project_state",
        "parse_events",
        "build_memory",
        "redact_memory",
        "validate_memory",
        "prepare_target",
    ):
        assert expected in names
    assert all(s.status in ("success", "skipped") for s in outcome.record.steps)


# ----------------------------------------------------------------------
# 端到端：完整注入
# ----------------------------------------------------------------------
def test_full_migration_writes_context_files(ctx, project_root):
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    assert outcome.record.status == "completed", outcome.record.errors
    assert outcome.injection is not None and outcome.injection.success

    transfer = project_root / ".agent-transfer"
    for name in ("manifest.json", "memory.json", "memory.md", "source.json"):
        assert (transfer / name).is_file(), f"缺少 {name}"

    claude_md = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@.agent-transfer/memory.md" in claude_md
    assert claude_md.count("agent-memory-transfer:begin") == 1
    assert "不要重复" in (transfer / "memory.md").read_text(encoding="utf-8")


def test_verify_step_reports_no_missing_artifacts(ctx, project_root):
    """verify 步骤不得把相对路径的计划产物误报为缺失。"""
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    step = outcome.record.step("verify")
    assert step is not None
    assert "✗" not in step.detail, step.detail
    assert "缺失" not in step.detail, step.detail


def test_injection_is_idempotent(ctx, project_root):
    orchestrator, _ = _orchestrator(ctx)
    for _ in range(3):
        orchestrator.migrate(
            source_agent="codex",
            target_agent="claude",
            session_id=SESSION_ID,
            options=MigrationOptions(use_llm=False, auto_launch=False),
        )
    claude_md = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.count("agent-memory-transfer:begin") == 1
    assert claude_md.count("@.agent-transfer/memory.md") == 1


def test_injected_memory_has_secrets_redacted(ctx, project_root):
    """注入产物中不得出现原始凭据。"""
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    assert outcome.record.secret_findings, "应检测到 fixture 中植入的凭据"

    blob = "\n".join(
        (project_root / ".agent-transfer" / name).read_text(encoding="utf-8")
        for name in ("memory.json", "memory.md")
    )
    assert OPENAI_KEY_SAMPLE not in blob
    assert PASSWORD_SAMPLE not in blob
    assert "Bearer eyJ" not in blob


def test_raw_session_snapshot_is_redacted(ctx, project_root):
    orchestrator, storage = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    session_dir = storage.config.sessions_dir / "codex" / SESSION_ID
    raw = (session_dir / "raw.json").read_text(encoding="utf-8")
    assert OPENAI_KEY_SAMPLE not in raw
    assert PASSWORD_SAMPLE not in raw
    assert outcome.memory is not None
    assert outcome.memory.conversation.raw_session_ref


def test_no_redact_option_keeps_text_but_reports_it(ctx, project_root):
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False, redact_secrets=False),
    )
    assert outcome.record.secret_findings == []
    step = outcome.record.step("redact_memory")
    assert step is not None and step.status == "skipped"


def test_cancelled_preview_keeps_memory_but_skips_injection(ctx, project_root):
    orchestrator, storage = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        on_preview=lambda memory: False,
    )
    assert outcome.cancelled is True
    assert outcome.record.status == "ready"
    assert outcome.injection is None
    assert not (project_root / ".agent-transfer").exists()
    assert storage.memory_path(outcome.record.memory_id).is_file(), "取消也要保留 Canonical Memory"


def test_launch_degrades_when_cli_missing(ctx, project_root):
    """本机没有 claude CLI 时必须降级而不是报错，且 Memory 仍可用。"""
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=True),
    )
    assert outcome.launch is not None
    if not outcome.launch.success:
        assert outcome.launch.degraded is True
        assert outcome.record.status == "partial"
        # 关键：注入产物仍然完整存在
        assert (project_root / ".agent-transfer" / "memory.md").is_file()


def test_migration_record_persisted_and_listed(ctx, project_root):
    orchestrator, storage = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        dry_run=True,
    )
    records = storage.list_migrations()
    assert any(r["migration_id"] == outcome.record.migration_id for r in records)
    loaded = storage.load_migration(outcome.record.migration_id)
    assert loaded.source_agent == "codex"
    assert loaded.memory_id == outcome.record.memory_id


def test_project_root_override_is_honoured(ctx, project_root, tmp_path):
    other = tmp_path / "another-checkout"
    other.mkdir()
    orchestrator, _ = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        project_root=str(other),
    )
    assert outcome.record.status == "completed"
    assert (other / ".agent-transfer" / "memory.md").is_file()
    assert not (project_root / ".agent-transfer").exists()


def test_source_detection_failure_is_reported_not_raised(tmp_path, project_root):
    """来源不可用时必须优雅失败并留下记录。"""
    from amt.config import AMTConfig, CodexConfig, LLMConfig
    from amt.context import AppContext

    empty_home = tmp_path / "empty-codex"
    empty_home.mkdir()
    ctx = AppContext(config=AMTConfig(home_dir=tmp_path / "amt2", llm=LLMConfig(), codex=CodexConfig(home=empty_home)))
    orchestrator, storage = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=None,
        options=MigrationOptions(use_llm=False),
    )
    assert outcome.record.status == "failed"
    assert outcome.record.errors
    assert outcome.memory is None


# ----------------------------------------------------------------------
# Claude 渲染器
# ----------------------------------------------------------------------
def test_claude_md_section_appended_to_existing_file():
    renderer = ClaudeContextRenderer()
    existing = "# 老项目说明\n\n已有内容\n"
    result = renderer.render_claude_md(existing)
    assert result.startswith("# 老项目说明")
    assert "已有内容" in result
    assert result.count("@.agent-transfer/memory.md") == 1


def test_claude_md_creates_file_when_absent():
    result = ClaudeContextRenderer().render_claude_md(None)
    assert result.startswith("# 项目说明")
    assert "@.agent-transfer/memory.md" in result


def test_claude_md_section_replaced_not_duplicated():
    renderer = ClaudeContextRenderer()
    first = renderer.render_claude_md(None)
    second = renderer.render_claude_md(first)
    assert second.count("agent-memory-transfer:begin") == 1
    assert second.count("@.agent-transfer/memory.md") == 1


def test_custom_memory_dir_is_honoured():
    renderer = ClaudeContextRenderer(memory_dir=".task-memory", context_file="AGENTS.md")
    text = renderer.render_claude_md(None)
    assert "@.task-memory/memory.md" in text
    assert renderer.memory_relative_path() == ".task-memory/memory.md"

# ----------------------------------------------------------------------
# 配置加载：损坏必须显式告警，不能静默退回默认值
# ----------------------------------------------------------------------
def test_broken_config_is_reported_not_silently_ignored(tmp_path):
    """真实教训：配置里一个 YAML 语法错误会让 load_config 静默退回默认值，
    表现为「用户设了 projects_dir 却不生效且毫无提示」，命令会去扫默认目录。"""
    from amt.config import load_config

    home = tmp_path / "amt"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text(
        "workbuddy: [unclosed\n", encoding="utf-8"
    )
    cfg = load_config(home / "config" / "config.yaml")
    assert cfg.config_error, "损坏的配置必须产生 config_error"
    assert "解析失败" in cfg.config_error


def test_misspelled_config_key_is_reported(tmp_path):
    """Pydantic 默认忽略未知字段 —— 拼错的配置项会被无声丢弃，必须报出来。"""
    from amt.config import load_config

    home = tmp_path / "amt-typo"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text(
        "workbuddy:\n  project_dir: D:/wb\n", encoding="utf-8"
    )
    cfg = load_config(home / "config" / "config.yaml")
    assert cfg.config_error, "拼错的配置项必须产生 config_error"
    assert "workbuddy.project_dir" in cfg.config_error


def test_valid_config_is_loaded_without_error(tmp_path):
    from amt.config import load_config

    home = tmp_path / "amt2"
    (home / "config").mkdir(parents=True)
    target = tmp_path / "wb"
    (home / "config" / "config.yaml").write_text(
        f"workbuddy:\n  projects_dir: {target.as_posix()}\n", encoding="utf-8"
    )
    cfg = load_config(home / "config" / "config.yaml")
    assert cfg.config_error is None
    assert cfg.workbuddy.resolved_projects_dir() == target


def test_missing_config_is_not_an_error(tmp_path):
    from amt.config import load_config

    cfg = load_config(tmp_path / "nope" / "config.yaml")
    assert cfg.config_error is None
