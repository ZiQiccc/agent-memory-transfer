"""Canonical Memory 测试：重建、校验、渲染（POC-06 ~ POC-09）。"""

from __future__ import annotations

from amt.core.memory import (
    MemoryEngine,
    MemoryValidator,
    render_initial_prompt,
    render_markdown,
)
from amt.core.models import CanonicalMemory, MigrationOptions, SessionMetadata
from amt.core.storage import Storage


def _build(ctx, codex_source, project_root, **option_overrides):
    raw = codex_source.load_session("01a08943-75e9-7953-bcc1-141f7ad3cc3d")
    events = codex_source.parse_events(raw)
    project = codex_source.collect_project_state(str(project_root))
    runtime = codex_source.collect_runtime_state(str(project_root))
    option_kwargs = {"use_llm": False}
    option_kwargs.update(option_overrides)
    options = MigrationOptions(**option_kwargs)
    engine = MemoryEngine(ctx)
    outcome = engine.build(
        memory_id="mem_test_001",
        session=SessionMetadata(agent="codex", session_id=raw.session_id, cwd=raw.cwd),
        raw_events=events,
        project=project,
        runtime=runtime,
        options=options,
    )
    return outcome, project


# ----------------------------------------------------------------------
# 重建
# ----------------------------------------------------------------------
def test_memory_has_required_structure(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    assert isinstance(memory, CanonicalMemory)
    assert memory.metadata.source_agent == "codex"
    assert memory.version == "1.0"
    assert memory.task.title
    assert memory.task.goal and memory.task.goal != "unknown"


def test_goal_excludes_pasted_json_payload(ctx, codex_source, project_root):
    """目标不能把用户粘贴的 JSON 载荷整段照抄进来（可读性要求）。"""
    outcome, _ = _build(ctx, codex_source, project_root)
    goal = outcome.memory.task.goal
    assert "failed" not in goal
    assert '"code"' not in goal
    assert "保养日期不能为空" in goal


def test_modified_file_captured_from_shell_heredoc_patch(ctx, codex_source, project_root):
    """经 shell heredoc 调用的 apply_patch 也必须进入 modified_files。"""
    outcome, _ = _build(ctx, codex_source, project_root)
    files = [f.path for f in outcome.memory.implementation.modified_files]
    assert any("EquipmentMaintenance.java" in f for f in files)
    summary = next(f.summary for f in outcome.memory.implementation.modified_files)
    assert "+" in summary and "-" in summary


def test_failed_attempts_exclude_normal_search_exit_code(ctx, codex_source, project_root):
    """未匹配到结果的检索命令不得进入失败尝试。"""
    outcome, _ = _build(ctx, codex_source, project_root)
    blob = " ".join(a.action for a in outcome.memory.failed_attempts())
    assert "NonexistentSymbol" not in blob
    # 真实的测试失败必须在
    assert any("mvn" in a.action and "test" in a.action for a in outcome.memory.failed_attempts())


def test_failed_attempt_carries_error_and_lesson(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    failed = outcome.memory.failed_attempts()
    assert failed
    assert any(a.error for a in failed), "失败尝试必须写明报错"
    assert all(a.success is False for a in failed)


def test_validation_records_test_results(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    tests = {t.command: t.status for t in outcome.memory.validation.tests}
    assert tests.get("mvn -q test") == "failed"


def test_unresolved_and_next_actions_are_populated(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    assert memory.unresolved
    assert memory.next_actions
    # 下一步必须带动作意图
    assert any("排查" in a.action or "修复" in a.action for a in memory.next_actions)


def test_completed_work_is_not_repeated_as_todo(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    completed = " ".join(memory.implementation.completed)
    for action in memory.open_actions():
        assert action.action[:12] not in completed


def test_heuristic_marks_its_own_confidence(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    assert outcome.memory.task.reconstructed_by == "heuristic"
    assert outcome.memory.task.confidence in ("unknown", "low", "medium", "high")
    assert outcome.llm_used is False


def test_warns_when_llm_enabled_but_unavailable(ctx, codex_source, project_root):
    """请求使用 LLM 但配置不可用时，必须显式告知已回退（不能让用户误以为用了 LLM）。"""
    outcome, _ = _build(ctx, codex_source, project_root, use_llm=True)
    assert outcome.llm_used is False
    assert any("LLM" in w for w in outcome.warnings)
    assert outcome.memory.task.reconstructed_by == "heuristic"


def test_explicitly_disabled_llm_does_not_warn(ctx, codex_source, project_root):
    """用户显式 --no-llm 时不产生噪声警告。"""
    outcome, _ = _build(ctx, codex_source, project_root, use_llm=False)
    assert not any("LLM" in w for w in outcome.warnings)


def test_options_control_sections(ctx, codex_source, project_root):
    outcome, _ = _build(
        ctx, codex_source, project_root, include_project=False, include_runtime=False, include_git=False
    )
    memory = outcome.memory
    assert memory.project.path == ""
    assert memory.runtime.working_directory == ""
    assert memory.git.branch is None


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def test_validator_flags_missing_file_reference(ctx, codex_source, project_root):
    outcome, project = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    # 人为制造一个项目中不存在的文件引用
    memory.implementation.modified_files.append(
        memory.implementation.modified_files[0].model_copy(update={"path": "src/不存在的文件.java"})
    )
    report = MemoryValidator().validate(memory, project)
    assert any("未找到" in w for w in report.warnings)


def test_validator_requires_title_and_goal(ctx, codex_source, project_root):
    outcome, project = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    memory.task.title = ""
    memory.task.goal = ""
    report = MemoryValidator().validate(memory, project)
    assert not report.ok
    assert len(report.errors) >= 2


def test_validator_warns_when_project_path_missing(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    from amt.services.filesystem import ProjectState

    memory = outcome.memory
    memory.project.path = "Z:/definitely-missing"
    report = MemoryValidator().validate(memory, ProjectState(cwd="Z:/definitely-missing", exists=False))
    assert any("项目路径不存在" in w for w in report.warnings)


def test_validator_prefers_memory_project_path_over_cwd(ctx, codex_source, project_root):
    """校验应以 Memory 记录的项目路径为准，而不是注入目标目录。"""
    outcome, _ = _build(ctx, codex_source, project_root)
    from amt.services.filesystem import ProjectState

    memory = outcome.memory
    memory.project.path = str(project_root)
    report = MemoryValidator().validate(memory, ProjectState(cwd="Z:/definitely-missing", exists=False))
    assert not any("项目路径不存在" in w for w in report.warnings)


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------
def test_markdown_section_numbers_are_continuous(ctx, codex_source, project_root):
    """章节编号必须连续。

    部分章节在无内容时会被整节省略（例如无风险时不输出「风险提示」、
    无运行时信息时不输出「运行环境」），硬编码编号会造成断号（7 → 9）。
    """
    import re

    from amt.core.models import RuntimeContext

    outcome, _ = _build(ctx, codex_source, project_root)
    memory = outcome.memory
    memory.risks = []                      # 迫使「风险提示」整节省略
    memory.runtime = RuntimeContext()      # 迫使「运行环境」整节省略

    text = render_markdown(memory)
    numbers = [int(m) for m in re.findall(r"^## (\d+)\.", text, re.MULTILINE)]
    assert numbers, "应至少渲染出一个章节"
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))), f"章节编号不连续：{numbers}"
    assert "风险提示" not in text
    assert "运行环境" not in text


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------
def test_markdown_render_contains_core_sections(ctx, codex_source, project_root):
    """核心章节必须存在。

    不硬编码章节号——编号由渲染器动态生成（见编号连续性用例）。
    """
    import re

    outcome, _ = _build(ctx, codex_source, project_root)
    text = render_markdown(outcome.memory)
    for title in (
        "任务",
        "已完成的工作",
        "已尝试但失败的方案 ⚠",
        "当前未解决的问题",
        "下一步行动",
        "冲突处理原则",
    ):
        assert re.search(rf"^## \d+\. {re.escape(title)}$", text, re.MULTILINE), f"缺少章节：{title}"
    assert "真实文件系统 > Git 状态" in text
    # 不可把 Markdown 表格撑破
    assert "| # | 动作 |" in text


def test_initial_prompt_states_non_repetition_rule(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    prompt = render_initial_prompt(outcome.memory, memory_ref="@.agent-transfer/memory.md")
    assert "@.agent-transfer/memory.md" in prompt
    assert "不要重新分析" in prompt
    assert "不要重复已经失败的方案" in prompt


def test_memory_roundtrip_through_storage(ctx, codex_source, project_root):
    outcome, _ = _build(ctx, codex_source, project_root)
    storage = Storage(ctx.config)
    storage.save_memory(outcome.memory, render_markdown(outcome.memory))
    loaded = storage.load_memory("mem_test_001")
    assert loaded.model_dump() == outcome.memory.model_dump()
    assert storage.load_memory_markdown("mem_test_001").startswith("# 任务记忆")


def test_memory_id_sequence_increments(ctx):
    storage = Storage(ctx.config)
    first = storage.new_memory_id()
    storage.save_memory(
        CanonicalMemory.model_validate(
            {
                "version": "1.0",
                "metadata": {"memory_id": first, "source_agent": "codex"},
            }
        )
    )
    assert storage.new_memory_id() != first
