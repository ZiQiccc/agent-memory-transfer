"""Migration Orchestrator（技术架构 §26 / §37，实现plan §二十三）。

完整链路::

    ① 探测来源 → ② 载入会话 → ③ 采集项目/Git 状态 → ④ 解析事件
    → ⑤ 归一化 + 压缩 → ⑥ 生成 Canonical Memory → ⑦ 校验
    → ⑧ 用户 Preview 确认 → ⑨ 生成目标上下文 → ⑩ 注入 → ⑪ 启动 → ⑫ 验证
    → ⑬ 落盘迁移记录

设计要点：
- **每个阶段都进状态机**，便于 GUI / CLI 展示进度与定位失败点。
- **dry_run** 只跑到「生成目标上下文」，不注入、不启动（实现plan §三十二）。
- 任何阶段失败都不丢 Canonical Memory（需求文档 §24）：
  Memory 在注入之前就已落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from amt.adapters.registry import AgentRegistry
from amt.context import AppContext
from amt.core.memory import MemoryEngine, MemoryValidator, redact_memory, render_markdown
from amt.core.models import (
    CanonicalMemory,
    InjectionResult,
    LaunchResult,
    MigrationOptions,
    MigrationRecord,
    MigrationState,
    SessionMetadata,
    SourceRef,
    TargetContext,
    ValidationReport,
)
from amt.core.migration.state_machine import MigrationStateMachine
from amt.core.storage import Storage
from amt.utils import now_local


class MigrationOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    record: MigrationRecord
    memory: CanonicalMemory | None = None
    target_context: TargetContext | None = None
    injection: InjectionResult | None = None
    launch: LaunchResult | None = None
    validation: ValidationReport | None = None
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    cancelled: bool = False
    normalizer_stats: dict = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.record.status in ("ready", "completed", "partial")


class MigrationOrchestrator:
    def __init__(self, ctx: AppContext, storage: Storage | None = None, registry: AgentRegistry | None = None) -> None:
        self.ctx = ctx
        self.storage = storage or Storage(ctx.config)
        self.registry = registry or AgentRegistry(ctx)
        self.engine = MemoryEngine(ctx)
        self.validator = MemoryValidator()

    # ------------------------------------------------------------------
    def migrate(
        self,
        *,
        source_agent: str,
        target_agent: str,
        session_id: str | None = None,
        options: MigrationOptions | None = None,
        dry_run: bool = False,
        project_root: str | None = None,
        on_preview: Callable[[CanonicalMemory], bool] | None = None,
    ) -> MigrationOutcome:
        """执行一次迁移。

        ``project_root`` 用于覆盖注入目标目录（默认取会话记录里的 cwd）。
        适用场景：把任务迁移到另一份工作副本，或在不改动原工程的前提下试跑。
        """
        options = options or MigrationOptions()
        record = MigrationRecord(
            migration_id=self.storage.new_migration_id(),
            dry_run=dry_run,
            source_agent=source_agent,
            source_session_id=session_id,
            target_agent=target_agent,
            options=options,
        )
        machine = MigrationStateMachine(record)
        outcome = MigrationOutcome(record=record)

        try:
            source = self.registry.source(source_agent)
            target = self.registry.target(target_agent)
        except Exception as exc:
            machine.fail(str(exc))
            record.status = "failed"
            machine.finish(now_local())
            self.storage.save_migration(record)
            return outcome

        try:
            # ① 探测来源
            with machine.step("detect_source", MigrationState.SOURCE_DETECTED) as step:
                detection = source.detect()
                step.detail = detection.detail
                if not detection.installed:
                    raise RuntimeError(detection.detail)
                if not detection.runtime_available:
                    outcome.warnings.append(
                        "来源 Agent 已安装但未检测到可用会话数据，尝试继续"
                    )

            # ② 载入会话（未指定则取最近一条）
            with machine.step("load_session", MigrationState.SESSION_LOADED) as step:
                if not session_id:
                    latest = getattr(source, "latest_session", lambda: None)()
                    if latest is None:
                        raise RuntimeError("未找到任何会话，请先用 --session 指定 session id")
                    record.source_session_id = latest.session_id
                raw = source.load_session(record.source_session_id or "")
                step.detail = f"{len(raw.records)} 条记录（解析失败 {raw.parse_errors} 条）"

            session = SessionMetadata(
                agent=source_agent,
                session_id=raw.session_id,
                cwd=raw.cwd,
                created_at=None,
                source=raw.path,
            )

            # ③ 采集项目与 Git 状态（真实事实来源）
            with machine.step("collect_project_state", MigrationState.STATE_COLLECTED) as step:
                effective_root = project_root or raw.cwd
                if project_root and project_root != raw.cwd:
                    outcome.warnings.append(
                        f"注入目标目录被覆盖为 {project_root}（会话原始工作目录为 {raw.cwd or 'unknown'}）"
                    )
                project = source.collect_project_state(effective_root)
                record.project_path = project.cwd
                record.project_name = project.project_name
                step.detail = (
                    f"{project.project_name or 'unknown'}"
                    f" | 语言 {','.join(project.languages) or 'unknown'}"
                    f" | git {'是' if project.is_git_repo else '否'}"
                )
                if not project.exists:
                    outcome.warnings.append(f"项目路径不可用：{project.cwd}")

            # ④ 解析事件
            with machine.step("parse_events") as step:
                agent_events = source.parse_events(raw)
                step.detail = f"{len(agent_events)} 个 AgentEvent"
                if not agent_events:
                    outcome.warnings.append("未解析出任何事件，Memory 将只包含项目与 Git 信息")

            # ⑤ 安全检查（原始会话落盘前必须脱敏）
            #    redact_structure 一次性完成「扫描 + 脱敏」，避免二次遍历与漏扫。
            scanner = self._scanner_for(options)
            if scanner.enabled:
                redacted_records, findings = scanner.redact_structure(raw.records, source=raw.path)
            else:
                redacted_records, findings = raw.records, []
            record.secret_findings = findings
            if findings:
                outcome.warnings.append(
                    f"检测到 {len(findings)} 处潜在敏感信息（模式：{scanner.mode}），落盘与注入前已处理"
                )
            session_dir = self.storage.save_raw_session(
                source_agent,
                raw.session_id,
                records=redacted_records,
                metadata={"cwd": raw.cwd, "parse_errors": raw.parse_errors, "format": raw.format_version},
                source_path=raw.path,
            )

            runtime = source.collect_runtime_state(project.cwd)

            # ⑥ 生成 Canonical Memory（归一化 + 压缩 + 提取）
            with machine.step("build_memory", MigrationState.MEMORY_EXTRACTED) as step:
                memory_id = self.storage.new_memory_id()
                build = self.engine.build(
                    memory_id=memory_id,
                    session=session,
                    raw_events=agent_events,
                    project=project,
                    runtime=runtime,
                    options=options,
                    secret_findings=len(findings),
                )
                memory = build.memory
                memory.conversation.raw_session_ref = str(session_dir / "raw.json")
                memory.project.path = memory.project.path or project.cwd

                # 用归一化结果补齐最近命令（先有事件才能有命令）
                if options.include_runtime:
                    memory.runtime.recent_commands = _recent_commands(build.normalized_events)

                outcome.memory = memory
                outcome.warnings.extend(build.warnings)
                outcome.normalizer_stats = build.bundle.stats
                record.memory_id = memory.metadata.memory_id
                record.memory_size_bytes = memory.size_bytes()
                step.detail = (
                    f"{memory.metadata.memory_id}"
                    f" | 重建方式 {memory.task.reconstructed_by}"
                    f" | 置信度 {memory.task.confidence}"
                )

            # ⑦ 记忆本体脱敏
            #    原始会话脱敏只覆盖落盘的 session 文件；而 memory.md/memory.json 会被
            #    注入到目标项目目录，因此 **Memory 自身也必须过一遍扫描**，
            #    否则从会话里带出的凭据会随注入产物泄露出项目工作区。
            with machine.step("redact_memory") as step:
                if not scanner.enabled:
                    step.status = "skipped"
                    step.detail = "已禁用脱敏（--no-redact）"
                else:
                    redacted_memory, memory_findings = redact_memory(memory, scanner)
                    if memory_findings:
                        memory = redacted_memory
                        outcome.memory = memory
                        record.secret_findings = list(record.secret_findings) + memory_findings
                        outcome.warnings.append(
                            f"Memory 本体中另发现 {len(memory_findings)} 处敏感信息，"
                            "已在落盘与注入前脱敏"
                        )
                    step.detail = (
                        f"会话内 {len(findings)} 处、Memory 本体 {len(memory_findings)} 处"
                    )

            # 记忆立即落盘：满足「注入失败也要保留 Canonical Memory」
            memory_paths = self.storage.save_memory(memory, render_markdown(memory))
            outcome.artifacts.extend(str(p) for p in memory_paths)
            record.memory_size_bytes = memory.size_bytes()

            # ⑧ 校验
            with machine.step("validate_memory", MigrationState.MEMORY_VALIDATED) as step:
                report = self.validator.validate(memory, project)
                outcome.validation = report
                if report.errors:
                    raise RuntimeError("；".join(report.errors))
                record.warnings.extend(report.warnings)
                step.detail = f"{len(report.checks)} 项校验，{len(report.warnings)} 条警告"

            # ⑧ 用户 Preview 确认
            if on_preview is not None:
                with machine.step("preview") as step:
                    approved = on_preview(memory)
                    if not approved:
                        step.status = "skipped"
                        step.detail = "用户取消了迁移"
                        outcome.cancelled = True
                        record.status = "ready"
                        record.warnings.append("用户取消了迁移；Canonical Memory 已保存，可稍后继续")
                        machine.finish(now_local())
                        self.storage.save_migration(record)
                        return outcome
                    step.detail = "用户已确认"

            # ⑨ 生成目标上下文
            with machine.step("prepare_target", MigrationState.TARGET_PREPARED) as step:
                target_context = target.prepare_context(memory, project, options)
                outcome.target_context = target_context
                outcome.artifacts.extend(target_context.artifacts)
                step.detail = f"目标 {target.display_name}，计划生成 {len(target_context.artifacts)} 个文件"
                if target_context.launch_command:
                    step.detail += f"；启动命令 {target_context.launch_command[0]}"

            if dry_run:
                record.status = "ready"
                machine.finish(now_local())
                self.storage.save_migration(record)
                return outcome

            # ⑩ 注入
            with machine.step("inject_context", MigrationState.CONTEXT_INJECTED) as step:
                if not options.auto_inject:
                    step.status = "skipped"
                    step.detail = "auto_inject=false，跳过注入"
                else:
                    if hasattr(target, "bind_source"):
                        target.bind_source(  # type: ignore[attr-defined]
                            SourceRef(
                                agent=source_agent,
                                session_id=raw.session_id,
                                session_path=raw.path,
                                cwd=raw.cwd,
                            )
                        )
                    injection = target.inject(target_context, memory)
                    outcome.injection = injection
                    outcome.artifacts.extend(injection.artifacts)
                    record.warnings.extend(injection.warnings)
                    if not injection.success:
                        raise RuntimeError(injection.message)
                    step.detail = injection.message

            # ⑪ 启动
            with machine.step("launch_agent", MigrationState.AGENT_STARTED) as step:
                launcher = getattr(target, "launch", None)
                if launcher is None:
                    step.status = "skipped"
                    step.detail = "目标 Adapter 未实现启动"
                else:
                    launch = launcher(target_context, auto_launch=options.auto_launch)  # type: ignore[call-arg]
                    outcome.launch = launch
                    step.detail = launch.message
                    # 启动器的说明（例如「命令行只传单行摘要」）必须一并带出，
                    # 否则用户不明白为什么命令行里看不到完整 Prompt。
                    record.warnings.extend(launch.warnings)
                    if launch.degraded:
                        record.warnings.append(launch.message)

            # ⑫ 验证
            with machine.step("verify", MigrationState.VERIFIED) as step:
                checks = self._verify(record, outcome)
                failed = [c for c in checks if c.startswith("✗")]
                step.detail = f"{len(checks) - len(failed)}/{len(checks)} 项产物校验通过"
                if failed:
                    step.detail += "；缺失：" + "、".join(c[2:].strip() for c in failed[:3])
                record.status = "completed"
                # 只有「确实尝试启动但失败」或「自动启动被启用却无法启动」才算部分完成；
                # 用户主动 --no-launch 时注入已经全部成功，不应降级。
                if outcome.launch is not None and not outcome.launch.success:
                    if outcome.launch.attempted or options.auto_launch:
                        record.status = "partial"
                    else:
                        record.warnings.append(
                            "已按 --no-launch 跳过自动启动；上下文文件已就绪，可手动启动目标 Agent"
                        )

            machine.to(MigrationState.COMPLETED)

        except Exception as exc:
            record.status = "failed"
            if not record.errors:
                record.errors.append(str(exc))
        finally:
            machine.finish(now_local())
            self.storage.save_migration(record)

        return outcome

    # ------------------------------------------------------------------
    def _scanner_for(self, options: MigrationOptions):
        """按迁移选项决定脱敏档位（选项优先于全局配置）。"""
        from amt.services.security import SecretScanner

        if not options.redact_secrets:
            return SecretScanner("off")
        return SecretScanner(options.redaction_mode or self.ctx.config.security.redaction_mode)

    def _verify(self, record: MigrationRecord, outcome: MigrationOutcome) -> list[str]:
        """迁移后校验：Memory 与注入产物是否真实落盘。

        注意：``prepare_target`` 产出的产物路径是**相对项目根**的（如
        ``.agent-transfer/memory.md``），必须解析到项目根再判断存在性，
        否则会把已生成的文件误报为缺失。
        """
        checks: list[str] = []
        project_root = Path(record.project_path) if record.project_path else None

        if record.memory_id:
            path = self.storage.memory_path(record.memory_id)
            checks.append(_mark_line(path.is_file(), f"Memory 落盘：{path.name}"))

        for artifact in outcome.artifacts:
            resolved = _resolve_artifact(artifact, project_root)
            if resolved is None:
                continue
            checks.append(_mark_line(resolved.is_file(), resolved.name))
        return checks


# ----------------------------------------------------------------------
def _recent_commands(events) -> list[str]:
    from amt.core.memory.normalizer import Normalizer

    return Normalizer.recent_commands(events)


def _mark_line(ok: bool, label: str) -> str:
    return f"{'✓' if ok else '✗'} {label}"


def _resolve_artifact(artifact: str, project_root: Path | None) -> Path | None:
    """把产物路径解析为绝对路径；无法归类的返回 None（不参与校验）。"""
    path = Path(artifact)
    if path.is_absolute():
        return path
    if project_root is None:
        return None
    # 只校验我明确知道会生成的产物，避免把无关路径也算进来
    if path.name in ("memory.md", "memory.json", "manifest.json", "source.json") or path.suffix == ".md":
        return project_root / path
    return None
