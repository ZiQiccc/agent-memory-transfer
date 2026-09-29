"""迁移域模型：探测结果、会话信息、迁移选项、目标上下文、迁移记录。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from amt.utils import now_utc

_BASE = ConfigDict(extra="ignore")


class MigrationState(str, Enum):
    """技术架构 §27 的迁移状态机状态。"""

    INIT = "INIT"
    SOURCE_DETECTED = "SOURCE_DETECTED"
    SESSION_LOADED = "SESSION_LOADED"
    STATE_COLLECTED = "STATE_COLLECTED"
    MEMORY_EXTRACTED = "MEMORY_EXTRACTED"
    MEMORY_VALIDATED = "MEMORY_VALIDATED"
    TARGET_PREPARED = "TARGET_PREPARED"
    CONTEXT_INJECTED = "CONTEXT_INJECTED"
    AGENT_STARTED = "AGENT_STARTED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"

    # 异常分支（需求文档 §22）
    ERROR = "ERROR"
    RETRY = "RETRY"
    RECOVER = "RECOVER"


class AgentInstallation(BaseModel):
    """Agent 安装探测结果。

    ``installed`` 与 ``runtime_available`` 必须分开表达（实现plan §4.1）：
    「Codex 已安装」不等于「Codex App Server 可用」。
    """

    model_config = _BASE

    agent: str
    display_name: str = ""
    installed: bool = False
    runtime_available: bool = False
    version: str | None = None
    executable: str | None = None
    home_dir: str | None = None
    data_dir: str | None = None
    source_supported: bool = False
    target_supported: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def usable_as_source(self) -> bool:
        return self.source_supported and self.installed

    @property
    def usable_as_target(self) -> bool:
        return self.target_supported


class DetectionResult(BaseModel):
    """Adapter.detect() 的统一返回。"""

    model_config = _BASE

    agent: str
    installed: bool = False
    runtime_available: bool = False
    detail: str = ""
    evidence: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    """实现plan §六 —— 会话列表项。"""

    model_config = _BASE

    agent: str
    session_id: str
    path: str

    cwd: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    title: str | None = None
    size_bytes: int = 0

    resumable: bool = False

    # --- 扩展：便于 GUI 与调试 ---
    model: str | None = None
    record_count: int | None = None
    user_message_count: int | None = None
    parse_errors: int = 0
    archived: bool = False


class RawSession(BaseModel):
    """Source Adapter 读取到的原始会话（记录级，不做语义解释）。"""

    model_config = _BASE

    agent: str
    session_id: str
    path: str
    cwd: str | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    parse_errors: int = 0
    total_records: int = 0
    format_version: str | None = None


class MigrationOptions(BaseModel):
    """技术架构 §28。"""

    model_config = _BASE

    include_project: bool = True
    include_task: bool = True
    include_conversation: bool = True
    include_runtime: bool = True
    include_git: bool = True

    compress_memory: bool = True
    redact_secrets: bool = True
    redaction_mode: Literal["strict", "balanced", "off"] | None = None

    auto_launch: bool = True
    auto_inject: bool = True

    use_llm: bool | None = None
    """None = 跟随配置；False = 强制使用确定性启发式重建。"""

    max_memory_tokens: int = 6000


class TargetContext(BaseModel):
    """技术架构 §33 —— Canonical Memory 与目标 Agent 之间的中间对象。"""

    model_config = _BASE

    agent: str
    system_context: str | None = None
    project_context: str | None = None
    task_context: str = ""
    memory_file: str | None = None
    launch_command: list[str] | None = None
    working_directory: str = ""
    artifacts: list[str] = Field(default_factory=list)
    injection_plan: list[str] = Field(default_factory=list)


class InjectionResult(BaseModel):
    model_config = _BASE

    success: bool = False
    artifacts: list[str] = Field(default_factory=list)
    message: str = ""
    warnings: list[str] = Field(default_factory=list)


class LaunchResult(BaseModel):
    model_config = _BASE

    attempted: bool = False
    success: bool = False
    command: list[str] = Field(default_factory=list)
    pid: int | None = None
    message: str = ""
    degraded: bool = False
    """目标 Agent 未安装 / 无法自动启动时为 True，此时 Memory 仍需保留供手动恢复。"""


class SecretFinding(BaseModel):
    model_config = _BASE

    kind: str
    severity: Literal["low", "medium", "high"] = "medium"
    preview: str = ""
    source: str | None = None
    line: int | None = None


class ValidationReport(BaseModel):
    """确定性校验结果（技术架构 §23 / 实现plan §十三）。"""

    model_config = _BASE

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class StepRecord(BaseModel):
    model_config = _BASE

    name: str
    state: MigrationState | None = None
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"
    detail: str = ""
    duration_ms: int | None = None
    error: str | None = None


class MigrationRecord(BaseModel):
    """需求文档 §23 的 Migration Record。"""

    model_config = _BASE

    migration_id: str
    dry_run: bool = False
    status: Literal["ready", "completed", "partial", "failed"] = "ready"

    source_agent: str
    source_session_id: str | None = None
    target_agent: str

    project_path: str = ""
    project_name: str = ""

    memory_id: str | None = None
    memory_version: str = "1.0"
    memory_size_bytes: int = 0

    options: MigrationOptions = Field(default_factory=MigrationOptions)

    steps: list[StepRecord] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    secret_findings: list[SecretFinding] = Field(default_factory=list)

    state: MigrationState = MigrationState.INIT
    timestamp: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None
    duration_ms: int | None = None

    def step(self, name: str) -> StepRecord | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None
