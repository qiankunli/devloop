# devloop plugin

**面向「聚合工作区 + 多 subproject」开发的 plugin**（native-first 实现）。一个 workspace 挂着项目的全部代码仓，开发者在其中转一个循环——enter 子模块 → 提需求（可能跨多个 subproject）→ 开发 → commit / 建 MR（含 lint/test）→ 人工到 GitLab merge → 下一轮；排查问题也在同一 workspace（as-ops / infra-ops 等发现问题后切到子模块开发）。devloop 靠**实时状态注入**（每轮 prompt 知道当前 subproject 的 branch / 工作区 / 近期 MR）+ **执行级硬拦截**（保护分支、`git add -A`、过期分支改文件等直接 deny），提高这个循环里 AI 的首次成功率。

> 架构 / 扩展看 [`AGENTS.md`](./AGENTS.md)；术语看 [`CONCEPTS.md`](./CONCEPTS.md)。

独立 `.devloop/` 命名空间状态，与其它工具互不干扰。当前 **Claude Code only**（Codex 等其 hook 协议跟上再接，架构已预留）。

## 它做什么

- **状态层**：workspace 维护 `<workspace>/.devloop/context.json`（subproject 清单含 symlink→真实路径映射 / AGENTS.md References / 注入节奏）+ `active.json`（最近活跃 repo），每个 git 仓库维护 `<repo>/.devloop/{meta,branch,mr,validation,injection}.json`（branch / 保护标记 / target / ahead-behind / 近期 MR 窗口 / lint-test 时间），每轮 prompt 注入；自动加入 `.git/info/exclude`，不会误提交。
- **硬拦截**（PreToolUse deny）：保护分支 commit/push、`git add -A`、过期分支（MR 已 merged/closed）改文件、别的 session 占用的 checkout 上切分支或改文件（引导 worktree）、工作区根跑子项目命令、裸 `pytest`、uv 项目 `pip install`、编辑 `requirements.txt`、lint 过期 commit gate。
- **MR 感知**：后台 monitor 周期轮询 GitLab，把当前分支的 MR + 近期 MR（窗口 `[mr_iid-2, latest]`，最多 5 条）写进状态，注入里以 `Recent MRs:` 呈现。
- **自动进项目**：`cd` 进子项目时（`CwdChanged`）自动刷新上下文、浮现 AGENTS.md References，无需手动 `/enter`。

## Slash 命令

| 命令 | 作用 |
|------|------|
| `/enter <name|path> [--worktree <tag>]` | 按名/路径跳进子项目（context 自动加载） |
| `/gcam "<msg>"` | 只 commit |
| `/gcamp "<msg>"` | commit + push |
| `/gcampr "<msg>" [--branch <name>]` | commit + push + 建/复用 MR |
| `/lint [<repo>]` | `make fix` + lint（有 `lint-ci` 优先，对齐 CI）+ 标记验证 |
| `/test [<repo>] [-- <args>]` | `make test` + 标记验证 |

保护 / 过期分支上，gcam* 需 `--branch <name>`，脚本会从 `origin/<target>` 切新分支（不给会拒绝并提示）。

gcam* / lint / test 都不依赖 cwd：默认解析 cwd 所在仓库，在 workspace 根则兜底到最近活跃子项目；用 `--repo <name|path>`（gcam*）或首参（lint/test）显式指定，无需 `cd` 前缀。

## 安装

```
# Claude Code 内
/plugin marketplace add https://github.com/qiankunli/devloop.git
/plugin install devloop@devloop
```

初始化（可选——hook 首次 cd 会自动建）：

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_repo.py            # 单仓库
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_workspace.py <dir> # 聚合工作区
```

## 配置（`~/.devloop/config.json` + 本地覆盖）

devloop 对外部的依赖（连哪个 GitLab、用什么 token）+ 工作区注册表 + 提交门禁，**统一收在一个全局文件** `~/.devloop/config.json` 里。放用户级目录（不是 plugin 目录）是因为 plugin 目录是版本化 cache，`/plugin update` 会把写进去的东西清掉。可用环境变量 `DEVLOOP_CONFIG_DIR` 覆写目录。

**本地覆盖（就近优先）**：任一 repo / workspace 可以在自己的 `.devloop/config.json` 里只写要改的几项（比如该 repo 用不同的 `gitlab.token` / `host`）。读取时按「**离 repo 最近的赢**」分层合并：`默认值 < 全局 ~/.devloop/config.json < 上层 .devloop/config.json（由外到内，最近的覆盖）`；没写的段落自动落回外层。本地文件**只读、手写**——devloop 自己的写入（如工作区自动注册）只落全局文件。

文件不存在也能用——所有项都有默认值，hook 首次运行时会按需创建。`devloop/config/config.example.json` 是带注释占位的模板，照着填即可（同一份 schema 既可作全局，也可裁剪成本地覆盖放进某个 `.devloop/`）：

```jsonc
{
  // 聚合工作区根目录（形态 A）。非 git 仓 + AGENTS.md 带子项目表的目录会自动注册，
  // 这里一般留空；也可用 init_workspace.py 显式补充。
  "workspaces": [],

  // GitLab 依赖。MR / 状态注入需要它；没有 token 时 MR 相关功能静默跳过，其余照常。
  "gitlab": {
    "token": "",   // 个人 access token。也可用环境变量 GITLAB_TOKEN（优先级更高）
    "host":  ""     // 可选：覆写从 origin remote 推断的 host（SSH 别名 / 镜像 / 非标准 remote 时用）；留空=按 origin 推断
  },

  // 每仓库 lint 提交门禁（默认关）。打开后 lint 过期时 `git commit` 会被拦。
  "precommit": {
    "default": { "commit_gate_lint": false },
    "repos":   { "/abs/path/to/repo": { "commit_gate_lint": true } }
  }
}
```

> token 以明文存在 config.json，请勿把它提交进任何仓库。全局文件在 `~/.devloop/` 下、不在项目里；若放本地覆盖到某 repo 的 `.devloop/config.json`，该目录已被 devloop 加进 `.git/info/exclude`、不会被提交。需要彻底避免落盘时改用 `GITLAB_TOKEN` 环境变量。

## v0.1 范围 / 限制

- **Claude only**：Codex manifest 与验证推迟（架构保持 CLI-agnostic）。
- **GitLab 经 stdlib facade**：未引 python-gitlab / MCP（接口已按其形态设计，将来低成本替换）。
- 多 repo 协同、convergent test（按改动收敛跑）留后续迭代。
