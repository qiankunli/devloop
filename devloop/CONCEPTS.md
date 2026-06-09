# devloop plugin — 共享概念

devloop 内多个 skill / 脚本共用的术语。架构理念见 [`AGENTS.md`](./AGENTS.md)。

## 路径术语

- **subproject**：聚合工作区直接子项中「是 / 指向 git 仓」的那些目录——存在性由**文件系统自发现**判定（`hooks/lib/context/workspace.py::discover_subproject_names`，判据：子目录含 `.git`），而非手写表格。workspace `AGENTS.md` 的子项目表是**可选润色**，按目录名 join 补 `aliases` / `role`（`language` 缺省自动探测，表格显式值覆盖）；表格里有、文件系统没有但目录尚存的行仍保留，渐进收敛。
- **`repo_dir`**：子项目目录入口（可能是软链接）。用前 `realpath` 确认真实路径。
- **`repo_code_dir`**：实际代码工程目录，`make` / `uv` 的 workdir。Python 常是 `<repo_dir>/server` 或 `backend`；Go / TS 通常就是 `repo_dir`。子项目 `AGENTS.md` 一定在它下面。`repo_layout.find_repo_code_dir` 探测。

## 保护分支

repo 级结构化状态里 `branch.json` 的 `protected=true` 时，注入头部以 `⚠️ PROTECTED` 提示，guard 据此拦截 commit/push。判定（`hooks/lib/git_state.py`）：`main` / `master` 严格匹配；`release` 严格匹配或以 `release` 开头 / 结尾；`release` 出现在中间不算（`feat/release-notes` 不受保护）。

## PR 模型（PR/MR 统一概念）

评审提案在 devloop 里是**中立领域对象 `PullRequest`**（GitHub PR / GitLab MR 同一概念），由 forge 层（`hooks/lib/forge/`）按 repo 的 origin 解析出对应实现产出；状态层只持久化与 join，不重定义。`PullRequest` **不带 provider**——provider 是 **repo 级事实**（一个仓要么 GitHub 要么 GitLab），存在 `pr.json` 段头；展示时用 `pr_label(provider, number)` / `vocab(provider)` 贴回词汇（GitHub `PR #`、GitLab `MR !`）。

- **`number`**：PR/MR 在 repo 内的编号（URL 里那个号；GitLab 的 `iid`、GitHub 的 PR number 统一为 `number`）。
- **`state`**：归一为 `open` / `merged` / `closed`（GitHub 的 open/closed + `merged` 布尔在 adapter 里收敛为这三态）。
- **`branch.pr_number`**：当前分支那条 PR 的编号（只存编号，整对象 join `prs`）。
- **`prs`**：近期 PR 窗口，monitor 周期 sweep 写入，cap 5。窗口策略是**领域层** `build_window`（最新 cap + 确保当前分支 anchor 在内），组合在 port 的 `recent()` + `get()` 原语之上——**对两家一致**，adapter 不各写一份；adapter 只管协议差异。
- **branch 归属**：`pr.json` 记录其写入时的 branch + provider；`RepoContext.load` 的名字相等 join（`pr.json.branch == branch.local.name`）是**展示级**——切分支时自失效，喂注入/提示够用。
- **分支失活（inactive）**：派生，不单独存。但**硬 gate 不读展示级 join**：`branch_merged_guard` / gcampr 走 `lib.context.gate.evaluate()`，按 **live 分支 + live HEAD** 在缓存窗口上做 SHA 祖先校验（`pick_branch_pr`），归属键是 `(branch, head_sha)` 而非分支名——观测不到的 checkout 后缓存陈旧也不会误判。详见 [`docs/branch-state.md`](./docs/branch-state.md)〈三态 freshness 模型〉。
- **远端 tip**：`remote_branches.json` 由 monitor `ls-remote` 拉服务端 trunk tips（同事 push 后本地 fetch 前就可见），带 `fetched_at`；注入据此给 ahead/behind 加"as of"限定，避免把落后的本地 checkout 读成"最新"。
- **在途（in-flight）**：同样按 join 派生（`state = open`，`branch_pr_in_flight`）——PR 已建、等人工 merge。与 inactive 互斥，二者加 protected / healthy 构成下面的四态。

## 分支状态流转

devloop 循环（`enter → 提需求 → 开发 → commit/PR → 人工 merge → 下一轮`）里，当前分支始终处于四态之一。处理强度按"是否有合法编辑场景"分两档——**没有合法编辑场景的硬拦，有合法例外的软提示**：

| 态 | 含义 | 派生自 | 处理 |
|----|------|--------|------|
| **protected** | main / master / release* | `is_protected_branch` | **硬拦** commit/push（`protect_branch`） |
| **healthy** | 普通 feature 分支，仍在开发（非以下三态） | 默认 | 正常开发 |
| **in-flight** | 已建 PR、等人工 merge | `branch_pr_in_flight`（state=open） | **软提示**（turn 注入 IN-FLIGHT 行）：新工作切新分支、改本 PR 才在此编辑 |
| **inactive** | PR 已 merged / closed | `branch_pr_inactive`（state∈{merged,closed}） | **硬拦** Edit/Write（`branch_merged_guard`） |

**为什么 in-flight 是软提示而非硬拦**：protected / inactive 在其上编辑**没有任何合法场景**，硬拦干净。in-flight 有一个合法例外——应评审改自己这条 MR（amend）——且"新工作 vs amend"无法可靠自动区分。硬拦就必然要逃逸口，逃逸口要么低命中（如 slash command）、要么把简单事弄复杂。所以把 in-flight 这个事实喂给 AI（turn 注入），由它自行选"切新分支"还是"在此 amend"，比硬拦更合适——这正是状态总线该干的（杠杆①），不是硬拦（杠杆②）。

一轮生命线全图与各拍的 hook/script 时序见 `docs/loop.md`。

**fork-off 的真正防线在提交期，而非提示**——`smart_git_ops` 的 **base 由意图定，不由 HEAD 当前态定**：开新工作（`--branch`）**永远** cut 自 `origin/<target>`（或显式 `--base` 栈式），与当前停在哪个态无关；新分支只许带本轮提交，夹带外来 commit 在 push/MR 前被自检拦下。这是结构性保证：哪怕 AI 没看提示，从 in-flight 分支 fork 也不会把它的提交带进新 PR。提示负责"别把新活提交到在途 PR 上"，base 规则负责"别 fork 出夹带"。

## Owner / guest session（checkout 占有）

聚合工作区下多个 CLI session（claude / codex …）并发操作同一 workspace 是常态；每个 checkout 同一时刻只属于一个 session：

- **owner**：第一个对该 checkout 做**变更动作**的 session——Edit/Write、切分支 / commit 等会碰可变面（working tree / index / 分支位置）的操作，任一建立占有；持有 `<git_root>/.devloop/owner.lock`（pid 存活 + ts-TTL 判活）。占有点：edit guard 首笔编辑 / checkout guard / posttool git 变更。**enter / 只读不占有**：enter 只是选中上下文，多 session 并读不互斥——判据与下面 gitignored 豁免同源：是否污染 owner 的 diff。
- **guest**：其它并发 session。guest 的两条破坏路径被硬拦——切分支（`checkout_owner_guard`）与直接 Edit/Write（`edit_owner_guard`），统一引导去 worktree（`/enter <repo> --worktree <tag>`）。
- **gitignored 文件豁免**：guest 写 gitignored 路径（eval 输出、运行日志…）放行——不进 owner 的 status/diff，无混入风险；放行不转移占有权。
- 刻意共享 checkout 的逃逸口：人工删 `owner.lock`。

为什么这样设计（acquire 跟活动走、edit 也要拦）见 AGENTS.md〈Owner 锁〉。

## 验证状态

repo 级 `validation.json`：`last_lint_at` / `last_test_at`（float epoch）+ `edits_since_lint`（距上次 lint 的编辑累计，PostToolUse Edit 自增，lint 通过清零）。

## 状态文件 `.devloop/`

AGENTS.md 是文字知识源；`.devloop/*.json` 是由 hooks / scripts / monitors 维护的结构化运行态，不保存 AGENTS.md 正文。

- Workspace 级：`<workspace_root>/.devloop/context.json`，保存 workspace AGENTS.md 的 References + 文件系统自发现的 subproject 清单（叠加 AGENTS.md 表润色，symlink 子项目附 canonical 路径映射）以及 session 注入节奏；`active.json` 保存最近活跃 repo（脚本在 workspace 根被调用时的解析兜底）；它的多个写入点（CwdChanged / PostToolUse / smart 脚本）写的是同一个事实"刚碰过哪个 repo"，last-write-wins，丢更新无害。
- Repo 级：`<git_root>/.devloop/meta.json` / `branch.json` / `remote_branches.json` / `pr.json` / `validation.json` / `injection.json`，按 writer-owner 分段保存 repo 运行态；`RepoContext.load()` 合并这些段成内存视图。其中 `branch.json`（local + worktrees，refresh owned）与 `remote_branches.json`（远端 trunk tips，monitor owned）是同一分支拓扑的两个 owner——见 [`docs/branch-state.md`](./docs/branch-state.md)〈落盘:按 writer-owner 拆段〉。

schema / TTL / cap 数值在 `hooks/lib/context/base.py`，不在文档复述。

## 脚本的 repo 解析

smart_git_ops / run_fixlint / run_tests 与 cwd 解耦（session cwd 在聚合工作区常驻 workspace 根）：repo 按"显式参数（`--repo` 名/路径）→ cwd 所在仓库 → workspace `active.json` 最近活跃仓"解析，解析来源自述在输出/PLAN 里。名字走与 `/enter` 相同的模糊匹配（`hooks/lib/repo_resolve.py`）。

## 占位符 `<PLUGIN_ROOT>`

skill / 文档里脚本调用写 `<PLUGIN_ROOT>`，AI 按当前 CLI 替换：Claude Code → `${CLAUDE_PLUGIN_ROOT}`。这样不写死某一 CLI 的 env，未来加 CLI 只多一行映射约定。
