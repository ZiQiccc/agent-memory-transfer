"""Mock LLM 服务（OpenAI 兼容），用于在**没有真实 API Key** 时验证 LLM 通道。

它不是一个模型，而是**确定性的规则模拟器**：从事件时间线里抽取字段，
按 MemoryPatch 契约返回 JSON。作用有三：

1. 证明 LLM 通道真的能端到端跑通（真实 HTTP 往返、能力阶梯、用量解析）；
2. 让「启发式 vs LLM」对比流程在没有凭据时也能演示；
3. 作为测试夹具，不依赖外部网络。

**不要用它评估真实模型质量** —— 它只是链路验证器。

用法::

    python tools/mock_llm_server.py --port 8181
    # 另一个终端：
    #   set AMT_LLM_API_KEY=mock
    #   amt compare --from codex --session <id> --llm \\
    #       --llm-base-url http://127.0.0.1:8181/v1 --llm-model mock-extractor
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

_LINE_RE = re.compile(
    r"^\[(?P<stamp>[^\]]+)\]\s+(?P<tag>[A-Z_]+)\s*(?P<body>.*)$"
)
_FAIL_MARK = "⚠失败"
_ERROR_MARK = "└─"
_TITLE_STOP = ("⚠", "└─")


def _extract_user_payload(body: dict[str, Any]) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _parse_timeline(user_text: str) -> list[dict[str, str]]:
    """把压缩时间线解析成结构化事件列表。"""
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in user_text.splitlines():
        match = _LINE_RE.match(raw.strip())
        if match:
            current = {
                "stamp": match.group("stamp").strip(),
                "tag": match.group("tag").strip(),
                "body": match.group("body").strip(),
            }
            if _FAIL_MARK in current["body"]:
                current["failed"] = "1"
                current["body"] = current["body"].replace(_FAIL_MARK, "").strip()
            events.append(current)
            continue
        if current is not None and raw.strip().startswith(_ERROR_MARK):
            detail = raw.strip()[len(_ERROR_MARK) :].strip()
            current.setdefault("error", detail)
    return events


def build_patch(user_text: str) -> dict[str, Any]:
    """从时间线里抽取字段，产出一个合法的 MemoryPatch。"""
    events = _parse_timeline(user_text)

    users = [e for e in events if e["tag"] == "USER_MESSAGE"]
    edits = [e for e in events if e["tag"] in ("FILE_EDIT",)]
    terminals = [e for e in events if e["tag"] in ("TERMINAL", "TEST", "BUILD")]
    assistant = [e for e in events if e["tag"] == "ASSISTANT_MESSAGE"]
    failures = [e for e in events if e.get("failed") == "1"]

    # ---- 任务 ----
    title = ""
    goal = ""
    if users:
        first = users[0]["body"]
        title = first.split("。")[0][:60]
        goal = first[:300]
    if not goal and assistant:
        goal = assistant[0]["body"][:300]

    requirements: list[str] = []
    for event in users:
        line = event["body"].split("。")[0]
        if line and line not in requirements:
            requirements.append(line[:120])
    requirements = requirements[:8]

    # ---- 已修改文件 ----
    modified: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for event in edits:
        for path in re.findall(r"([^\s：，；、（]+\.(?:java|ts|tsx|js|py|vue|go|cs|xml|yml|yaml|json|md))", event["body"]):
            path = path.strip("（(,，")
            if path and path not in seen_paths:
                seen_paths.add(path)
                modified.append({"path": path, "summary": "由 LLM 归纳的改动", "status": "modified"})

    # ---- 已完成 ----
    completed: list[str] = []
    if modified:
        completed.append(f"修改了 {len(modified)} 个文件")
    passed = [e for e in terminals if not e.get("failed") and e["tag"] in ("TEST", "BUILD")]
    for event in passed[-2:]:
        completed.append(f"通过验证：{event['body'][:100]}")

    # ---- 失败尝试（LLM 版：给出更完整的「是什么/为什么」） ----
    attempts: list[dict[str, Any]] = []
    for event in failures[:10]:
        action = event["body"][:180]
        error = (event.get("error") or "")[:240]
        attempts.append(
            {
                "action": action,
                "purpose": "由 LLM 归纳该次尝试的意图",
                "result": "未能达成预期结果",
                "success": False,
                "error": error or None,
                "lesson": "该方向已被验证无效，不要重复相同做法",
            }
        )

    # ---- 决策（LLM 版：从助手表述中归纳） ----
    decisions: list[dict[str, Any]] = []
    decision_re = re.compile(r"(决定|采用|选择|方案|建议|推荐|改为)")
    for event in assistant:
        for sentence in re.split(r"[。\n]", event["body"]):
            sentence = sentence.strip()
            if 10 <= len(sentence) <= 120 and decision_re.search(sentence):
                if any(d["decision"] == sentence for d in decisions):
                    continue
                decisions.append(
                    {"decision": sentence, "reason": "由 LLM 从对话中归纳", "alternatives": []}
                )
                if len(decisions) >= 3:
                    break
        if len(decisions) >= 3:
            break

    # ---- 未解决 / 下一步 ----
    unresolved: list[dict[str, Any]] = []
    next_actions: list[dict[str, Any]] = []
    for index, event in enumerate(failures[-4:], start=1):
        error = (event.get("error") or event["body"])[:180]
        unresolved.append(
            {
                "description": f"仍未解决：{error}",
                "priority": "high" if event["tag"] in ("TEST", "BUILD") else "medium",
                "context": event["body"][:180],
                "suspected_cause": "由 LLM 归纳的推测原因",
            }
        )
        next_actions.append(
            {
                "action": f"针对「{event['body'][:80]}」做根因定位后再修复",
                "reason": "LLM 归纳：该问题在会话结束时仍未解决",
                "priority": index,
                "completed": False,
            }
        )

    patch: dict[str, Any] = {
        "task": {
            "title": title or None,
            "goal": goal or None,
            "background": "由 mock LLM 从事件时间线归纳",
            "requirements": requirements,
            "constraints": [],
            "status": "blocked" if unresolved else "in_progress",
        },
        "conversation": {
            "summary": f"会话包含 {len(users)} 条用户消息、{len(terminals)} 次命令执行，其中 {len(failures)} 次失败。",
            "key_points": [e["body"][:120] for e in assistant[-2:]],
            "user_preferences": [],
            "important_messages": [e["body"][:200] for e in users[:2]],
        },
        "implementation": {"completed": completed, "modified_files": modified},
        "validation": {"tests": [], "lint": [], "manual_validation": []},
        "decisions": decisions,
        "attempts": attempts,
        "unresolved": unresolved,
        "next_actions": next_actions,
        "risks": [],
    }
    return patch


class MockHandler(BaseHTTPRequestHandler):
    server_version = "AMT-MockLLM/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持输出干净
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-extractor", "object": "model"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except Exception:
            self.send_error(400, "invalid json")
            return

        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return

        response_format = body.get("response_format") or {}
        strategy = response_format.get("type") or "plain"
        user_text = _extract_user_payload(body)
        patch = build_patch(user_text)
        content = json.dumps(patch, ensure_ascii=False)

        attempts = len(_parse_timeline(user_text))
        self._json(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": body.get("model") or "mock-extractor",
                "strategy_echo": strategy,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": max(1, len(user_text) // 4),
                    "completion_tokens": max(1, len(content) // 4),
                    "total_tokens": max(2, (len(user_text) + len(content)) // 4),
                },
                "_mock": {"events_seen": attempts, "note": "这是规则模拟器，不是真实模型"},
            }
        )

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="AMT Mock LLM（OpenAI 兼容）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8181)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), MockHandler)
    print(f"Mock LLM 已启动： http://{args.host}:{args.port}/v1")
    print("提示：它只做规则模拟，用于验证链路，不代表真实模型质量。Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
