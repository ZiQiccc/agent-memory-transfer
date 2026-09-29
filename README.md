# Agent Memory Transfer (AMT)

跨 Coding Agent 的任务记忆迁移工具。把某个 Agent 会话中**已经完成的工作、失败过的方案、
未解决的问题和下一步行动**，归一成一份与 Agent 无关的 `Canonical Memory`，
再注入到另一个 Agent，让它从中断处继续，而不是重新分析项目。

```text
Codex / Claude Code / Cursor 会话
   ↓ Source Adapter（理解各自私有格式）
AgentEvent[]                        ← 跨 Agent 第一层协议
   ↓ Normalizer
NormalizedEvent[]
   ↓ Compress + Extract（确定性重建 + 可选 LLM 归纳）
Canonical Memory                    ← 跨 Agent 唯一事实协议
   ↓ Target Adapter
.agent-transfer/memory.md + CLAUDE.md(@导入) 或 AGENTS.md(内联) + 初始 Prompt
   ↓
目标 Agent 接着干
```

---

## 1. 实现范围

| 能力 | 状态 |
| --- | --- |
| **Source** | ✅ Codex（rollout JSONL）、✅ Claude Code（projects JSONL）、✅ Cursor（state.vscdb）、✅ WorkBuddy（projects JSONL） |
| **Target** | ✅ Claude Code（`CLAUDE.md` + `@` 导入）、✅ Codex（`AGENTS.md` 内联）、❌ Cursor / WorkBuddy（无**已验证**的注入入口，如实标注不支持） |
| 迁移组合 | 4 来源 × 2 目标 = **6 条**，由中间协议推导，无需为每一对单独开发 |
| Memory Engine | 归一化 / 压缩 / 确定性重建 / 校验 / 渲染 / 脱敏 |
| LLM 通道 | ✅ OpenAI 兼容（中转站 / DeepSeek / 通义 / 本地 vLLM / Ollama / One-API），带能力降级阶梯 + `amt llm-check` 连通性自检 |
| 质量对比 | ✅ `amt compare` 启发式 vs LLM，并校验「事实字段不变量」 |
| GUI | ✅ `amt report` 生成自包含 HTML（概览 / 会话 / 记忆 / 迁移历史 / 完整对话） |
| 未实现 | Mimo Code / OpenCode / Antigravity 的 Adapter；桌面端 GUI（用 HTML 报告替代） |

`amt agents` 会**如实列出**未实现的 Agent 与能力缺口，不会让人误以为都支持。

> **隐私边界**：各 Adapter 只在**用户显式调用** `amt` 并指定该 Agent 为来源时读取本机数据；
> 工具自身不会主动扫描，也不会把会话内容发往外部服务（除用户自行开启的 LLM 通道外）。
> `examples/` 下的示例产物全部由合成数据生成，可安全随仓库外发。

---

## 2. 安装与运行

```bash
cd agent-memory-transfer
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -e ".[dev]"
```

> **安装依赖若报 `No matching distribution found`**：检查是否设置了 `HTTPS_PROXY`。
> 本机实测该代理会让清华镜像返回空索引，改用官方源即可：
> `pip install -i https://pypi.org/simple -e ".[dev]"`。

### 常用命令

```bash
amt agents                      # Agent 能力矩阵（-v 看每个 Agent 的详细说明）
amt sessions codex              # 列会话（也可 claude / cursor / workbuddy）
amt extract codex <session-id>  # 只生成 Canonical Memory
amt preview <memory-id>         # 查看已生成的 Memory
amt migrate --from codex --to claude --dry-run     # 只看不写
amt migrate --from codex --to claude --session <id>
amt llm-check --llm-base-url ... --llm-model ...   # 验证 LLM 端点（真实最小调用）
amt compare --session <id> --llm --llm-base-url ... --llm-model ...   # 质量对比
amt report                      # 生成 HTML 报告并打开
amt history                     # 迁移历史
amt config [--init]             # 查看 / 初始化配置
```

`amt migrate` 主要参数：

```text
--from / --to            来源 / 目标 Agent
--session, -s <id>       指定会话（默认最近一条；支持 ID 前缀）
--dry-run                只跑到「生成目标上下文」，不注入不启动
--yes, -y                跳过 Memory Preview 确认
--no-launch              不自动启动目标 Agent
--no-inject              不写入项目目录
--project-root <dir>     覆盖注入目标目录（默认取会话记录的 cwd）
--llm / --no-llm         是否用 LLM 归纳
--include <范围>         只迁移 project/task/conversation/runtime/git
--no-redact              关闭脱敏（不推荐）
```

### 启用 LLM

```bash
# 方式一：配置文件（把 llm.enabled 改为 true，填 base_url 与 model）
export AMT_LLM_API_KEY=sk-xxxx        # 建议用环境变量，不要把 Key 写进配置文件
amt config --init
amt llm-check                          # 先自检：端点可达 / 鉴权 / 策略 / token

# 方式二：命令行直接指定（凭据不必落盘）
amt llm-check --llm-base-url https://your-relay.example.com/v1 \
  --llm-model gpt-4o-mini --llm-api-key sk-xxxx

# 没有凭据时，用内置模拟器验证链路（真实 HTTP 往返）
python tools/mock_llm_server.py --port 8181
amt llm-check --llm-base-url http://127.0.0.1:8181/v1 --llm-model mock-extractor
```

`amt llm-check` 会打印生效策略（`json_schema` / `json_object` / `plain`）、耗时与 tokens，
并在失败时给出排查顺序（base_url 是否要带 `/v1`、模型名、Key、代理）。
**建议在跑迁移前先过一遍这一步**，否则配置问题会等到迁移中途才暴露。

> 配置文件写错时（YAML 语法错误、或 `workbuddy: project_dir` 这类**拼写错误**），
> 任何 `amt` 命令都会在开头显示醒目告警。早期版本会静默退回默认值，
> 表现为「设了却不生效」，排查成本极高。

---

## 3. 目录结构

```text
agent-memory-transfer/
├── src/amt/
│   ├── cli/main.py                    CLI（typer + rich）
│   ├── config.py / context.py / utils.py
│   ├── core/
│   │   ├── models/
│   │   │   ├── events.py              AgentEvent / NormalizedEvent —— 跨 Agent 第一层协议
│   │   │   ├── memory.py              CanonicalMemory —— 跨 Agent 唯一事实协议
│   │   │   └── migration.py           SessionInfo / MigrationOptions / 迁移记录
│   │   ├── memory/
│   │   │   ├── normalizer.py          跨 Agent 工具语义表 + 事件归一 + 回环防护
│   │   │   ├── compressor.py          聚合 / 裁剪 / token 预算
│   │   │   ├── heuristic.py           确定性任务状态重建（不依赖 LLM）
│   │   │   ├── extractor.py           Memory Engine 入口（确定性基线 + LLM 增强）
│   │   │   ├── llm_schema.py          LLM 输出契约（由 Pydantic 生成 JSON Schema）
│   │   │   ├── compare.py             启发式 vs LLM 质量对比 + 事实不变量校验
│   │   │   ├── prompts.py             Task State Reconstruction Prompt
│   │   │   ├── validator.py           Schema + Semantic 校验
│   │   │   └── renderer.py            memory.md / 初始 Prompt / 注入 Prompt 反解
│   │   └── migration/
│   │       ├── state_machine.py       迁移状态机
│   │       └── orchestrator.py        全链路编排
│   ├── adapters/
│   │   ├── base.py / registry.py      Adapter 抽象 + 能力目录 + 组合推导
│   │   ├── codex/                     detector / discovery / parser / source
│   │   │                              renderer / injector / launcher / target
│   │   ├── claude/                    detector / discovery / parser / source
│   │   │                              renderer / injector / launcher / target
│   │   ├── cursor/                    detector / parser / source（Source only）
│   │   └── workbuddy/                 detector / parser / source（Source only）
│   ├── services/                      git / filesystem / process / shell / security
│   ├── providers/llm.py               OpenAI 兼容 Provider（含能力降级阶梯）
│   └── gui/report.py                  自包含 HTML 报告
├── tools/
│   ├── generate_example.py            在沙箱里跑一遍链路并产出 examples/（合成数据）
│   └── mock_llm_server.py             OpenAI 兼容的规则模拟器（链路验证用）
├── examples/                          example-memory.md / report.html（均为合成数据）
└── tests/                             166 项测试，不依赖本机真实会话数据
```

### 不可违反的架构边界

```text
Agent 私有格式  →  只允许出现在 adapters/<agent>/ 内
Parser 只做结构转换 → 不生成 Memory、不调用 LLM
Normalizer 不调用 LLM
LLM  →  理解 / 归纳；程序  →  读取 / 写入 / 执行 / 启动 / 校验
```

这些边界由测试固化，例如 `test_target_choice_does_not_change_memory`
直接断言「换目标 Agent 后 Canonical Memory 逐字节不变」。

---

## 4. Canonical Memory 协议

`src/amt/core/models/memory.py` 是协议的唯一定义处。顶层结构：

```text
metadata / project / task / conversation / implementation
decisions / attempts / validation / git / unresolved / next_actions / risks / runtime / stats
```

三个高优先级字段（也是本工具区别于「聊天记录导出」的地方）：

- `attempts`（`success=false`）—— **Negative Knowledge**。目标 Agent 必须知道
  「哪些方案已经试过、为什么失败」，否则会重复执行同一方案。
- `unresolved` —— 会话结束时仍未解决的问题。
- `next_actions` —— 明确的下一步，成功迁移的标志就是目标 Agent 从这里继续。

### Agent 无关性

`memory.py` 中不允许出现任何 `codex_*` / `claude_*` / `cursor_*` 字段。
Agent 差异全部由 Adapter 在自己的目录内消化。

### 冲突处理原则

Memory 只描述状态，**真实状态优先**：

```text
真实文件系统 > Git 状态 > 运行时状态 > Canonical Memory > 会话摘要
```

这条原则写进了 `memory.md` 的最后一节，目标 Agent 读到的第一手材料就包含它。

---

## 5. 关键设计决策

以下 9 项是实现选择，其中带 **（偏离）** 的属于对文档的有意偏离，均在此说明理由。

### 5.1 增加「确定性重建」通道（偏离）

文档假设由 LLM 完成记忆提取（技术架构 §22 / §43），但实测环境**没有可用的 LLM 凭据**。
若只实现 LLM 路径，整条链路当天就不可运行，POC 的核心命题也无法验证。

因此 Memory Engine 采用 **「确定性基线 + LLM 语义增强」**：

1. 先用 `HeuristicReconstructor` 生成一份字段完整、可离线运行的 Memory；
2. LLM 可用时，用它的归纳结果覆盖**语义字段**；
3. **事实字段永不被 LLM 覆盖** —— 项目、Git、运行时、测试结果一律来自程序读取，
   而且 LLM 的输出契约里**根本不含**这些字段（见 `llm_schema.py`）。

LLM 调用失败时自动回退，并把原因写进 warnings。重建方式与置信度会写进
`task.reconstructed_by` / `task.confidence` 并在 CLI 与报告中展示。

### 5.2 Memory 本体也必须脱敏（偏离，强化）

文档只要求扫描 Raw Session（需求文档 §25）。但 `memory.md` / `memory.json` 会被
**写入项目目录并交给目标 Agent**，若只脱敏落盘的会话快照，凭据仍会随注入产物
泄漏到项目工作区。因此本实现把脱敏做成两道：会话快照 + Memory 本体。

同时做了**误报抑制**：中置信度规则要求值本身可信（长度 ≥ 12 或含数字，
且非 `Bearer` 这类字面量），否则 `private static final String TOKEN_TYPE = "Bearer";`
这种源码常量会被误伤。

### 5.3 EventType 增加 2 个扩展类型（偏离）

`实现plan.md` §八 规定的 10 个规范事件类型全部保留，另加 `SESSION_META` / `REASONING`
两个**辅助**扩展：会话元信息与模型思考摘要在真实数据中确实存在，丢弃会同时丢掉
决策依据。扩展类型不参与协议契约，Target Adapter 不得依赖。

### 5.4 Codex 侧必须**内联**上下文，Claude 侧才能用 `@` 导入

这是两个 Target 的**实质差异**，也是最能说明「Agent 差异由 Adapter 处理」的例子：

| | Claude Code | Codex |
| --- | --- | --- |
| 上下文机制 | `CLAUDE.md` 支持 `@path` 导入 | `AGENTS.md` 是纯文本，**不解析 `@`** |
| 因此 | 写一行 `@.agent-transfer/memory.md` | 必须把核心上下文**内联**进标记段，并指明完整记忆文件路径 |

内联段落受长度约束（默认 ≤2600 字符），因为它会在**每个会话**被加载；
装配优先级为「任务 → 失败方案 → 未解决 → 下一步 → 已完成 → 决策」——
失败方案的优先级高于已完成工作：重复劳动只是浪费，重走失败的路会把任务带偏。

### 5.5 Cursor 只做 Source，如实标注不做 Target

Cursor 没有官方的上下文注入入口，可行手段只剩「项目规则文件 + 剪贴板/UI 自动化」，
属技术架构 §36 里优先级最低的两档。与其提供一个假装能用的 Target Adapter，
不如在能力目录里如实标注 `target_supported=False`。

### 5.6 回环防护：注入的 Prompt 不能变成下一轮的「用户需求」

场景：Codex → Claude → Codex。第二轮迁移时，目标 Agent 会话的第一条「用户消息」
其实就是**我们注入的初始 Prompt**。实测确认：不处理的话新一轮的记忆会把
`你正在继续一个已经进行中的开发任务…## 任务目标…` 当成用户目标，目标字段直接废掉。

解法在 Normalizer 层（`unwrap_injected_prompt`）：识别本工具的 Prompt 并还原其中的
真实目标。放在归一化层而不是各个消费点，是为了让下游全部自动正确。

### 5.7 探测必须零副作用

早期版本用 `claude -p ping` 探测登录状态 —— 结果**在用户的 `~/.claude/projects` 里
留下了垃圾会话**（实测发现并已清理）。现在改为**证据式判断**：读最近一条会话，
看它是否以 `isApiErrorMessage` 收尾。既不发起请求，也不需要凭据。

同类约束也写进了测试：`ctx` 夹具把 CLI 路径指向不存在的文件，
保证测试永远不会启动真实 Agent。

### 5.8 失败判定遵循命令语义，而不是只看退出码

`rg` / `grep` / `Select-String` 未匹配到结果时返回退出码 1，这是**正常返回**。
不处理时一次真实会话的「失败尝试」会从 6 条膨胀到 21 条（15 条噪音），
`unresolved` 与 `next_actions` 也会被污染。该语义表位于 `normalizer.py`，由测试锁定。

### 5.9 待办必须是祈使式，而不是「含有动词的句子」

实测教训：`钩子：sessionStart 会在会话开始时**运行**，从第一条消息起…` 含有动词「运行」，
但它是一句**说明**，不是待办。因此判据收紧为「**以动作动词开头**」或
「以『下一步/接下来』引导」，并且只在任务确实未收尾时才从对话里挖待办。

### 5.10 WorkBuddy 与 Claude 的注入陷阱**方向相反**

两边都会在用户消息里混入 harness 注入，但处理方式完全相反：

| | Claude Code | WorkBuddy |
| --- | --- | --- |
| 注入形式 | 整条 `user` 记录是 `<system-reminder data-role="user-context">`，**真实需求不在其中** | 注入与真实需求**在同一个文本块**：整块以 `<system-reminder>` 开头（实测上万字符），真实需求在结尾的 `<user_query>…</user_query>` |
| 正确做法 | **整条丢弃** | **从标签里抽取**，绝不能整条丢弃 |

判断错方向就会丢掉整个任务需求 —— 所以「注入过滤」不能在 Core 里做，
必须留给各自 Adapter。WorkBuddy 的消息里 `timestamp` 也是**毫秒整数**而非 ISO 字符串。

### 5.11 配置读不动必须报错，不能静默退回默认值

真实教训：配置里一个 YAML 语法错误（或 `project_dir` 这种拼写错误），
早期实现会静默退回默认配置 —— 表现是「用户设了 `workbuddy.projects_dir` 却不生效、
毫无提示」，命令转而去读**默认目录**，结果完全出乎意料。

现在 `AMTConfig.config_error` 会记录原因，任何 `amt` 命令开头都会显示醒目告警，
并且**同时检查拼写错误**（Pydantic 默认忽略未知字段，这点很容易埋雷）。
同类问题也写进了测试。

---

## 6. 不同 Target 的注入策略

```text
Canonical Memory
   ├── Claude Code：.agent-transfer/{memory.md,memory.json,manifest.json,source.json}
   │                + CLAUDE.md 追加 @.agent-transfer/memory.md（幂等）
   │                + 初始 Prompt（含「不要重复已完成/失败方案」「真实状态优先」）
   │
   └── Codex：      .agent-transfer/（同一份任务包）
                    + AGENTS.md 内联核心上下文（幂等标记段）
                    + 初始 Prompt
```

两个 Target **共用同一个任务包目录**（`AMTConfig.memory_dir`），
因此在 Agent 之间来回迁移时复用的是同一份 memory package。

---

## 7. 验证结果

### 7.1 测试

```bash
pytest            # 166 passed
```

测试**不依赖本机真实会话数据**，也不启动任何外部 Agent：

- Codex 夹具复刻跨行记录、信封格式输出、经 shell heredoc 调用的 `apply_patch`、注入文本；
- Claude 夹具复刻实测的 2.1.284 格式，含 `attachment` / `queue-operation` /
  `cost-state` / `atis-latch` / `last-prompt` 等 harness 记账，以及 `isApiErrorMessage`；
- WorkBuddy 夹具复刻实测格式，并**故意把注入文本与 `<user_query>` 放在同一个块里**，
  锁定「必须抽取而不是丢弃」；
- Cursor 夹具用合成 SQLite（`composerHeaders` + `cursorDiskKV`），并**故意让气泡 UUID
  顺序与时间顺序相反**，锁定「必须按 createdAt 排序」；
- LLM 测试起一个**真实的 HTTP 服务**（规则模拟器）跑完整请求链路，而不是打桩。

覆盖的关键行为（节选）：

| 主题 | 用例 |
| --- | --- |
| 容错解析 | 跨行记录恢复；行式解析会丢记录的反证 |
| 注入污染 | harness 文本不得进入用户需求；`isApiErrorMessage` 必须成为 ERROR |
| 回环 | 本工具注入的 Prompt 必须被还原成真实目标 |
| 命令语义 | `rg` 未匹配不算失败；`mvn test` 归 test、`mvn compile` 归 build |
| 待办精度 | 说明性文本不得被当成待办 |
| 脱敏 | 同片段不重复上报；密码全掩码；源码常量不误伤；注入产物无原始凭据 |
| LLM | schema 策略生效与降级；契约不含事实字段；**事实字段不变量** |
| 目标无关性 | 换目标 Agent 后 Memory 逐字节不变 |
| 注入幂等 | `CLAUDE.md` / `AGENTS.md` 三次迁移后仍只有一个标记段 |
| 降级 | 目标 CLI 缺失 → 生成上下文 + 手动启动，Memory 完整保留 |
| 探测副作用 | 登录状态探测不得新增会话文件 |

### 7.2 真实数据端到端

| 链路 | 实测结果 |
| --- | --- |
| Codex → Canonical Memory | 554 条记录 0 解析失败 → 240 个事件 → 2 个文件改动（含 `+4/-0`、`+7/-0`）、6 条去重失败尝试、4 条未解决问题 |
| Codex → Claude Code | 5 个上下文文件注入，`CLAUDE.md` 幂等引入，产物校验 13/13 |
| Claude Code → Codex | 真实会话解析（19 条记录 → 用户消息 + API 错误），`AGENTS.md` 内联注入成功 |
| Cursor → Codex | 真实 Composer 会话（9 个气泡 → 3 用户 + 3 助手 + 3 思考），`AGENTS.md` 内联注入成功 |
| **WorkBuddy → Codex** | **217 条记录 0 解析失败** → 45 条快照记账被跳过、**2 条需求从 `<user_query>` 还原**、64 对工具调用全部配对、27 次命令 / 22 次写文件 / 9 次读文件；标题是真实提问而非上万字符的注入文本 |
| 启发式 vs LLM 对比 | 事实字段 **10/10 全部一致**；语义差异（决策、失败经验条数）如实列出 |
| 敏感信息 | 会话内 33 处（24 处 key=value、9 处 bearer token），落盘与注入前均脱敏 |

> ⚠ Claude Code 的 CLI 已安装（2.1.284），但当前**未登录**（`Not logged in · Please run /login`）。
> 因此「自动启动并接续」这一步在本机无法完成 —— 这也正是 POC 保留
> 「生成上下文 + 手动启动」降级路径的原因。**任务接续率（T3）仍未验证**。

---

## 8. 已知限制

1. **任务接续率（T3）未验证**。文档 §33 的 Level 3 指标需要在目标 Agent 中实际观察
   「是否重复已完成工作」。本机 Claude CLI 未登录，只完成了 Level 1/2。
   这是当前最重要的未验证项。
2. **LLM 通道未用真实模型验证**。本机没有可用端点，端到端验证用的是内置的
   **规则模拟器**（`tools/mock_llm_server.py`）。它能证明链路的正确性，
   **不能代表真实模型的归纳质量**。`amt compare` 的输出会显式标注这一点。
3. **Claude Code 解析器基于合成夹具 + 实测格式**。真实会话结构已在本机确认
   （2.1.284），但样本只有登录失败的空会话；工具调用/编辑路径由夹具覆盖。
4. **Cursor 的 diff → 文件改动分支未在真实数据上验证**。本机 Cursor 里只有 Q&A 会话，
   没有编辑动作；该分支已标记 `unverified`。
5. **WorkBuddy 暂不支持作为 Target**。其工作区记忆文件
   （`<workspace>/.workbuddy/memory/MEMORY.md`）会被注入上下文，原理上可作为注入点，
   但本实现**无法验证**它是否在所有场景下自动加载、以及追加内容是否会影响用户自己的记忆笔记。
   按「宁可不支持也不假装支持」的原则标注为不支持。
6. **WorkBuddy/Cursor 的 Source 会读取本机数据目录**。`workbuddy` 默认扫描
   `~/.workbuddy/projects`；如果不希望默认行为，可在配置里把 `workbuddy.projects_dir`
   指向别处（示例见 `tools/generate_example.py`，它把全部数据源都指向沙箱）。
7. **`build` 与 `test` 共用 `EventType.TEST`**，靠 `category` 区分（为保持 10 个规范类型不变）。
8. **`collect_project_state` 在大仓库上耗时约 0.7–7 秒**（主要是 `git status` / `git diff --stat`）。
9. **`SessionInfo.resumable`**：Codex/Claude 恒为 `True`（会话文件可读即可恢复），
   Cursor/WorkBuddy 恒为 `False`（无 CLI 恢复入口）。

---

## 9. 下一步

1. **验证 T3**：在有可用 Claude/Codex 额度（或已登录）的环境跑一次完整接续，观察是否重复劳动。
2. **用真实模型跑 `amt compare`**：先 `amt llm-check` 打通中转站，再对比启发式与 LLM 的质量增量。
3. **WorkBuddy Target**：确认工作区记忆文件的加载机制后，按同一套 Adapter 结构补上。
4. **Mimo Code / OpenCode / Antigravity Adapter**：同样只需新增 Adapter 目录。
5. **桌面端 GUI**：当前用 HTML 报告替代；若需要常驻工具，再考虑 Tauri + Python sidecar。
