# devloop plugin — 共享概念

devloop 内多个 skill / 脚本共用的术语。架构理念见 [`AGENTS.md`](./AGENTS.md)。

## 路径术语

- **subproject**：聚合工作区直接子项中「是 / 指向 git 仓」的那些目录——存在性由**文件系统自发现**判定（`hooks/lib/context/workspace.py::discover_subproject_names`，判据：子目录含 `.git`），而非手写表格。workspace `AGENTS.md` 的子项目表是**可选润色**，按目录名 join 补 `aliases` / `role`（`language` 缺省自动探测，表格显式值覆盖）；表格里有、文件系统没有但目录尚存的行仍保留，渐进收敛。
- **`repo_dir`**：子项目目录入口（可能是软链接）。用前 `realpath` 确认真实路径。
- **`code unit`**（`repo_layout.CodeUnit`）：仓库内一个可独立 build/lint/test 的项目目录，也是该 unit 工具命令的 workdir。**身份由语言的项目清单定义**（`_is_code_unit`：`go.mod` / `pyproject.toml` / `setup.py` / `package.json`——TS 与 JS 同为 `package.json`，`tsconfig.json` 只是编译配置、一个 package 里可有多份，不定义边界）。**`Makefile` 刻意不算身份**：它是 unit 的**动作入口**（怎么 lint/test，见下）——doctor 的 `server/` 与 `cli/` 都有 Makefile，但让它们成为 unit 的是 pyproject / package.json；反过来 `docs/` 里一个 sphinx Makefile、仓根一个转发用的编排 Makefile，都不该因此变成 code unit。`requirements.txt` 同理不算（依赖清单不是项目边界，常见形态正是仓根放一份给容器构建、真项目在 `server/`），但它仍是 `detect_language` 的语言线索——「这是什么语言」和「这是不是一个项目」是两个问题。一个 git 仓可含**多个** unit（`server/` + `cli/`、`packages/*`、`cmd/*`），**仓根也可以是其中之一**（根是 Go module + `server/` 是 Python unit 时两者都在）——所以「哪个 unit」由**路径**决定、不是 repo 的单值属性。这里有**两个不同的问题**，别混：
  - **归属**（`owning_code_unit`）：「这个**文件属于**谁」。从它向上找最近的项目清单目录，止于仓根；**没有 unit 拥有它就是 `None`**——仓根的 `README.md` / `docs/` / `.github/` 在「根不是 unit」的仓里确实不在任何 unit 的项目边界内，`None` 是答案不是失败。改动投影（`select_units`）与 lint 指纹用它：共享路径**不贡献 unit 也不减少**，于是纯文档改动不触发任何 unit 的验证。刻意**不做**「共享路径→全部 unit」——那是拿最常见的情况（改文档/CI）去付最罕见情况的账，每个文档 PR 都跑全仓、任一 unit 有存量错就拦你。代价是根级 tooling 配置（`ruff.toml`）改动不触发验证：它在下一次任何 lint 里立刻暴露，且 CI 兜底。
  - **站位**（`enclosing_code_unit`）：「我**在这儿干活**算哪个 unit」。必给答案：有 owner 就是它，没有则回落 default 的选择启发式。命令侧 guard（站在仓根跑 `pip install`，总得有个 unit 判 uv）与 `select_units(explicit=…)` 用它。
  拿站位去答归属，就是让「没有具体目标该选谁」的启发式去回答「这个文件属于谁」——结论是「改 README → 跑 server 的 lint」，而「为什么是 server 不是 cli」没有任何理由。这正是过去单值 `repo_code_dir` 在多代码目录仓上选错目录的根因。「本轮落在哪些 unit」由 `select_units` 按**本次改动**算（产出 `WorkSet`），**不挂在 `ResolvedRepo` 上当单值属性**——解析结果只带 `target_path` 作 explicit 信号。unit 有**两个身份**，别混：`path`（绝对路径）是「这次在哪跑 make」的执行事实；`id`（仓相对路径）是**持久化身份**，跨 checkout / worktree 稳定，落 `.devloop` 的一切 key 都用它（见〈验证状态〉）。两者都在**出生点**由唯一构造入口 `CodeUnit.at(path, git_root)` 一次算清——`id` 必填、无默认值：给默认值则生产路径漏传就是静默的空 key（多个 unit 撞进同一个戳），让消费方自己算又要各自再传一次 `git_root`（传错 root 算出的 key 就是错的）。绑在出生点，这两类错都不可表达。unit 还**拥有自己的工具链动作**（`has_target` / `lint_target` / `test_command`）：一个 unit「能不能 / 该跑哪个 lint/test 命令」是它自己的事实，checks / gate rules 直接问 unit，不再各自拿 `str` 路径去重解析 Makefile 或 Go module。
- **`repo_code_dir`** / **default unit**：repo 级**默认** unit——没有更具体目标路径时用（按名字 `/enter` 一个仓、cwd 就是仓根）。探测规则 `server/` > `backend/` > `repo_dir`（`repo_layout.find_repo_code_dir` / `default_code_unit`）。这是**选择**启发式（「没有具体目标该选谁」），与上面的**身份**判据（「这个目录是不是 unit」）是两个问题，别拿它回答身份——`discover_code_units` 曾用它补根 unit，而 `server/` 存在时它返回 `server/`（早被枚举收过），于是补根永远补不进、根的 go.mod 从 catalog 里消失。Go / TS 单 unit 仓通常就是 `repo_dir`。子项目 `AGENTS.md` 一定在默认 unit 下面。

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

branch 域 `branches/<b>/lint.json` + `test.json`：验证戳**按 code unit 键**——key 是 unit 的仓相对身份 `CodeUnit.id`（`.` / `server` / `cli`），每个 unit 各记自己的 lint / test 通过时间，以及 **lint 通过那一刻的内容指纹**（`repo_resolve.unit_fingerprint`）。落盘拆两个段、`RepoContext.load()` 合并成一个内存视图（消费方看不到拆分）。

**三个维度各归各位**：**branch** 是目录（域，切分支即自动隔离）、**check** 是文件（= writer-role）、**unit** 是文件内的 JSON key。
- **check 必须拆到文件**：`lifecycle.dispatch` 用线程池**并发**跑 lint 与 test，而 segment 的纪律是「single-writer whole-file overwrite」。两个 writer 写同一个文件 = load-modify-write 互相覆盖，实测会丢戳——丢 lint 戳只是白跑一遍，丢 **test** 戳则是状态说「没测过」而其实测过，是**记录失真**。拆开后各写各的，这一类结构上不可能，不需要锁、也不需要「写前重读合并」（那只是把「不可能」降级成窗口更窄的 race）。
- **unit 不拆成目录**：unit **不是 writer**（同一 check 内 fan-out 是顺序的），拆了不解决这个 race；且 unit id 不是安全路径分量（根 unit 是 `.`、可含斜杠如 `eval/reviewbench`），当目录还得枚举目录才能读回全集。当 JSON key 三个问题都没有。

**lint 通行证绑内容，不绑「改了几次」**：gate 拿当前指纹与戳上的比，不等 = 内容变过 = 通行证作废。指纹 hash 的是该 unit 名下**已改动文件的当前 bytes**（删除写 tombstone、symlink 写 target），不是 diff——新建的未跟踪文件改了内容路径不变，diff 看不出来，而 lint 会 lint 它。算不出 → gate 按未验证处理（fail-closed）。

**为什么不是编辑计数**：旧的 `edits_since_lint` 由 PostToolUse 计数，而那个 hook 只认 `Edit`/`Write`/`NotebookEdit`——**Codex 用 `apply_patch` 改文件一次都不会计**（`MultiEdit` 同理，Bash 里的 `sed -i` / 脚本更不会）。它读出的 0 是「没人报告」而不是「没改过」：一个只在部分 CLI、部分工具上生效的计数器守不住硬 gate。改问内容之后，谁改的、用什么工具改的都不重要，那个 hook 也随之删掉（少一个会漂的活动部件）。

**turn 注入只报「什么时候验过」，不报「还算不算数」**：后者要现算指纹（读改动文件的 bytes），而注入每轮都跑——按〈成本原则〉不值当，且陈旧与否本就该由 gate 在 commit 时判（那里算一次是合算的），gcam 路径更是直接把 lint 跑掉。

**为什么不是 repo 级单值**：lint / test 本就按 unit 执行（一个仓可有 `server/` + `cli/` 两套工具链），repo 级单戳表达不了「A 过 B 挂」——A 通过盖下的戳会让 `precommit_gate` 读到「已验、无待验编辑」而放行整个仓，于是一次 partial-fail 的 fan-out 恰好把防绕过守卫的锁打开（gate 挡住了 gcampr，却给裸 `git commit` 发通行证）。key 与执行范围（`WorkSet`）同粒度，这类偏差才**不可表达**，而不是靠各消费方记得多问一句。

**为什么 key 是仓相对路径而非 `CodeUnit.path`**：后者是绝对路径（「这次在哪跑 make」），而 validation 段统一落**主仓** `branches/<b>/`——worktree 里的 `<repo>/.worktrees/foo/server` 与主 checkout 的 `<repo>/server` 是同一个 unit，用绝对路径会让同一 unit 在不同 checkout 下拿到不同 key（worktree 里 lint 过的戳回主 checkout 查不到，白跑一遍），且 key 随 worktree 增删无限累积。

旧的扁平格式读进来是「都没验过」，要求重跑一次 lint——**刻意不写迁移**：`.devloop` 是 cache 不是事实源，退化方向是 fail-closed。

## 状态文件 `.devloop/`

AGENTS.md 是文字知识源；`.devloop/*.json` 是由 hooks / scripts / monitors 维护的结构化运行态，不保存 AGENTS.md 正文。

- Workspace 级：`<workspace_root>/.devloop/context.json`，保存 workspace AGENTS.md 的 References + 文件系统自发现的 subproject 清单（叠加 AGENTS.md 表润色，symlink 子项目附 canonical 路径映射）以及 session 注入节奏；`active/<session_id>.json` **一 session 一文件**保存该 session 绑定的最近活跃 repo（脚本在 workspace 根被调用时的解析兜底）——这是 session 态而非 workspace 态，owner 即 session，按 writer-owner 铁律落盘零例外；多 session 各干各的仓互不劫持。语义与生命周期见〈Session 运行态〉。
- Repo 级（按**三域**布局，linked worktree 一律解析到**主仓**的 `.devloop`）：**repo 域**根下 `meta.json` / `remote_branches.json` / `pr.json` + ledgers（`requirements/` / `friction.jsonl` / `review-history.jsonl`）；**branch 域** `branches/<branch>/` 下 `branch.json` / `lint.json` / `test.json` / `injection.json` / `review.json`，`RepoContext.load()` 按 **live 分支**取段后合并成内存视图；**working-tree 域** owner 锁留各 worktree 自己的 `.devloop`（并行 worktree 互不干扰）；`tmp/` 收进程间随手产物（commit_msg / review.log / ccr history feed），随时可删。`branch.json`（local + worktrees，refresh owned）与 `remote_branches.json`（远端 trunk tips，monitor owned）是同一分支拓扑的两个 owner——见 [`docs/branch-state.md`](./docs/branch-state.md)〈落盘:按 writer-owner 拆段〉。

schema / TTL / cap 数值在 `hooks/lib/context/base.py`，不在文档复述。

## 脚本的 repo 解析

smart_git_ops / run_fixlint / run_tests 与 cwd 解耦（session cwd 在聚合工作区常驻 workspace 根）：repo 按"显式参数（`--repo` 名/路径）→ cwd 所在仓库 → 本 session 绑定的最近活跃仓（`active/<sid>.json`，见〈Session 运行态〉）"解析，解析来源自述在输出/PLAN 里。本 session 无绑定即拒绝兜底，报错附其它 session 的活跃仓做候选提示。名字走与 `/enter` 相同的模糊匹配（`hooks/lib/repo_resolve.py`）。

## 占位符 `<PLUGIN_ROOT>`

skill / 文档里脚本调用写 `<PLUGIN_ROOT>`，AI 按当前 CLI 替换：Claude Code → `${CLAUDE_PLUGIN_ROOT}`。这样不写死某一 CLI 的 env，未来加 CLI 只多一行映射约定。

## 变更策略引擎（Change → Target → Rule → Decision）

PreToolUse 守卫统一成一个策略引擎（`hooks/lib/core/` + `hooks/lib/rules/`）：一次工具调用投影成 **`Change`**（携带 `Command` / `FileChange` 等 **`Target`**），跑匹配的 **`Rule`** 产出 `Finding`，聚合成 **`Decision`**（allow/warn/deny）。两个入口 hook——`pretool_policy_bash`（命令侧）、`pretool_policy_edit`（编辑侧）——把原先 10 个独立 guard 收成 2 个。

- **`Target`**：被规则评判的主体（开放层级，仿 k8s resource）。`Command`（`cmdtree` 投影）/ `FileChange`（`codemodel` 投影，惰性带 `imports`/`decls`/`layer`）。**无顶层 operation 轴**，动词内置（`Command.subcommand` / `FileChange.mode`）。
- **`Rule`**：可积累的判定单元，子类住 `hooks/lib/rules/{command,edit,code}/`、登记进 `rules.REGISTRY`；引擎按 `target_kind` 路由。`needs_content` 的 FileChange 规则才触发内容解析。逐规则 **fail-open**（守卫的 bug 绝不拦用户）。命令侧规则按风险黑名单设计：默认放行，只拦高置信的项目级 / 协作级风险，不把未知命令当违规。
- **`Decision`**：`Finding.severity` 聚合——`deny`→硬拦（杠杆②）、`warn`→软提示。**刻意不叫 `verdict`**：`verdict` 专指评测 harness 的结构化输出。
- **`arch` 配置**（`config.arch`）：层级依赖规则（`layers`/`order`/`enabled`），走 config 分层、默认 opt-in off。

完整设计与取舍见工作区 `docs/loop-architecture-v2.md`。
