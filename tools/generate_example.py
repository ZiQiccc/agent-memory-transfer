"""生成示例产物（examples/example-memory.md / .json / report.html）。

全部使用**合成会话**（`tests/conftest.py` 的夹具），因此示例文件里
不含任何真实路径、内网地址或凭据 —— `examples/` 目录可以放心随仓库外发。

用法::

    PYTHONPATH=src python tools/generate_example.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from amt.config import (  # noqa: E402
    AMTConfig,
    ClaudeConfig,
    CodexConfig,
    CursorConfig,
    LLMConfig,
    SecurityConfig,
    WorkBuddyConfig,
)
from amt.context import AppContext  # noqa: E402
from amt.core.memory import MemoryEngine, render_markdown  # noqa: E402
from amt.core.migration import MigrationOrchestrator  # noqa: E402
from amt.core.models import MigrationOptions, SessionMetadata  # noqa: E402
from amt.core.storage import Storage  # noqa: E402
from conftest import SESSION_ID, build_rollout, raw_session_from  # noqa: E402


def main() -> int:
    """在临时沙箱里跑一次完整链路，然后把产物复制到 examples/。"""
    from amt.adapters.codex import CodexSourceAdapter
    from amt.adapters.registry import default_registry
    from amt.gui import write_report

    sandbox = ROOT / ".example-workspace"
    shutil.rmtree(sandbox, ignore_errors=True)

    # ---- 合成工程 ----
    project = sandbox / "demo-project"
    (project / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    (project / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><artifactId>demo</artifactId></project>",
        encoding="utf-8",
    )
    (project / "AGENTS.md").write_text("# 项目约定\n- 所有新增接口必须写单元测试\n", encoding="utf-8")
    patched = project / "src" / "main" / "java" / "EquipmentMaintenance.java"
    patched.write_text("package demo;\n\nimport java.util.Date;\n", encoding="utf-8")

    # ---- 合成会话 ----
    codex_home = sandbox / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "09" / "10"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"rollout-2026-09-10T11-01-35-{SESSION_ID}.jsonl").write_text(
        build_rollout(str(project)), encoding="utf-8"
    )

    config = AMTConfig(
        home_dir=sandbox / "amt",
        llm=LLMConfig(enabled=False),
        security=SecurityConfig(redaction_mode="balanced"),
        codex=CodexConfig(home=codex_home, executable=str(sandbox / "no-cli")),
        claude=ClaudeConfig(projects_dir=sandbox / "no-claude", executable=str(sandbox / "no-cli")),
        # 其余 Agent 的数据源也全部指向沙箱：示例产物绝不能带出本机的真实会话
        cursor=CursorConfig(
            global_storage=sandbox / "no-cursor",
            workspace_storage=sandbox / "no-cursor-ws",
        ),
        workbuddy=WorkBuddyConfig(projects_dir=sandbox / "no-workbuddy"),
    )
    ctx = AppContext(config=config)
    storage = Storage(config)
    registry = default_registry(ctx)

    # ---- 生成 Memory（extract 路径）----
    source = CodexSourceAdapter(ctx)
    raw = raw_session_from(build_rollout(str(project)))
    raw.cwd = str(project)
    events = source.parse_events(raw)
    project_state = source.collect_project_state(str(project))
    runtime = source.collect_runtime_state(str(project))

    outcome = MemoryEngine(ctx).build(
        memory_id="mem_example_001",
        session=SessionMetadata(agent="codex", session_id=SESSION_ID, cwd=str(project)),
        raw_events=events,
        project=project_state,
        runtime=runtime,
        options=MigrationOptions(use_llm=False),
    )

    out_dir = ROOT / "examples"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "example-memory.md").write_text(render_markdown(outcome.memory), encoding="utf-8")
    (out_dir / "example-memory.json").write_text(
        outcome.memory.model_dump_json(indent=2), encoding="utf-8"
    )

    # ---- 跑一次 dry-run 迁移，让报告里有迁移记录与会话快照 ----
    orchestrator = MigrationOrchestrator(ctx, storage=storage, registry=registry)
    orchestrator.migrate(
        source_agent="codex",
        target_agent="claude",
        session_id=SESSION_ID,
        options=MigrationOptions(use_llm=False, auto_launch=False),
        dry_run=True,
    )

    # ---- 示例报告（同样来自合成数据；不枚举本机现存会话）----
    report = write_report(
        config,
        storage,
        registry,
        out_path=out_dir / "report.html",
        include_live_sessions=False,
    )

    print(f"已生成示例：{out_dir / 'example-memory.md'}")
    print(f"            {out_dir / 'example-memory.json'}")
    print(f"            {report}")
    print(f"项目状态：{project_state.project_name} | 语言 {project_state.languages}")
    print(f"事件数：{len(events)} | 失败尝试 {len(outcome.memory.failed_attempts())}")

    shutil.rmtree(sandbox, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
