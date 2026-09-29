"""AMT 命令行（实现plan §三十一）。

    amt agents                     查看 Agent 可用状态
    amt sessions codex             列出 Codex 会话
    amt extract codex <session>    生成 Canonical Memory
    amt preview <memory_id>        查看已生成的 Memory
    amt migrate --from codex --to claude --session <id> [--dry-run]
    amt history                    查看迁移历史
    amt config [--init]            查看 / 初始化配置
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from amt import __version__
from amt.adapters.registry import default_registry
from amt.config import write_default_config
from amt.context import build_context
from amt.core.models import CanonicalMemory, MigrationOptions
from amt.core.storage import Storage, timestamp_label
from amt.utils import now_local

app = typer.Typer(
    name="amt",
    help="Agent Memory Transfer —— 跨 Agent 任务记忆迁移工具",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _configure_stdio() -> None:
    """Windows 控制台编码兜底，保证中文与符号正常输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


@app.callback()
def main_callback() -> None:
    _configure_stdio()
    _warn_on_config_error()


def _warn_on_config_error() -> None:
    """配置读不动时必须显式告警。

    否则会出现「用户设了 workbuddy.projects_dir 却不生效、且毫无提示」，
    命令会拿默认路径去跑，结果完全出乎意料。
    """
    try:
        from amt.config import load_config

        error = load_config().config_error
    except Exception:
        return
    if error:
        console.print(
            Panel(
                f"[bold red]配置文件未能加载，当前使用的是默认配置。[/bold red]\n\n{error}\n\n"
                "请修复后重试；这会直接影响 projects_dir / 端点 / 脱敏等设置。",
                title="⚠ 配置告警",
                border_style="red",
            )
        )


# ----------------------------------------------------------------------
# agents
# ----------------------------------------------------------------------
@app.command("agents")
def agents_command(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示完整说明"),
) -> None:
    """查看各 Agent 的安装状态与 Adapter 支持情况。"""
    ctx = build_context()
    registry = default_registry(ctx)
    installations = registry.installations()

    table = Table(title="Agent 状态", show_lines=False)
    table.add_column("Agent", style="bold", no_wrap=True)
    table.add_column("已安装", no_wrap=True)
    table.add_column("数据可读", no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Target", no_wrap=True)
    table.add_column("版本", no_wrap=True, overflow="ellipsis", max_width=22)
    table.add_column("用途", overflow="ellipsis", no_wrap=True)

    for item in installations:
        roles = []
        if item.source_supported:
            roles.append("作为来源")
        if item.target_supported:
            roles.append("作为目标")
        table.add_row(
            item.display_name or item.agent,
            _mark(item.installed),
            _mark(item.runtime_available),
            _mark(item.source_supported),
            _mark(item.target_supported),
            item.version or "-",
            "、".join(roles) or "-",
        )
    console.print(table)

    if verbose:
        for item in installations:
            if not item.notes:
                continue
            console.print(f"\n[bold]{item.display_name or item.agent}[/bold]")
            for note in item.notes:
                console.print(f"  [dim]·[/dim] {note}")
    else:
        console.print("[dim]（加 --verbose 查看每个 Agent 的完整说明）[/dim]")

    pairs = registry.migration_pairs()
    console.print(
        f"[dim]可用迁移组合 {len(pairs)} 条（{len(registry.source_agents)} 个来源 × "
        f"{len(registry.target_agents)} 个目标），由 Canonical Memory 中间协议推导得出，"
        "无需为每一对单独开发转换逻辑。[/dim]"
    )

# ----------------------------------------------------------------------
# sessions
# ----------------------------------------------------------------------
@app.command("sessions")
def sessions_command(
    agent: str = typer.Argument("codex", help="来源 Agent"),
    limit: int = typer.Option(15, "--limit", "-n", help="最多显示多少条"),
    deep: bool = typer.Option(False, "--deep", help="完整扫描会话以统计消息数（较慢）"),
    all_sessions: bool = typer.Option(False, "--all", help="忽略 limit，显示全部"),
) -> None:
    """列出指定 Agent 的会话。"""
    ctx = build_context()
    registry = default_registry(ctx)
    source = registry.source(agent)

    sessions = source.list_sessions(limit=None if all_sessions else limit, deep=deep)  # type: ignore[call-arg]
    if not sessions:
        console.print(f"[yellow]未找到 {agent} 的会话[/yellow]")
        return

    table = Table(title=f"{agent} 会话（共显示 {len(sessions)} 条）")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("时间", no_wrap=True)
    table.add_column("大小", justify="right", no_wrap=True)
    table.add_column("会话 ID", style="dim", no_wrap=True)
    table.add_column("项目", no_wrap=True, overflow="ellipsis")
    table.add_column("标题", overflow="fold")

    for index, item in enumerate(sessions, start=1):
        table.add_row(
            str(index),
            _time_short(item.updated_at or item.created_at),
            _human_size(item.size_bytes),
            _short_id(item.session_id),
            Path(item.cwd).name if item.cwd else "-",
            item.title or "-",
        )
    console.print(table)
    console.print("[dim]会话 ID 已截断显示；迁移时可使用完整 ID 或其前缀。[/dim]")


# ----------------------------------------------------------------------
# extract
# ----------------------------------------------------------------------
@app.command("extract")
def extract_command(
    agent: str = typer.Argument(..., help="来源 Agent"),
    session_id: str = typer.Argument(..., help="会话 ID"),
    use_llm: Optional[bool] = typer.Option(None, "--llm/--no-llm", help="是否使用 LLM 归纳（默认跟随配置）"),
    no_redact: bool = typer.Option(False, "--no-redact", help="关闭敏感信息脱敏"),
    max_tokens: int = typer.Option(6000, "--max-tokens", help="Memory 的 token 预算"),
) -> None:
    """从会话生成 Canonical Memory（不迁移）。"""
    ctx = build_context()
    registry = default_registry(ctx)
    storage = Storage(ctx.config)

    from amt.core.memory import (
        MemoryEngine,
        MemoryValidator,
        redact_memory,
        render_markdown,
    )
    from amt.services.security import SecretScanner

    source = registry.source(agent)
    raw = source.load_session(session_id)
    events = source.parse_events(raw)
    project = source.collect_project_state(raw.cwd)
    runtime = source.collect_runtime_state(raw.cwd)

    options = MigrationOptions(
        use_llm=use_llm,
        redact_secrets=not no_redact,
        max_memory_tokens=max_tokens,
        redaction_mode="off" if no_redact else None,
    )
    scanner = (
        SecretScanner(options.redaction_mode or ctx.config.security.redaction_mode)
        if options.redact_secrets
        else SecretScanner("off")
    )

    engine = MemoryEngine(ctx)
    build = engine.build(
        memory_id=storage.new_memory_id(),
        session=_session_meta(raw),
        raw_events=events,
        project=project,
        runtime=runtime,
        options=options,
    )

    from amt.core.memory.normalizer import Normalizer

    build.memory.runtime.recent_commands = Normalizer.recent_commands(build.normalized_events)

    # 与本工具的所有落盘路径保持一致：Memory 本体也要过一遍脱敏
    memory, findings = redact_memory(build.memory, scanner) if scanner.enabled else (build.memory, [])
    build.memory = memory

    report = MemoryValidator().validate(memory, project)
    paths = storage.save_memory(memory, render_markdown(memory))

    _render_memory(memory)
    for warning in build.warnings:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for warning in report.warnings:
        console.print(f"[yellow]⚠ 校验：{warning}[/yellow]")
    if report.errors:
        for error in report.errors:
            console.print(f"[red]✗ 校验失败：{error}[/red]")
    if findings:
        console.print(f"[yellow]⚠ 已脱敏 {len(findings)} 处潜在敏感信息（模式 {scanner.mode}）[/yellow]")
    console.print(f"\n[green]已保存：[/green]{'，'.join(str(p) for p in paths)}")


# ----------------------------------------------------------------------
# preview
# ----------------------------------------------------------------------
@app.command("preview")
def preview_command(
    memory_id: str = typer.Argument(..., help="memory_id"),
    markdown: bool = typer.Option(False, "--markdown", help="输出原始 Markdown"),
) -> None:
    """查看已生成的 Memory。"""
    ctx = build_context()
    storage = Storage(ctx.config)
    memory = storage.load_memory(memory_id)
    if markdown:
        console.print(storage.load_memory_markdown(memory_id) or "")
        return
    _render_memory(memory)


# ----------------------------------------------------------------------
# migrate
# ----------------------------------------------------------------------
@app.command("migrate")
def migrate_command(
    source: str = typer.Option("codex", "--from", "--source", help="来源 Agent"),
    target: str = typer.Option("claude", "--to", "--target", help="目标 Agent"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="会话 ID（默认最近一条）"),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", help="注入目标目录（默认取会话记录的 cwd）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只生成 Memory 与目标上下文，不注入不启动"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过 Preview 确认"),
    no_launch: bool = typer.Option(False, "--no-launch", help="不自动启动目标 Agent"),
    no_inject: bool = typer.Option(False, "--no-inject", help="不写入项目目录"),
    use_llm: Optional[bool] = typer.Option(None, "--llm/--no-llm", help="是否使用 LLM 归纳"),
    no_redact: bool = typer.Option(False, "--no-redact", help="关闭敏感信息脱敏（不推荐）"),
    include: Optional[List[str]] = typer.Option(
        None, "--include", help="只迁移指定范围：project/task/conversation/runtime/git"
    ),
) -> None:
    """一键迁移：Codex → Claude Code。"""
    ctx = build_context()
    storage = Storage(ctx.config)
    registry = default_registry(ctx)

    options = _build_options(
        include=include,
        use_llm=use_llm,
        no_redact=no_redact,
        no_launch=no_launch,
        no_inject=no_inject,
    )

    from amt.core.migration import MigrationOrchestrator

    orchestrator = MigrationOrchestrator(ctx, storage=storage, registry=registry)

    def on_preview(memory: CanonicalMemory) -> bool:
        _render_memory(memory, title="Memory Preview —— 确认后再迁移")
        if yes:
            console.print("[dim]--yes 已指定，跳过确认[/dim]")
            return True
        return typer.confirm("以上 Memory 是否正确？开始迁移", default=True)

    mode = "DRY RUN" if dry_run else "迁移"
    console.rule(f"[bold]{mode}：{source} → {target}")
    outcome = orchestrator.migrate(
        source_agent=source,
        target_agent=target,
        session_id=session,
        options=options,
        dry_run=dry_run,
        project_root=project_root,
        on_preview=on_preview,
    )
    _render_outcome(outcome, dry_run=dry_run)


# ----------------------------------------------------------------------
# history
# ----------------------------------------------------------------------
@app.command("history")
def history_command(
    limit: int = typer.Option(15, "--limit", "-n"),
) -> None:
    """查看迁移历史。"""
    ctx = build_context()
    storage = Storage(ctx.config)
    records = storage.list_migrations(limit=limit)
    if not records:
        console.print("[yellow]暂无迁移记录[/yellow]")
        return

    table = Table(title="迁移历史")
    table.add_column("时间")
    table.add_column("迁移")
    table.add_column("状态")
    table.add_column("Memory")
    table.add_column("项目", overflow="fold")
    for item in records:
        arrow = f"{item['source_agent']} → {item['target_agent']}"
        if item.get("dry_run"):
            arrow += " (dry-run)"
        table.add_row(
            timestamp_label(item.get("timestamp")),
            arrow,
            _status_text(item.get("status")),
            item.get("memory_id") or "-",
            item.get("project_path") or "-",
        )
    console.print(table)


# ----------------------------------------------------------------------
# compare
# ----------------------------------------------------------------------
@app.command("compare")
def compare_command(
    agent: str = typer.Option("codex", "--from", "--source", help="来源 Agent"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="会话 ID（默认最近一条）"),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="OpenAI 兼容端点，如 https://api.deepseek.com/v1"),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="模型名"),
    llm_api_key: Optional[str] = typer.Option(None, "--llm-api-key", help="API Key（也可用环境变量 AMT_LLM_API_KEY）"),
    force_llm: bool = typer.Option(False, "--llm", help="强制尝试启用 LLM（即使配置里是关闭的）"),
    save: bool = typer.Option(True, "--save/--no-save", help="把对比报告写入 AMT 数据目录"),
) -> None:
    """对比「确定性重建」与「LLM 归纳」的记忆质量。

    同时校验一个关键不变量：**事实字段（项目/Git/运行时/测试结果）在两条件下必须完全一致**。

    没有真实 API Key 时，可先用内置 mock 验证链路：

    \b
        python tools/mock_llm_server.py --port 8181
        amt compare --session <id> --llm --llm-base-url http://127.0.0.1:8181/v1 --llm-model mock-extractor
    """
    ctx = build_context()
    registry = default_registry(ctx)
    storage = Storage(ctx.config)

    from amt.core.memory import MemoryEngine
    from amt.core.memory.compare import MemoryComparer, render_comparison_markdown

    _apply_llm_overrides(ctx, base_url=llm_base_url, model=llm_model, api_key=llm_api_key, enable=force_llm)

    source = registry.source(agent)
    raw = source.load_session(session) if session else _latest_or_die(source)
    events = source.parse_events(raw)
    project = source.collect_project_state(raw.cwd)
    runtime = source.collect_runtime_state(raw.cwd)
    session_meta = _session_meta(raw)

    engine = MemoryEngine(ctx)
    console.rule(f"[bold]对比：{agent} 会话 {raw.session_id[:8]}")

    with console.status("运行确定性重建..."):
        heuristic = engine.build(
            memory_id=storage.new_memory_id(),
            session=session_meta,
            raw_events=events,
            project=project,
            runtime=runtime,
            options=MigrationOptions(use_llm=False),
        )

    with console.status("运行 LLM 重建..."):
        llm = engine.build(
            memory_id=storage.new_memory_id(),
            session=session_meta,
            raw_events=events,
            project=project,
            runtime=runtime,
            options=MigrationOptions(use_llm=True),
        )

    # 与编排器保持一致：最近命令来自归一化后的事件（先有事件才能有命令）。
    # 不回填会让事实字段校验「两侧都为空」而空洞通过。
    from amt.core.memory.normalizer import Normalizer

    heuristic.memory.runtime.recent_commands = Normalizer.recent_commands(heuristic.normalized_events)
    llm.memory.runtime.recent_commands = Normalizer.recent_commands(llm.normalized_events)

    comparison = MemoryComparer().compare(heuristic, llm)
    _render_comparison(comparison)

    if save:
        markdown = render_comparison_markdown(comparison)
        out_dir = ctx.config.home_dir / "compare"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"compare_{raw.session_id[:20]}_{timestamp_label(now_local()).replace(':', '').replace(' ', '_')}.md"
        path.write_text(markdown, encoding="utf-8")
        console.print(f"[green]对比报告已保存：[/green]{path}")
        console.print("[dim]提示：也可以在 `amt report` 生成的 HTML 报告里查看。[/dim]")

    if not comparison.llm_available:
        console.print(
            "[yellow]⚠ LLM 未生效，右侧数据仍是确定性重建结果。"
            "请用 --llm --llm-base-url ... --llm-model ... 配置端点后重试。[/yellow]"
        )
    if not comparison.facts_are_invariant:
        console.print("[red]✗ 事实字段出现不一致——请检查 LLM 结果合并逻辑（事实不应被 LLM 覆盖）[/red]")


# ----------------------------------------------------------------------
# llm-check
# ----------------------------------------------------------------------
@app.command("llm-check")
def llm_check_command(
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="OpenAI 兼容端点（中转站/官方/本地）"),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="模型名"),
    llm_api_key: Optional[str] = typer.Option(None, "--llm-api-key", help="API Key（建议改用环境变量 AMT_LLM_API_KEY）"),
    enable: bool = typer.Option(True, "--enable/--no-enable", help="是否同时把 LLM 写入本次配置（仅内存，不落盘）"),
) -> None:
    """验证 LLM 端点是否可用（真实发起一次最小结构化调用）。

    在配置中转站/官方 API 后先跑这个，能一次性看清：
    端点是否可达、鉴权是否通过、用哪种结构化输出策略、模型名是否被接受。

    \b
    示例：
        set AMT_LLM_API_KEY=sk-xxx
        amt llm-check --llm-base-url https://your-relay.example.com/v1 --llm-model gpt-4o-mini
    """
    ctx = build_context()
    _apply_llm_overrides(
        ctx, base_url=llm_base_url, model=llm_model, api_key=llm_api_key, enable=enable
    )

    from amt.core.memory.llm_schema import memory_patch_schema
    from amt.providers.llm import LLMUnavailable, build_provider

    cfg = ctx.config.llm
    api_key = cfg.resolve_api_key()
    origin = cfg.origin
    table = Table(title="LLM 配置")
    table.add_column("项")
    table.add_column("值", overflow="fold")
    table.add_column("来源", overflow="fold")
    table.add_row("enabled", str(cfg.enabled), origin.get("enabled", "默认值/配置文件"))
    table.add_row("base_url", cfg.base_url or "-", origin.get("base_url", "默认值/配置文件"))
    table.add_row("model", cfg.model or "-", origin.get("model", "默认值/配置文件"))
    table.add_row(
        "api_key",
        f"已设置（{api_key[:4]}***）" if api_key else "[yellow]未设置[/yellow]",
        cfg.key_source(),
    )
    table.add_row("timeout", f"{cfg.timeout}s", "默认值/配置文件")
    table.add_row(
        "期望输出契约",
        f"{memory_patch_schema()['title']}（{len(memory_patch_schema().get('properties', {}))} 个字段）",
        "-",
    )
    console.print(table)
    if ctx.config.env_file is not None:
        console.print(f"[dim]凭据文件：{ctx.config.env_file}[/dim]")
    else:
        console.print(
            "[dim]未找到 .env（查找顺序：AMT_ENV_FILE → $AMT_HOME/.env → 当前目录/.env）[/dim]"
        )

    provider = build_provider(cfg)
    available, reason = provider.available()
    if not available:
        console.print(f"[red]✗ 不可用：[/red]{reason}")
        raise typer.Exit(code=1)

    console.print("[dim]正在发起一次最小结构化调用...[/dim]")
    try:
        result = provider.generate_structured(
            system="你是结构化信息抽取器。只输出 JSON。",
            user="[01-01 00:00] USER_MESSAGE 验证连通性\n[01-01 00:01] TERMINAL 执行命令：echo ok",
        )
    except LLMUnavailable as exc:
        console.print(f"[red]✗ 调用失败：[/red]{exc}")
        console.print(
            "[dim]排查顺序：① base_url 是否要带 /v1 ② 中转站是否要求特定模型名 "
            "③ API Key 是否正确 ④ 是否需要走代理[/dim]"
        )
        raise typer.Exit(code=1)

    ok_table = Table(title="调用结果")
    ok_table.add_column("项")
    ok_table.add_column("值", overflow="fold")
    ok_table.add_row("状态", "[green]✓ 成功[/green]")
    ok_table.add_row("结构化策略", f"{result.strategy}")
    ok_table.add_row("请求模型 / 返回模型", f"{cfg.model} / {result.model}")
    ok_table.add_row("耗时", f"{result.duration_ms} ms")
    ok_table.add_row(
        "tokens",
        f"prompt {result.prompt_tokens} + completion {result.completion_tokens} = {result.total_tokens}",
    )
    ok_table.add_row("finish_reason", result.finish_reason or "-")
    ok_table.add_row("返回字段", "、".join(sorted(result.data.keys())) or "（空）")
    ok_table.add_row("降级过程", "；".join(result.attempts) or "无（首选策略即成功）")
    console.print(ok_table)

    if result.strategy != "json_schema":
        console.print(
            f"[yellow]⚠ 生效策略是 {result.strategy}，说明端点不支持 json_schema 结构化输出。"
            "链路仍可用，但输出约束会弱一些。[/yellow]"
        )
    console.print(
        Panel(
            "端点可用。接下来可以运行：\n"
            "  [bold]amt compare --session <id> --llm[/bold]　对比启发式与 LLM 的记忆质量",
            border_style="green",
        )
    )


# ----------------------------------------------------------------------
# report（GUI）
# ----------------------------------------------------------------------
@app.command("report")
def report_command(
    out: Optional[str] = typer.Option(None, "--out", "-o", help="输出路径（默认 <AMT_HOME>/report.html）"),
    no_open: bool = typer.Option(False, "--no-open", help="生成后不自动打开浏览器"),
    live_sessions: bool = typer.Option(True, "--live/--no-live", help="是否枚举各 Agent 的现存会话"),
) -> None:
    """生成自包含 HTML 报告：概览 / 会话 / 记忆 / 迁移历史 / 完整对话。

    无需安装、可离线打开，用来核对「Memory 里到底写了什么」以及
    「迁移到底做了什么」。
    """
    ctx = build_context()
    storage = Storage(ctx.config)
    registry = default_registry(ctx)

    from amt.gui import build_report_data, render_html

    with console.status("正在汇总数据..."):
        data = build_report_data(
            ctx.config, storage, registry, include_live_sessions=live_sessions
        )
        html = render_html(data)

    target = Path(out) if out else (ctx.config.home_dir / "report.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    console.print(f"[green]报告已生成：[/green]{target}")
    console.print(
        f"[dim]包含 {len(data['agents'])} 个 Agent、{len(data['sessions'])} 个会话、"
        f"{len(data['memories'])} 份记忆、{len(data['migrations'])} 条迁移记录、"
        f"{len(data['conversations'])} 份会话快照。[/dim]"
    )
    for note in data["notes"]:
        console.print(f"[yellow]⚠ {note}[/yellow]")

    if not no_open:
        try:
            import webbrowser

            webbrowser.open(target.as_uri())
        except Exception as exc:
            console.print(f"[dim]未能自动打开浏览器（{exc}），请手动打开上述路径。[/dim]")


# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------
@app.command("config")
def config_command(
    init: bool = typer.Option(False, "--init", help="写入默认配置文件"),
) -> None:
    """查看配置。"""
    ctx = build_context()
    cfg = ctx.config
    if init:
        path = write_default_config(cfg)
        console.print(f"[green]配置已就绪：[/green]{path}")
    table = Table(title="AMT 配置")
    table.add_column("项")
    table.add_column("值", overflow="fold")
    table.add_row("home_dir", str(cfg.home_dir))
    table.add_row("config_file", str(cfg.config_file))
    table.add_row("env_file", str(cfg.env_file) if cfg.env_file else "未找到（凭据将只读环境变量）")
    table.add_row("llm.enabled", str(cfg.llm.enabled))
    table.add_row("llm.model", cfg.llm.model if cfg.llm.enabled else "-")
    table.add_row("llm.base_url", cfg.llm.base_url if cfg.llm.enabled else "-")
    table.add_row("llm.api_key", cfg.llm.key_source())
    table.add_row("security.redaction_mode", cfg.security.redaction_mode)
    table.add_row("codex.sessions_dir", str(cfg.codex.sessions_dir()))
    table.add_row("claude.memory_dir", cfg.claude.memory_dir)
    table.add_row("claude.context_file", cfg.claude.context_file)
    table.add_row("shell", ctx.shell.describe())
    table.add_row("git", ctx.git.executable or "未找到")
    console.print(table)


@app.command("version")
def version_command() -> None:
    """显示版本。"""
    console.print(f"agent-memory-transfer {__version__}")


# ======================================================================
# 渲染辅助
# ======================================================================


def _build_options(
    *,
    include: Optional[List[str]],
    use_llm: Optional[bool],
    no_redact: bool,
    no_launch: bool,
    no_inject: bool,
) -> MigrationOptions:
    options = MigrationOptions(
        use_llm=use_llm,
        redact_secrets=not no_redact,
        redaction_mode="off" if no_redact else None,
        auto_launch=not no_launch,
        auto_inject=not no_inject,
    )
    if include:
        wanted = {item.strip().lower() for item in include if item.strip()}
        valid = {"project", "task", "conversation", "runtime", "git"}
        unknown = wanted - valid
        if unknown:
            raise typer.BadParameter(f"未知的迁移范围：{', '.join(sorted(unknown))}（可选：{', '.join(sorted(valid))}）")
        options.include_project = "project" in wanted
        options.include_task = "task" in wanted
        options.include_conversation = "conversation" in wanted
        options.include_runtime = "runtime" in wanted
        options.include_git = "git" in wanted
    return options


def _render_memory(memory: CanonicalMemory, title: str = "Canonical Memory") -> None:
    """按需求文档 §14 的结构展示 Memory。"""
    task = Text()
    task.append(f"任务　{memory.task.title or 'unknown'}\n", style="bold")
    task.append(f"状态　{memory.task.status}　")
    task.append(f"置信度　{memory.task.confidence}　")
    task.append(f"重建　{memory.task.reconstructed_by or 'unknown'}\n", style="dim")
    task.append(f"目标　{memory.task.goal or 'unknown'}", style="default")
    console.print(Panel(task, title=f"{title} · {memory.metadata.memory_id}", border_style="cyan"))

    if memory.implementation.completed:
        console.print("[bold]已完成[/bold]")
        for item in memory.implementation.completed:
            console.print(f"  [green]✓[/green] {item}")

    if memory.implementation.modified_files:
        console.print("[bold]已修改文件[/bold]")
        for file in memory.implementation.modified_files:
            console.print(f"  [cyan]·[/cyan] {file.path} [dim]({file.status})[/dim]")
            if file.summary:
                console.print(f"      [dim]{file.summary}[/dim]")

    failed = memory.failed_attempts()
    if failed:
        console.print("[bold red]失败尝试（勿重复）[/bold red]")
        for index, attempt in enumerate(failed, start=1):
            console.print(f"  [red]✗[/red] {index}. {attempt.action}")
            if attempt.result:
                console.print(f"      结果：{attempt.result}")
            if attempt.error:
                console.print(f"      报错：[dim]{attempt.error}[/dim]")
            if attempt.lesson:
                console.print(f"      经验：[dim]{attempt.lesson}[/dim]")

    if memory.unresolved:
        console.print("[bold]当前问题[/bold]")
        for issue in memory.unresolved:
            console.print(f"  [yellow]![/yellow] [{issue.priority}] {issue.description}")

    actions = memory.open_actions()
    if actions:
        console.print("[bold]下一步[/bold]")
        for action in actions:
            console.print(f"  [blue]→[/blue] {action.action}")

    if memory.validation.tests:
        console.print("[bold]验证[/bold]")
        for test in memory.validation.tests:
            mark = "[green]✓[/green]" if test.status == "passed" else "[red]✗[/red]"
            console.print(f"  {mark} {test.command} [dim]({test.status})[/dim]")

    if memory.risks:
        console.print("[bold]风险[/bold]")
        for risk in memory.risks:
            console.print(f"  [magenta]![/magenta] {risk.description}")


def _render_outcome(outcome, *, dry_run: bool) -> None:
    record = outcome.record

    if record.steps:
        table = Table(title="迁移步骤", show_lines=False)
        table.add_column("步骤")
        table.add_column("状态")
        table.add_column("耗时", justify="right")
        table.add_column("说明", overflow="fold")
        for step in record.steps:
            table.add_row(
                step.name,
                _step_status(step.status),
                f"{step.duration_ms} ms" if step.duration_ms is not None else "-",
                step.detail or step.error or "",
            )
        console.print(table)

    if outcome.record.secret_findings:
        table = Table(title=f"敏感信息（{len(record.secret_findings)} 处，已按 {record.options.redaction_mode or 'balanced'} 处理）")
        table.add_column("类型")
        table.add_column("级别")
        table.add_column("预览")
        for finding in record.secret_findings[:12]:
            table.add_row(finding.kind, finding.severity, finding.preview)
        console.print(table)

    for warning in record.warnings:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for error in record.errors:
        console.print(f"[red]✗ {error}[/red]")

    if outcome.target_context is not None:
        context = outcome.target_context
        console.print(Panel(
            "\n".join(
                [
                    f"目标 Agent　{context.agent}",
                    f"工作目录　{context.working_directory}",
                    f"记忆文件　{context.memory_file}",
                    "启动命令　" + (" ".join(context.launch_command) if context.launch_command else "（未找到 CLI，需手动启动）"),
                    "",
                    "注入计划：",
                    *[f"  · {item}" for item in context.injection_plan],
                ]
            ),
            title="Target Context",
            border_style="green" if outcome.injection is None or outcome.injection.success else "red",
        ))

    if outcome.injection is not None:
        console.print(
            f"[green]✓ 注入完成[/green]（{len(outcome.injection.artifacts)} 个文件）"
            if outcome.injection.success
            else f"[red]✗ 注入失败：{outcome.injection.message}[/red]"
        )
        if outcome.injection.success and not dry_run:
            for artifact in outcome.injection.artifacts:
                console.print(f"   [dim]{artifact}[/dim]")

    if outcome.launch is not None:
        if outcome.launch.success:
            console.print(f"[green]✓ {outcome.launch.message}[/green]")
        else:
            console.print(f"[yellow]◐ {outcome.launch.message}[/yellow]")

    if outcome.validation is not None:
        console.print(
            f"[dim]校验：{len(outcome.validation.checks)} 项通过，"
            f"{len(outcome.validation.warnings)} 条警告[/dim]"
        )

    if outcome.memory is not None:
        console.print(f"[dim]Memory：[bold]{outcome.memory.metadata.memory_id}[/bold]"
                      f"（{_human_size(outcome.memory.size_bytes())}）[/dim]")

    status = record.status
    if outcome.cancelled:
        console.print(Panel("已取消。Canonical Memory 已保存，可随时继续。", border_style="yellow"))
    elif status == "ready":
        console.print(Panel(
            f"状态 READY（dry-run={dry_run}）\n"
            f"迁移记录：{record.migration_id}\n"
            "下一步：去掉 --dry-run 即可注入并启动目标 Agent。",
            border_style="cyan",
        ))
    elif status == "completed":
        console.print(Panel(
            f"迁移完成 ✅　记录号 {record.migration_id}\n"
            "目标 Agent 将读取 .agent-transfer/memory.md 并从中断处继续。",
            border_style="green",
        ))
    elif status == "partial":
        console.print(Panel(
            f"迁移部分完成 ◐　记录号 {record.migration_id}\n"
            "Memory 与上下文文件已就绪，但目标 Agent 未能自动启动——请手动启动，"
            "它会通过 CLAUDE.md 自动加载上下文。",
            border_style="yellow",
        ))
    else:
        console.print(Panel(
            f"迁移失败 ✗　记录号 {record.migration_id}\n"
            + "\n".join(record.errors),
            border_style="red",
        ))


# ----------------------------------------------------------------------


def _mark(value: bool) -> str:
    return "[green]✓[/green]" if value else "[dim]–[/dim]"


def _apply_llm_overrides(
    ctx,
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    enable: bool = False,
) -> None:
    """允许在命令行直接指定 LLM 端点，避免把凭据写进配置文件。"""
    llm = ctx.config.llm
    if base_url:
        llm.base_url = base_url.rstrip("/")
        llm.enabled = True
    if model:
        llm.model = model
        llm.enabled = True
    if api_key:
        llm.api_key = api_key
        llm.enabled = True
    if enable:
        llm.enabled = True


def _latest_or_die(source):
    latest = getattr(source, "latest_session", lambda: None)()
    if latest is None:
        console.print("[red]未找到任何会话，请用 --session 指定[/red]")
        raise typer.Exit(code=1)
    return source.load_session(latest.session_id)


def _render_comparison(comparison) -> None:
    table = Table(title="语义字段对比（LLM 有权改写的字段）")
    table.add_column("字段", no_wrap=True)
    table.add_column("启发式", overflow="fold", max_width=28)
    table.add_column("LLM", overflow="fold", max_width=28)
    table.add_column("差异", no_wrap=True, justify="right")
    table.add_column("说明", overflow="fold")
    for diff in comparison.semantic_fields:
        table.add_row(
            diff.field,
            _clip_cell(diff.heuristic),
            _clip_cell(diff.llm),
            diff.delta,
            diff.note or "",
        )
    console.print(table)

    fact_table = Table(title="事实字段不变量校验（必须全部一致）")
    fact_table.add_column("字段", no_wrap=True)
    fact_table.add_column("启发式", overflow="fold", max_width=34)
    fact_table.add_column("LLM", overflow="fold", max_width=34)
    fact_table.add_column("判定", no_wrap=True)
    for diff in comparison.fact_fields:
        style = "green" if diff.delta == "一致" else "red"
        fact_table.add_row(
            diff.field,
            _clip_cell(diff.heuristic),
            _clip_cell(diff.llm),
            f"[{style}]{diff.delta}[/{style}]",
        )
    console.print(fact_table)

    if comparison.provider_is_mock:
        console.print(
            Panel(
                "本次使用的是 [bold]mock LLM（规则模拟器）[/bold]。\n"
                "差异只反映规则模拟与启发式的区别，[bold]不能代表真实模型的能力[/bold]。\n"
                "要评估真实模型，请指向真实 OpenAI 兼容端点。",
                title="关于本次对比的结论适用范围",
                border_style="yellow",
            )
        )
    console.print(
        Panel(
            f"事实字段：{'✅ 全部一致' if comparison.facts_are_invariant else '❌ 存在不一致'}（{len(comparison.fact_fields)} 项）\n"
            f"LLM：{'已生效' if comparison.llm_available else '未生效'}　"
            f"模型 {comparison.llm_meta.get('model', '-')}　"
            f"策略 {comparison.llm_meta.get('strategy', '-')}　"
            f"耗时 {comparison.llm_meta.get('duration_ms', '-')} ms　"
            f"tokens {comparison.llm_meta.get('total_tokens', '-')}",
            title="对比结论",
            border_style="green" if comparison.facts_are_invariant else "red",
        )
    )

    # 构建警告必须显示出来：早期版本把「LLM 为什么没生效」吞掉了，
    # 用户只看到「右侧与左侧一模一样」，完全无从下手排查。
    llm_meta = comparison.llm_meta or {}
    if comparison.llm_available:
        details: list[str] = []
        reasoning = int(llm_meta.get("reasoning_chars") or 0)
        if reasoning:
            details.append(f"思维链 {reasoning} 字（正文取自 {llm_meta.get('content_source', 'content')}）")
        if llm_meta.get("truncated"):
            details.append(f"首轮被 max_tokens 截断，已提升到 {llm_meta.get('max_tokens_used')}")
        if llm_meta.get("attempts"):
            details.append("降级记录：" + "；".join(llm_meta["attempts"]))
        if details:
            console.print("[dim]" + "\n".join(details) + "[/dim]")
    else:
        console.print(
            f"[yellow]⚠ LLM 未生效：[/yellow]{llm_meta.get('reason', '原因未知')}"
        )

    if comparison.warnings:
        console.print("[bold]构建警告：[/bold]")
        for warning in comparison.warnings:
            console.print(f"  [yellow]·[/yellow] {warning}")


def _clip_cell(value: str, limit: int = 40) -> str:
    text = (value or "-").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _step_status(status: str) -> str:
    return {
        "success": "[green]✓[/green]",
        "failed": "[red]✗[/red]",
        "skipped": "[yellow]–[/yellow]",
        "running": "[cyan]…[/cyan]",
        "pending": "[dim]·[/dim]",
    }.get(status, status)


def _status_text(status: str | None) -> str:
    return {
        "completed": "[green]完成[/green]",
        "partial": "[yellow]部分完成[/yellow]",
        "ready": "[cyan]就绪[/cyan]",
        "failed": "[red]失败[/red]",
    }.get(status or "", status or "-")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _time_short(value) -> str:
    """表格用的短时间戳（mm-dd HH:MM），避免列换行。"""
    if value is None:
        return "-"
    try:
        return value.strftime("%m-%d %H:%M")
    except AttributeError:
        return str(value)[:11]


def _short_id(session_id: str, length: int = 8) -> str:
    return session_id if len(session_id) <= length else session_id[:length]


def _blob(records) -> str:
    import json

    chunks = []
    for record in records:
        try:
            chunks.append(json.dumps(record, ensure_ascii=False))
        except Exception:
            continue
    return "\n".join(chunks)


def _session_meta(raw):
    from amt.core.models import SessionMetadata

    return SessionMetadata(agent=raw.agent, session_id=raw.session_id, cwd=raw.cwd, source=raw.path)


if __name__ == "__main__":
    app()
