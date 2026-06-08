# devloop

跨 CLI 的 plugin marketplace。`devloop` 是第一个真实 plugin（开发者工作流），`example` 是占位 plugin——本仓库从一开始就按**多 plugin marketplace** 设计，devloop 只是第一个。

设计理念 / 架构 / 多 CLI 策略见 [AGENTS.md](./AGENTS.md)。各 plugin 的细节见各自目录的 README。

## 安装

### Claude Code

```
/plugin marketplace add https://github.com/qiankunli/devloop.git
/plugin install devloop@devloop
```

安装后建议跑一次 init（按你的使用形态选其一）：

```
# 形态 A：聚合工作区（一个根目录下有多个 git 子项目）
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_workspace.py <your-aggregate-workspace>

# 形态 B：单 git 仓库
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_repo.py
```

不跑 init 也能用，hook 会在你第一次 cd 到 git repo 时自动初始化。但形态 A 必须显式注册（plugin 不会自动判断哪些目录算聚合工作区）。

GitLab（MR / 状态注入）相关功能依赖一个 GitLab token，配置见 [devloop/README.md](./devloop/README.md#配置)。

### Codex / opencode 等

marketplace 结构是 CLI 无关的：新 CLI 接入只需在仓库根加 `.<cli>-plugin/marketplace.json`（已有 `.agents/plugins/marketplace.json` 给 Codex、`.opencode/marketplace.json` 占位），并在每个 plugin 子目录加对应 manifest。

当前 `devloop` 是 **Claude Code only**（它的硬拦截 / 状态注入坐在 Claude 原生事件上，Codex hook 协议跟上再接，架构已预留）。Codex 侧目前只有 `example` 占位 plugin 演示 marketplace 结构。

## Plugin 列表

| Plugin | 简介 | README |
|--------|------|--------|
| `devloop` | 开发者工作流：git/MR + cwd-aware enter + lint/test gates + 实时状态注入 + 执行级硬拦截（Claude-only） | [devloop/README.md](./devloop/README.md) |
| `example` | 占位 plugin，演示多 plugin marketplace 结构 | [example/README.md](./example/README.md) |

## 新增 plugin

见 [CONTRIBUTING.md](./CONTRIBUTING.md)。
