"""LLM 输出的结构化契约。

技术架构 §22 的硬性要求：LLM 必须走 Structured Output / JSON Schema，
禁止自由格式 Markdown 再解析。

这里用 Pydantic 模型定义 LLM 的输出契约，并从模型直接生成 JSON Schema
发送给模型 —— 这样**契约只有一份定义**，不会出现「提示词里写的字段」
和「代码里解析的字段」不一致的情况。

注意：本模块只定义 LLM 可以填的**语义字段**。项目状态、Git、运行时、
测试结果这些**事实字段不在契约内**——它们由程序读取，LLM 无权改写。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from amt.core.models import (
    Action,
    Attempt,
    Decision,
    Issue,
    ModifiedFile,
    Risk,
)
from amt.core.models.memory import TaskStatus


class LLMTaskPatch(BaseModel):
    """任务语义（LLM 可填）。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, description="一句话任务标题，不超过 60 字")
    goal: str | None = Field(default=None, description="用户想达成的最终目标，不要复述粘贴的代码或 JSON")
    background: str | None = Field(default=None, description="任务的来龙去脉，一两句话")
    requirements: list[str] = Field(default_factory=list, description="用户明确提出的要求")
    constraints: list[str] = Field(default_factory=list, description="项目或用户施加的约束")
    status: TaskStatus | None = Field(default=None, description="任务当前状态")


class LLMConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, description="这一轮对话发生了什么")
    key_points: list[str] = Field(default_factory=list, description="关键结论")
    user_preferences: list[str] = Field(default_factory=list, description="用户表现出的偏好")
    important_messages: list[str] = Field(default_factory=list, description="原话引用，需能追溯")


class LLMImplementationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: list[str] = Field(default_factory=list, description="已经完成的工作（不要写待办）")
    modified_files: list[ModifiedFile] = Field(default_factory=list)


class LLMTestResult(BaseModel):
    """LLM 视角的验证结果。

    刻意**不含** ``TestResult.source``：来源由程序标注，不能由模型声明
    （否则模型可以自称「程序校验通过」）。
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="实际执行的验证命令")
    status: Literal["passed", "failed", "skipped"] = "passed"
    output_summary: str | None = None


class LLMValidationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tests: list[LLMTestResult] = Field(default_factory=list)
    lint: list[str] = Field(default_factory=list)
    manual_validation: list[str] = Field(default_factory=list)


class MemoryPatch(BaseModel):
    """LLM 对 Canonical Memory 的语义补丁。

    只包含语义字段；未提及的字段留空即可，程序会保留确定性结果。
    """

    model_config = ConfigDict(extra="forbid")

    task: LLMTaskPatch | None = None
    conversation: LLMConversationPatch | None = None
    implementation: LLMImplementationPatch | None = None
    validation: LLMValidationPatch | None = None
    decisions: list[Decision] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    unresolved: list[Issue] = Field(default_factory=list)
    next_actions: list[Action] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)


def memory_patch_schema() -> dict:
    """生成发送给模型的 JSON Schema。"""
    return MemoryPatch.model_json_schema()
