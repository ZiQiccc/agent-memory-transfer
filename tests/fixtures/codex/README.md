# 测试夹具说明

本目录**故意不放静态 fixture**。

原因：Codex 的 rollout JSONL 是**私有格式且会变**。如果这里存一份真实会话快照，
一旦 Codex 改格式，测试会因为「快照过时」而失败，而不是因为解析器出错——
这种失败没有诊断价值。

因此夹具由 `tests/conftest.py` 的 `build_rollout()` **在运行时合成**，
它复刻的是本机实测到的**格式特征**（而不是某一次具体会话的内容）：

| 特征 | 来源 |
| --- | --- |
| 记录跨多个物理行 | 本机 72 个会话实测，行式解析会误报 35 处失败 |
| `response_item` / `event_msg` / `session_meta` / `world_state` / `turn_context` 外层结构 | 真实 rollout 采样 |
| 工具输出信封（`Chunk ID` / `Wall time` / `Process exited with code` / `Original token count` / `Output:`） | 真实 rollout 采样 |
| `exec_command`、经 shell heredoc 调用的 `apply_patch` | 真实 rollout 采样 |
| 注入的 `developer` 消息与 `<environment_context>` 用户文本 | 真实 rollout 采样 |
| 带凭据的调试命令（伪造值） | 用于验证脱敏链路 |

好处是：**格式契约由测试显式声明**。Codex 若改格式，这里的新增用例会直接指出
「哪一类记录不再被识别」，比对比一份快照更容易定位。
