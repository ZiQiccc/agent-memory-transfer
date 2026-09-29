"""GUI：生成自包含 HTML 报告。

为什么先做**静态报告**而不是桌面 GUI：
`实现plan.md` §三十三 把 GUI 列在 P1，而 POC 阶段真正缺的是「能一眼看清
Memory 里到底写了什么、迁移到底做了什么」。一份无需安装、双击即开、
可离线查看的 HTML 报告就能满足这个需求，成本远低于 Tauri/Electron 工程。

报告包含五块：

    概览       Agent 能力矩阵、可用迁移组合
    会话       各 Agent 的会话列表（可直接看到哪些会话可迁移）
    记忆       已生成的 Canonical Memory（结构化浏览）
    迁移历史   每一条迁移记录与它的步骤耗时
    完整对话   从已脱敏的会话快照还原事件时间线（把 Memory 与原始记录对照）

设计约束：
- **零外部依赖**：不引 CDN、不联网，所有 CSS/JS 内联，数据以 JSON 内嵌；
- **只读**：报告不修改任何数据，也不包含未脱敏内容（读的是已脱敏快照）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amt.config import AMTConfig
from amt.core.models import CanonicalMemory
from amt.core.storage import Storage
from amt.utils import now_local

#: 单个会话最多还原多少条事件（防止报告膨胀）
MAX_EVENTS_PER_SESSION = 400


# ----------------------------------------------------------------------
# 数据组装
# ----------------------------------------------------------------------


def build_report_data(
    config: AMTConfig,
    storage: Storage,
    registry=None,
    include_live_sessions: bool = True,
    live_session_limit: int = 20,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "generated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "home_dir": str(config.home_dir),
        "agents": [],
        "migration_pairs": [],
        "sessions": [],
        "memories": [],
        "migrations": [],
        "conversations": [],
        "notes": [],
    }

    if registry is not None:
        for installation in registry.installations():
            data["agents"].append(
                {
                    "agent": installation.agent,
                    "display_name": installation.display_name or installation.agent,
                    "installed": installation.installed,
                    "runtime_available": installation.runtime_available,
                    "source_supported": installation.source_supported,
                    "target_supported": installation.target_supported,
                    "version": installation.version,
                    "executable": installation.executable,
                    "data_dir": installation.data_dir,
                    "notes": installation.notes,
                }
            )
        data["migration_pairs"] = [list(pair) for pair in registry.migration_pairs()]

        if include_live_sessions:
            for agent in registry.source_agents:
                try:
                    source = registry.source(agent)
                    for session in source.list_sessions(limit=live_session_limit):
                        data["sessions"].append(
                            {
                                "agent": agent,
                                "session_id": session.session_id,
                                "title": session.title,
                                "cwd": session.cwd,
                                "updated_at": _iso(session.updated_at or session.created_at),
                                "size_bytes": session.size_bytes,
                                "resumable": session.resumable,
                                "archived": session.archived,
                                "migratable": bool(registry.target_agents),
                            }
                        )
                except Exception as exc:
                    data["notes"].append(f"{agent} 会话枚举失败：{exc}")

    # ---- 记忆 ----
    for item in storage.list_memories(limit=200):
        memory_id = item.get("memory_id")
        if not memory_id:
            continue
        try:
            memory = storage.load_memory(memory_id)
        except Exception as exc:
            data["notes"].append(f"读取记忆 {memory_id} 失败：{exc}")
            continue
        data["memories"].append(_memory_digest(memory, item))

    # ---- 迁移历史 ----
    # list_migrations() 只返回轻量索引，报告需要 steps / secret_findings / options
    # 等明细，因此逐条载入完整记录（索引本身就指向同一批文件）。
    for summary in storage.list_migrations(limit=200):
        migration_id = summary.get("migration_id")
        record: dict[str, Any] = summary
        if migration_id:
            try:
                loaded = storage.load_migration(migration_id)
                record = loaded.model_dump(mode="json")
                record["path"] = summary.get("path")
            except Exception as exc:
                data["notes"].append(f"读取迁移记录 {migration_id} 失败：{exc}")
        record.setdefault("steps", [])
        record.setdefault("warnings", [])
        record.setdefault("errors", [])
        record.setdefault("secret_findings", [])
        record.setdefault("options", {})
        data["migrations"].append(record)

    # ---- 会话快照（完整对话）----
    data["conversations"] = _load_conversations(config, storage)

    return data


def _memory_digest(memory: CanonicalMemory, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": memory.metadata.memory_id,
        "source_agent": memory.metadata.source_agent,
        "source_session_id": memory.metadata.source_session_id,
        "created_at": _iso(memory.metadata.created_at),
        "updated_at": item.get("updated_at"),
        "path": item.get("path"),
        "project": {
            "name": memory.project.name,
            "path": memory.project.path,
            "language": memory.project.language,
            "framework": memory.project.framework,
        },
        "task": memory.task.model_dump(mode="json"),
        "conversation": memory.conversation.model_dump(mode="json"),
        "implementation": memory.implementation.model_dump(mode="json"),
        "decisions": [d.model_dump(mode="json") for d in memory.decisions],
        "attempts": [a.model_dump(mode="json") for a in memory.attempts],
        "validation": memory.validation.model_dump(mode="json"),
        "git": memory.git.model_dump(mode="json"),
        "unresolved": [i.model_dump(mode="json") for i in memory.unresolved],
        "next_actions": [a.model_dump(mode="json") for a in memory.next_actions],
        "risks": [r.model_dump(mode="json") for r in memory.risks],
        "runtime": memory.runtime.model_dump(mode="json"),
        "stats": memory.stats,
        "markdown": memory.metadata.memory_id,
    }


def _load_conversations(config: AMTConfig, storage: Storage) -> list[dict[str, Any]]:
    """从已脱敏的会话快照还原事件时间线。"""
    from amt.core.models import RawSession

    parsers = _parsers()
    out: list[dict[str, Any]] = []
    sessions_root = config.sessions_dir
    if not sessions_root.is_dir():
        return out

    for raw_path in sorted(sessions_root.rglob("raw.json")):
        relative = raw_path.relative_to(sessions_root).parts
        agent = relative[0] if relative else "unknown"
        session_id = relative[1] if len(relative) > 1 else raw_path.parent.name
        try:
            records = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(records, list):
            continue

        meta_path = raw_path.with_name("metadata.json")
        metadata: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}

        parser = parsers.get(agent)
        events: list[dict[str, Any]] = []
        if parser is not None:
            raw_session = RawSession(
                agent=agent,
                session_id=session_id,
                path=str(raw_path),
                cwd=metadata.get("cwd"),
                records=records,
                total_records=len(records),
            )
            try:
                parsed = parser.parse(raw_session)
                from amt.core.memory.normalizer import Normalizer

                normalized = Normalizer().normalize(parsed.events)
                for event in normalized[:MAX_EVENTS_PER_SESSION]:
                    events.append(
                        {
                            "timestamp": _iso(event.timestamp),
                            "type": event.type.value,
                            "category": event.category,
                            "role": event.role,
                            "summary": event.summary,
                            "content": (event.content or "")[:4000],
                            "result": (event.result or "")[:4000],
                            "is_failure": event.is_failure,
                            "tool_name": event.tool_name,
                        }
                    )
            except Exception as exc:
                events = [{"type": "error", "summary": f"事件还原失败：{exc}"}]

        out.append(
            {
                "agent": agent,
                "session_id": session_id,
                "cwd": metadata.get("cwd"),
                "record_count": len(records),
                "parse_errors": metadata.get("parse_errors"),
                "events": events,
                "truncated": len(events) >= MAX_EVENTS_PER_SESSION,
            }
        )
    return out


def _parsers() -> dict[str, Any]:
    from amt.adapters.claude import ClaudeCodeParser
    from amt.adapters.codex import CodexParser
    from amt.adapters.cursor import CursorParser

    return {
        "codex": CodexParser(),
        "claude": ClaudeCodeParser(),
        "cursor": CursorParser(),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


# ----------------------------------------------------------------------
# HTML 渲染
# ----------------------------------------------------------------------


def write_report(
    config: AMTConfig,
    storage: Storage,
    registry=None,
    out_path: Path | None = None,
    include_live_sessions: bool = True,
) -> Path:
    """生成报告。

    ``include_live_sessions=False`` 时只渲染已落盘的数据（记忆/迁移/快照），
    不枚举本机各 Agent 的现存会话 —— 用于生成可随仓库外发的示例报告。
    """
    data = build_report_data(
        config, storage, registry, include_live_sessions=include_live_sessions
    )
    html = render_html(data)
    target = Path(out_path) if out_path else (config.home_dir / "report.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__AMT_DATA__", payload)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Memory Transfer · 报告</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#ffffff; --ink:#1c2024; --muted:#6b7280;
    --line:#e3e6ea; --accent:#2563eb; --ok:#15803d; --warn:#b45309; --bad:#b91c1c;
    --chip:#eef2f7;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
  header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 24px}
  h1{margin:0 0 4px;font-size:19px;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px}
  nav{display:flex;gap:6px;padding:12px 24px 0;flex-wrap:wrap}
  nav button{border:1px solid var(--line);background:var(--panel);color:var(--ink);
    padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
  nav button.active{background:var(--accent);border-color:var(--accent);color:#fff}
  main{padding:16px 24px 48px;max-width:1240px}
  section{display:none} section.active{display:block}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:16px 18px;margin-bottom:14px}
  .card h2{margin:0 0 10px;font-size:15px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600;background:#fafbfc;position:sticky;top:0}
  tr.clickable{cursor:pointer} tr.clickable:hover{background:#f2f6ff}
  code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px;
    font-family:ui-monospace,Consolas,monospace}
  .muted{color:var(--muted)}
  .chip{display:inline-block;background:var(--chip);border-radius:999px;
    padding:1px 9px;font-size:12px;margin-right:4px}
  .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
  .kv{display:grid;grid-template-columns:112px 1fr;gap:4px 10px;font-size:13px}
  .kv div:nth-child(odd){color:var(--muted)}
  pre{white-space:pre-wrap;word-break:break-word;background:#fafbfc;border:1px solid var(--line);
    border-radius:8px;padding:10px;font-size:12.5px;margin:6px 0;max-height:420px;overflow:auto}
  .evt{border-left:3px solid var(--line);padding:6px 10px;margin-bottom:6px;background:#fcfcfd}
  .evt.user{border-color:#2563eb} .evt.assistant{border-color:#7c3aed}
  .evt.test{border-color:#15803d} .evt.terminal{border-color:#0891b2}
  .evt.file_edit{border-color:#c2410c} .evt.error{border-color:#b91c1c}
  .evt .head{font-size:12px;color:var(--muted)}
  .fail{color:var(--bad);font-weight:600}
  .banner{background:#fffbeb;border:1px solid #fde68a;color:#92400e;
    padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:13px}
  details{margin-top:6px} summary{cursor:pointer;color:var(--accent);font-size:13px}
  .scroll{max-height:520px;overflow:auto}
</style>
</head>
<body>
<header>
  <h1>Agent Memory Transfer · 报告</h1>
  <div class="sub" id="subtitle"></div>
</header>
<nav id="tabs"></nav>
<main>
  <section id="tab-overview" class="active"></section>
  <section id="tab-sessions"></section>
  <section id="tab-memories"></section>
  <section id="tab-migrations"></section>
  <section id="tab-conversations"></section>
</main>
<script id="amt-data" type="application/json">__AMT_DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('amt-data').textContent);
const esc = s => (s==null? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yes = v => v ? '<span class="ok">✓</span>' : '<span class="muted">–</span>';
const fmtTime = s => s ? String(s).replace('T',' ').slice(0,19) : '-';

document.getElementById('subtitle').innerHTML =
  `生成时间 ${esc(DATA.generated_at)} · 数据目录 <code>${esc(DATA.home_dir)}</code> · `
  + `${DATA.memories.length} 份记忆 · ${DATA.migrations.length} 条迁移记录 · `
  + `${DATA.sessions.length} 个可迁移会话`;

const TABS = [
  ['overview','概览'], ['sessions','会话'], ['memories','记忆'],
  ['migrations','迁移历史'], ['conversations','完整对话']
];
document.getElementById('tabs').innerHTML = TABS.map(([id,label],i)=>
  `<button data-tab="${id}" class="${i===0?'active':''}">${label}</button>`).join('');
document.getElementById('tabs').addEventListener('click', e=>{
  const btn = e.target.closest('button'); if(!btn) return;
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active', b===btn));
  TABS.forEach(([id])=>{ document.getElementById('tab-'+id).classList.toggle('active', id===btn.dataset.tab); });
});

/* ---------- 概览 ---------- */
(function(){
  const rows = DATA.agents.map(a=>`<tr>
    <td><strong>${esc(a.display_name)}</strong></td>
    <td>${yes(a.installed)}</td><td>${yes(a.runtime_available)}</td>
    <td>${yes(a.source_supported)}</td><td>${yes(a.target_supported)}</td>
    <td class="muted">${esc(a.version||'-')}</td>
    <td class="muted">${esc(a.data_dir||a.executable||'-')}</td>
  </tr>`).join('');
  const notes = DATA.agents.filter(a=>a.notes && a.notes.length).map(a=>
    `<details><summary>${esc(a.display_name)} 说明</summary><ul>${
      a.notes.map(n=>`<li class="muted">${esc(n)}</li>`).join('')}</ul></details>`).join('');
  const pairs = DATA.migration_pairs.map(p=>`<span class="chip">${esc(p[0])} → ${esc(p[1])}</span>`).join(' ');
  const warn = DATA.notes.length ? `<div class="banner">${DATA.notes.map(esc).join('<br>')}</div>` : '';
  document.getElementById('tab-overview').innerHTML = `
    ${warn}
    <div class="card"><h2>Agent 能力矩阵</h2>
      <table><thead><tr><th>Agent</th><th>已安装</th><th>数据可读</th><th>作为来源</th>
      <th>作为目标</th><th>版本</th><th>数据位置</th></tr></thead><tbody>${rows}</tbody></table>
      ${notes}
    </div>
    <div class="card"><h2>可用迁移组合（${DATA.migration_pairs.length} 条）</h2>
      <p class="muted">由 Canonical Memory 中间协议推导：来源数 × 目标数，而不是为每一对单独开发。</p>
      <p>${pairs || '<span class="muted">无</span>'}</p>
    </div>`;
})();

/* ---------- 会话 ---------- */
(function(){
  if(!DATA.sessions.length){ document.getElementById('tab-sessions').innerHTML =
    '<div class="card"><p class="muted">暂无会话。</p></div>'; return; }
  const byAgent = {};
  DATA.sessions.forEach(s=>{ (byAgent[s.agent] = byAgent[s.agent] || []).push(s); });
  const html = Object.keys(byAgent).sort().map(agent=>{
    const rows = byAgent[agent].map(s=>`<tr>
      <td>${esc(s.title||'-')}</td>
      <td class="muted">${esc(s.cwd||'-')}</td>
      <td class="muted">${fmtTime(s.updated_at)}</td>
      <td class="muted">${Math.round((s.size_bytes||0)/1024)} KB</td>
      <td><code>${esc((s.session_id||'').slice(0,8))}</code></td>
    </tr>`).join('');
    return `<div class="card"><h2>${esc(agent)} · ${byAgent[agent].length} 个会话</h2>
      <div class="scroll"><table><thead><tr><th>标题</th><th>工作目录</th><th>时间</th>
      <th>大小</th><th>会话 ID</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }).join('');
  document.getElementById('tab-sessions').innerHTML = html;
})();

/* ---------- 记忆 ---------- */
let memoryIndex = 0;
function renderMemories(){
  const el = document.getElementById('tab-memories');
  if(!DATA.memories.length){ el.innerHTML = '<div class="card"><p class="muted">暂无记忆。</p></div>'; return; }
  const list = DATA.memories.map((m,i)=>`<tr class="clickable" data-i="${i}">
    <td><code>${esc(m.memory_id)}</code></td>
    <td>${esc(m.source_agent)}</td>
    <td>${esc(m.task.title||'-')}</td>
    <td>${esc(m.task.confidence||'-')}</td>
    <td class="muted">${esc((m.task.reconstructed_by||'-')).slice(0,24)}</td>
    <td class="muted">${fmtTime(m.created_at)}</td>
  </tr>`).join('');
  el.innerHTML = `<div class="card"><h2>已生成的 Canonical Memory（${DATA.memories.length}）</h2>
    <div class="scroll"><table><thead><tr><th>memory_id</th><th>来源</th><th>标题</th>
    <th>置信度</th><th>重建方式</th><th>生成时间</th></tr></thead><tbody>${list}</tbody></table></div></div>
    <div id="memory-detail"></div>`;
  el.querySelectorAll('tr.clickable').forEach(tr=>tr.addEventListener('click',()=>{
    memoryIndex = +tr.dataset.i; renderMemoryDetail();
  }));
  renderMemoryDetail();
}
function renderMemoryDetail(){
  const m = DATA.memories[memoryIndex]; const el = document.getElementById('memory-detail');
  if(!m){ el.innerHTML=''; return; }
  const failed = (m.attempts||[]).filter(a=>!a.success);
  const sec = (title, body) => body ? `<div class="card"><h2>${title}</h2>${body}</div>` : '';
  const ul = arr => arr && arr.length ? `<ul>${arr.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` :
    '<p class="muted">（无）</p>';
  const failedHtml = failed.length ? `<table><thead><tr><th>动作</th><th>结果</th><th>报错</th>
      <th>经验</th></tr></thead><tbody>${failed.map(a=>`<tr>
      <td>${esc(a.action)}</td><td>${esc(a.result)}</td>
      <td class="muted">${esc(a.error||'-')}</td><td class="muted">${esc(a.lesson||'-')}</td>
      </tr>`).join('')}</tbody></table>` : '';
  el.innerHTML = `
    <div class="card"><h2>${esc(m.task.title||m.memory_id)}</h2>
      <div class="kv">
        <div>memory_id</div><div><code>${esc(m.memory_id)}</code></div>
        <div>来源</div><div>${esc(m.source_agent)} · <code>${esc(m.source_session_id||'-')}</code></div>
        <div>项目</div><div>${esc(m.project.name||'-')} <span class="muted">${esc(m.project.path||'')}</span></div>
        <div>状态</div><div>${esc(m.task.status)}　置信度 ${esc(m.task.confidence)}　
          重建 ${esc(m.task.reconstructed_by||'-')}</div>
        <div>目标</div><div>${esc(m.task.goal||'-')}</div>
      </div>
    </div>
    ${sec('需求', ul(m.task.requirements))}
    ${sec('约束', ul(m.task.constraints))}
    ${sec('已完成的工作', ul(m.implementation.completed))}
    ${sec('已修改文件', (m.implementation.modified_files||[]).length ?
      `<table><thead><tr><th>文件</th><th>状态</th><th>说明</th></tr></thead><tbody>${
      m.implementation.modified_files.map(f=>`<tr><td><code>${esc(f.path)}</code></td>
      <td>${esc(f.status)}</td><td class="muted">${esc(f.summary)}</td></tr>`).join('')}</tbody></table>` : '')}
    ${sec('已做出的关键决策', (m.decisions||[]).length ?
      `<ul>${m.decisions.map(d=>`<li>${esc(d.decision)}
        ${d.reason?`<span class="muted">（${esc(d.reason)}）</span>`:''}</li>`).join('')}</ul>` : '')}
    ${sec(`已尝试但失败的方案（${failed.length}）`, failedHtml)}
    ${sec('当前未解决的问题', (m.unresolved||[]).length ?
      `<ul>${m.unresolved.map(i=>`<li><span class="bad">[${esc(i.priority)}]</span>
        ${esc(i.description)}${i.suspected_cause?`<span class="muted"> — ${esc(i.suspected_cause)}</span>`:''}</li>`).join('')}</ul>` : '')}
    ${sec('下一步行动', (m.next_actions||[]).length ?
      `<ol>${m.next_actions.map(a=>`<li>${esc(a.action)}</li>`).join('')}</ol>` : '')}
    ${sec('验证结果', (m.validation.tests||[]).length ?
      `<table><thead><tr><th>命令</th><th>结果</th><th>摘要</th></tr></thead><tbody>${
      m.validation.tests.map(t=>`<tr><td><code>${esc(t.command)}</code></td>
      <td>${t.status==='passed'?'<span class="ok">通过</span>':'<span class="bad">失败</span>'}</td>
      <td class="muted">${esc(t.output_summary||'-')}</td></tr>`).join('')}</tbody></table>` : '')}
    ${sec('Git 状态', `<div class="kv"><div>分支</div><div>${esc(m.git.branch||'-')}</div>
      <div>HEAD</div><div><code>${esc(m.git.commit||'-')}</code></div>
      <div>状态</div><div>${esc(m.git.status_summary||'-')}</div></div>`)}
    ${sec('风险提示', (m.risks||[]).length ?
      `<ul>${m.risks.map(r=>`<li>${esc(r.description)}
        ${r.mitigation?`<span class="muted"> — 建议：${esc(r.mitigation)}</span>`:''}</li>`).join('')}</ul>` : '')}
  `;
}
renderMemories();

/* ---------- 迁移历史 ---------- */
(function(){
  if(!DATA.migrations.length){ document.getElementById('tab-migrations').innerHTML =
    '<div class="card"><p class="muted">暂无迁移记录。</p></div>'; return; }
  const rows = DATA.migrations.map(m=>{
    const steps = (m.steps||[]).map(s=>`<tr><td>${esc(s.name)}</td>
      <td>${s.status==='success'?'<span class="ok">✓</span>':(s.status==='failed'?'<span class="bad">✗</span>':'–')}</td>
      <td class="muted">${s.duration_ms==null?'-':s.duration_ms+' ms'}</td>
      <td class="muted">${esc(s.detail||s.error||'')}</td></tr>`).join('');
    return `<div class="card">
      <h2>${esc(m.source_agent)} → ${esc(m.target_agent)}
        ${m.dry_run?'<span class="chip">dry-run</span>':''}
        <span class="chip">${esc(m.status)}</span></h2>
      <div class="kv">
        <div>记录号</div><div><code>${esc(m.migration_id)}</code></div>
        <div>会话</div><div><code>${esc(m.source_session_id||'-')}</code></div>
        <div>记忆</div><div><code>${esc(m.memory_id||'-')}</code>
          <span class="muted">(${Math.round((m.memory_size_bytes||0)/1024)} KB)</span></div>
        <div>项目</div><div>${esc(m.project_path||'-')}</div>
        <div>时间</div><div>${fmtTime(m.timestamp)}</div>
        <div>敏感信息</div><div>${(m.secret_findings||[]).length} 处
          <span class="muted">（模式 ${esc((m.options||{}).redaction_mode||'balanced')}）</span></div>
      </div>
      ${(m.warnings||[]).length?`<div class="banner">${m.warnings.map(esc).join('<br>')}</div>`:''}
      ${(m.errors||[]).length?`<div class="banner"><span class="bad">${m.errors.map(esc).join('<br>')}</span></div>`:''}
      <details><summary>步骤（${(m.steps||[]).length}）</summary>
      <table><thead><tr><th>步骤</th><th>状态</th><th>耗时</th><th>说明</th></tr></thead>
      <tbody>${steps}</tbody></table></details>
    </div>`;
  }).join('');
  document.getElementById('tab-migrations').innerHTML = rows;
})();

/* ---------- 完整对话 ---------- */
(function(){
  const el = document.getElementById('tab-conversations');
  if(!DATA.conversations.length){
    el.innerHTML = `<div class="card"><p class="muted">暂无会话快照。</p>
      <p class="muted">运行一次 <code>amt migrate</code> 或 <code>amt extract</code> 后，
      会话快照（已脱敏）会保存在数据目录，这里就能对照查看完整对话。</p></div>`;
    return;
  }
  const options = DATA.conversations.map((c,i)=>
    `<option value="${i}">${esc(c.agent)} · ${esc((c.session_id||'').slice(0,8))} · ${c.events.length} 事件</option>`).join('');
  el.innerHTML = `<div class="card"><h2>会话事件时间线</h2>
    <p class="muted">数据来自已脱敏的会话快照，经 Parser + Normalizer 还原——
    可与上面「记忆」页的内容逐条对照，验证记忆是否忠实于原始记录。</p>
    <select id="conv-select" style="padding:6px 10px;border-radius:8px;border:1px solid var(--line)">
      ${options}</select>
    <div id="conv-body" style="margin-top:12px"></div></div>`;
  const select = document.getElementById('conv-select');
  const render = () => {
    const c = DATA.conversations[+select.value]; if(!c) return;
    const body = c.events.map(e=>{
      const cls = (e.category||e.type||'').replace(/[^a-z_]/g,'');
      const head = `${esc(e.type)}${e.category?` / ${esc(e.category)}`:''}`
        + `${e.tool_name?` · ${esc(e.tool_name)}`:''}`
        + `${e.is_failure?' <span class="fail">失败</span>':''}`
        + `　<span>${fmtTime(e.timestamp)}</span>`;
      const text = e.summary ? `<div>${esc(e.summary)}</div>` : '';
      const content = e.content && e.content !== e.summary
        ? `<details><summary>原文</summary><pre>${esc(e.content)}</pre></details>` : '';
      const result = e.result ? `<details><summary>输出</summary><pre>${esc(e.result)}</pre></details>` : '';
      return `<div class="evt ${cls}"><div class="head">${head}</div>${text}${content}${result}</div>`;
    }).join('');
    document.getElementById('conv-body').innerHTML =
      `<div class="kv"><div>工作目录</div><div>${esc(c.cwd||'-')}</div>
        <div>原始记录</div><div>${c.record_count} 条${c.truncated?'（事件已截断）':''}</div></div>
       <div style="margin-top:10px">${body}</div>`;
  };
  select.addEventListener('change', render); render();
})();
</script>
</body>
</html>
"""
