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
    render_markdown,
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

    def fake_post(strategy, system, user, schema_name, max_tokens):
        calls.append(strategy)
        if strategy == "json_schema":
            from amt.providers.llm import _RetryableStrategy

            raise _RetryableStrategy("HTTP 400：不支持 response_format")
        return original(strategy, system, user, schema_name, max_tokens)

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
    # 只比对「程序解析」的验证结果：LLM 补充的线索属于语义，见下一个用例
    assert [t for t in llm.memory.validation.tests if t.source == "program"] == [
        t for t in heuristic.memory.validation.tests if t.source == "program"
    ]


# ----------------------------------------------------------------------
# 中转站实测出的两类适配（思维链模型 + 输出截断）
# ----------------------------------------------------------------------
def test_truncated_output_escalates_token_budget(mock_llm, monkeypatch):
    """``finish_reason=length`` 时应自动加预算重试，而不是报「JSON 解析失败」。

    真实场景：中转站转发的思维链模型会把 4096 预算先花在推理上，
    正文才写到一半就被截断 —— 报一个「解析失败」会让人以为是格式问题。
    """
    config = LLMConfig(
        enabled=True, base_url=mock_llm, model="mock", timeout=15, max_output_tokens=1024
    )
    provider = OpenAICompatibleProvider(config)

    budgets: list[int] = []

    def fake_post(strategy, system, user, schema_name, max_tokens):
        budgets.append(max_tokens)
        if len(budgets) == 1:
            # 第一次：被截断的 JSON（未闭合）
            return {"choices": [{"finish_reason": "length", "message": {"content": '{"task": {'}}]}, "length"
        return (
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"next_actions": []}'}}]},
            "stop",
        )

    monkeypatch.setattr(provider, "_post", fake_post)
    result = provider.generate_structured(system="s", user="u")

    assert budgets[0] == 1024
    assert budgets[1] == 2048, "应把预算翻倍后重试"
    assert result.truncated is True
    assert result.max_tokens_used == 2048
    assert any("截断" in a for a in result.attempts)


def test_reasoning_content_is_used_when_content_is_empty(mock_llm, monkeypatch):
    """思维链模型若只把结果放在 ``reasoning_content``，不能被判成「什么都没返回」。"""
    provider = OpenAICompatibleProvider(
        LLMConfig(enabled=True, base_url=mock_llm, model="mock", timeout=15)
    )

    def fake_post(strategy, system, user, schema_name, max_tokens):
        return (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "",
                            "reasoning_content": '{"task": {"title": "来自思考块"}}',
                        },
                    }
                ]
            },
            "stop",
        )

    monkeypatch.setattr(provider, "_post", fake_post)
    result = provider.generate_structured(system="s", user="u")
    assert result.data["task"]["title"] == "来自思考块"
    assert result.content_source == "reasoning_content"
    assert result.reasoning_chars > 0


def test_empty_content_reports_finish_reason(mock_llm, monkeypatch):
    """真的拿不到内容时，错误信息必须带上 finish_reason，便于定位是不是被截断。"""
    provider = OpenAICompatibleProvider(
        LLMConfig(enabled=True, base_url=mock_llm, model="mock", timeout=15, max_output_tokens=16384)
    )

    def fake_post(strategy, system, user, schema_name, max_tokens):
        return (
            {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]},
            "content_filter",
        )

    monkeypatch.setattr(provider, "_post", fake_post)
    with pytest.raises(LLMUnavailable) as excinfo:
        provider.generate_structured(system="s", user="u")
    assert "finish_reason=content_filter" in str(excinfo.value)


# ----------------------------------------------------------------------
# LLM 补充的验证线索：有用，但必须与「程序校验的事实」区分开
# ----------------------------------------------------------------------
def test_llm_supplied_tests_are_tagged_and_keep_fact_invariant(
    ctx, codex_source, project_root, monkeypatch
):
    """程序没识别出验证命令时，允许 LLM 补充 —— 但必须打上来源标记，
    且不得破坏「事实字段不变量」，否则自检与实现会自相矛盾。"""
    from amt.core.memory import extractor as extractor_module
    from amt.providers.llm import LLMResult

    class StubProvider:
        name = "stub"

        def available(self):
            return True, "ok"

        def generate_structured(self, *, system, user, schema_name="canonical_memory"):
            return LLMResult(
                data={
                    "validation": {
                        "tests": [
                            {"command": "slidep-validate", "status": "passed", "output_summary": "ok"}
                        ]
                    }
                },
                provider=self.name,
                model="stub",
                strategy="json_schema",
            )

    monkeypatch.setattr(extractor_module, "build_provider", lambda _cfg: StubProvider())

    ctx.config.llm.enabled = True
    raw = codex_source.load_session("01a08943-75e9-7953-bcc1-141f7ad3cc3d")
    events = codex_source.parse_events(raw)
    project = codex_source.collect_project_state(str(project_root))
    runtime = codex_source.collect_runtime_state(str(project_root))
    engine = MemoryEngine(ctx)
    outcome = engine.build(
        memory_id="mem_t",
        session=None,
        raw_events=events,
        project=project,
        runtime=runtime,
        options=MigrationOptions(use_llm=True),
    )

    program_tests = [t for t in outcome.memory.validation.tests if t.source == "program"]
    llm_tests = [t for t in outcome.memory.validation.tests if t.source == "llm"]
    if program_tests:
        # 程序识别到了验证命令 → LLM 不得覆盖
        assert not llm_tests
    else:
        assert [t.command for t in llm_tests] == ["slidep-validate"]

    # 渲染必须标明来源，不能让目标 Agent 误以为是程序校验通过
    text = render_markdown(outcome.memory)
    assert "程序解析" in text or "LLM 归纳" in text

    # 事实不变量仍然成立：LLM 补充的线索不计入事实
    heuristic = engine.build(
        memory_id="mem_h",
        session=None,
        raw_events=events,
        project=project,
        runtime=runtime,
        options=MigrationOptions(use_llm=False),
    )
    comparison = MemoryComparer().compare(heuristic, outcome)
    mismatched = [d.field for d in comparison.fact_fields if d.delta != "一致"]
    assert not mismatched, mismatched
    fields = {d.field for d in comparison.semantic_fields}
    assert "validation.tests LLM 补充线索（条）" in fields
