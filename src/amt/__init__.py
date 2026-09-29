"""Agent Memory Transfer (AMT).

跨 Agent 任务记忆迁移工具。

架构边界（不可违反）：
    Agent 私有格式  →  只能存在于 adapters/<agent>/
    AgentEvent[]    →  跨 Agent 第一层协议（core/models/events.py）
    CanonicalMemory →  跨 Agent 唯一事实协议（core/models/memory.py）
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
