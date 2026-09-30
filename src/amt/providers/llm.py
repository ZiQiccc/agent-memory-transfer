"""LLM Provider 抽象（技术架构 §42）。

支持 OpenAI Compatible API，因此下列端点都能直接接入：

    OpenAI / DeepSeek / 通义千问(DashScope 兼容模式) / 本地 vLLM / Ollama /
    LM Studio / Xinference / One-API 网关

默认**关闭** —— POC 必须能在无 LLM 环境下跑通（见 core/memory/heuristic.py）。

结构化输出的能力阶梯（按能力降级，而不是一步失败）：

    ① ``response_format={"type":"json_schema"}``  OpenAI 结构化输出 / vLLM guided json
    ② ``response_format={"type":"json_object"}``  多数兼容网关
    ③ 纯提示词约束 + 容错解析                    最后的兜底

另有两项针对真实中转站的适配（都是实测踩出来的）：

    * **思维链模型**（如经中转站转发的 Claude / DeepSeek-R 系）会把推理过程
      放在 ``reasoning_content`` 里，正文在 ``content``。若正文为空则回退取
      ``reasoning_content``，否则会误判为「模型什么都没返回」。
    * **``finish_reason == "length"``** 表示输出被 ``max_tokens`` 截断：
      思维链会先吃掉大量预算，导致 JSON 只写了一半。此时自动把预算翻倍重试一次，
      而不是把「JSON 解析失败」这种误导性错误丢给用户。

每次调用都返回 :class:`LLMResult`，带上用量与耗时 —— 这是
``amt compare``（启发式 vs LLM 质量对比）需要的数据。
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from amt.config import LLMConfig
from amt.core.memory.llm_schema import memory_patch_schema


class LLMUnavailable(RuntimeError):
    """LLM 不可用（未启用 / 缺依赖 / 网络或鉴权失败）。"""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

#: 输出预算的硬上限。思维链模型在长输入上很容易把 4K 预算全花在推理上，
#: 需要能翻倍；但也不能无限放大，否则一次调用会拖很久、费用也失控。
_HARD_TOKEN_CAP = 16384


@dataclass
class LLMResult:
    """一次结构化生成的结果。"""

    data: dict[str, Any]
    provider: str = ""
    model: str = ""
    strategy: str = ""
    """实际生效的请求策略：json_schema / json_object / plain"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    finish_reason: str | None = None
    attempts: list[str] = field(default_factory=list)

    max_tokens_used: int = 0
    """实际生效的 ``max_tokens``（若发生过截断重试，会高于配置值）。"""

    content_source: str = "content"
    """JSON 取自 ``content`` 还是回退到的 ``reasoning_content``。"""

    reasoning_chars: int = 0
    """``reasoning_content`` 长度 —— 用于识别思维链模型。"""

    truncated: bool = False
    """首轮是否因 ``finish_reason=length`` 被截断。"""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "strategy": self.strategy,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "finish_reason": self.finish_reason,
            "attempts": self.attempts,
            "max_tokens_used": self.max_tokens_used,
            "content_source": self.content_source,
            "reasoning_chars": self.reasoning_chars,
            "truncated": self.truncated,
        }


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema_name: str = "canonical_memory",
    ) -> LLMResult:
        """返回结构化结果。失败必须抛 LLMUnavailable。"""

    def available(self) -> tuple[bool, str]:
        return True, "ok"


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        #: 记住上次成功的策略，避免每次都从最高能力重试
        self._preferred_strategy: str | None = None

    # ------------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "LLM 未启用（config.llm.enabled = false）"
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False, "缺少 httpx 依赖（pip install httpx）"
        if not self.config.base_url:
            return False, "未配置 base_url"
        if not self.config.model:
            return False, "未配置 model"
        return True, "ok"

    # ------------------------------------------------------------------
    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema_name: str = "canonical_memory",
    ) -> LLMResult:
        ok, reason = self.available()
        if not ok:
            raise LLMUnavailable(reason)

        strategies = ["json_schema", "json_object", "plain"]
        if self._preferred_strategy in strategies:
            # 上次成功的策略优先，失败后再按阶梯下探
            strategies = [self._preferred_strategy] + [
                s for s in strategies if s != self._preferred_strategy
            ]

        attempts: list[str] = []
        last_error = "未知错误"
        truncated = False
        for strategy in strategies:
            budget = self.config.max_output_tokens
            escalated = False
            while True:
                started = time.monotonic()
                try:
                    body, finish_reason = self._post(
                        strategy, system, user, schema_name, budget
                    )
                except _RetryableStrategy as exc:
                    attempts.append(f"{strategy}: {exc}")
                    last_error = str(exc)
                    break
                except LLMUnavailable:
                    raise
                except Exception as exc:
                    attempts.append(f"{strategy}: {exc}")
                    last_error = str(exc)
                    break

                content, source, reasoning_chars = _extract_content(body)

                # 截断：思维链模型会把预算先花在推理上，正文还没写完就断了。
                # 正确反应是「加预算重试」，而不是报「JSON 解析失败」。
                if finish_reason == "length" and not escalated and budget < _HARD_TOKEN_CAP:
                    new_budget = min(budget * 2, _HARD_TOKEN_CAP)
                    attempts.append(
                        f"{strategy}: 输出被截断（finish_reason=length，"
                        f"max_tokens={budget}，正文 {len(content)} 字）→ 提升到 {new_budget} 重试"
                    )
                    budget = new_budget
                    escalated = True
                    truncated = True
                    continue

                if not content:
                    detail = (
                        f"LLM 返回内容为空（finish_reason={finish_reason}，"
                        f"max_tokens={budget}，"
                        f"completion_tokens={_usage_int(body, 'completion_tokens')}）"
                    )
                    attempts.append(f"{strategy}: {detail}")
                    last_error = detail
                    break

                try:
                    data = _loads_tolerant(content)
                except LLMUnavailable as exc:
                    attempts.append(f"{strategy}: {exc}（finish_reason={finish_reason}）")
                    last_error = str(exc)
                    break

                self._preferred_strategy = strategy
                usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
                return LLMResult(
                    data=data,
                    provider=self.name,
                    model=str(body.get("model") or self.config.model),
                    strategy=strategy,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    finish_reason=finish_reason,
                    attempts=attempts,
                    max_tokens_used=budget,
                    content_source=source,
                    reasoning_chars=reasoning_chars,
                    truncated=truncated,
                )

        raise LLMUnavailable(f"所有结构化输出策略均失败：{last_error}")

    # ------------------------------------------------------------------
    def _post(
        self, strategy: str, system: str, user: str, schema_name: str, max_tokens: int
    ) -> tuple[dict[str, Any], str | None]:
        import httpx

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = self.config.resolve_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
        }
        if strategy == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": memory_patch_schema(),
                    "strict": False,
                },
            }
        elif strategy == "json_object":
            payload["response_format"] = {"type": "json_object"}

        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
        except Exception as exc:
            # 网络层错误重试没有意义（策略无关），直接抛给上层
            raise LLMUnavailable(f"请求 LLM 失败：{exc}") from exc

        if response.status_code >= 400:
            body = response.text[:300]
            # 4xx 多半是「这个端点不支持该 response_format」→ 换下一档策略
            if response.status_code in (400, 404, 415, 422) and strategy != "plain":
                raise _RetryableStrategy(f"HTTP {response.status_code}：{body}")
            if response.status_code in (401, 403):
                raise LLMUnavailable(f"鉴权失败（HTTP {response.status_code}）：请检查 API Key")
            raise LLMUnavailable(f"LLM 返回 {response.status_code}：{body}")

        try:
            body = response.json()
        except Exception as exc:
            raise LLMUnavailable(f"LLM 响应不是合法 JSON：{response.text[:200]}") from exc

        finish_reason = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
        return body, finish_reason


class _RetryableStrategy(RuntimeError):
    """当前结构化输出策略不被端点接受，应换下一档。"""


class NullProvider(LLMProvider):
    """占位 Provider：始终不可用。"""

    name = "none"

    def __init__(self, reason: str = "未配置 LLM Provider") -> None:
        self._reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self._reason

    def generate_structured(self, *, system: str, user: str, schema_name: str = "") -> LLMResult:
        raise LLMUnavailable(self._reason)


def build_provider(config: LLMConfig) -> LLMProvider:
    if not config.enabled:
        # 区分「用户没开」与「配置不认识」，否则报错信息会误导排查方向
        return NullProvider("LLM 未启用（config.llm.enabled = false）")
    if config.provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    return NullProvider(f"未知的 LLM Provider：{config.provider}")


# ----------------------------------------------------------------------
# 响应解析
# ----------------------------------------------------------------------


def _usage_int(body: dict[str, Any], key: str) -> int:
    usage = body.get("usage")
    if isinstance(usage, dict):
        return int(usage.get(key) or 0)
    return 0


def _extract_content(body: dict[str, Any]) -> tuple[str, str, int]:
    """提取正文，返回 ``(文本, 来源, reasoning 长度)``。

    兼容 OpenAI 与部分自建网关的响应结构。对**思维链模型**（正文在
    ``content``、推理过程在 ``reasoning_content``）额外做一次回退：
    若正文缺失就取 ``reasoning_content`` —— 有些网关只把结果放在那里，
    否则会被误判成「模型什么都没返回」。
    """
    content = ""
    reasoning = ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                raw = message.get("content")
                if isinstance(raw, str):
                    content = raw
                elif isinstance(raw, list):
                    content = "\n".join(
                        part.get("text", "")
                        for part in raw
                        if isinstance(part, dict) and part.get("text")
                    )
                # reasoning_content / reasoning 均为实测见过的键名
                for key in ("reasoning_content", "reasoning"):
                    value = message.get(key)
                    if isinstance(value, str) and value:
                        reasoning = value
                        break
            if not content and isinstance(first.get("text"), str):
                content = first["text"]
    if not content:
        for key in ("output_text", "content", "text"):
            if isinstance(body.get(key), str) and body[key]:
                content = body[key]
                break

    if not content and reasoning:
        return reasoning, "reasoning_content", len(reasoning)
    return content, "content", len(reasoning)


def _loads_tolerant(content: str) -> dict[str, Any]:
    """容忍模型输出 Markdown 代码围栏或前后缀说明文字。"""
    text = _FENCE_RE.sub("", content).strip()
    try:
        data = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise LLMUnavailable("LLM 输出中找不到 JSON 对象")
        try:
            data = json.loads(text[start : end + 1])
        except Exception as exc:
            raise LLMUnavailable(f"LLM 输出 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("LLM 输出不是 JSON 对象")
    return data
