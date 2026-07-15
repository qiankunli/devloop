# devloop plugin

**面向「聚合工作区 + 多 subproject」开发的 plugin**（native-first 实现）。一个 workspace 挂着项目的全部代码仓，开发者在其中转一个循环——enter 子模块 → 提需求（可能跨多个 subproject）→ 开发 → commit / 建 PR（含 lint/test）→ 人工 merge → 下一轮；排查问题也在同一 workspace（as-ops / infra-ops 等发现问题后切到子模块开发）。devloop 靠**实时状态注入**（每轮 prompt 知道当前 subproject 的 branch / 工作区 / 近期 PR）+ **执行级硬拦截**（保护分支、`git add -A`、过期分支改文件等直接 deny），提高这个循环里 AI 的首次成功率。评审平台 GitHub / GitLab 均支持，按 repo 的 origin 自动识别。

> 架构 / 扩展看 [`AGENTS.md`](./AGENTS.md)；术语看 [`CONCEPTS.md`](./CONCEPTS.md)。

独立 `.devloop/` 命名空间状态，与其它工具互不干扰。当前支持 **Claude Code + Codex**；Claude 使用完整 native event，Codex 使用其 hook 子集并在少数事件上降级。

## 它做什么

- **状态总线**：workspace 维护 `<workspace>/.devloop/context.json`（subproject 清单含 symlink→真实路径映射 / AGENTS.md References / 注入节奏）+ `active.json`（最近活跃 repo），每个 git 仓库维护 `<repo>/.devloop/{meta,branch,pr,validation,injection}.json`（branch / 保护标记 / target / ahead-behind / 近期 PR 窗口 / 各 code unit 的 lint-test 时间），每轮 prompt 注入；自动加入 `.git/info/exclude`，不会误提交。
- **code unit 感知**（多代码目录仓）：一个 git 仓可能有多个自带工具链、可独立 lint/test 的目录——`server/` + `cli/`、`packages/*`、`cmd/*`。devloop 按**本次改动**决定跑哪些：改了 `cli/**` 就只跑 `cli` 的 lint/test，不静默回落仓根或 `server/`；改动跨多个 unit 就都跑；clean tree 从仓根发起时枚举**全部** unit（绝不替你猜一个）。验证戳也按 unit 记——「A 过 B 挂」不会被记成整仓已验。术语见 [`CONCEPTS.md`](./CONCEPTS.md)。
- **硬拦截**（PreToolUse deny）：保护分支 commit/push、`git add -A`、过期分支（PR 已 merged/closed）改文件、别的 session 占用的 checkout 上切分支或改文件（引导 worktree）、工作区根跑子项目命令、裸 `pytest`、uv 项目 `pip install`、编辑 `requirements.txt`、`lifecycle.pre_commit` 含 lint 时 lint 过期的裸 `git commit` gate。
- **PR 感知**：后台 monitor 周期轮询 forge（GitHub/GitLab），把当前分支的 PR + 近期 PR 窗口写进状态，注入里以 `Recent PRs:` / `Recent MRs:` 呈现（按 provider）。
- **自动进项目**：`cd` 进子项目时（`CwdChanged`）自动刷新上下文、浮现 AGENTS.md References，无需手动 `/enter`。
- **Codex 降级**：Codex 没有 `CwdChanged / FileChanged / SessionEnd`，`hooks.codex.json` 用 `PostToolUse` 刷新 cwd / command-scoped repo 状态；AGENTS.md 变更重注入与 owner 锁释放走已有 TTL / 下一轮兜底。
- **生命周期 hook**：`pre_commit / post_commit / pre_mr / post_mr` 四相位可挂 hook，挂哪相位由 config 决定；两类——**inline 门禁**（失败挡 commit/MR）与 **signal hook**（advisory、后台跑、不挡）。当前内置三个：`lint`、`test`（门禁），`review`（signal——后台跑 [ocr](https://github.com/alibaba/open-code-review) 审全量改动、结果回流会话、有开放 MR 时发评论）。机制见 [`docs/lifecycle-hooks.md`](./docs/lifecycle-hooks.md)；code-review 细节见 [`docs/code-review.md`](./docs/code-review.md)。

## Slash 命令

| 命令 | 作用 |
|------|------|
| `/enter <name|path> [--worktree <tag>]` | 按名/路径跳进子项目（context 自动加载） |
| `/gcam "<msg>"` | 只 commit |
| `/gcamp "<msg>"` | commit + push |
| `/gcampr "<msg>" [--branch <name>]` | commit + push + 建/复用 PR/MR |

lint / test 无独立 slash 命令：正常由 gcam* 的 `pre_commit` gate 自动触发；手动跑走 fix-lint / run-test skill（自然语言"修下 lint"/"跑下测试"）。两条路径**共用同一套 code unit 选择**，所以 gate 替你跑的和你手动跑的永远是同一批 unit；每次执行会自述本轮选了哪些 unit、为什么（`changed files under: cli` / `clean tree, all units: …`），选错一眼可见，不用等错的测试跑完再猜。

保护 / 过期分支上，gcam* 需 `--branch <name>`，脚本会从 `origin/<target>` 切新分支（不给会拒绝并提示）。

gcam* 与 fix-lint / run-test 都不依赖 cwd：默认解析 cwd 所在仓库，在 workspace 根则兜底到最近活跃子项目；用 `--repo <name|path>` 显式指定，无需 `cd` 前缀。

## 安装

运行时要求：**Python 3.10+**。devloop launcher 会从 PATH 自动选择首个满足版本的 `python3`、
`python` 或带版本号的 `python3.x`；需要固定解释器时设置 `DEVLOOP_PYTHON`。

```
# Claude Code 内
/plugin marketplace add https://github.com/qiankunli/devloop.git
/plugin install devloop@devloop
```

Codex：

```
codex plugin marketplace add https://github.com/qiankunli/devloop.git
codex plugin add devloop@devloop
```

也可以添加 marketplace 后，在 `/plugins` 中安装 `devloop`。安装后建议新开一个 Codex session；如果 Codex 提示需要审核 hook，打开 `/hooks` 并信任 devloop hooks。

更新：

```
# Claude Code
/plugin marketplace update devloop
/plugin update devloop

# Codex
codex plugin marketplace upgrade devloop
codex plugin remove devloop@devloop
codex plugin add devloop@devloop
```

更新后建议新开一个 session，让运行时重新加载最新 hooks 和 skills。用户级配置保存在 `~/.devloop/`，不会被 plugin 更新删掉。

初始化（可选——hook 首次 cd 会自动建）：

```
# Claude Code
"${CLAUDE_PLUGIN_ROOT}/scripts/python" "${CLAUDE_PLUGIN_ROOT}/scripts/init_repo.py"            # 单仓库
"${CLAUDE_PLUGIN_ROOT}/scripts/python" "${CLAUDE_PLUGIN_ROOT}/scripts/init_workspace.py" <dir> # 聚合工作区

# Codex
"${PLUGIN_ROOT}/scripts/python" "${PLUGIN_ROOT}/scripts/init_repo.py"            # 单仓库
"${PLUGIN_ROOT}/scripts/python" "${PLUGIN_ROOT}/scripts/init_workspace.py" <dir> # 聚合工作区
```

## 配置（`~/.devloop/config.json` + 本地覆盖）

devloop 对外部的依赖（连哪个 forge、用什么 token）+ 工作区注册表 + 提交门禁，**统一收在一个全局文件** `~/.devloop/config.json` 里。放用户级目录（不是 plugin 目录）是因为 plugin 目录是版本化 cache，`/plugin update` 会把写进去的东西清掉。可用环境变量 `DEVLOOP_CONFIG_DIR` 覆写目录。

**本地覆盖（就近优先）**：任一 repo / workspace 可以在自己的 `.devloop/config.json` 里只写要改的几项（比如该 repo 用不同的 forge token）。读取时按「**离 repo 最近的赢**」分层合并：`默认值 < 全局 ~/.devloop/config.json < 上层 .devloop/config.json（由外到内，最近的覆盖）`；没写的段落自动落回外层。本地文件**只读、手写**——devloop 自己的写入（如工作区自动注册）只落全局文件。

文件不存在也能用——所有项都有默认值，hook 首次运行时会按需创建。`devloop/config/config.example.json` 是带注释占位的模板，照着填即可（同一份 schema 既可作全局，也可裁剪成本地覆盖放进某个 `.devloop/`）：

```jsonc
{
  // 聚合工作区根目录（形态 A）。非 git 仓 + AGENTS.md 带子项目表的目录会自动注册，
  // 这里一般留空；也可用 init_workspace.py 显式补充。
  "workspaces": [],

  // 代码评审平台，按 repo 的 origin host 索引。PR/MR 创建与状态注入需要 token；
  // 没有匹配 token 时相关功能静默跳过，其余照常。provider 由 host 推断，type 可覆写。
  "forges": {
    "github.com": {
      "type": "github",
      "token": ""     // 也可用环境变量 GITHUB_TOKEN / GH_TOKEN（优先级更高）
    },
    "gitlab.example.com": {
      "type": "gitlab",
      "token": "",     // 也可用环境变量 GITLAB_TOKEN（优先级更高）
      "api_host": ""   // 可选：origin 是 SSH 别名 / 镜像时，覆写真实 API host
    }
  },

  // 生命周期 hook：相位 → [hook 名]（opt-in，默认全空 = 零行为变化）。
  // lint/test 是阻塞门禁（失败拦 commit/MR）；review 是 advisory——后台跑 ocr 审全量改动、
  // 结果回流会话、分支有开放 MR 时发 MR 评论，从不挡 commit（需 ocr 自备 LLM）。
  "lifecycle": {
    "default": { "pre_commit": [], "post_commit": [], "pre_mr": [], "post_mr": [] },
    "repos":   { "/abs/path/to/repo": { "pre_commit": ["lint", "test"], "post_mr": ["review"] } }
  }
}
```

> token 以明文存在 config.json，请勿把它提交进任何仓库。全局文件在 `~/.devloop/` 下、不在项目里；若放本地覆盖到某 repo 的 `.devloop/config.json`，该目录已被 devloop 加进 `.git/info/exclude`、不会被提交。需要彻底避免落盘时改用 `GITHUB_TOKEN` / `GITLAB_TOKEN` 环境变量。

## v0.1 范围 / 限制

- **Codex 事件子集**：Claude 使用完整 native event；Codex 暂无 `CwdChanged / FileChanged / SessionEnd`，靠 `PostToolUse` 与已有 prompt / TTL 路径降级。
- **forge 经 stdlib facade**：GitHub / GitLab 各一个 adapter，缝在 provider 层；未引 SDK / MCP（接口已按中立领域设计，将来低成本替换）。
- **收敛只到 code unit 粒度**：跑哪些 unit 已按本次改动收敛（见上〈code unit 感知〉）；unit **内部**仍跑全量测试——按 diff 选具体测试（convergent test）留后续迭代。同理 test 失败只通报不挡 commit：判断"挂掉的测试是否与本次 diff 相关"需要 baseline-aware 分析，现阶段交给 CI / 人；lint 仍是硬拦截。
- 多 repo 协同（跨 subproject fanout / 发包依赖顺序）留后续迭代。
