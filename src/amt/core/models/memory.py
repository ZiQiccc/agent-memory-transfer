"""Canonical Memory —— 跨 Agent 唯一事实协议。

设计约束（技术架构 §3）：
1. **Agent 无关**：本文件中不允许出现任何 ``codex_*`` / ``claude_*`` / ``cursor_*`` 字段。
2. **真实状态与记忆分离**：此处只描述「发生了什么 / 为什么 / 下一步」，
   真实代码归文件系统，真实变更状态归 Git。
3. **机器交换格式为 JSON**，Markdown 只是 Render Format（见 core/memory/renderer.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from amt.utils import now_utc

TaskStatus = Literal["pending", "in_progress", "blocked", "completed"]
Priority = Literal["low", "medium", "high", "critical"]
FileStatus = Literal["modified", "added", "deleted"]
TestStatus = Literal["passed", "failed", "skipped"]
BuildStatus = Literal["passed", "failed", "skipped", "unknown"]

_BASE = ConfigDict(extra="ignore")


class Metadata(BaseModel):
    """技术架构 §6。"""

    model_config = _BASE

    memory_id: str
    source_agent: str
    source_session_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    schema_version: str = "1.0"


class ProjectContext(BaseModel):
    """技术架构 §7。"""

    model_config = _BASE

    name: str = ""
    path: str = ""
    language: list[str] = Field(default_factory=list)
    framework: list[str] = Field(default_factory=list)
    architecture: str | None = None
    structure_summary: str | None = None
    important_modules: list[str] = Field(default_factory=list)
    project_constraints: list[str] = Field(default_factory=list)


class TaskContext(BaseModel):
    """技术架构 §8 —— 整个 Memory 中优先级最高的部分。"""

    model_config = _BASE

    id: str = ""
    title: str = ""
    goal: str = ""
    background: str | None = None
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    status: TaskStatus = "in_progress"

    # --- 本实现扩展：让「恢复状态」可自证 ---
    confidence: Literal["unknown", "low", "medium", "high"] = "unknown"
    reconstructed_by: str | None = None
    """重建方式：llm:<model> / heuristic"""


class ConversationContext(BaseModel):
    """技术架构 §9 —— 不承担完整会话存储，只保留压缩后的语义。"""

    model_config = _BASE

    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    user_preferences: list[str] = Field(default_factory=list)
    important_messages: list[str] = Field(default_factory=list)
    raw_session_ref: str | None = None


class ModifiedFile(BaseModel):
    """技术架构 §10。"""

    model_config = _BASE

    path: str
    summary: str = ""
    status: FileStatus = "modified"


class ImplementationContext(BaseModel):
    """技术架构 §10。"""

    model_config = _BASE

    completed: list[str] = Field(default_factory=list)
    modified_files: list[ModifiedFile] = Field(default_factory=list)
    created_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """技术架构 §11 —— 目标 Agent 不必重新讨论已定方案。"""

    model_config = _BASE

    decision: str
    reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None


class Attempt(BaseModel):
    """技术架构 §12 —— Negative Knowledge Transfer 的载体。

    ``success=False`` 的记录是项目核心价值：告诉目标 Agent
    「这个方案已经试过，不要重复」。
    """

    model_config = _BASE

    action: str
    purpose: str | None = None
    result: str = ""
    success: bool = False
    error: str | None = None
    lesson: str | None = None


class TestResult(BaseModel):
    """技术架构 §13。"""

    model_config = _BASE

    command: str
    status: TestStatus
    output_summary: str | None = None
    source: Literal["program", "llm"] = "program"
    """这条验证结果是谁给的。

    ``program``：由程序从会话记录里**确定性解析**出来的——这是事实，
    会被写入「事实字段不变量」比对，LLM 无权改动。
    ``llm``：程序没能识别出验证命令，由 LLM 从会话里**归纳**得到。
    它是有价值的线索，但**不是程序校验过的事实**，因此必须在渲染时标明，
    并且不参与事实不变量比对。
    """


class BuildResult(BaseModel):
    model_config = _BASE

    command: str | None = None
    status: BuildStatus = "unknown"
    output_summary: str | None = None


class ValidationContext(BaseModel):
    """技术架构 §13。"""

    model_config = _BASE

    tests: list[TestResult] = Field(default_factory=list)
    build: BuildResult | None = None
    lint: list[str] = Field(default_factory=list)
    manual_validation: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    """技术架构 §15。"""

    model_config = _BASE

    description: str
    priority: Priority = "medium"
    context: str | None = None
    suspected_cause: str | None = None


class Action(BaseModel):
    """技术架构 §16。"""

    model_config = _BASE

    action: str
    reason: str | None = None
    priority: int = 1
    completed: bool = False


class Risk(BaseModel):
    model_config = _BASE

    description: str
    impact: str | None = None
    mitigation: str | None = None


class GitContext(BaseModel):
    """技术架构 §14 —— Git Diff 默认不完整进入 Memory，只保留摘要。"""

    model_config = _BASE

    repository: str | None = None
    branch: str | None = None
    commit: str | None = None
    status_summary: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    has_uncommitted_changes: bool = False


class RuntimeContext(BaseModel):
    """技术架构 §17 —— 不保存完整环境变量，尤其禁止 API_KEY/PASSWORD/TOKEN/SECRET。"""

    model_config = _BASE

    working_directory: str = ""
    operating_system: str = ""
    shell: str | None = None
    environment_summary: dict[str, str] = Field(default_factory=dict)
    running_processes: list[str] = Field(default_factory=list)
    recent_commands: list[str] = Field(default_factory=list)


class CanonicalMemory(BaseModel):
    """顶层协议对象。"""

    model_config = _BASE

    version: str = "1.0"
    metadata: Metadata

    project: ProjectContext = Field(default_factory=ProjectContext)
    task: TaskContext = Field(default_factory=TaskContext)
    conversation: ConversationContext = Field(default_factory=ConversationContext)
    implementation: ImplementationContext = Field(default_factory=ImplementationContext)

    decisions: list[Decision] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    validation: ValidationContext = Field(default_factory=ValidationContext)
    git: GitContext = Field(default_factory=GitContext)

    unresolved: list[Issue] = Field(default_factory=list)
    next_actions: list[Action] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    runtime: RuntimeContext = Field(default_factory=RuntimeContext)

    # --- 便于 GUI / CLI 展示的派生信息，不参与协议语义 ---
    stats: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------
    def failed_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if not a.success]

    def succeeded_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if a.success]

    def open_actions(self) -> list[Action]:
        return sorted(
            [a for a in self.next_actions if not a.completed],
            key=lambda a: a.priority,
        )

    def high_priority_issues(self) -> list[Issue]:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return sorted(
            [i for i in self.unresolved if i.priority in ("critical", "high")],
            key=lambda i: order[i.priority],
        )

    def size_bytes(self) -> int:
        return len(self.model_dump_json().encode("utf-8"))


# ----------------------------------------------------------------------
# Task Memory Package（实现plan §35–36）
# ----------------------------------------------------------------------


class PackageManifest(BaseModel):
    """``manifest.json`` —— 任务包元数据。"""

    model_config = _BASE

    version: str = "1.0"
    memory_id: str
    source_agent: str
    target_agent: str
    project: str = ""
    project_path: str = ""
    created_at: datetime = Field(default_factory=now_utc)
    schema_version: str = "1.0"


class SourceRef(BaseModel):
    """``source.json`` —— 记录记忆的出处，保证可追溯。"""

    model_config = _BASE

    agent: str
    session_id: str | None = None
    session_path: str | None = None
    cwd: str | None = None


class MemoryPackage(BaseModel):
    """可携带的任务包：manifest + Canonical Memory + source。

    这是「打开任务包 → 选 Agent → 继续任务」能力的基石。
    """

    model_config = _BASE

    manifest: PackageManifest
    memory: CanonicalMemory
    source: SourceRef
