"""LLM 通道与质量对比测试（Phase 1）。

不依赖任何外部服务：测试里起一个**真实的 HTTP 服务**（OpenAI 兼容的规则模拟器），
因此走的是完整的请求/响应链路 —— 而不是打桩。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from amt.config import LLMConfig
from amt.core.memory.compare import MemoryComparer, render_comparison_markdown
from amt.core.memory.extractor import MemoryEngine
from amt.core.memory.llm_schema import MemoryPatch, memory_patch_schema
from amt.core.memory.renderer import (
    PROMPT_GOAL_HEADER,
    render_initial_prompt,
    unwrap_injected_prompt,
)
from amt.core.models import MigrationOptions, SessionMetadata
from amt.providers.llm import LLMUnavailable, OpenAICompatibleProvider, build_provider

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from mock_llm_server import MockHandler  # noqa: E402


@pytest.fixture(scope="module")
def mock_llm():
    """在本机起一个 OpenAI 兼容的规则模拟器，返回 base_url。"""
    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/v1"
    server.shutdown()


# ----------------------------------------------------------------------
# 契约
# ----------------------------------------------------------------------
def test_schema_is_generated_from_pydantic_contract():
    """LLM 的输出契约只能有一处定义（Pydantic 模型），避免提示词与代码脱节。"""
    schema = memory_patch_schema()
    assert schema["title"] == MemoryPatch.__name__
    assert "properties" in schema
    for field in ("task", "attempts", "unresolved", "next_actions", "decisions"):
        assert field in schema["properties"]


def test_contract_excludes_fact_fields():
    """事实字段（Git / 运行时）不得出现在 LLM 契约里。"""
    text = json.dumps(memory_patch_schema(), ensure_ascii=False)
    assert "git" not in text
    assert "runtime" not in text


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------
def test_provider_unavailable_when_disabled():
    provider = build_provider(LLMConfig(enabled=False))
    ok, reason = provider.available()
    assert ok is False
    assert "未启用" in reason


def test_provider_reports_missing_base_url():
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, base_url="", model="m"))
    ok, reason = provider.available()
    assert ok is False and "base_url" in reason


def test_provider_uses_json_schema_strategy(mock_llm):
    config = LLMConfig(enabled=True, base_url=mock_llm, model="mock-extractor", timeout=15)
    provider = OpenAICompatibleProvider(config)
    result = provider.generate_structured(system="s", user="[01-01 00:00] USER_MESSAGE hello")
    assert result.strategy == "json_schema"
    assert result.data["task"]["title"] == "hello"
    assert result.total_tokens > 0
    assert result.duration_ms >= 0


def test_provider_falls_back_when_schema_unsupported(mock_llm, monkeypatch):
    """端点不支持 json_schema 时应降级而不是直接失败。"""
    config = LLMConfig(enabled=True, base_url=mock_llm, model="mock", timeout=15)
    provider = OpenAICompatibleProvider(config)

    calls: list[str] = []
    original = provider._post

    def fake_post(strategy, system, user, schema_name):
        calls.append(strategy)
        if strategy == "json_schema":
            from amt.providers.llm import _RetryableStrategy

            raise _RetryableStrategy("HTTP 400：不支持 response_format")
        return original(strategy, system, user, schema_name)

    monkeypatch.setattr(provider, "_post", fake_post)
    result = provider.generate_structured(system="s", user="[01-01 00:00] USER_MESSAGE hi")
    assert calls[:2] == ["json_schema", "json_object"]
    assert result.strategy == "json_object"


def test_provider_raises_when_endpoint_unreachable():
    provider = OpenAICompatibleProvider(
        LLMConfig(enabled=True, base_url="http://127.0.0.1:1/v1", model="m", timeout=3)
    )
    with pytest.raises(LLMUnavailable):
        provider.generate_structured(system="s", user="u")


def test_provider_tolerates_fenced_json():
    from amt.providers.llm import _loads_tolerant

    assert _loads_tolerant('```json\n{"a": 1}\n```')["a"] == 1
    assert _loads_tolerant('好的，结果如下：{"b": 2} 以上')["b"] == 2


# ----------------------------------------------------------------------
# 回环防护：我们注入的 Prompt 不能被当成用户需求
# ----------------------------------------------------------------------
def test_injected_prompt_is_unwrapped():
    from amt.core.models import Action, CanonicalMemory, Issue, Metadata, TaskContext

    memory = CanonicalMemory(
        metadata=Metadata(memory_id="m", source_agent="codex"),
        task=TaskContext(goal="修复登录接口 401", status="blocked"),
        unresolved=[Issue(description="401 未解决")],
        next_actions=[Action(action="检查 Session 刷新")],
    )
    prompt = render_initial_prompt(memory, memory_ref="@.agent-transfer/memory.md")
    assert PROMPT_GOAL_HEADER in prompt
    assert unwrap_injected_prompt(prompt) == "修复登录接口 401"


def test_unwrap_returns_none_for_normal_user_text():
    assert unwrap_injected_prompt("帮我修一下登录问题") is None
    assert unwrap_injected_prompt("") is None
    assert unwrap_injected_prompt(None) is None


def test_normalizer_strips_injected_prompt(claude_ctx):
    """端到端：Claude 会话里的首条消息是本工具的 Prompt 时，需求必须被还原。"""
    from amt.core.models import AgentEvent
    from amt.core.memory.normalizer import Normalizer
    from amt.core.models import EventType

    prompt = (
        "你正在继续一个已经进行中的开发任务。\n\n请先读取 @.agent-transfer/memory.md。\n\n"
        "## 任务目标\n\n修复登录接口 401\n\n## 执行要求\n\n1. 不要重做\n"
    )
    normalized = Normalizer().normalize(
        [AgentEvent(id="x", type=EventType.USER_MESSAGE, role="user", content=prompt)]
    )
    assert normalized[0].content == "修复登录接口 401"
    assert normalized[0].metadata.get("injected_prompt") is True


# ----------------------------------------------------------------------
# 端到端：LLM 增强 + 事实不变量
# ----------------------------------------------------------------------
def _build_both(ctx, source, project_root, base_url: str | None, model: str = "mock-extractor"):
    if base_url:
        ctx.config.llm.enabled = True
        ctx.config.llm.base_url = base_url
        ctx.config.llm.model = model
        ctx.config.llm.timeout = 30
    raw = source.load_session("01a08943-75e9-7953-bcc1-141f7ad3cc3d")
    events = source.parse_events(raw)
    project = source.collect_project_state(str(project_root))
    runtime = source.collect_runtime_state(str(project_root))
    session = SessionMetadata(agent=raw.agent, session_id=raw.session_id, cwd=raw.cwd)
    engine = MemoryEngine(ctx)
    heuristic = engine.build(
        memory_id="mem_h", session=session, raw_events=events, project=project,
        runtime=runtime, options=MigrationOptions(use_llm=False),
    )
    llm = engine.build(
        memory_id="mem_l", session=session, raw_events=events, project=project,
        runtime=runtime, options=MigrationOptions(use_llm=True),
    )
    from amt.core.memory.normalizer import Normalizer

    heuristic.memory.runtime.recent_commands = Normalizer.recent_commands(heuristic.normalized_events)
    llm.memory.runtime.recent_commands = Normalizer.recent_commands(llm.normalized_events)
    return heuristic, llm


def test_llm_path_enriches_semantics(ctx, codex_source, project_root, mock_llm):
    heuristic, llm = _build_both(ctx, codex_source, project_root, mock_llm)
    assert heuristic.llm_used is False
    assert llm.llm_used is True
    assert llm.memory.task.reconstructed_by.startswith("llm:")
    assert llm.memory.task.confidence == "high"
    assert llm.llm_meta.get("strategy") == "json_schema"
    assert llm.llm_meta.get("total_tokens", 0) > 0


def test_fact_fields_are_invariant_across_channels(ctx, codex_source, project_root, mock_llm):
    """核心原则的可执行验证：事实字段在两条件下必须完全一致。"""
    heuristic, llm = _build_both(ctx, codex_source, project_root, mock_llm)
    comparison = MemoryComparer().compare(heuristic, llm)
    mismatched = [d for d in comparison.fact_fields if d.delta != "一致"]
    assert not mismatched, [d.field for d in mismatched]
    assert comparison.facts_are_invariant is True
    assert len(comparison.fact_fields) >= 8


def test_comparison_surfaces_semantic_differences(ctx, codex_source, project_root, mock_llm):
    heuristic, llm = _build_both(ctx, codex_source, project_root, mock_llm)
    comparison = MemoryComparer().compare(heuristic, llm)
    assert comparison.llm_available is True
    assert comparison.provider_is_mock is True
    fields = {d.field for d in comparison.semantic_fields}
    assert {"task.title", "task.goal", "decisions（条）", "attempts 带 lesson（条）"} <= fields

    markdown = render_comparison_markdown(comparison)
    assert "事实字段不变量校验" in markdown
    assert "mock LLM" in markdown, "使用模拟器时必须显式提示结论适用范围"


def test_llm_failure_falls_back_to_heuristic(ctx, codex_source, project_root):
    """LLM 不可用时必须回退且不影响事实字段。"""
    ctx.config.llm.enabled = True
    ctx.config.llm.base_url = "http://127.0.0.1:1/v1"
    ctx.config.llm.timeout = 3
    heuristic, llm = _build_both(ctx, codex_source, project_root, None)
    assert llm.llm_used is False
    assert any("LLM" in w for w in llm.warnings)
    comparison = MemoryComparer().compare(heuristic, llm)
    assert comparison.llm_available is False
    assert comparison.facts_are_invariant is True


def test_llm_cannot_override_git_or_project(ctx, codex_source, project_root, mock_llm):
    """即使模拟器想改事实也没机会——契约里根本没有这些字段。"""
    heuristic, llm = _build_both(ctx, codex_source, project_root, mock_llm)
    assert llm.memory.git.branch == heuristic.memory.git.branch
    assert llm.memory.project.path == heuristic.memory.project.path
    assert llm.memory.validation.tests == heuristic.memory.validation.tests
