"""跨 Agent 统一事件协议。

这是「第一层协议」：把各 Agent 私有的会话记录格式，归一成同一种事件流。
只有 ``adapters/<agent>/`` 允许理解 Agent 私有格式，此处只定义统一结构。

关于 ``EventType`` 的取值边界
-----------------------------
实现plan §八 / 技术架构 §二十 规定了 10 个规范事件类型，它们是所有 Adapter
必须实现的跨 Agent 契约（``CANONICAL_EVENT_TYPES``）。

本模块额外定义 2 个**扩展类型**（``SESSION_META`` / ``REASONING``）：它们来自
Codex 真实 Session 中确实存在、且对任务状态重建有实际价值的数据（会话元信息、
模型的思考摘要）。扩展类型仅作辅助，Target Adapter 不得依赖它们；若某个 Agent
无对应数据，直接不产出即可。这样既不破坏协议，也不丢信息。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amt.utils import now_utc


class EventType(str, Enum):
    # ---- 规范类型（跨 Agent 协议，10 项）----
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_READ = "file_read"
    FILE_EDIT = "file_edit"
    TERMINAL = "terminal"
    TEST = "test"
    ERROR = "error"
    PLAN = "plan"

    # ---- 扩展类型（本实现新增，辅助用途）----
    SESSION_META = "session_meta"
    REASONING = "reasoning"


CANONICAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.USER_MESSAGE,
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.FILE_READ,
        EventType.FILE_EDIT,
        EventType.TERMINAL,
        EventType.TEST,
        EventType.ERROR,
        EventType.PLAN,
    }
)

EXTENSION_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.SESSION_META, EventType.REASONING}
)


class AgentEvent(BaseModel):
    """Source Adapter 从原始会话解析出的统一事件。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    timestamp: datetime = Field(default_factory=now_utc)
    type: EventType

    role: str | None = None
    content: str | None = None
    tool_name: str | None = None
    file_path: str | None = None
    command: str | None = None
    result: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_canonical(self) -> bool:
        return self.type in CANONICAL_EVENT_TYPES


class NormalizedEvent(BaseModel):
    """Normalizer 的产物：跨 Agent 语义一致的事件。

    在 ``AgentEvent`` 之上补齐 Normalizer 推断出的语义标签，供 Memory Engine
    与 LLM 消费。任何 Agent 的同类动作，在这里必须表现一致。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    timestamp: datetime = Field(default_factory=now_utc)
    type: EventType

    category: str | None = None
    """语义分类：terminal / file_edit / file_read / test / error / message / plan / meta"""

    role: str | None = None
    summary: str = ""
    content: str | None = None

    tool_name: str | None = None
    paths: list[str] = Field(default_factory=list)
    command: str | None = None
    result: str | None = None

    is_failure: bool = False
    exit_code: int | None = None
    significance: int = 0
    """0=噪音 1=普通 2=重要 3=关键（压缩阶段按此裁剪）"""

    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def event_type(self) -> EventType:
        return self.type


class SessionMetadata(BaseModel):
    """会话级元信息（不属于事件流的上下文）。"""

    model_config = ConfigDict(extra="ignore")

    agent: str
    session_id: str
    cwd: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model: str | None = None
    cli_version: str | None = None
    originator: str | None = None
    source: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
