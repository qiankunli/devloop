# Contributing — 新增 plugin

## 目录约定

每个 plugin 是仓库根的一个子目录（不嵌 `plugins/` 一层）：

```
<plugin-name>/
├── .claude-plugin/plugin.json     # Claude manifest (本期必需)
├── .codex-plugin/plugin.json      # Codex 占位 (内容写最小可解析 JSON + _status 字段)
├── skills/                        # 可选：SKILL.md 子目录
├── commands/                      # 可选：slash 命令
├── hooks/                         # 可选：hook 脚本 + hooks.json
│   └── lib/                       # CLI-agnostic 纯逻辑模块（推荐）
├── scripts/                       # 可选：shell/python 工具脚本
├── config/                        # 可选：plugin 默认配置
├── README.md                      # 必需：用户文档
└── .version-bump.json             # 可选：多 manifest 同步版本号
```

## 设计理念约束

每个 plugin 都应遵循 [AGENTS.md](./AGENTS.md) 里的"动作-行为-状态"三元组：

1. **动作 (Action)**：明确你的 plugin 拦截或响应哪些事件（PreToolUse/PostToolUse/Stop/UserPromptSubmit）
2. **行为 (Behavior)**：分清三类——硬规则校验 / 副作用触发 / 状态更新 + 注入
3. **状态 (State)**：如有跨 hook 共享状态，存到 `.devloop/context.json`（如果本插件复用 devloop 的状态总线）或自己的状态文件

## 跨 CLI 友好

- **共享纯逻辑放 `hooks/lib/`**：CLI-agnostic，将来给 Codex 写 hook 时直接 import
- **manifest 各自一份**：`.claude-plugin/plugin.json` / `.codex-plugin/plugin.json` 等
- **skills / commands 两个 CLI 兼容**：SKILL.md 格式两边都用；commands frontmatter 各 CLI 略有差异，按需调整

## 注册到 marketplace

新 plugin 加好后：
1. 在 `.claude-plugin/marketplace.json` 的 `plugins` 数组追加一项
2. 同步追加到 `.codex-plugin/marketplace.json`（占位）和 `.opencode/marketplace.json`（占位）
3. 更新仓库根 `README.md` 的 plugin 清单

## Hook 编写规范

- Hook 脚本放 `<plugin>/hooks/`，命名按事件类型前缀：`pretool_*` / `posttool_*` / `stop_*` / `userprompt_*`
- Hook 注册在 `<plugin>/hooks/hooks.json`
- 业务逻辑下沉到 `hooks/lib/` 模块，hook 脚本只做"输入解析 + 调 lib + 输出序列化"三步
- 失败兜底：hook 报错时 `sys.exit(0)` 输出空 JSON，**不要阻塞用户**
