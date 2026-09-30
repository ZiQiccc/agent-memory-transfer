# Agent Memory Transfer (AMT)

跨 Coding Agent 的任务记忆迁移工具。把某个 Agent 会话中**已经完成的工作、失败过的方案、
未解决的问题和下一步行动**，归一成一份与 Agent 无关的 `Canonical Memory`，
再注入到另一个 Agent，让它从中断处继续，而不是重新分析项目。

```text
Codex / Claude Code / Cursor / WorkBuddy 会话
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
| LLM 通道 | ✅ OpenAI 兼容（中转站 / DeepSeek / 通义 / 本地 vLLM / Ollama / One-API），带能力降级阶梯 + 思维链模型适配 + `amt llm-check` 连通性自检 |
| 凭据管理 | ✅ `.env` 文件（`AMT_ENV_FILE` → `$AMT_HOME/.env` → 当前目录），密钥不落盘、不序列化、不打印 |
| 质量对比 | ✅ `amt compare` 启发式 vs LLM，并校验「事实字段不变量」（已用真实模型跑通） |
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
amt agents                      # Agent 能力矩阵（-v 看每个 Agent 的详细说明与可用性）
amt sessions codex              # 列会话（也可 claude / cursor / workbuddy）
amt extract codex <session-id>  # 只生成 Canonical Memory
amt preview <memory-id>         # 查看已生成的 Memory
amt migrate --from workbuddy --to codex --dry-run  # 只看不写
amt migrate --from workbuddy --to codex --session <id> --project-root <dir>
amt llm-check                   # 验证 LLM 端点（读 .env；真实最小调用）
amt compare --from workbuddy --session <id>        # 质量对比（启发式 vs LLM）
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

推荐把凭据放进 `.env`（已被 `.gitignore` 排除，不会误提交）：

```bash
cd agent-memory-transfer
cp .env.example .env      # 然后填 base_url / model / 密钥
amt llm-check             # 先自检：端点可达 / 鉴权 / 策略 / 耗时 / token
```

`.env` 的查找顺序与优先级：

```text
查找：AMT_ENV_FILE → $AMT_HOME/.env → 当前目录/.env
优先级：命令行参数 > 真实环境变量 > .env 文件 > config.yaml > 内置默认值
```

`.env` 里可写 `AMT_LLM_API_KEY` / `AMT_LLM_BASE_URL` / `AMT_LLM_MODEL`，
也接受更短的 `api_key` / `base_url` / `model`（配合中转站时更顺手）；
`AMT_LLM_TIMEOUT`、`AMT_LLM_MAX_TOKENS` 可选。
**真实环境变量只认 `AMT_LLM_*` 前缀** —— 否则系统里任何一个叫 `MODEL`、`API_KEY`
的变量都会串味进来。

三条与凭据安全相关的约定：

- `base_url` + `model` + `api_key` 齐全时**自动启用** LLM，避免「配好了却忘了开、
  静默走了启发式」；想显式关掉就在 `.env` 里写 `AMT_LLM_ENABLED=false`。
- 从 `.env` 读到的 `api_key` 只存在私有属性里，**不参与任何序列化**，
  因此 `amt config --init` 不会把密钥写进 `config.yaml`。
- `amt llm-check` 的「来源」列会写清每个值是谁给的（`.env` / 环境变量 / 配置文件），
  但**永不打印密钥本身**。

也可以完全不走 `.env`：

```bash
# 命令行直接指定（凭据不落盘）
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
│   │                                  launch.py：交互式启动的参数投递策略
│   ├── providers/llm.py               OpenAI 兼容 Provider（含能力降级阶梯）
│   └── gui/report.py                  自包含 HTML 报告
├── tools/
│   ├── generate_example.py            在沙箱里跑一遍链路并产出 examples/（合成数据）
│   └── mock_llm_server.py             OpenAI 兼容的规则模拟器（链路验证用）
├── examples/                          example-memory.md / report.html（均为合成数据）
├── .env.example                       凭据文件模板（.env 已被 .gitignore 排除）
└── tests/                             199 项测试，不依赖本机真实会话数据
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

文档假设由 LLM 完成记忆提取（技术架构 §22 / §43）。本实现改为
**「确定性基线 + LLM 语义增强」** 双通道：

1. 先用 `HeuristicReconstructor` 生成一份字段完整、可离线运行的 Memory；
2. LLM 可用时，用它的归纳结果覆盖**语义字段**；
3. **事实字段永不被 LLM 覆盖** —— 项目、Git、运行时、测试结果一律来自程序读取，
   而且 LLM 的输出契约里**根本不含**这些字段（见 `llm_schema.py`）。

理由不只是「当时没有凭据」：这条设计让**无 LLM 的 CI、离线环境、额度耗尽**都能跑通
完整链路，LLM 只是质量增量而不是单点依赖；LLM 调用失败时自动回退并把原因写进 warnings。
重建方式与置信度写进 `task.reconstructed_by` / `task.confidence`，在 CLI 与报告中展示。

LLM 通道现已用**真实模型（经中转站）**验证，增量数据见 §7.2。

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

### 5.7 探测必须零副作用（Claude 与 Codex 都适用）

早期版本用 `claude -p ping` 探测登录状态 —— 结果**在用户的 `~/.claude/projects` 里
留下了 5 个垃圾会话**（实测发现并已清理）。现在两边都改为**证据式判断**：

| Agent | 判据 | 不做什么 |
| --- | --- | --- |
| Claude Code | 读最近一条会话，看是否以 `isApiErrorMessage` 收尾 | 不发请求 |
| Codex | `~/.codex/auth.json` 是否存在且有凭据字段 + 最近 5 个会话是否出现鉴权失败标记 | 不发请求 |

结论措辞也刻意保守：Codex 侧只会说「凭据文件存在且最近会话未见鉴权失败
（**未主动发请求验证**）」，不会把「看起来可用」说成「已验证可用」。

同类约束也写进了测试：`ctx` 夹具把 CLI 路径指向不存在的文件，
保证测试永远不会启动真实 Agent（`test_codex_login_probe_accepts_credential_file`
还会断言探测前后文件集合完全一致，即零副作用）。

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

### 5.12 思维链模型会吃掉输出预算：`finish_reason=length` 要重试而不是报解析失败

接中转站后实测到的第一个真问题：`deepseek-v4-pro` 这类**思维链模型**会先输出一大段
推理（实测 9.5K–12.4K 字），再写 JSON 正文。默认 `max_tokens=4096` 时预算被推理吃完，
正文只写了一半就断在 `finish_reason=length`，最终表现为「**JSON 解析失败**」或
「**模型什么都没返回**」—— 这两句报错都会把人引向完全错误的方向。

正确处理：
- 检测到 `finish_reason == "length"` 时，把预算**翻倍重试一次**（上限 16384），
  而不是当作格式问题放弃；
- 正文为空时回退读 `reasoning_content`（有些网关只把结果放在那里）；
- 所有失败路径的报错都带上 `finish_reason` / `max_tokens` / `completion_tokens`，
  让「是不是被截断了」一眼可判。

中转站较慢时用 `AMT_LLM_MAX_TOKENS=8192` 预设预算可省掉那轮翻倍重试。
另外 `amt compare` 现在会把「LLM 为什么没生效」直接打出来 ——
早期版本只显示「左右两侧一模一样」，用户无从下手。

### 5.13 「验证结果」也分事实与线索，否则自检会与实现打架

早期实现允许 LLM 在「程序没识别出验证命令」时补上 `validation.tests`，
但 `amt compare` 的事实不变量里又把 `validation.tests` 当**事实字段**比对 ——
于是同一份代码既声称「测试结果是事实、LLM 不得覆盖」，又允许 LLM 写进去，
自检必然报警。真实数据上就撞到了：WorkBuddy 会话用 `slidep-validate` 校验了 18 页幻灯片，
程序没把它识别成测试命令，LLM 补上后事实字段立即不一致。

现在按来源拆开：

| 来源 | 含义 | 是否参与事实不变量 | 渲染时 |
| --- | --- | --- | --- |
| `source="program"` | 程序从会话记录**确定性解析**出的验证结果 | ✅ 参与 | 标注「程序解析」 |
| `source="llm"` | 程序没识别到，由 LLM **归纳**出的线索 | ❌ 不参与 | 标注「⚠ LLM 归纳（未经程序校验）」 |

LLM 的契约里也不含 `source` 字段 —— 来源由程序标注，不允许模型自称「程序校验通过」。

### 5.14 交互式启动：必须新建独立控制台，且参数不能经过 shell

用户实测反馈：迁移完启动 Codex 时**没有弹出新窗口**，而是「在原有窗口里继续」，
导致点不进去、没法交互。根因与修复如下。

**① 独立控制台**。早期实现只传了 `CREATE_NEW_PROCESS_GROUP` —— 那只是新建进程组，
子进程仍然**继承父进程的控制台**，于是 Codex TUI 直接接管了运行 `amt` 的那个终端。
实测（`GetConsoleWindow` 句柄）：

| 创建方式 | 子进程控制台句柄 | 结果 |
| --- | --- | --- |
| `CREATE_NEW_PROCESS_GROUP`（修复前） | 0 | 没有自己的控制台 → 占用当前窗口 |
| `CREATE_NEW_CONSOLE`（修复后） | 330838 | 独立控制台 ✓ |

同时尝试 `CREATE_BREAKAWAY_FROM_JOB`，让窗口在父进程（或它所在的 Job 对象）退出后
仍然存活；Job 不允许 breakaway 时该标志会让 `CreateProcess` 失败，因此按阶梯回退。
「降级」的判据是**用户是否失去独立窗口**，而不是「用了几次尝试」——
前两档都拿到了新控制台，退到第二档不算降级。

**② 参数不能经过 shell**。Codex 在本机是 `codex.CMD`，`CreateProcess` 无法直接执行
`.cmd`，必须经 `cmd.exe /c` 包装 —— 而**只要经过 shell，参数就会被再解析一次**。
实测（`cmd.exe /c shim.cmd "<arg>"`，由 `tests/test_launch.py` 锁定）：

| 参数内容 | 实测结果 |
| --- | --- |
| 多行文本 | **只送到第一行**，其余被当成独立命令 |
| `%PATH%` | 变量被展开，26 字 → 1473 字 |
| `&` / `\|` | 命令被切断 |
| `<` / `>` | 整行消失 |
| `"` | 参数边界被破坏 |

而初始 Prompt 恰好包含换行、引号与可能的 `%`/`&`（内容来自用户原话或 LLM 归纳），
所以「原样塞进命令行」在 `.cmd` 目标上会**静默残缺** —— 修复前，送进 Codex 的
实际上只有第一行。现在的策略按目标类型分流：

| 目标形态 | 命令行参数 | 完整 Prompt |
| --- | --- | --- |
| 真实可执行文件（`claude.exe` 等，不经 shell） | **完整原文**（含多行） | 原样传递 |
| `.cmd` / `.bat`（必须经 cmd.exe） | **安全化的单行摘要**（cmd 元字符换同形全角字符） | 写入 `.agent-transfer/initial-prompt.md`，并在摘要中指路 |

**③ 闪退可诊断**。独立窗口里的输出本工具拿不到，若目标 CLI 启动即崩，
用户只会看到「窗口闪了一下」。因此启动后会短暂观察（1.2 s）：
进程若已退出，直接报失败并给出退出码 + 引导手动执行，而不是谎报「已启动」。

> 另一条实测约束：**`.cmd` / `.bat` 内不能放非 ASCII 文本**。cmd.exe 按 OEM 代码页
> 读取批处理，中文注释会被解析成乱码并当作命令执行（本机验证时因此踩到）。

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
pytest            # 213 passed
```

测试**不依赖本机真实会话数据**，也不启动任何外部 Agent：

- Codex 夹具复刻跨行记录、信封格式输出、经 shell heredoc 调用的 `apply_patch`、注入文本；
- Claude 夹具复刻实测的 2.1.284 格式，含 `attachment` / `queue-operation` /
  `cost-state` / `atis-latch` / `last-prompt` 等 harness 记账，以及 `isApiErrorMessage`；
- WorkBuddy 夹具复刻实测格式，并**故意把注入文本与 `<user_query>` 放在同一个块里**，
  锁定「必须抽取而不是丢弃」；
- Cursor 夹具用合成 SQLite（`composerHeaders` + `cursorDiskKV`），并**故意让气泡 UUID
  顺序与时间顺序相反**，锁定「必须按 createdAt 排序」；
- LLM 测试起一个**真实的 HTTP 服务**（规则模拟器）跑完整请求链路，而不是打桩；
- 凭据测试**不会碰真实 `.env`**：每个用例都在 `tmp_path` 里造自己的文件，
  并断言密钥不会出现在 `model_dump` / `origin` / `key_source()` 的任何输出里。

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
| LLM 适配 | 思维链模型截断后**自动翻倍预算重试**；正文为空时回退 `reasoning_content`；报错带 `finish_reason` |
| 验证结果归属 | LLM 补充的验证线索打 `source="llm"`，不参与事实不变量，且渲染时标注 |
| 凭据文件 | `.env` 键名别名、查找顺序、环境变量优先、密钥不落盘、`--init` 不固化运行时覆盖 |
| 目标无关性 | 换目标 Agent 后 Memory 逐字节不变 |
| 注入幂等 | `CLAUDE.md` / `AGENTS.md` 三次迁移后仍只有一个标记段 |
| 交互式启动 | 必须请求 `CREATE_NEW_CONSOLE`；breakaway 被拒时回退且**不误报降级**；`.cmd` 目标只传安全化单行摘要 + 完整 Prompt 落盘；启动即退时必须报失败 |
| 降级 | 目标 CLI 缺失 → 生成上下文 + 手动启动，Memory 完整保留 |
| 探测副作用 | 登录状态探测不得新增会话文件（Claude / Codex 各一条） |

### 7.2 真实数据端到端

| 链路 | 实测结果 |
| --- | --- |
| Codex → Canonical Memory | 554 条记录 0 解析失败 → 240 个事件 → 2 个文件改动（含 `+4/-0`、`+7/-0`）、6 条去重失败尝试、4 条未解决问题 |
| Codex → Claude Code | 5 个上下文文件注入，`CLAUDE.md` 幂等引入，产物校验 13/13 |
| Claude Code → Codex | 真实会话解析（19 条记录 → 用户消息 + API 错误），`AGENTS.md` 内联注入成功 |
| Cursor → Codex | 真实 Composer 会话（9 个气泡 → 3 用户 + 3 助手 + 3 思考），`AGENTS.md` 内联注入成功 |
| **WorkBuddy → Codex（含真实 LLM）** | 见下 |
| 敏感信息 | 会话内 33 处（24 处 key=value、9 处 bearer token），落盘与注入前均脱敏 |

#### WorkBuddy → Codex（真实模型 + 真实交接）

来源是用户提供的 WorkBuddy 会话（PPT 制作任务），全程真实数据、真实中转站模型：

| 环节 | 实测 |
| --- | --- |
| 解析 | 217 条记录 **0 解析失败**；172 个 AgentEvent；45 条 `file-history-snapshot` 记账正确跳过 |
| 需求还原 | 2 条需求从 `<user_query>` 抽出（标题是真实提问，不是上万字符的注入文本） |
| Memory 构建 | LLM（`deepseek-v4-pro`）97.7 s，12386 tokens，策略 `json_schema`，思维链 9588 字；置信度 `high` |
| 脱敏 | 会话内 0 处、Memory 本体 0 处（该会话无凭据） |
| 注入 | 5 个文件：`.agent-transfer/{memory.md,memory.json,manifest.json,source.json}` + `AGENTS.md` 内联段 |
| 校验 | 产物校验 **13/13**；2 条警告均为真实情况（沙箱里没有那份 pptx；目录不是 Git 仓库） |
| 幂等 | 同一 Memory 连续注入 3 次，`AGENTS.md` **sha256 完全一致**、标记段恒为 1 组、原项目约定保留 |
| 验证结果归属 | `slidep-validate`（程序未识别）被打上 `source="llm"`，渲染为「⚠ LLM 归纳（未经程序校验）」 |

#### 启发式 vs 真实模型的质量增量（`amt compare`）

同一会话跑两条通道，真实中转站模型（`deepseek-v4-pro`，96.1 s / 12461 tokens）：

| 语义字段 | 启发式 | LLM |
| --- | --- | --- |
| `task.title` | `给一些会使用codex进行开发的同事进行分享，你觉得我分享什么内容比较好`（原话照搬） | `Codex 高效开发经验分享（30分钟）` |
| `task.goal` | 同上 + 截断的后续要求 | 凝练成「为使用 Codex 约三个月、以问答式生成和修改代码的同事制作一场 30 分钟…」 |
| `requirements` | 2 条 | **4 条** |
| `constraints` | 0 条 | **2 条** |
| `decisions` | 0 条 | **4 条** |
| `implementation.completed` | 2 条 | **8 条** |
| `conversation.summary` | 101 字 | 136 字 |
| `confidence` | medium | high |
| **事实字段不变量** | — | **10/10 全部一致 ✅** |

结论很清楚：**启发式的短板是「归纳」，不是「读取」**——目标字段基本是把用户原话
原样搬过来，决策与约束几乎零召回；而 LLM 的主要增益正好落在这一块。
事实字段（项目 / Git / 运行时 / 程序解析出的测试结果）两侧完全一致，
证明「LLM 不得覆盖事实」这条约束在真实模型上确实成立，而不只是设计意图。

#### 换模型：同一会话下的两个模型对比

同一会话、同一条件，只换 `--llm-model`（该中转站共 16 个可用模型）：

| 指标 | `deepseek-v4-pro` | `deepseek-v4.1-flash` |
| --- | --- | --- |
| 耗时 | 96.1 s | **38.7 s** |
| total tokens | 12461 | 10572 |
| 思维链长度 | 10533 字 | 5694 字 |
| `task.title` | `Codex 高效开发经验分享（30分钟）` | `为使用 Codex 的同事做 30 分钟分享并产出 18 页 PPT` |
| requirements / constraints | 4 / 2 | 4 / 2 |
| decisions | 4 | **5** |
| completed | 8 | **12** |
| unresolved | 1 | **3** |
| next_actions | 1 | **3** |
| risks | 0 | **3** |
| summary 字数 | 136 | **280** |
| **事实字段不变量** | 10/10 ✅ | 10/10 ✅ |

两点结论：

1. **两者都安全** —— 事实字段在任意模型下都保持不变，这是结构性保证而非模型行为。
2. **本例中 flash 更划算** —— 快 2.5 倍，且对「未解决问题 / 下一步 / 风险」的召回更全
   （这几项恰恰是交接最需要的信息）；pro 的优势只体现在标题更凝练。
   条数多不等于质量高，但交接场景下**漏掉未解决问题**的代价明显更大。

#### 目标 Agent 真的读到了吗（T3 的替代验证）

只证明「文件写对了」还不够。Codex CLI 在本机可用（`codex-cli 0.149.1`，
另有独立 `CODEX_HOME` 做隔离、`--ephemeral` 不落会话文件），因此在沙箱工程里实跑了一次：

```text
workdir: D:\Memory_transfer\_wb2codex\proj     sandbox: read-only
user> （只读任务）接续上下文里的任务目标和状态是什么？已完成的工作一共多少页？
codex> 目标：为已使用 Codex 三个月的同事准备 30 分钟分享，产出含逐页讲稿的完整 PPT；
       状态：completed。已完成的分享材料共做成 18 页完整胶片，并在每页备注中写现场逐字讲稿。
```

Codex **没有重新分析项目**，直接答出了注入上下文里的目标、状态与页数。
隔离校验：运行前后用户真实 `~/.codex` 的会话文件均为 **71 个（未新增）**，
沙箱工程只读未改动，隔离 `CODEX_HOME`（含凭据副本）已整体删除。

> ⚠ Claude Code 侧的任务接续仍**未验证**：CLI 已安装（2.1.284）但未登录
> （`Not logged in · Please run /login`），只能走到 Level 1/2。
> 这也正是 POC 保留「生成上下文 + 手动启动」降级路径的原因。

---

## 8. 已知限制

1. **Claude Code 侧的任务接续未验证**。文档 §33 的 Level 3 指标需要在目标 Agent 中
   实际观察「是否重复已完成工作」。Codex 方向已实跑验证（见 §7.2），但 Claude CLI
   在本机**未登录**，只能走到 Level 1/2。这是当前最重要的未验证项。
2. **LLM 只在中转站的两个模型上验证过**（`deepseek-v4-pro`、`deepseek-v4.1-flash`，
   均为 Anthropic 风格网关 + 思维链）。官方 OpenAI / 本地 vLLM 的
   `response_format` 行为可能不同 —— 降级阶梯与截断重试已覆盖这类差异，但未逐一实测。
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
7. **思维链模型的时间与费用成本不低**。一个 172 事件、5.4K prompt tokens 的会话，
   `deepseek-v4-pro` 实测 96 s / 12.4K tokens，`deepseek-v4.1-flash` 为 38.7 s / 10.6K。
   更大的会话（digest 14329 tokens）实测 **279.7 s**，请把 `AMT_LLM_TIMEOUT` 放宽到 600–900。
8. **交互式启动的开销与边界**。目标 CLI 为 `.cmd`/`.bat` 时，命令行只能传安全化摘要
   （完整 Prompt 落盘为 `initial-prompt.md`）；独立窗口的输出本工具无法捕获，
   因此只能靠 1.2 s 的存活探测识别「启动即崩」，看不到具体报错内容。
9. **`build` 与 `test` 共用 `EventType.TEST`**，靠 `category` 区分（为保持 10 个规范类型不变）。
10. **`collect_project_state` 在大仓库上耗时约 0.7–7 秒**（主要是 `git status` / `git diff --stat`）。
11. **`SessionInfo.resumable`**：Codex/Claude 恒为 `True`（会话文件可读即可恢复），
    Cursor/WorkBuddy 恒为 `False`（无 CLI 恢复入口）。
12. **`amt agents` 的「可自动启动」只是证据式推断**，不是实跑验证 ——
    凭据可能存在但已过期，真正能否接续要以实际迁移为准。

---

## 9. 下一步

1. **Claude Code 侧的任务接续验证**：登录后跑
   `amt migrate --from codex --to claude --session <id>`，观察目标 Agent 是否重复劳动。
2. **多模型横向对比**：已跑通 2 个模型（见 §7.2）；可把 `claude-opus-5`、
   `qwen3.8-max`、`glm-5.3` 等一并纳入，形成「模型 → 记忆质量 / 耗时 / 成本」的选型表。
3. **WorkBuddy Target**：确认工作区记忆文件的加载机制后，按同一套 Adapter 结构补上。
4. **Mimo Code / OpenCode / Antigravity Adapter**：同样只需新增 Adapter 目录。
5. **桌面端 GUI**：当前用 HTML 报告替代；若需要常驻工具，再考虑 Tauri + Python sidecar。
