"""Memory Engine 入口：Normalize → Compress → Extract（LLM 优先，确定性回退）。

分工原则（技术架构 §26）::

    LLM  ：理解 / 总结 / 提取 / 归纳
    程序 ：读取 / 写入 / 执行 / 启动 / 校验

因此本模块的构建策略是 **「确定性基线 + LLM 语义增强」**：

1. 先用 :class:`HeuristicReconstructor` 生成一份**字段完整、可离线运行**的 Memory；
2. 若 LLM 可用，用它的归纳结果**覆盖语义字段**（目标、决策、失败方案、下一步）；
3. **确定性事实永不被 LLM 覆盖**——项目状态、Git 状态、运行时、测试结果
   一律来自程序读取的真实数据。

这样任何 LLM 故障都不会让整条链路失效，也不会污染事实。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from amt.context import AppContext
from amt.core.memory.compressor import CompressedBundle, Compressor
from amt.core.memory.heuristic import HeuristicReconstructor
from amt.core.memory.normalizer import Normalizer
from amt.core.memory.prompts import SYSTEM_PROMPT, build_user_prompt
from amt.core.models import (
    Action,
    Attempt,
    CanonicalMemory,
    ConversationContext,
    Decision,
    Issue,
    ModifiedFile,
    Risk,
    RuntimeContext,
    SessionMetadata,
    TaskContext,
    TestResult,
)
from amt.core.memory.normalizer import NormalizedEvent
from amt.providers.llm import LLMUnavailable, build_provider
from amt.services.filesystem import ProjectState


class BuildOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memory: CanonicalMemory
    warnings: list[str] = Field(default_factory=list)
    bundle: CompressedBundle
    normalized_events: list[NormalizedEvent] = Field(default_factory=list)
    llm_used: bool = False
    llm_meta: dict = Field(default_factory=dict)
    """LLM 调用元信息（模型 / 策略 / 用量 / 耗时）——供 amt compare 与报告使用。"""


class MemoryEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.normalizer = Normalizer()
        self.compressor = Compressor()
        self.reconstructor = HeuristicReconstructor()

    # ------------------------------------------------------------------
    def build(
        self,
        *,
        memory_id: str,
        session: SessionMetadata | None,
        raw_events,
        project: ProjectState,
        runtime: RuntimeContext,
        options,
        secret_findings: int = 0,
    ) -> BuildOutcome:
        warnings: list[str] = []

        normalized = self.normalizer.normalize(list(raw_events))
        if not normalized:
            warnings.append("未从会话中解析出任何事件，Memory 将只有项目与 Git 信息")

        budget = options.max_memory_tokens * 3 if options.compress_memory else 200_000
        bundle = self.compressor.compress(normalized, max_tokens=budget)

        memory = self.reconstructor.reconstruct(
            memory_id=memory_id,
            session=session,
            events=normalized,
            bundle=bundle,
            project=project,
            runtime=runtime,
            options=options,
            secret_findings=secret_findings,
        )
        memory.conversation.raw_session_ref = None  # 由 Orchestrator 回填

        llm_used = False
        llm_meta: dict = {}
        use_llm = options.use_llm if options.use_llm is not None else self.ctx.config.llm.enabled
        if use_llm:
            provider = build_provider(self.ctx.config.llm)
            available, reason = provider.available()
            if not available:
                warnings.append(
                    f"LLM 未使用：{reason}；已使用确定性重建（confidence={memory.task.confidence}）"
                )
                llm_meta = {"available": False, "reason": reason}
            else:
                try:
                    result = provider.generate_structured(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(
                            digest=bundle.digest,
                            project=project,
                            source_agent=(session.agent if session else "unknown"),
                            session_id=session.session_id if session else None,
                        ),
                    )
                    warnings.extend(self._merge_llm(memory, result.data))
                    memory.task.reconstructed_by = f"llm:{result.model}"
                    memory.task.confidence = "high"
                    llm_used = True
                    llm_meta = {"available": True, **result.as_dict()}
                    if result.attempts:
                        warnings.append(
                            "结构化输出降级记录：" + "；".join(result.attempts)
                        )
                except LLMUnavailable as exc:
                    warnings.append(f"LLM 调用失败，已回退确定性重建：{exc}")
                    llm_meta = {"available": False, "reason": str(exc)}
                except Exception as exc:  # 防御：任何异常都不能让整条链路失败
                    warnings.append(f"LLM 处理异常，已回退确定性重建：{exc}")
                    llm_meta = {"available": False, "reason": str(exc)}

        memory.stats["llm_used"] = llm_used
        if llm_meta:
            memory.stats["llm"] = llm_meta
        return BuildOutcome(
            memory=memory,
            warnings=warnings,
            bundle=bundle,
            normalized_events=normalized,
            llm_used=llm_used,
            llm_meta=llm_meta,
        )

    # ------------------------------------------------------------------
    # LLM 结果合并
    # ------------------------------------------------------------------

    def _merge_llm(self, memory: CanonicalMemory, data: dict) -> list[str]:
        """用 LLM 的语义归纳覆盖 Memory 的语义字段；逐字段容错。"""
        warnings: list[str] = []
        if not isinstance(data, dict):
            return ["LLM 返回结构不是对象，已忽略"]

        task = data.get("task")
        if isinstance(task, dict):
            merged = _validate(TaskContext, {**memory.task.model_dump(), **{
                k: v for k, v in task.items() if v not in (None, "", [])
            }})
            if merged is None:
                warnings.append("LLM 的 task 字段不合法，已保留确定性结果")
            else:
                merged.confidence = "high"
                merged.reconstructed_by = f"llm:{self.ctx.config.llm.model}"
                memory.task = merged

        conversation = data.get("conversation")
        if isinstance(conversation, dict):
            merged = _validate(
                ConversationContext,
                {**memory.conversation.model_dump(), **{
                    k: v for k, v in conversation.items() if v not in (None, "", [])
                }},
            )
            if merged is None:
                warnings.append("LLM 的 conversation 字段不合法，已保留确定性结果")
            else:
                merged.raw_session_ref = memory.conversation.raw_session_ref
                memory.conversation = merged

        items = _validated_list(data.get("decisions"), Decision)
        if items:
            memory.decisions = items

        items = _validated_list(data.get("unresolved"), Issue)
        if items:
            memory.unresolved = items

        items = _validated_list(data.get("next_actions"), Action)
        if items:
            memory.next_actions = _normalize_actions(items)

        items = _validated_list(data.get("risks"), Risk)
        if items:
            memory.risks = items

        # attempts：以 LLM 为主，但确定性检测到的失败必须保留
        llm_attempts = _validated_list(data.get("attempts"), Attempt)
        if llm_attempts:
            memory.attempts = _merge_attempts(llm_attempts, memory.attempts)

        implementation = data.get("implementation")
        if isinstance(implementation, dict):
            completed = implementation.get("completed")
            if isinstance(completed, list):
                cleaned = [str(x).strip() for x in completed if str(x).strip()]
                if cleaned:
                    memory.implementation.completed = cleaned
            files = _validated_list(implementation.get("modified_files"), ModifiedFile)
            if files:
                # 保留确定性解析出的真实改动量信息
                deterministic = {f.path: f for f in memory.implementation.modified_files}
                for file in files:
                    known = deterministic.get(file.path)
                    if known and known.summary and not file.summary:
                        file.summary = known.summary
                memory.implementation.modified_files = files

        validation = data.get("validation")
        if isinstance(validation, dict):
            tests = _validated_list(validation.get("tests"), TestResult)
            # 测试结果属于**事实**（技术架构 §「事实与语义分离」）：程序解析到的
            # 一律保留，LLM 只能补充「程序没能识别的验证命令」这一空档。
            # 补充进来的必须打上 source="llm"：渲染时会标明「未经程序校验」，
            # 且不参与 amt compare 的事实不变量比对 —— 否则「事实字段必须一致」
            # 这条自检会与「允许 LLM 补测试」的实现自相矛盾。
            if tests and not memory.validation.tests:
                for item in tests:
                    item.source = "llm"
                memory.validation.tests = tests
        return warnings


def redact_memory(memory: CanonicalMemory, scanner) -> tuple[CanonicalMemory, list]:
    """对 Memory 本体做脱敏。

    为什么必须单独做一次：原始会话的脱敏只覆盖落盘的 session 快照，
    而 ``memory.json`` / ``memory.md`` 会被**注入到目标项目目录**并交给目标 Agent。
    若不做这一步，从会话里带出的凭据会随注入产物泄露到项目工作区。

    做法：序列化 → 递归脱敏 → 回填为同类型对象；仅在真的命中时才重建，
    重建失败时保留原记忆（宁可留 warning 也不能丢数据）。
    """
    payload = memory.model_dump(mode="json")
    redacted, findings = scanner.redact_structure(payload, source="memory")
    if not findings:
        return memory, []
    try:
        return CanonicalMemory.model_validate(redacted), findings
    except Exception:
        return memory, []


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _validate(model, payload):
    try:
        return model.model_validate(payload)
    except Exception:
        return None


def _validated_list(items, model) -> list:
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        validated = _validate(model, item)
        if validated is not None:
            out.append(validated)
    return out


def _normalize_actions(actions: list[Action]) -> list[Action]:
    for index, action in enumerate(actions, start=1):
        if action.priority <= 0:
            action.priority = index
    return sorted(actions, key=lambda a: a.priority)


def _merge_attempts(llm_attempts: list[Attempt], deterministic: list[Attempt]) -> list[Attempt]:
    """合并失败知识，避免 LLM 漏掉程序确定性检测到的失败。"""
    merged = list(llm_attempts)
    existing = {a.action.strip().lower()[:40] for a in merged}
    for attempt in deterministic:
        if attempt.success:
            continue
        key = attempt.action.strip().lower()[:40]
        if key in existing:
            continue
        merged.append(attempt)
    return merged
