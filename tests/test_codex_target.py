"""Codex Target 与双向闭环测试（Phase 2）。

重点验证三件事：

1. Codex **不支持 @ 导入**，因此上下文必须内联进 AGENTS.md（与 Claude 的差异）；
2. **目标 Agent 不应影响 Memory 内容** —— 同一来源迁移到不同目标，
   Canonical Memory 必须逐字节一致（协议对称性）；
3. 目标 CLI 缺失/不可用时必须降级，且不启动外部进程。
"""

from __future__ import annotations

import re
import sys

from amt.adapters.codex import CodexContextRenderer, CodexTargetAdapter
from amt.adapters.codex.renderer import AGENTS_MD_BEGIN, AGENTS_MD_END
from amt.adapters.registry import default_registry
from amt.core.migration import MigrationOrchestrator
from amt.core.models import CanonicalMemory, MigrationOptions, MigrationRecord
from amt.core.storage import Storage

from conftest import SESSION_ID


def _orchestrator(ctx):
    return MigrationOrchestrator(ctx, storage=Storage(ctx.config), registry=default_registry(ctx))


# ----------------------------------------------------------------------
# 与 Claude 的差异：内联 vs @ 导入
# ----------------------------------------------------------------------
def test_codex_context_is_inlined_not_referenced(ctx, codex_source, project_root):
    raw = codex_source.load_session(SESSION_ID)
    events = codex_source.parse_events(raw)
    project = codex_source.collect_project_state(str(project_root))
    from amt.core.memory import MemoryEngine

    outcome = MemoryEngine(ctx).build(
        memory_id="m1",
        session=None,
        raw_events=events,
        project=project,
        runtime=codex_source.collect_runtime_state(str(project_root)),
        options=MigrationOptions(use_llm=False),
    )
    renderer = CodexContextRenderer()
    inline = renderer.render_inline_section(outcome.memory)

    # 必须内联真实内容，而不是只写一句「请读取 xxx.md」
    assert "失败" in inline or "未解决" in inline
    assert renderer.memory_relative_path() in inline
    assert AGENTS_MD_BEGIN in inline and AGENTS_MD_END in inline
    assert len(inline) < 4000, "内联段落必须受长度约束（AGENTS.md 每个会话都会加载）"


def test_inline_section_puts_failures_before_completed(ctx, codex_source, project_root):
    """失败方案的优先级高于已完成工作：重走失败的路比重复劳动更伤。"""
    from amt.core.memory import MemoryEngine

    raw = codex_source.load_session(SESSION_ID)
    project = codex_source.collect_project_state(str(project_root))
    outcome = MemoryEngine(ctx).build(
        memory_id="m1",
        session=None,
        raw_events=codex_source.parse_events(raw),
        project=project,
        runtime=codex_source.collect_runtime_state(str(project_root)),
        options=MigrationOptions(use_llm=False),
    )
    inline = CodexContextRenderer().render_inline_section(outcome.memory)
    if "已尝试且失败的方案" in inline and "## 已完成" in inline:
        assert inline.index("已尝试且失败的方案") < inline.index("## 已完成")


def test_agents_md_section_is_idempotent():
    renderer = CodexContextRenderer()
    section = f"{AGENTS_MD_BEGIN}\n内容\n{AGENTS_MD_END}"
    first = renderer.render_agents_md(None, section)
    second = renderer.render_agents_md(first, section)
    assert second.count(AGENTS_MD_BEGIN) == 1
    assert second.count(AGENTS_MD_END) == 1


def test_agents_md_preserves_existing_content():
    renderer = CodexContextRenderer()
    existing = "# 老项目说明\n\n已有约定\n"
    result = renderer.render_agents_md(existing, f"{AGENTS_MD_BEGIN}\nX\n{AGENTS_MD_END}")
    assert result.startswith("# 老项目说明")
    assert "已有约定" in result


# ----------------------------------------------------------------------
# 注入
# ----------------------------------------------------------------------
def test_codex_target_injection(ctx, project_root):
    """codex → codex 自迁移也应当可用（同一 Agent 也可当目标）。"""
    orchestrator = _orchestrator(ctx)
    outcome = orchestrator.migrate(
        source_agent="codex",
        target_agent="codex",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
    )
    assert outcome.record.status == "completed", outcome.record.errors
    agents_md = project_root / "AGENTS.md"
    assert agents_md.is_file()
    text = agents_md.read_text(encoding="utf-8")
    assert text.count(AGENTS_MD_BEGIN) == 1
    assert ".agent-transfer/memory.md" in text
    assert (project_root / ".agent-transfer" / "memory.md").is_file()


def test_codex_injection_is_idempotent(ctx, project_root):
    orchestrator = _orchestrator(ctx)
    for _ in range(3):
        orchestrator.migrate(
            source_agent="codex",
            target_agent="codex",
            session_id=SESSION_ID,
            options=MigrationOptions(use_llm=False, auto_launch=False),
        )
    text = (project_root / "AGENTS.md").read_text(encoding="utf-8")
    assert text.count(AGENTS_MD_BEGIN) == 1


# ----------------------------------------------------------------------
# 启动降级
# ----------------------------------------------------------------------
def test_launcher_degrades_without_cli(ctx, project_root):
    adapter = CodexTargetAdapter(ctx)
    assert adapter.cli_available is False
    context = adapter.prepare_context(
        CanonicalMemory.model_validate(
            {"version": "1.0", "metadata": {"memory_id": "m", "source_agent": "codex"}}
        ),
        codex_project_state(ctx, project_root),
        MigrationOptions(),
    )
    result = adapter.launch(context, auto_launch=True)
    assert result.success is False
    assert result.degraded is True
    assert "AGENTS.md" in result.message


def test_launcher_uses_configured_executable(ctx, project_root):
    """用解释器冒充 CLI，验证启动路径本身可用（不启动任何真实 Agent）。"""
    ctx.config.codex.executable = sys.executable
    adapter = CodexTargetAdapter(ctx)
    assert adapter.cli_available is True
    context = adapter.prepare_context(
        CanonicalMemory.model_validate(
            {"version": "1.0", "metadata": {"memory_id": "m", "source_agent": "claude"}}
        ),
        codex_project_state(ctx, project_root),
        MigrationOptions(),
    )
    result = adapter.launch(context, auto_launch=True)
    assert result.attempted is True
    assert result.pid is not None
    assert result.command and result.command[0]


def test_launch_skipped_when_disabled(ctx, project_root):
    adapter = CodexTargetAdapter(ctx)
    context = adapter.prepare_context(
        CanonicalMemory.model_validate(
            {"version": "1.0", "metadata": {"memory_id": "m", "source_agent": "claude"}}
        ),
        codex_project_state(ctx, project_root),
        MigrationOptions(),
    )
    result = adapter.launch(context, auto_launch=False)
    assert result.attempted is False


def codex_project_state(ctx, project_root):
    from amt.services.filesystem import ProjectStateCollector

    return ProjectStateCollector().collect(str(project_root))


# ----------------------------------------------------------------------
# 协议对称性
# ----------------------------------------------------------------------
def test_target_choice_does_not_change_memory(ctx, project_root, tmp_path):
    """同一来源迁移到不同目标，Canonical Memory 必须完全一致。

    这是「不做 N×N 转换」的可执行验证：目标差异只体现在 Adapter 的渲染与注入，
    绝不能渗进协议本体。

    实现细节：准备**两份内容相同的工程副本**。因为第一次迁移会把上下文文件写进
    工程目录，若两次共用同一个目录，第二次采集到的项目结构就已经变了——
    那样测到的是「注入改了项目」，而不是「目标污染了协议」。
    """
    import json
    import shutil

    # 同名工程、不同父目录：两次运行仅有「目标 Agent」这一个变量。
    root_a = tmp_path / "run-claude" / "proj"
    root_b = tmp_path / "run-codex" / "proj"
    shutil.copytree(project_root, root_a)
    shutil.copytree(project_root, root_b)

    orchestrator = _orchestrator(ctx)
    to_claude = orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        project_root=str(root_a),
    )
    to_codex = orchestrator.migrate(
        source_agent="codex",
        target_agent="codex",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        project_root=str(root_b),
    )
    assert to_claude.memory is not None and to_codex.memory is not None

    def normalized(memory, run_root):
        payload = memory.model_dump(mode="json")
        # memory_id 天然不同；运行目录是本次测试的变量，不是协议的一部分
        payload.pop("metadata", None)
        payload.pop("stats", None)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # JSON 里反斜杠是转义的，因此原样与转义后的两种写法都要替换
        for token in (str(run_root), json.dumps(str(run_root))[1:-1]):
            text = text.replace(token, "<RUN>")
        return text

    assert normalized(to_claude.memory, root_a) == normalized(to_codex.memory, root_b), (
        "Memory 因目标 Agent 不同而改变——协议被目标污染了"
    )


def test_registry_exposes_symmetric_pairs(ctx):
    registry = default_registry(ctx)
    pairs = set(registry.migration_pairs())
    assert ("codex", "claude") in pairs
    assert ("claude", "codex") in pairs
    assert ("cursor", "codex") in pairs
    assert ("cursor", "claude") in pairs
    # Cursor 不能作为目标
    assert not any(target == "cursor" for _, target in pairs)
    assert ("workbuddy", "codex") in pairs
    # 组合数 = 来源数 × 目标数 - 自身对自身（4 × 2 - 2 = 6）
    assert len(pairs) == 4 * 2 - 2
