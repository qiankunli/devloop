# devloop

跨 CLI 的 plugin marketplace。一个 marketplace 仓库 + 多个 plugin 子目录 + 各 CLI 各自的 manifest。本仓库**不是单 plugin 仓库**——根目录的 `devloop/` / `example/` 等子目录每个都是独立 plugin。`devloop` 是第一个真实 plugin，`example` 是占位（表明本仓库初衷即多 plugin）。

---

## 项目定位与边界

devloop 托管聚焦开发者效率的 plugin 集合。本仓库**根层**只负责：

- 多 plugin marketplace 的索引（`.claude-plugin/marketplace.json` 等）
- 跨 plugin / 跨 CLI 的共用约定（`<PLUGIN_ROOT>` 占位、CLI-agnostic 共享路径布局）
- 多 CLI 接入位置约定（Claude / Codex / opencode）

明确**不在本文档展开**的内容：

- 具体 plugin 的设计动机、内部架构、hook 列表、状态文件、配置项 → 见对应 `<plugin>/README.md`
- 整个工作流落地的方案记录（feature 矩阵、版本规划等）→ 见 plan 文档

当前实施范围：`devloop` 支持 Claude Code 与 Codex；Claude 侧使用完整 native event（含 CwdChanged / FileChanged / SessionEnd），Codex 侧使用其已支持的 hook 子集（PreToolUse / PostToolUse / SessionStart / UserPromptSubmit / PostCompact）并在 PostToolUse 做 cwd/state 刷新降级。opencode 侧目前只有 `example` 占位 plugin 演示 marketplace 结构。

---

## 代码地图与核心模块

```
devloop/                              # ← 仓库根（marketplace）
│
├── .claude-plugin/marketplace.json   # Claude marketplace 索引
├── .agents/plugins/marketplace.json  # Codex marketplace 索引（Codex 标准路径）
├── .opencode/marketplace.json        # opencode marketplace 索引（占位，按协议补）
│
├── devloop/                          # plugin: 开发者日常工作流（第一个真实 plugin，Claude + Codex）
│   │                                 #   git / MR / lint / test / cwd-aware enter / Board / 硬拦截
│   │                                 #   Claude 坐到原生事件上：CwdChanged / PostCompact / FileChanged / monitors；Codex 用 hooks.codex.json 的事件子集
│   ├── .claude-plugin/plugin.json    #     Claude manifest
│   ├── .codex-plugin/plugin.json     #     Codex manifest（hooks 指向 hooks.codex.json）
│   ├── skills/                       #     6 个 skill（CLI 共享）
│   ├── commands/                     #     slash commands（Claude 端）
│   ├── domain/                       #     领域模型与状态变化（workspace/repo/component/context/lifecycle）
│   ├── lib/                          #     技术能力（git/forge/ecosystem/notify/config/parser）
│   ├── hooks/                        #     事件驱动 adapter 与 PreToolUse policy
│   ├── scripts/                      #     git-ops 系列 + init_repo / init_workspace
│   ├── config/                       #     用户配置模板（config.json：workspaces / gitlab / precommit）
│   ├── monitors/monitors.json        #     MR-sweep 后台轮询
│   └── README.md / AGENTS.md / CONCEPTS.md
│
├── example/                          # plugin: 占位演示，证明这是多 plugin marketplace
│   ├── .claude-plugin/plugin.json    #     Claude ✅
│   ├── .codex-plugin/plugin.json     #     Codex  ✅
│   └── commands/hello.md
│
├── scripts/                          # 仓库级工具脚本（跨 plugin），如版本号 bump
│   └── bump_plugin_version.py        #   被 `make bump-version` 调用
├── Makefile                          # 仓库级入口（`make help` 查看）
├── AGENTS.md                         # 本文档（仓库级）
├── README.md                         # 用户向 marketplace 总览（安装方式）
└── CONTRIBUTING.md                   # 新 plugin 接入规范
```

**CLI 范围差异**：`devloop` 当前支持 Claude Code 与 Codex。`skills/` 共享；`commands/` 仍是 Claude slash command 入口，Codex 无同构 slash command，主要由 skill 名 + bundled hooks 作为入口；Claude hooks 用 `hooks.json` 的完整 native event，Codex hooks 用 `hooks.codex.json` 的事件子集；opencode 待协议明确。

详细：[`devloop/README.md`](./devloop/README.md)（使用向） · [`devloop/AGENTS.md`](./devloop/AGENTS.md)（开发向）。

---

## 关键约定

### `<PLUGIN_ROOT>` 占位符（跨 plugin 通用）

按"谁来解析"分两层处理，两层都不需要为新 CLI 做 sed：

**(1) 配置文件层（CLI 解析）**——`hooks/hooks.json` / `plugin.json` 这种由 CLI 直接读取的配置文件：

- 命令字符串里**统一写 `${CLAUDE_PLUGIN_ROOT}`**。
- Claude Code 原生认这个占位符；Codex 提供 `PLUGIN_ROOT` 作为标准名，同时保留 `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA` 作为兼容别名。
- Codex 还额外提供 `PLUGIN_DATA`（可写数据目录）供插件持久化状态。
- **单一 `hooks/hooks.json` 跨 Claude / Codex 复用**，业务代码零修改。

**(2) 文档/SKILL.md 层（AI 解析）**——人写给 AI 看的 markdown：

- 写 `<PLUGIN_ROOT>` 占位，AI 执行时按当前 CLI 映射到实际 env 变量值。
- Claude → `${CLAUDE_PLUGIN_ROOT}`；Codex → `${PLUGIN_ROOT}`（`CLAUDE_PLUGIN_ROOT` 亦可用，为兼容别名）。

加新 CLI 时：配置文件层多半零改动（如果新 CLI 也兼容 `CLAUDE_PLUGIN_ROOT`），文档层在 AI 提示词里多列一行映射即可。

### CLI-agnostic 共享路径布局

每个 plugin 内部建议以下目录跨 CLI 共享：

- `<plugin>/skills/`、`<plugin>/commands/`、`<plugin>/scripts/`：内容 CLI 无关
- `<plugin>/domain/`：领域模型、状态变化与生命周期规则；归属看领域事实的 owner
- `<plugin>/lib/`：跨入口复用的技术能力和外部适配，不放领域对象
- `<plugin>/hooks/` / `<plugin>/scripts/`：事件与工作流两类驱动 adapter；依赖方向是 `hooks/scripts → domain/lib`，`domain/lib` 不反向依赖入口

Codex 与 Claude 的 hook payload schema 几乎一致（同样 stdin JSON、同样字段名 `session_id` / `transcript_path` / `cwd` / `hook_event_name` / `tool_name` / `tool_input` 等），入口脚本用 `sys.path.insert(0, Path(__file__).parent)` 自定位 lib、不读任何 plugin-root env var → 跨两端零修改运行。opencode 待协议明确时再决定差异隔离层。

### 加新 plugin 的流程

1. 新建 `<plugin>/` 子目录
2. 写需要支持的 CLI 的 manifest（`.claude-plugin/plugin.json` 等）
3. 在每个 CLI 的 `marketplace.json` 追加一项
4. 写 `<plugin>/README.md`，至少包含：能做什么、安装、配置、限制
5. 遵守 `<PLUGIN_ROOT>` 与共享路径布局约定

### 改动 plugin 后：bump 版本号

影响用户体验的 plugin 改动，跑 `make bump-version PLUGIN=<name>`（详见 `make help`）——否则 `/plugin update` 拉不到新版。

---

## References

- 各 plugin 使用 / 安装 / 配置：`<plugin>/README.md`
- 各 plugin 设计与开发：`<plugin>/AGENTS.md`
- 各 plugin 内跨 skill 共享术语：`<plugin>/CONCEPTS.md`（若有）
