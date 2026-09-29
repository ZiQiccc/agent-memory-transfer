"""pytest 公共夹具。

原则：测试**不依赖本机真实的 ~/.codex 数据**。
所有会话数据由 :func:`build_rollout` 合成，包含本机实测到的真实格式特征
（多行记录、注入的 harness 文本、信封格式的工具输出等）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amt.config import (
    AMTConfig,
    ClaudeConfig,
    CodexConfig,
    CursorConfig,
    LLMConfig,
    SecurityConfig,
    WorkBuddyConfig,
)
from amt.context import AppContext

SESSION_ID = "01a08943-75e9-7953-bcc1-141f7ad3cc3d"
CWD_PLACEHOLDER = "__CWD__"

#: 伪造凭据样本，用于验证脱敏链路（非真实凭据）
OPENAI_KEY_SAMPLE = "sk-proj-FAKEabcdefghijklmnopqrstuvwxyz012345"
BEARER_SAMPLE = "Bearer eyJhbGciOiJIUzI1NiJ9.FAKEpayloadpart.FAKEsignaturepart"
PASSWORD_SAMPLE = "FakePassword987654"

#: 项目里被 apply_patch 修改的文件（供 Validator 的引用检查使用）
PATCHED_FILE = "src/main/java/EquipmentMaintenance.java"


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_rollout(cwd: str, *, session_id: str = SESSION_ID) -> str:
    """构造一份 Codex rollout JSONL 文本。"""
    records: list[str] = [
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:35.000Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "timestamp": "2026-09-10T03:00:00.000Z",
                    "cwd": cwd,
                    "originator": "Codex Desktop",
                    "cli_version": "0.149.1",
                    "source": "vscode",
                    "model_provider": "custom",
                },
            }
        ),
        # harness 注入的 developer 指令 → 必须被丢弃
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:35.100Z",
                "ordinal": 1,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "<app-context>harness 注入</app-context>"}],
                },
            }
        ),
        # harness 注入的用户侧环境上下文 → 必须被丢弃
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:35.200Z",
                "ordinal": 2,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"<environment_context>\n  <current_date>2026-09-10</current_date>\n  <filesystem><root>{cwd}</root></filesystem>\n</environment_context>",
                        }
                    ],
                },
            }
        ),
        # 真正的用户需求
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:36.000Z",
                "ordinal": 3,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "接口测试返回保养日期不能为空。响应是200ok，但是{\n"
                            '  "failed": true,\n  "code": "保养日期不能为空"\n}\n帮我检测修复\n',
                        }
                    ],
                },
            }
        ),
        # 模型思考
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:37.000Z",
                "ordinal": 4,
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "日期字段在反序列化时被当成 Date。"}],
                },
            }
        ),
        # ① 成功的编译命令
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:38.000Z",
                "ordinal": 5,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "mvn -q -DskipTests compile"}, ensure_ascii=False),
                    "call_id": "c1",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:50.000Z",
                "ordinal": 6,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": (
                        "Chunk ID: a1\nWall time: 12.0 seconds\n"
                        "Process exited with code 0\nOriginal token count: 20\nOutput:\nBUILD SUCCESS\n"
                    ),
                },
            }
        ),
        # ② 检索命令：退出码 1 = 未匹配，**不是失败**
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:51.000Z",
                "ordinal": 7,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": 'rg -n "NonexistentSymbol" src -S'}, ensure_ascii=False),
                    "call_id": "c2",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:52.000Z",
                "ordinal": 8,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c2",
                    "output": "Chunk ID: a2\nWall time: 0.3 seconds\nProcess exited with code 1\nOutput:\n",
                },
            }
        ),
        # ③ 测试失败：真实失败
        _dumps(
            {
                "timestamp": "2026-09-10T03:01:53.000Z",
                "ordinal": 9,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "mvn -q test"}, ensure_ascii=False),
                    "call_id": "c3",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:02:30.000Z",
                "ordinal": 10,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c3",
                    "output": (
                        "Chunk ID: a3\nWall time: 30.0 seconds\nProcess exited with code 1\n"
                        "Output:\n[ERROR] Tests run: 3, Failures: 1\n"
                        "EquipmentMaintenanceTest.saveDate: expected true but was false\n"
                    ),
                },
            }
        ),
        # ④ 通过 shell 以 heredoc 方式调用 apply_patch（本机实测的真实形态）
        _dumps(
            {
                "timestamp": "2026-09-10T03:02:31.000Z",
                "ordinal": 11,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": "apply_patch <<'PATCH'\n*** Begin Patch\n"
                            f"*** Update File: {PATCHED_FILE}\n@@\n"
                            "-import java.util.Date;\n"
                            "+import com.fasterxml.jackson.annotation.JsonFormat;\n"
                            "*** End Patch\nPATCH"
                        },
                        ensure_ascii=False,
                    ),
                    "call_id": "c4",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:02:35.000Z",
                "ordinal": 12,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c4",
                    "output": (
                        "Exit code: 0\nWall time: 4.0 seconds\nOutput:\n"
                        "Success. Updated the following files:\n"
                        f"M {PATCHED_FILE}\n"
                    ),
                },
            }
        ),
        # ⑤ 同一文件再次 apply_patch 失败
        _dumps(
            {
                "timestamp": "2026-09-10T03:02:36.000Z",
                "ordinal": 13,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": "apply_patch <<'PATCH'\n*** Begin Patch\n"
                            f"*** Update File: {PATCHED_FILE}\n@@\n"
                            "-  private String maintenanceDate;\n"
                            "+  private String maintenanceDate;\n"
                            "*** End Patch\nPATCH"
                        },
                        ensure_ascii=False,
                    ),
                    "call_id": "c5",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:02:40.000Z",
                "ordinal": 14,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c5",
                    "output": (
                        "Exit code: 1\nWall time: 1.0 seconds\nOutput:\n"
                        f"apply_patch verification failed: Failed to find expected lines in {PATCHED_FILE}:\n"
                    ),
                },
            }
        ),
        # ⑥ 回合结束
        _dumps(
            {
                "timestamp": "2026-09-10T03:03:00.000Z",
                "ordinal": 15,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "已把日期字段改为 String 接收，请重新测试。",
                },
            }
        ),
        # ⑦ 本轮被中断
        _dumps(
            {
                "timestamp": "2026-09-10T03:03:01.000Z",
                "ordinal": 16,
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "reason": "interrupted"},
            }
        ),
        # ⑧ 带凭据的调试命令：验证「Memory 自身也必须脱敏」
        _dumps(
            {
                "timestamp": "2026-09-10T03:03:02.000Z",
                "ordinal": 17,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {"cmd": f'curl -H "Authorization: {BEARER_SAMPLE}" http://10.0.0.1/api'},
                        ensure_ascii=False,
                    ),
                    "call_id": "c6",
                },
            }
        ),
        _dumps(
            {
                "timestamp": "2026-09-10T03:03:03.000Z",
                "ordinal": 18,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c6",
                    "output": (
                        "Chunk ID: a6\nWall time: 0.5 seconds\nProcess exited with code 0\n"
                        f"Output:\nenv OPENAI_API_KEY={OPENAI_KEY_SAMPLE}\n"
                        f"db_password={PASSWORD_SAMPLE}\n"
                    ),
                },
            }
        ),
        # 嘈杂记录：必须被忽略而不是报错
        _dumps({"timestamp": "2026-09-10T03:03:04.000Z", "ordinal": 19, "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 12345}}}}),
    ]

    # 关键：制造一条**跨多个物理行**的记录（JSON 允许 token 之间换行）。
    # 本机实测：行式 json.loads 会把这类记录误判为解析失败并丢弃。
    multiline_record = (
        "{\n"
        '  "timestamp": "2026-09-10T03:03:05.000Z",\n'
        '  "ordinal": 20,\n'
        '  "type": "response_item",\n'
        '  "payload": {\n'
        '    "type": "message",\n'
        '    "role": "assistant",\n'
        '    "content": [{"type": "output_text", "text": "跨行记录的助手消息"}]\n'
        "  }\n"
        "}"
    )
    records.append(multiline_record)

    return "\n".join(records) + "\n"


def write_rollout(home: Path, text: str, *, day: str = "2026/09/10", name: str | None = None) -> Path:
    """把 rollout 文本写入 <home>/sessions/<day>/rollout-*.jsonl。"""
    target_dir = home / "sessions" / day
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = name or f"rollout-2026-09-10T11-01-35-{SESSION_ID}.jsonl"
    path = target_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def records_from(text: str) -> list[dict]:
    """用与生产代码一致的容错读取器把文本转成记录列表。"""
    from amt.adapters.codex.parser import iter_json_records

    return [r for r in iter_json_records(text) if r is not None]


def raw_session_from(text: str, cwd: str = "/tmp/proj"):
    from amt.core.models import RawSession

    records = records_from(text)
    return RawSession(
        agent="codex",
        session_id=SESSION_ID,
        path="memory://fixture",
        cwd=cwd,
        records=records,
        total_records=len(records),
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """一个最小可用的 Java 项目目录（含被 patch 的文件，供引用检查通过）。"""
    root = tmp_path / "proj"
    (root / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><artifactId>demo</artifactId></project>",
        encoding="utf-8",
    )
    (root / PATCHED_FILE).write_text("package demo;\nimport java.util.Date;\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# 约定\n- 所有新增接口必须写单元测试\n", encoding="utf-8")
    return root


@pytest.fixture
def codex_home(tmp_path: Path, project_root: Path) -> Path:
    home = tmp_path / "codex-home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    write_rollout(home, build_rollout(str(project_root)))
    return home


@pytest.fixture
def ctx(tmp_path: Path, codex_home: Path) -> AppContext:
    """隔离的 AppContext。

    关键：CLI 路径指向**不存在的可执行文件**。
    否则当开发机装了 claude / codex 时，`auto_launch=True` 的用例会**真的启动
    外部 Agent**（实测：会在用户的 ~/.claude/projects 里留下垃圾会话）。
    测试必须是 hermetic 且零副作用的。
    """
    config = AMTConfig(
        home_dir=tmp_path / "amt",
        llm=LLMConfig(enabled=False),
        security=SecurityConfig(redaction_mode="balanced"),
        codex=CodexConfig(home=codex_home, executable=str(tmp_path / "no-such-codex")),
        claude=ClaudeConfig(
            home=tmp_path / "claude-home",
            projects_dir=tmp_path / "claude-home" / "projects",
            executable=str(tmp_path / "no-such-claude"),
        ),
        cursor=CursorConfig(
            global_storage=tmp_path / "cursor-global",
            workspace_storage=tmp_path / "cursor-ws",
        ),
    )
    return AppContext(config=config)


@pytest.fixture
def codex_source(ctx: AppContext):
    from amt.adapters.codex import CodexSourceAdapter

    return CodexSourceAdapter(ctx)


# ----------------------------------------------------------------------
# Claude Code：合成会话（复刻本机实测的 2.1.284 真实格式）
# ----------------------------------------------------------------------

CLAUDE_SESSION_ID = "9a63c237-d3db-44ea-b941-29fe6be09703"


def build_claude_session(cwd: str, *, session_id: str = CLAUDE_SESSION_ID) -> str:
    """构造一份 Claude Code JSONL。

    复刻的是**本机实测**的结构，包括那些容易踩坑的细节：

    - ``attachment`` / ``queue-operation`` / ``cost-state`` / ``atis-latch``
      / ``last-prompt`` 都是 harness 记账，不是任务状态；
    - 工具输出以 ``type=user`` + ``tool_result`` 回传；
    - API 错误用 ``isApiErrorMessage`` 标记（未登录时就是这样）。
    """
    def line(obj: dict) -> str:
        return _dumps(obj)

    common = {
        "cwd": cwd,
        "sessionId": session_id,
        "version": "2.1.284",
        "gitBranch": "feature/login",
        "userType": "external",
        "entrypoint": "cli",
    }

    records = [
        # harness 记账：必须被忽略
        line({"type": "queue-operation", "operation": "enqueue", "content": "修复登录问题",
              "sessionId": session_id, "timestamp": "2026-09-29T02:00:00.000Z"}),
        # 用户真实需求
        line({"parentUuid": None, "isSidechain": False, "type": "user",
              "message": {"role": "user", "content": "登录接口偶发 401，帮我排查并修复"},
              "uuid": "u-1", "timestamp": "2026-09-29T02:00:01.000Z", **common}),
        # 环境快照附件：不是用户说的话
        line({"parentUuid": "u-1", "isSidechain": False, "type": "attachment",
              "attachment": {"type": "environment",
                             "snapshot": {"workingDirectory": cwd, "isGitRepo": True,
                                          "platform": "win32", "shell": "PowerShell"}},
              "rendered": [{"content": "<system-reminder>\n# Environment\n</system-reminder>"}],
              "uuid": "a-1", "timestamp": "2026-09-29T02:00:01.100Z", **common}),
        # 助手：思考 + 文本
        line({"parentUuid": "u-1", "isSidechain": False, "type": "assistant",
              "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                          "content": [{"type": "thinking", "thinking": "先看 Session 刷新逻辑"},
                                      {"type": "text", "text": "我先检查 Session 刷新与 TTL 配置。"}]},
              "uuid": "s-1", "timestamp": "2026-09-29T02:00:05.000Z", **common}),
        # 助手：工具调用
        line({"parentUuid": "s-1", "isSidechain": False, "type": "assistant",
              "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                          "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                       "input": {"command": "mvn -q test"}}]},
              "uuid": "s-2", "timestamp": "2026-09-29T02:00:10.000Z", **common}),
        # 工具输出（以 user 记录回传）：失败
        line({"parentUuid": "s-2", "isSidechain": False, "type": "user",
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_1",
                   "content": "[ERROR] Tests run: 2, Failures: 1\nLoginServiceTest.refresh: expected ok",
                   "is_error": True}]},
              "toolUseResult": {"stdout": "", "stderr": "Tests failed"},
              "uuid": "r-1", "timestamp": "2026-09-29T02:00:40.000Z", **common}),
        # 助手：编辑文件
        line({"parentUuid": "r-1", "isSidechain": False, "type": "assistant",
              "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                          "content": [{"type": "tool_use", "id": "toolu_2", "name": "Edit",
                                       "input": {"file_path": "src/service/LoginService.java",
                                                 "old_string": "ttl", "new_string": "ttl2"}}]},
              "uuid": "s-3", "timestamp": "2026-09-29T02:01:00.000Z", **common}),
        line({"parentUuid": "s-3", "isSidechain": False, "type": "user",
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_2",
                   "content": "Applied 1 edit to src/service/LoginService.java"}]},
              "uuid": "r-2", "timestamp": "2026-09-29T02:01:05.000Z", **common}),
        # 第二条用户需求（Gemini 侧的多轮）：补一个要求
        line({"parentUuid": "r-2", "isSidechain": False, "type": "user",
              "message": {"role": "user", "content": "另外帮我确认一下 Redis 的 TTL 配置"},
              "uuid": "u-2", "timestamp": "2026-09-29T02:01:30.000Z", **common}),
        # 侧链（subagent）：必须跳过
        line({"parentUuid": "s-3", "isSidechain": True, "type": "assistant",
              "message": {"role": "assistant", "content": [{"type": "text", "text": "侧链内容"}]},
              "uuid": "side-1", "timestamp": "2026-09-29T02:01:10.000Z", **common}),
        # API 错误（未登录）
        line({"parentUuid": "r-2", "isSidechain": False, "type": "assistant",
              "isApiErrorMessage": True, "error": "authentication_error",
              "message": {"role": "assistant", "model": "<synthetic>",
                          "content": [{"type": "text", "text": "Not logged in · Please run /login"}]},
              "uuid": "e-1", "timestamp": "2026-09-29T02:02:00.000Z", **common}),
        # 会话统计：真实数据，进 metadata
        line({"type": "cost-state", "sessionId": session_id, "totalCostUSD": 0.0123,
              "totalLinesAdded": 4, "totalLinesRemoved": 1,
              "modelUsage": {"claude-sonnet-4-5": {"inputTokens": 100}} }),
        line({"type": "atis-latch", "atis": "", "sessionId": session_id}),
        line({"type": "last-prompt", "lastPrompt": "登录接口偶发 401", "sessionId": session_id}),
    ]
    return "\n".join(records) + "\n"


@pytest.fixture
def claude_home(tmp_path: Path, project_root: Path) -> Path:
    home = tmp_path / "claude-home-alt"
    target_dir = home / "projects" / "D--proj-demo"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{CLAUDE_SESSION_ID}.jsonl").write_text(
        build_claude_session(str(project_root)), encoding="utf-8"
    )
    return home


@pytest.fixture
def claude_ctx(tmp_path: Path, claude_home: Path) -> AppContext:
    """使用合成 Claude 会话目录的隔离上下文。"""
    return AppContext(
        config=AMTConfig(
            home_dir=tmp_path / "amt-claude",
            llm=LLMConfig(enabled=False),
            codex=CodexConfig(home=tmp_path / "no-codex", executable=str(tmp_path / "none")),
            claude=ClaudeConfig(
                home=claude_home,
                projects_dir=claude_home / "projects",
                executable=str(tmp_path / "no-such-claude"),
            ),
        )
    )


# ----------------------------------------------------------------------
# Cursor：合成 SQLite（复刻本机实测的表结构）
# ----------------------------------------------------------------------

CURSOR_COMPOSER_ID = "8eba9e02-6973-4abd-9ce9-125bf3c92031"


def build_cursor_db(path: Path) -> Path:
    """构造一个 Cursor 风格的 state.vscdb。"""
    import json as _json
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute(
        "CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT, createdAt INTEGER, "
        "lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, recency INTEGER, "
        "checkpointAt INTEGER, subagentTypeName TEXT, value TEXT)"
    )
    cur.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CURSOR_COMPOSER_ID, "proj", 1789091164829, 1789103800070, 0, 0, 1789103800070, None, "", "{}"),
    )
    # 空草稿 composer：没有气泡，应被过滤掉
    cur.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("empty-state-draft", "proj", 1789032635896, 1789032636041, 0, 0, 1789032636041, None, "", "{}"),
    )

    composer_data = {
        "_v": 1,
        "composerId": CURSOR_COMPOSER_ID,
        "name": "Login fix session",
        "status": "completed",
        "createdAt": 1789091164829,
        "lastUpdatedAt": 1789103800070,
        "unifiedMode": "agent",
        "modelConfig": {"modelName": "claude-sonnet-4-5"},
        "totalLinesAdded": 6,
        "totalLinesRemoved": 2,
    }
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (f"composerData:{CURSOR_COMPOSER_ID}", _json.dumps(composer_data, ensure_ascii=False)),
    )
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        ("composerData:empty-state-draft", _json.dumps({"composerId": "empty-state-draft"})),
    )

    # 注意：key 是 UUID，顺序与时间顺序无关 —— 解析器必须按 createdAt 排序
    bubbles = [
        ("b-zz", {"type": 1, "text": "登录接口偶发 401，帮我排查", "createdAt": "2026-09-29T02:00:00.000Z",
                  "modelInfo": {"modelName": "claude-sonnet-4-5"}}),
        ("b-aa", {"type": 2, "text": "", "createdAt": "2026-09-29T02:00:05.000Z",
                  "thinking": {"text": "先看 Session 刷新逻辑与 TTL。", "signature": ""}}),
        ("b-mm", {"type": 2, "text": "我先检查 Session 刷新与 TTL 配置。",
                  "createdAt": "2026-09-29T02:00:06.000Z"}),
        ("b-cc", {"type": 1, "text": "顺便看下日志", "createdAt": "2026-09-29T02:01:00.000Z"}),
        ("b-nn", {"type": 2, "text": "日志里 401 集中在刷新之后。",
                  "createdAt": "2026-09-29T02:01:30.000Z",
                  "assistantSuggestedDiffs": [
                      {"path": "src/service/LoginService.java",
                       "diff": "@@\n-old\n+new\n+extra\n"}
                  ]}),
    ]
    for bubble_id, bubble in bubbles:
        cur.execute(
            "INSERT INTO cursorDiskKV VALUES (?,?)",
            (f"bubbleId:{CURSOR_COMPOSER_ID}:{bubble_id}", _json.dumps(bubble, ensure_ascii=False)),
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def cursor_ctx(tmp_path: Path) -> AppContext:
    global_dir = tmp_path / "cursor-global"
    build_cursor_db(global_dir / "state.vscdb")
    return AppContext(
        config=AMTConfig(
            home_dir=tmp_path / "amt-cursor",
            llm=LLMConfig(enabled=False),
            codex=CodexConfig(home=tmp_path / "no-codex", executable=str(tmp_path / "none")),
            claude=ClaudeConfig(projects_dir=tmp_path / "no-claude", executable=str(tmp_path / "none")),
            cursor=CursorConfig(
                global_storage=global_dir,
                workspace_storage=tmp_path / "cursor-ws",
            ),
        )
    )


# ----------------------------------------------------------------------
# WorkBuddy：合成会话（复刻本机实测的真实格式）
# ----------------------------------------------------------------------

WORKBUDDY_SESSION_ID = "2eba0e77-2558-41d6-83e2-f59bcc40ca9f"
WORKBUDDY_WORKSPACE = "c-Users-HUAWEI-WorkBuddy-2026-09-11-14-31-00"


def build_workbuddy_session(cwd: str, *, session_id: str = WORKBUDDY_SESSION_ID) -> str:
    """构造一份 WorkBuddy JSONL。

    复刻实测结构，重点是两个容易踩坑的细节：

    - ``timestamp`` 是**毫秒整数**；
    - 用户消息把 harness 注入（``<system-reminder>``，实测上万字符）与真实需求
      （``<user_query>``）放在**同一个文本块**里 —— 整条丢弃会把需求一起丢掉。
    """
    import json as _json

    injected_prefix = (
        '<system-reminder data-role="user-context">\n<user_info>\nOS Version: win32\n'
        "Shell: bash\n</user_info>\n<identity_context>\n## SOUL.md\n"
        + ("注入的上下文内容。" * 400)
        + "\n</system-reminder>\n"
    )

    def line(obj: dict) -> str:
        return _json.dumps(obj, ensure_ascii=False)

    records = [
        # 用户消息：注入 + 真实需求同块
        line(
            {
                "id": "u-1",
                "timestamp": 1789108260533,
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"{injected_prefix}<user_query>登录接口偶发 401，帮我排查并修复</user_query>"}
                ],
                "cwd": cwd,
                "sessionId": session_id,
            }
        ),
        # 纯记账：必须跳过
        line({"id": "s-1", "timestamp": 1789108294469, "type": "file-history-snapshot",
              "isSnapshotUpdate": False, "snapshot": {"trackedFileBackups": {}}, "cwd": cwd}),
        # 思考
        line({"id": "r-1", "parentId": "u-1", "timestamp": 1789108351373, "type": "reasoning",
              "providerData": {"model": "hy4-preview-f", "requestModelName": "Hy4 preview"},
              "content": [], "rawContent": [{"type": "reasoning_text", "text": "先看 Session 刷新逻辑与 TTL。"}],
              "sessionId": session_id, "cwd": cwd}),
        # 工具调用 1：Bash（成功）
        line({"id": "gen-1", "parentId": "r-1", "timestamp": 1789108351713, "type": "function_call",
              "name": "Bash", "arguments": _json.dumps({"command": "mvn -q test", "description": "跑测试"}, ensure_ascii=False),
              "callId": "call-1", "sessionId": session_id, "cwd": cwd}),
        line({"id": "c-1", "parentId": "gen-1", "timestamp": 1789108352960, "type": "function_call_result",
              "name": "Bash", "callId": "call-1", "status": "completed",
              "output": [{"type": "input_text", "text": "[ERROR] Tests run: 2, Failures: 1\nLoginServiceTest.refresh"}],
              "sessionId": session_id, "cwd": cwd}),
        # 工具调用 2：Write（编辑文件）
        line({"id": "gen-2", "parentId": "c-1", "timestamp": 1789108360000, "type": "function_call",
              "name": "Write",
              "arguments": _json.dumps({"file_path": "src/service/LoginService.java", "content": "// patched"}, ensure_ascii=False),
              "callId": "call-2", "sessionId": session_id, "cwd": cwd}),
        line({"id": "c-2", "parentId": "gen-2", "timestamp": 1789108361000, "type": "function_call_result",
              "name": "Write", "callId": "call-2", "status": "completed",
              "output": [{"type": "input_text", "text": "File written: src/service/LoginService.java"}],
              "sessionId": session_id, "cwd": cwd}),
        # 工具调用 3：失败状态
        line({"id": "gen-3", "parentId": "c-2", "timestamp": 1789108362000, "type": "function_call",
              "name": "Bash", "arguments": _json.dumps({"command": "mvn -q package"}, ensure_ascii=False),
              "callId": "call-3", "sessionId": session_id, "cwd": cwd}),
        line({"id": "c-3", "parentId": "gen-3", "timestamp": 1789108363000, "type": "function_call_result",
              "name": "Bash", "callId": "call-3", "status": "failed",
              "output": [{"type": "input_text", "text": "BUILD FAILURE"}],
              "sessionId": session_id, "cwd": cwd}),
        # 助手回复
        line({"id": "a-1", "parentId": "c-3", "timestamp": 1789108422465, "type": "message",
              "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "已修改 LoginService 的 Session 刷新逻辑，但测试仍未通过。"}],
              "sessionId": session_id, "cwd": cwd}),
        # 第二条用户需求
        line({"id": "u-2", "timestamp": 1789108496583, "type": "message", "role": "user",
              "content": [{"type": "input_text", "text": f"{injected_prefix}<user_query>再确认一下 Redis 的 TTL 配置</user_query>"}],
              "sessionId": session_id, "cwd": cwd}),
    ]
    return "\n".join(records) + "\n"


@pytest.fixture
def workbuddy_ctx(tmp_path: Path, project_root: Path) -> AppContext:
    projects = tmp_path / "wb-home" / "projects"
    ws = projects / WORKBUDDY_WORKSPACE
    ws.mkdir(parents=True, exist_ok=True)
    (ws / f"{WORKBUDDY_SESSION_ID}.jsonl").write_text(
        build_workbuddy_session(str(project_root)), encoding="utf-8"
    )
    # 一个空的其它工作区，用于验证扫描不会因空目录出错
    (projects / "empty-workspace").mkdir(parents=True, exist_ok=True)
    return AppContext(
        config=AMTConfig(
            home_dir=tmp_path / "amt-wb",
            llm=LLMConfig(enabled=False),
            codex=CodexConfig(home=tmp_path / "no-codex", executable=str(tmp_path / "none")),
            claude=ClaudeConfig(projects_dir=tmp_path / "no-claude", executable=str(tmp_path / "none")),
            workbuddy=WorkBuddyConfig(projects_dir=projects),
        )
    )
