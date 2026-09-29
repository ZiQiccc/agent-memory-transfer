"""确定性任务状态重建（Heuristic Task State Reconstruction）。

**为什么需要它**：技术架构 §22 / §43 假设用 LLM 完成记忆提取，但 LLM 存在
不确定性，且在没有可用 LLM 凭据的机器上整条迁移链路会直接不可运行。
因此本模块提供一条**完全确定性、可离线运行**的重建通道：

    LLM 可用  → LLMExtractor 负责语义归纳
    LLM 不可用 → 本模块负责重建（并显式标注 confidence）

诚实性约束（实现plan §十一）：
- 不猜测不存在的信息；
- 推断出来的结论必须以「（推断）」标注，与直接观测到的信息区分开；
- 失败方案必须保留；
- 已完成的工作不得重复描述为待办。
"""

from __future__ import annotations

import re
from typing import Iterable

from amt.core.memory.compressor import CompressedBundle
from amt.core.memory.normalizer import Normalizer, event_output, first_error_line
from amt.core.models import (
    Action,
    Attempt,
    BuildResult,
    CanonicalMemory,
    ConversationContext,
    Decision,
    GitContext,
    ImplementationContext,
    Issue,
    Metadata,
    ModifiedFile,
    ProjectContext,
    Risk,
    RuntimeContext,
    SessionMetadata,
    TaskContext,
    TestResult,
    ValidationContext,
    EventType,
)
from amt.core.memory.normalizer import NormalizedEvent
from amt.services.filesystem import ProjectState
from amt.utils import collapse, first_line, truncate

_TAIL_SIZE = 15
_MAX_USER_REQUIREMENTS = 10
_MAX_ATTEMPTS = 15
_MAX_DECISIONS = 3
_MAX_DECISION_LEN = 140
"""启发式提取的决策句子长度上限。过长通常是多个分句粘连在一起，
读起来不像一条「已确定的方案」，反而增加噪声。"""
_MAX_ISSUES = 6
_MAX_ACTIONS = 8

_NUMBERED_ITEM_RE = re.compile(r"^\s*(?:\d+[.、)]|[-*•])\s+(.{6,140})$", re.MULTILINE)
_DECISION_RE = re.compile(
    r"(决定|采用|选择|确定为|方案是|建议使用|推荐使用|改为使用)", re.IGNORECASE
)

#: 待办的判据——必须**以祈使式动作开头**。
#: 只要求「句中出现动词」是不够的：实测里「钩子：sessionStart 会在会话开始时*运行*」
#: 这类说明性文字含有动词，却不是待办，会污染 next_actions。
_ACTION_LEAD_RE = re.compile(
    r"^\s*(?:\*\*|`|[-*•]\s*)*(?:请|先|需要|建议|可以)?\s*"
    r"(检查|确认|修复|排查|定位|验证|核对|补充|添加|新增|修改|实现|运行|执行|测试|重建|调整|删除|查看|读取|复现|回滚|升级|替换|比较|对比|梳理|补全|重试|收敛|统一"
    r"|check|fix|verify|add|update|remove|run|test|investigate|confirm|refactor|debug)",
    re.IGNORECASE,
)

#: 明确的「下一步」引导语，命中即认为是待办
_PENDING_FRAMING_RE = re.compile(r"^\s*(?:\*\*)?(下一步|接下来|后续|待办|TODO)", re.IGNORECASE)

#: 把整段用户消息清洗成可读的目标陈述
_FENCE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:https?|ftp)://[^)\s]*\)")
_URL_RE = re.compile(r"(?:https?|ftp)://\S+")
_TRAILING_PUNCT = "，,。.；;：:、！!？?… 　\t"


def _clean_user_text(text: str | None) -> str:
    """把用户消息里的代码块 / JSON 载荷 / 裸 URL 去掉，只留自然语言。

    用户消息经常是「一句话 + 一大段粘贴的 JSON/代码」。直接截取前 N 个字符
    会得到一串不可读的载荷，因此这里先剥离结构性内容，再取自然语言部分。
    """
    if not text:
        return ""
    cleaned = _FENCE_BLOCK_RE.sub(" ", text)

    # 剥离 JSON / 数组块（按括号深度跟踪，避免残留半截载荷）。
    # 注意：载荷经常是**行内起始**的（如「响应是 200 但 {"failed":true,...}」），
    # 因此不能只看行首，必须扫描行内第一个未闭合的 [ 或 {。
    kept: list[str] = []
    depth = 0
    for line in cleaned.splitlines():
        stripped = line.strip()
        if depth > 0:
            depth += stripped.count("[") + stripped.count("{")
            depth -= stripped.count("]") + stripped.count("}")
            depth = max(depth, 0)
            continue
        opener = _find_unbalanced_opener(stripped)
        if opener is None:
            kept.append(line)
            continue
        prefix = stripped[:opener].strip()
        if prefix:
            kept.append(prefix)
        rest = stripped[opener:]
        depth = rest.count("[") + rest.count("{") - rest.count("]") - rest.count("}")
        depth = max(depth, 0)

    text = "\n".join(kept)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    return collapse(text)


def _find_unbalanced_opener(line: str) -> int | None:
    """返回行内第一个「开启且未在本行闭合」的 ``[`` 或 ``{`` 的下标。"""
    if not line:
        return None
    for index, char in enumerate(line):
        if char not in "[{":
            continue
        rest = line[index:]
        if rest.count("[") + rest.count("{") > rest.count("]") + rest.count("}"):
            return index
    return None


def _first_sentence(text: str, limit: int = 60) -> str:
    """取第一句话并去掉句尾标点，用作标题。"""
    if not text:
        return ""
    for chunk in re.split(r"[。！？!?\n]", text):
        chunk = chunk.strip().strip(_TRAILING_PUNCT)
        if len(chunk) >= 2:
            return truncate(chunk, limit)
    return truncate(text.strip().strip(_TRAILING_PUNCT), limit)


def _normalize_for_dedupe(text: str, length: int = 60) -> str:
    """去重用的归一化键：去掉空白与标点，只留实义字符。"""
    return re.sub(r"[\s，,。.；;：:、！!？?（）()\[\]【】\"'`*_\-]+", "", (text or ""))[:length]


class HeuristicReconstructor:
    """不依赖 LLM 的确定性重建器。"""

    def reconstruct(
        self,
        *,
        memory_id: str,
        session: SessionMetadata | None,
        events: list[NormalizedEvent],
        bundle: CompressedBundle,
        project: ProjectState,
        runtime: RuntimeContext,
        options,
        secret_findings: int = 0,
    ) -> CanonicalMemory:
        agent = (session.agent if session else None) or "unknown"
        analysis = _Analysis(events)

        memory = CanonicalMemory(
            version="1.0",
            metadata=Metadata(
                memory_id=memory_id,
                source_agent=agent,
                source_session_id=session.session_id if session else None,
                schema_version="1.0",
            ),
            project=self._project(project) if options.include_project else ProjectContext(),
            task=self._task(session, analysis, project),
            conversation=self._conversation(session, analysis, bundle) if options.include_conversation else ConversationContext(),
            implementation=self._implementation(analysis),
            decisions=self._decisions(analysis),
            attempts=self._attempts(analysis),
            validation=self._validation(analysis),
            git=project.git if options.include_git else GitContext(),
            unresolved=self._unresolved(analysis, project),
            next_actions=self._next_actions(analysis),
            risks=self._risks(analysis, project, secret_findings),
            runtime=runtime if options.include_runtime else RuntimeContext(),
        )

        # 已完成的工作不得重复描述为待办（实现plan §十一 规则）
        self._drop_completed_from_actions(memory)
        memory.task.reconstructed_by = "heuristic"
        memory.task.confidence = analysis.confidence
        memory.stats = {
            "reconstruction": "heuristic",
            "total_events": len(events),
            "user_messages": analysis.user_messages_count,
            "tool_calls": analysis.tool_calls_count,
            "failures": len(analysis.failures),
            "test_runs": len(analysis.tests),
            "edited_files": len(analysis.edited_paths),
            "digest_tokens_estimate": bundle.estimated_tokens,
            "compression": bundle.stats,
        }
        return memory

    # ------------------------------------------------------------------
    def _project(self, project: ProjectState) -> ProjectContext:
        return ProjectContext(
            name=project.project_name,
            path=project.cwd,
            language=project.languages,
            framework=project.frameworks,
            architecture=None,
            structure_summary=project.structure_summary or None,
            important_modules=project.important_modules,
            project_constraints=project.constraints,
        )

    # ------------------------------------------------------------------
    def _task(
        self,
        session: SessionMetadata | None,
        analysis: "_Analysis",
        project: ProjectState,
    ) -> TaskContext:
        first_user_raw = analysis.user_messages[0].content if analysis.user_messages else ""
        cleaned = _clean_user_text(first_user_raw)
        # 目标要能当一段话读：把残余换行压成空格
        flattened = re.sub(r"\s*\n\s*", " ", cleaned).strip()

        title = _first_sentence(cleaned, 60) or "（未能从会话中识别任务标题）"
        goal = truncate(flattened, 400)

        # 首条消息过短时（例如只有一句寒暄 + 一段粘贴的载荷），
        # 用最后一条需求补全目标，避免 goal 无信息量。
        if len(goal) < 60 and analysis.user_messages:
            last = re.sub(r"\s*\n\s*", " ", _clean_user_text(analysis.user_messages[-1].content)).strip()
            if last and _normalize_for_dedupe(last, 40) != _normalize_for_dedupe(goal, 40):
                goal = truncate(f"{goal}；（后续要求：{last}）" if goal else last, 400)
        goal = goal or "unknown"

        requirements: list[str] = []
        seen: set[str] = set()
        for event in analysis.user_messages:
            line = _first_sentence(_clean_user_text(event.content), 120)
            key = _normalize_for_dedupe(line, 50)
            if line and key and key not in seen:
                seen.add(key)
                requirements.append(line)
            if len(requirements) >= _MAX_USER_REQUIREMENTS:
                break

        background_parts: list[str] = []
        if session:
            background_parts.append(f"会话来自 {session.agent}")
            if session.cwd:
                background_parts.append(f"工作目录 {session.cwd}")
            if session.model:
                background_parts.append(f"模型 {session.model}")
        background_parts.append(
            f"共 {analysis.user_messages_count} 条用户消息、"
            f"{analysis.tool_calls_count} 次工具调用、{len(analysis.failures)} 次失败"
        )
        if not project.exists:
            background_parts.append("⚠ 项目路径当前不存在，状态以会话记录为准")

        return TaskContext(
            id=f"task_{session.session_id if session else 'unknown'}",
            title=title,
            goal=goal,
            background="；".join(background_parts) or None,
            requirements=requirements,
            constraints=project.constraints[:8],
            status=_infer_status(analysis),
        )

    # ------------------------------------------------------------------
    def _conversation(
        self,
        session: SessionMetadata | None,
        analysis: "_Analysis",
        bundle: CompressedBundle,
    ) -> ConversationContext:
        parts: list[str] = []
        if session and session.cwd:
            parts.append(f"在 {session.cwd} 中，")
        parts.append(
            f"用户先后提出 {analysis.user_messages_count} 项需求；"
            f"Agent 执行 {analysis.tool_calls_count} 次工具调用"
            f"（其中 {len(analysis.failures)} 次失败），"
            f"改动 {len(analysis.edited_paths)} 个文件。"
        )
        summary = "".join(parts)

        key_points: list[str] = []
        # 首尾的用户需求最能代表任务边界
        for event in analysis.user_messages[:2] + analysis.user_messages[-2:]:
            line = first_line(event.content, 140)
            if line and line not in key_points:
                key_points.append(line)
        # 最后若干条 Agent 结论
        for event in analysis.assistant_messages[-3:]:
            line = first_line(event.content, 140)
            if line and line not in key_points:
                key_points.append(f"Agent 结论：{line}")
        if bundle.truncated:
            key_points.append("注：事件流已按预算压缩，完整历史见会话原始文件")

        important: list[str] = []
        for event in analysis.user_messages[:3]:
            if event.content:
                important.append(truncate(collapse(event.content), 300))

        return ConversationContext(
            summary=summary,
            key_points=key_points[:8],
            user_preferences=[],
            important_messages=important,
            raw_session_ref=None,
        )

    # ------------------------------------------------------------------
    def _implementation(self, analysis: "_Analysis") -> ImplementationContext:
        modified: list[ModifiedFile] = []
        created: list[str] = []
        deleted: list[str] = []

        for path, info in analysis.edited_paths.items():
            status = info["status"]
            summary = _file_summary(info)
            if status == "added":
                created.append(path)
            elif status == "deleted":
                deleted.append(path)
            modified.append(ModifiedFile(path=path, summary=summary, status=status))

        completed: list[str] = []
        if analysis.read_paths:
            completed.append(f"读取/检索了 {len(analysis.read_paths)} 个文件")
        if modified:
            names = "、".join(m.path for m in modified[:6])
            completed.append(f"修改了 {len(modified)} 个文件：{names}")
        passed_tests = [t for t in analysis.tests if not t.is_failure]
        for test in passed_tests[-3:]:
            completed.append(f"通过验证：{truncate(test.command or '', 120)}")
        # 说明：失败次数属于「尝试记录」而非「已完成工作」，不放进 completed，
        # 避免 completed 与 attempts 语义混杂。

        return ImplementationContext(
            completed=completed,
            modified_files=modified,
            created_files=created,
            deleted_files=deleted,
        )

    # ------------------------------------------------------------------
    def _decisions(self, analysis: "_Analysis") -> list[Decision]:
        """从 Agent 表述中保守提取已做出的技术决策。

        启发式提取存在噪声风险，因此：
        - 只在句中出现明确决策动词时采纳；
        - 一律在 reason 中标注「启发式提取，未经确认」；
        - 上限 3 条，宁缺毋滥。
        """
        decisions: list[Decision] = []
        for event in analysis.assistant_messages:
            if not event.content:
                continue
            for sentence in re.split(r"[。\n]", event.content):
                sentence = sentence.strip().strip(_TRAILING_PUNCT).replace("**", "").strip()
                if len(sentence) < 12 or len(sentence) > _MAX_DECISION_LEN:
                    continue
                if not _DECISION_RE.search(sentence):
                    continue
                if "？" in sentence or "?" in sentence:
                    continue
                if any(d.decision == sentence for d in decisions):
                    continue
                decisions.append(
                    Decision(
                        decision=sentence,
                        reason="启发式提取，未经确认",
                        alternatives=[],
                        timestamp=event.timestamp,
                    )
                )
                if len(decisions) >= _MAX_DECISIONS:
                    return decisions
        return decisions

    # ------------------------------------------------------------------
    def _attempts(self, analysis: "_Analysis") -> list[Attempt]:
        attempts: list[Attempt] = []
        grouped: dict[str, int] = {}

        for event in analysis.failures:
            action = _attempt_action(event)
            key = _normalize_for_dedupe(action, 80)
            if key in grouped:
                # 同一动作重复失败：合并计数，避免用 3 条相同记录淹没其他信息
                grouped[key] += 1
                continue
            grouped[key] = 1
            error = first_error_line(event_output(event), 240)
            attempts.append(
                Attempt(
                    action=action,
                    purpose=None,
                    result=_failure_result(event, error),
                    success=False,
                    error=error or None,
                    lesson=_lesson(event, error, analysis),
                )
            )
            if len(attempts) >= _MAX_ATTEMPTS:
                break

        # 回填重复次数（保留失败知识的同时控制噪声）
        for attempt in attempts:
            count = grouped.get(_normalize_for_dedupe(attempt.action, 80), 1)
            if count > 1:
                attempt.result = f"{attempt.result}（同一动作重复失败 {count} 次）"

        # 补充少量成功尝试：目标 Agent 需要知道哪些方向已经验证有效
        for event in [t for t in analysis.tests if not t.is_failure][-3:]:
            attempts.append(
                Attempt(
                    action=truncate(event.command or "运行测试", 200),
                    purpose=None,
                    result="执行成功",
                    success=True,
                    error=None,
                    lesson=None,
                )
            )
        return attempts

    # ------------------------------------------------------------------
    def _validation(self, analysis: "_Analysis") -> ValidationContext:
        tests: list[TestResult] = []
        seen: set[str] = set()
        for event in reversed(analysis.tests):  # 同一命令取最后一次结果
            key = (event.command or "").strip()
            if key in seen:
                continue
            seen.add(key)
            tests.append(
                TestResult(
                    command=truncate(key or "unknown", 200),
                    status="failed" if event.is_failure else "passed",
                    output_summary=(
                        _failure_summary(event)
                        if event.is_failure
                        else first_line(event.result, 160) or "执行成功"
                    ),
                )
            )
        tests = list(reversed(tests))

        build: BuildResult | None = None
        builds = [e for e in analysis.events if e.category == "build"]
        if builds:
            last = builds[-1]
            build = BuildResult(
                command=truncate(last.command or "", 200),
                status="failed" if last.is_failure else "passed",
                output_summary=(
                    _failure_summary(last)
                    if last.is_failure
                    else first_line(last.result, 160) or "执行成功"
                ),
            )

        return ValidationContext(
            tests=tests,
            build=build,
            lint=Normalizer.lint_commands(analysis.events),
            manual_validation=[],
        )

    # ------------------------------------------------------------------
    def _unresolved(self, analysis: "_Analysis", project: ProjectState) -> list[Issue]:
        issues: list[Issue] = []
        seen: set[str] = set()

        def add(description: str, priority: str, context: str | None, cause: str | None) -> None:
            key = _normalize_for_dedupe(description, 40)
            if not key or key in seen or len(issues) >= _MAX_ISSUES:
                return
            seen.add(key)
            issues.append(
                Issue(
                    description=description,
                    priority=priority,  # type: ignore[arg-type]
                    context=context,
                    suspected_cause=cause,
                )
            )

        # 1) 尾部窗口内未解决的失败
        for event in analysis.tail_failures:
            if _is_interrupt(event) or analysis.is_resolved(event):
                continue
            error = first_error_line(event_output(event), 200)
            kind = "测试/构建" if event.category in ("test", "build") else "命令执行"
            if error:
                description = f"{kind}失败：{truncate(error, 200)}"
            elif event.exit_code is not None:
                description = (
                    f"{kind}失败：{_attempt_action(event)}"
                    f"（输出中无可读报错，退出码 {event.exit_code}）"
                )
            else:
                description = f"{kind}失败：{_attempt_action(event)}"
            add(
                description,
                "high" if event.category in ("test", "build") else "medium",
                context=truncate(event.command or "", 200) or None,
                cause="需进一步定位（推断：该问题在会话结束时仍未被解决）",
            )

        # 2) 会话被中断（单独处理，不与上面的失败项重复）
        for event in analysis.events:
            if _is_interrupt(event):
                add(
                    "上一轮任务被中断，工作未收尾",
                    "medium",
                    context=str(event.metadata.get("reason") or "") or None,
                    cause=None,
                )

        # 3) 项目状态异常
        if not project.exists and project.cwd:
            add(
                f"项目路径不存在或不可访问：{project.cwd}",
                "high",
                context=project.cwd,
                cause="路径可能已被移动或删除",
            )
        elif project.exists and not project.is_git_repo:
            add(
                "项目不是 Git 仓库，无法通过 Git 校验改动",
                "low",
                context=project.cwd,
                cause=None,
            )

        # 4) 反复失败
        for error, count in analysis.repeated_errors(threshold=3)[:2]:
            add(
                f"同一报错重复出现 {count} 次：{truncate(error, 160)}",
                "high",
                context=None,
                cause="（推断）根因可能未被定位，重复尝试同一方向无效",
            )
        return issues

    # ------------------------------------------------------------------
    def _next_actions(self, analysis: "_Analysis") -> list[Action]:
        actions: list[Action] = []
        seen: set[str] = set()
        priority = 1

        open_failures = [
            e for e in analysis.tail_failures if not _is_interrupt(e) and not analysis.is_resolved(e)
        ]

        for event in open_failures:
            if not event.command and not event.paths:
                continue
            error = first_error_line(event_output(event), 160)
            label = _attempt_action(event)
            key = _normalize_for_dedupe(label, 40)
            if key in seen:
                continue
            seen.add(key)
            actions.append(
                Action(
                    action=f"排查并修复：{label}" + (f"（当前报错：{error}）" if error else ""),
                    reason="该操作在会话结束前仍未成功",
                    priority=priority,
                    completed=False,
                )
            )
            priority += 1
            if len(actions) >= _MAX_ACTIONS:
                return actions

        # 任务未收尾时，才去对话里挖 Agent 自己给出的后续步骤。
        # 会话顺利结束时，助手消息里的编号列表通常只是**说明**而非待办，
        # 硬挖会产出「钩子：xxx 会在会话开始时运行」这种假待办（实测如此）。
        if not actions and analysis.assistant_messages:
            last = analysis.assistant_messages[-1].content or ""
            for item in _NUMBERED_ITEM_RE.findall(last):
                item = collapse(item)
                if len(item) < 8:
                    continue
                if not (_ACTION_LEAD_RE.match(item) or _PENDING_FRAMING_RE.match(item)):
                    continue
                key = _normalize_for_dedupe(item, 40)
                if key in seen:
                    continue
                seen.add(key)
                actions.append(
                    Action(
                        action=truncate(item, 200),
                        reason="来自上一轮 Agent 给出的后续步骤",
                        priority=priority,
                        completed=False,
                    )
                )
                priority += 1
                if len(actions) >= _MAX_ACTIONS:
                    break

        if not actions:
            actions.append(
                Action(
                    action="复核当前改动并确认原任务是否已闭环",
                    reason="会话中未发现明确失败或后续步骤",
                    priority=1,
                    completed=False,
                )
            )
        return actions

    # ------------------------------------------------------------------
    def _risks(
        self, analysis: "_Analysis", project: ProjectState, secret_findings: int
    ) -> list[Risk]:
        risks: list[Risk] = []
        if analysis.repeated_errors(threshold=3):
            risks.append(
                Risk(
                    description="同类报错反复出现，可能存在未定位的根因",
                    impact="继续沿原方向尝试可能持续无效",
                    mitigation="先做根因定位，再决定修复方案",
                )
            )
        if project.is_git_repo and project.git.has_uncommitted_changes:
            count = len(project.git.changed_files)
            risks.append(
                Risk(
                    description=f"工作区存在 {count} 个未提交改动",
                    impact="跨 Agent 接续时若误回滚将丢失改动",
                    mitigation="接续前先用 git diff 确认改动范围；本工具默认不自动 commit",
                )
            )
        if secret_findings:
            risks.append(
                Risk(
                    description=f"会话中检测到 {secret_findings} 处潜在敏感信息",
                    impact="若被注入到目标 Agent 上下文可能造成泄露",
                    mitigation="已按配置脱敏；建议人工复核原始会话文件",
                )
            )
        if not project.exists:
            risks.append(
                Risk(
                    description="项目路径不可访问",
                    impact="目标 Agent 无法核对真实代码状态",
                    mitigation="接续前确认项目路径正确",
                )
            )
        return risks[:5]

    # ------------------------------------------------------------------
    @staticmethod
    def _drop_completed_from_actions(memory: CanonicalMemory) -> None:
        """已完成工作不得重复描述为待办。"""
        completed_blob = " ".join(memory.implementation.completed).lower()
        if not completed_blob:
            return
        kept: list[Action] = []
        for action in memory.next_actions:
            probe = action.action.strip().lower()[:16]
            if probe and probe in completed_blob:
                action.completed = True
                continue
            kept.append(action)
        memory.next_actions = kept


# ----------------------------------------------------------------------
# 分析视图
# ----------------------------------------------------------------------


class _Analysis:
    """对事件流的一次性统计视图，避免各处重复遍历。"""

    def __init__(self, events: list[NormalizedEvent]) -> None:
        self.events = events
        self.user_messages = [e for e in events if e.type is EventType.USER_MESSAGE]
        self.assistant_messages = [e for e in events if e.type is EventType.ASSISTANT_MESSAGE]
        self.tests = [e for e in events if e.category in ("test", "build")]
        self.tool_calls = [
            e for e in events if e.category in ("terminal", "file_edit", "file_read", "build", "test", "tool_call")
        ]
        self.failures = [
            e
            for e in events
            if e.is_failure and e.category in ("terminal", "file_edit", "file_read", "build", "test", "tool_call")
        ]
        self.tail = events[-_TAIL_SIZE:]
        self.tail_failures = [e for e in self.tail if e.is_failure]

        self.read_paths: list[str] = []
        for event in events:
            if event.category == "file_read":
                for path in event.paths:
                    if path not in self.read_paths:
                        self.read_paths.append(path)

        self.edited_paths: dict[str, dict] = {}
        for event in events:
            if event.category != "file_edit":
                continue
            statuses = event.metadata.get("patch_statuses") or {}
            stats = event.metadata.get("patch_file_stats") or {}
            for path in event.paths:
                info = self.edited_paths.setdefault(
                    path, {"status": "modified", "hunks": 0, "added": 0, "removed": 0, "edits": 0}
                )
                info["edits"] += 1
                info["status"] = statuses.get(path, info["status"])
                stat = stats.get(path) or {}
                info["hunks"] += stat.get("hunks", 0)
                info["added"] += stat.get("added", 0)
                info["removed"] += stat.get("removed", 0)

        self.user_messages_count = len(self.user_messages)
        self.tool_calls_count = len(self.tool_calls)

        total_signals = self.user_messages_count + self.tool_calls_count
        if total_signals == 0:
            self.confidence = "unknown"
        elif self.user_messages_count == 0:
            self.confidence = "low"
        elif total_signals < 8:
            self.confidence = "low"
        else:
            self.confidence = "medium"

    # ------------------------------------------------------------------
    def is_resolved(self, event: NormalizedEvent) -> bool:
        """该失败是否在后续被同一命令的成功执行消解。"""
        if not event.command:
            return False
        for later in self.events:
            if later is event or later.timestamp <= event.timestamp:
                continue
            if later.command and later.command.strip() == event.command.strip() and not later.is_failure:
                return True
        return False

    def repeated_errors(self, threshold: int = 3) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        order: list[str] = []
        for event in self.failures:
            error = first_error_line(event_output(event), 160)
            if not error or len(error) < 12:
                continue
            if error not in counts:
                counts[error] = 0
                order.append(error)
            counts[error] += 1
        return [(e, counts[e]) for e in order if counts[e] >= threshold]


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _attempt_action(event: NormalizedEvent) -> str:
    """生成可读的「失败动作」描述。

    文件类操作不直接照抄命令行——apply_patch 的命令里含整段补丁文本，
    照抄会得到一串不可读的内容。
    """
    if event.category == "file_edit" and event.paths:
        extra = f" 等 {len(event.paths)} 个文件" if len(event.paths) > 1 else ""
        return f"修改文件：{truncate(event.paths[0], 160)}{extra}"
    if event.category == "file_read" and event.paths:
        return f"读取文件：{truncate(event.paths[0], 160)}"
    if event.command:
        return truncate(event.command, 200)
    return truncate(event.summary or "未识别动作", 200)


def _is_interrupt(event: NormalizedEvent) -> bool:
    """该事件是否代表「上一轮被中断」。"""
    return (
        event.type is EventType.ERROR
        and str(event.metadata.get("kind") or "") == "turn_aborted"
    )


def _infer_status(analysis: _Analysis) -> str:
    aborted = any(_is_interrupt(e) for e in analysis.events)
    last = analysis.events[-1] if analysis.events else None
    ended_clean = bool(
        last is not None
        and last.type is EventType.ASSISTANT_MESSAGE
        and last.metadata.get("kind") == "task_complete"
    )
    open_failures = [e for e in analysis.tail_failures if not analysis.is_resolved(e)]

    if aborted:
        return "in_progress"
    if open_failures:
        return "blocked"
    if ended_clean and not analysis.failures:
        return "completed"
    return "in_progress"


def _file_summary(info: dict) -> str:
    status = info.get("status", "modified")
    hunks = info.get("hunks", 0)
    added = info.get("added", 0)
    removed = info.get("removed", 0)
    if hunks:
        return f"修改 {hunks} 处（+{added}/-{removed}），共 {info.get('edits', 1)} 次编辑"
    verb = {"added": "新增", "deleted": "删除"}.get(status, "修改")
    return f"{verb}文件（共 {info.get('edits', 1)} 次编辑，未解析到改动行数）"


def _failure_result(event: NormalizedEvent, error: str) -> str:
    if event.category in ("test", "build"):
        label = "构建" if event.category == "build" else "测试"
        base = f"{label}执行失败"
    elif error:
        return truncate(error, 200)
    else:
        base = "执行失败"
    # 没有可读报错时用退出码兜底，避免给出误导性的「首行」
    if not error and event.exit_code is not None:
        return f"{base}（退出码 {event.exit_code}）"
    return base


def _failure_summary(event: NormalizedEvent) -> str:
    """失败事件的输出摘要（供 validation / unresolved 使用）。"""
    error = first_error_line(event_output(event), 240)
    if error:
        return error
    if event.exit_code is not None:
        return f"执行失败（退出码 {event.exit_code}）"
    return "执行失败"


def _lesson(event: NormalizedEvent, error: str, analysis: _Analysis) -> str | None:
    """给出可复用的经验。所有推断性结论显式标注（推断）。"""
    if analysis.is_resolved(event):
        return "（推断）该命令在后续重试中已成功，当前失败可能与环境或时序有关"

    same_error = [
        e
        for e in analysis.failures
        if e is not event and first_error_line(event_output(e), 160) == error
    ]
    if error and len(same_error) >= 2:
        return "（推断）同一报错在多次尝试中重复出现，说明问题可能不在本次改动本身，建议先定位根因"

    if event.category == "file_edit":
        return "（推断）该文件改动未取得预期效果，建议回看改动点是否命中真正的故障位置"
    return None
