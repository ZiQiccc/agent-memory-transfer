# storage 目录说明

运行时的数据根目录**默认不是这里**，而是：

```text
~/.agent-memory-transfer/          # 可用 AMT_HOME 环境变量或 config.yaml 覆盖
├── config/config.yaml
├── sessions/{agent}/{session_id}/raw.json + metadata.json   原始会话快照（已脱敏）
├── memories/{memory_id}.json + .md                          Canonical Memory
├── migrations/{migration_id}.json                           迁移记录
├── logs/
└── cache/
```

选择用户主目录而不是项目目录的原因：

1. **迁移是跨项目行为**，数据不应绑在某个工程里；
2. 会话快照可能包含源码片段，放在工程内容易被误提交进 Git；
3. 与 Codex 自身的 `~/.codex/sessions` 保持一致的使用习惯。

本目录仅作为文档占位，避免「目录清单里有、实际找不到」的困惑。
