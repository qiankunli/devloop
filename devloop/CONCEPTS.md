# devloop plugin — 共享概念

devloop 内多个 skill / 脚本共用的术语。架构理念见 [`AGENTS.md`](./AGENTS.md)。

## 路径术语

- **subproject**：聚合工作区直接子项中「是 / 指向 git 仓」的那些目录——存在性由**文件系统自发现**判定（`hooks/lib/context/workspace.py::discover_subproject_names`，判据：子目录含 `.git`），而非手写表格。workspace `AGENTS.md` 的子项目表是**可选润色**，按目录名 join 补 `aliases` / `role`（`language` 缺省自动探测，表格显式值覆盖）；表格里有、文件系统没有但目录尚存的行仍保留，渐进收敛。
- **`repo_dir`**：子项目目录入口（可能是软链接）。用前 `realpath` 确认真实路径。
- **`repo_code_dir`**：实际代码工程目录，`make` / `uv` 的 workdir。Python 常是 `<repo_dir>/server` 或 `backend`；Go / TS 通常就是 `repo_dir`。子项目 `AGENTS.md` 一定在它下面。`repo_layout.find_repo_code_dir` 探测。

## 保护分支

分支是否受保护是**派生**的（`Branch.is_protected()`，**不存进** `branch.json`）：注入头部据此打 `⚠️ PROTECTED`，而 `protect_branch` 硬 gate 走 `gate.evaluate()` 按 **live 分支名**判——缓存陈旧（观测不到的 checkout 后）也不会漏判保护分支。判定规则（`hooks/lib/git_state.py`）：`main` / `master` 严格匹配；`release` 严格匹配或以 `release` 开头 / 结尾；`release` 出现在中间不算（`feat/release-notes` 不受保护）。

## PR 模型（PR/MR 统一概念）

评审提案在 devloop 里是**中立领域对象 `PullRequest`**（GitHub PR / GitLab MR 同一概念），由 forge 层（`hooks/lib/forge/`）按 repo 的 origin 解析出对应实现产出；状态总线只持久化与 join，不重定义。`PullRequest` **不带 provider**——provider 是 **repo 级事实**（一个仓要么 GitHub 要么 GitLab），存在 `pr.json` 段头；展示时用 `pr_label(provider, number)` / `vocab(provider)` 贴回词汇（GitHub `PR #`、GitLab `MR !`）。

- **`number`**：PR/MR 在 repo 内的编号（URL 里那个号；GitLab 的 `iid`、GitHub 的 PR number 统一为 `number`）。
- **`state`**：归一为 `open` / `merged` / `closed`（GitHub 的 open/closed + `merged` 布尔在 adapter 里收敛为这三态）。
- **`branch.pr_number`**：当前分支那条 PR 的编号（只存编号，整对象 join `prs`）。
- **`prs`**：近期 PR 窗口，monitor 周期 sweep 写入，定长 cap（数值在 `hooks/lib/context/base.py`）。窗口策略是**领域层** `build_window`（最新 cap + 确保当前分支 anchor 在内），组合在 port 的 `recent()` + `get()` 原语之上——**对两家一致**，adapter 不各写一份；adapter 只管协议差异。
- **branch 归属**：`pr.json` 记录其写入时的 branch + provider；`RepoContext.load` 的名字相等 join（`pr.json.branch == branch.local.name`）是**展示级**——切分支时自失效，喂注入/提示够用。
- **分支失活（inactive）**：派生，不单独存。但**硬 gate 不读展示级 join**：`branch_merged_guard` / gcampr 走 `lib.context.gate.evaluate()`，按 **live 分支 + live HEAD** 在缓存窗口上做 SHA 祖先校验（`pick_branch_pr`），归属键是 `(branch, head_sha)` 而非分支名——观测不到的 checkout 后缓存陈旧也不会误判。详见 [`docs/branch-state.md`](./docs/branch-state.md)〈三态 freshness 模型〉。
- **远端 tip**：`remote_branches.json` 由 monitor `ls-remote` 拉服务端 trunk tips（同事 push 后本地 fetch 前就可见），带 `fetched_at`；注入据此给 ahead/behind 加"as of"限定，避免把落后的本地 checkout 读成"最新"。
- **在途（in-flight）**：同样按 join 派生（`state = open`，`branch_pr_in_flight`）——PR 已建、等人工 merge。与 inactive 互斥，二者加 protected / healthy 构成下面的四态。
- **title / description**：commit message 首行是 PR/MR title，body（首行之后）是 description——给细节一个出口，title 才不会被挤成超长单行。创建时直填；往在途 PR 续传 commit（gcamp / gcampr 复用路径）时 **append-only + 包含性去重**：人工编辑过的 description 不被覆盖，重试不重复；同步失败仅记 PLAN 注记（commit/push 已落地，description 是装饰性的，不因它失败）。

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
- **释放（两层都有）**：session 正常结束由 SessionEnd hook（`sessionend_release`）立即清掉本 session 的全部运行态（owner 锁含 worktree checkout + active 绑定）；崩溃 / hook 没跑到时退化到 pid 死亡判活，ts-TTL 仅在 pid 不可探测时兜底。
- 刻意共享 checkout 的逃逸口：人工删 `owner.lock`。

为什么这样设计（acquire 跟活动走、edit 也要拦）见 AGENTS.md〈Owner 锁〉。

## Session 运行态

session-scoped 运行状态的统一生命周期约定：**activity 时创建 → SessionEnd 释放（`sessionend_release` hook）→ pid / TTL 兜底过期**；落盘一文件一 owner（owner = session）。实现统一在 `hooks/lib/context/session.py`——状态总线按 owner 粒度分模块（session / workspace / repo），模块归属看事实的 owner，不看文件落在哪个目录。当前两个实例：

- **checkout 占有**：`<git_root>/.devloop/owner.lock`（上节）。
- **repo 绑定**：`<workspace_root>/.devloop/active/<session_id>.json`——"该 session 最近在干哪个仓"，喂脚本兜底解析与 workspace 根的 turn 注入。hook 写入带 payload 的 session_id，脚本经 `CLAUDE_CODE_SESSION_ID` 自识别。**绝不读别的 session 的绑定当答案**：无绑定即拒绝兜底、要求显式 `--repo`，他人绑定仅在报错里作候选提示——拿不准时最多麻烦一次，绝不静默走错仓。

## 验证状态

branch 域 `branches/<b>/validation.json`：`last_lint_at` / `last_test_at`（float epoch）+ `edits_since_lint`（距上次 lint 的编辑累计，PostToolUse Edit 自增，lint 通过清零）。

## 状态文件 `.devloop/`

AGENTS.md 是文字知识源；`.devloop/*.json` 是由 hooks / scripts / monitors 维护的结构化运行态，不保存 AGENTS.md 正文。

- Workspace 级：`<workspace_root>/.devloop/context.json`，保存 workspace AGENTS.md 的 References + 文件系统自发现的 subproject 清单（叠加 AGENTS.md 表润色，symlink 子项目附 canonical 路径映射）以及 session 注入节奏；`active/<session_id>.json` **一 session 一文件**保存该 session 绑定的最近活跃 repo（脚本在 workspace 根被调用时的解析兜底）——这是 session 态而非 workspace 态，owner 即 session，按 writer-owner 铁律落盘零例外；多 session 各干各的仓互不劫持。语义与生命周期见〈Session 运行态〉。
- Repo 级（按**三域**布局，linked worktree 一律解析到**主仓**的 `.devloop`）：**repo 域**根下 `meta.json` / `remote_branches.json` / `pr.json` + ledgers（`requirements/` / `friction.jsonl` / `review-history.jsonl`）；**branch 域** `branches/<branch>/` 下 `branch.json` / `validation.json` / `injection.json` / `review.json`，`RepoContext.load()` 按 **live 分支**取段后合并成内存视图；**working-tree 域** owner 锁留各 worktree 自己的 `.devloop`（并行 worktree 互不干扰）。`branch.json`（local + worktrees，refresh owned）与 `remote_branches.json`（远端 trunk tips，monitor owned）是同一分支拓扑的两个 owner——见 [`docs/branch-state.md`](./docs/branch-state.md)〈落盘:按 writer-owner 拆段〉。

schema / TTL / cap 数值在 `hooks/lib/context/base.py`，不在文档复述。

## 脚本的 repo 解析

smart_git_ops / run_fixlint / run_tests 与 cwd 解耦（session cwd 在聚合工作区常驻 workspace 根）：repo 按"显式参数（`--repo` 名/路径）→ cwd 所在仓库 → 本 session 绑定的最近活跃仓（`active/<sid>.json`，见〈Session 运行态〉）"解析，解析来源自述在输出/PLAN 里。本 session 无绑定即拒绝兜底，报错附其它 session 的活跃仓做候选提示。名字走与 `/enter` 相同的模糊匹配（`hooks/lib/repo_resolve.py`）。

## 占位符 `<PLUGIN_ROOT>`

skill / 文档里脚本调用写 `<PLUGIN_ROOT>`，AI 按当前 CLI 替换：Claude Code → `${CLAUDE_PLUGIN_ROOT}`。这样不写死某一 CLI 的 env，未来加 CLI 只多一行映射约定。

## 变更策略引擎（Change → Target → Rule → Decision）

PreToolUse 守卫统一成一个策略引擎（`hooks/lib/core/` + `hooks/lib/rules/`）：一次工具调用投影成 **`Change`**（携带 `Command` / `FileChange` 等 **`Target`**），跑匹配的 **`Rule`** 产出 `Finding`，聚合成 **`Decision`**（allow/warn/deny）。两个入口 hook——`pretool_policy_bash`（命令侧）、`pretool_policy_edit`（编辑侧）——把原先 10 个独立 guard 收成 2 个。

- **`Target`**：被规则评判的主体（开放层级，仿 k8s resource）。`Command`（`cmdtree` 投影）/ `FileChange`（`codemodel` 投影，惰性带 `imports`/`decls`/`layer`）。**无顶层 operation 轴**，动词内置（`Command.subcommand` / `FileChange.mode`）。
- **`Rule`**：可积累的判定单元，子类住 `hooks/lib/rules/{command,edit,code}/`、登记进 `rules.REGISTRY`；引擎按 `target_kind` 路由。`needs_content` 的 FileChange 规则才触发内容解析。逐规则 **fail-open**（守卫的 bug 绝不拦用户）。
- **`Decision`**：`Finding.severity` 聚合——`deny`→硬拦（杠杆②）、`warn`→软提示。**刻意不叫 `verdict`**：`verdict` 专指评测 harness 的结构化输出。
- **`arch` 配置**（`config.arch`）：层级依赖规则（`layers`/`order`/`enabled`），走 config 分层、默认 opt-in off。

完整设计与取舍见工作区 `docs/loop-architecture-v2.md`。
