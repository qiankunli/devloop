# 分支状态——三态 freshness 模型

devloop 关于分支的所有事实,过去挤在一个 `branch.json` + 一次 `RepoContext.load()` 里、同一种 freshness 待遇。它们其实分三类,**信任来源根本不同**,该用不同待遇。本文讲清这件事的 why;字段/常量的具体形状以代码为准(`hooks/lib/context/repo.py` / `gate.py` / `prstate.py`、`hooks/lib/git_state.py`)。

## 1. 理念:一个事实该多"新",取决于它会变得多快、读它要多贵、以及谁会偷偷改它

| 事实 | 谁会改它 | 读取成本 | 待遇 | owner / 落盘 |
|------|----------|----------|------|--------------|
| **身份**:当前分支名 + HEAD commit | 只有本会话(checkout) | ~1ms 本地 | 决策点 **live 现导**,从不缓存为真相 | refresh / `branch.json` |
| **写-gate**:当前分支的 PR/MR 合没合 | **同事 merge**(你观测不到) | forge 网络 | monitor 缓存 + gate 处对 live HEAD 校验 | monitor / `pr.json` |
| **读-freshness**:我落后 trunk 没 | **同事 push**(你观测不到) | git 网络(ls-remote / fetch) | monitor 拉远端 tip + `fetched_at` provenance | monitor / `remote_branches.json` |

后两类的共同点——**真相在你观测不到的通道里移动(同事的动作)**——决定了它们只能由 **monitor 主动去拉**:本地永远等不到"同事 merge / push"这个事件。这是整套模型的轴。

## 2. 核心对象:`Branch` / `BranchTopology`

- **`Branch`**(值对象):一条分支的事实——`name` + `commit`(tip sha)+ `fork_from` + `path`(worktree 用)。纯值对象,**不自带 freshness**:"这份认知多新"是持有它的段的属性(`fetched_at`),不是分支的属性。
- **`BranchTopology`**(容器,原 `BranchState`):仓的分支拓扑,跨 vantage:
  - `local: Branch` —— 本地 HEAD,refresh 拥有,是**展示副本**;
  - `remotes: list[Branch]` —— 服务端 trunk tips(`release` / `master` …),monitor 拥有;
  - `worktrees: list[Branch]` —— 本地 worktrees;
  - `target` / `pr_number` / `remotes_fetched_at` —— 从各自 owner 段 join。

`RepoContext.load()` 把三个 owner-disjoint 段合成这一个内存视图——**内存统一,落盘按 owner 散开**。

## 3. 关键设计

### 3.1 身份在决策点 live 现导,不信缓存(gate 真相)

`RepoContext.branch` 是**观测视图**,靠"被观测到的事件"(cd、可解析的 git 命令)刷新。一次**观测不到的 checkout**(子 shell `cd "$var" && git checkout`、`make`、另一个终端)之后,`branch.local.name` 就陈旧——基于它的 gate 会:

- **fail-open**:本地实际在保护分支、缓存还说 feature → 放行 push 到 main;
- **fail-closed**:切到新分支、缓存还说旧分支 → 误拦新分支上的编辑(本模型要根治的那次事故)。

所以所有 hard gate 走唯一入口 **`lib.context.gate.evaluate()`**:`branch` / `head_sha` 是 live `git rev-parse`,PR 活性用 live 分支 + live HEAD 在 monitor 缓存窗口上**本地 SHA 校验**(`pick_branch_pr` 的 merge-base 祖先判定,零 forge 调用)。CI 不变量测试(`test_gates_use_gate_seam_not_cached_identity`)钉死:任何 guard 不得读 `ctx.branch.*` / `branch_pr_inactive()` 做决策。

成本分层:edit-频率 guard(protect / merged)只付本地 git;低频出口 gcampr 传 `live_refresh=True` 先做一次权威 forge poll+persist。

### 3.2 PR 归属键是 `(branch, head_sha)`,不是分支名

`pick_branch_pr`(`prstate.py`):开 PR 优先,否则取 **source sha 是 live HEAD 祖先**的最近 finished PR。这让缓存窗口陈旧时**最多是找不到 PR(→ 不拦)**,绝不会为一个 HEAD 已不指向的 merged PR 复活拦截——同名复用(删了重建同名分支)因此不会误判(`test_gate_branch_name_reuse_not_falsely_inactive`)。

旧 `load()` 的二次弱键(`pr.json.branch == branch.json.current` 名字相等)被绕过:两段同源、同步陈旧时等式照样成立,会把旧 merged PR 号 join 回来——那正是事故根。gate 不吃这条 join,直接对 live 现算。

### 3.3 远端是缓存,带 provenance;ahead/behind 现算不存

`remotes` 由 monitor 每 tick `git ls-remote`(便宜,只拿 SHA,不 fetch 对象)写入 `remote_branches.json`,带 `fetched_at`。注入展示 behind 时强制带它:`behind N (vs main, as of 18:07)`;若 monitor 的真 tip 与本地 `origin/<base>` 镜像不一致,显示 `⚠ trunk moved since fetch … — fetch to recount`。**ahead/behind 是关系不是状态**,读时按 `local` vs `base_branch()` 的镜像现算,从不持久化(存它就是上次"behind 0 撒谎"的来源)。

`/enter`(刻意的"去看这个仓")是唯一付得起网络的点:进仓时若 `remote_branches` 陈旧(`REMOTE_VIEW_STALE_SEC`)就重拉 tips;若本地 trunk 镜像确实落后真 tip,再做**一次**有界 `git fetch`(只在真落后时付,clean enter 零成本)。

### 3.4 `fork_from`:git 不持久记录的事实,sticky

git 不durably记录"从哪条分支 fork"。它只在 devloop **切分支那一刻**精确已知,由 gcampr 写入(`set_fork_from`)。所以 refresh 从 live git 整段重建 `local` 时,**保留**已记录的 `fork_from`(分支名不变时;切分支则丢弃,旧分支的 fork 点不适用)——和 `pr_number` 同一待遇(`test_fork_from_sticky_across_refresh`)。手切 / 外部分支没有记录值,读时可用 `remotes` 做 merge-base 推断;**别把推断当真相**。

## 4. 落盘:按 writer-owner 拆段

一文件一 owner 是 devloop 铁律(跨 owner 的 read-modify-write 丢更新从结构上消除):

| 段 | owner | 内容 |
|----|-------|------|
| `branch.json` | refresh | `local` + `worktrees` + `target`(+ `local.fork_from` sticky) |
| `remote_branches.json` | monitor | `remotes` + `fetched_at` |
| `pr.json` | monitor | PR 窗口 + 当前分支 number(+ `head_sha` provenance) |

`remotes` 必须从 `branch.json` 拆出:owner 是 monitor(主动拉同事改动),与 `local`(refresh,本地事件触发)不同 owner。monitor own `pr.json` + `remote_branches.json` 两文件不违背铁律("没有文件有两个 owner")。

## 5. 一句话

**身份 = live;他人动作(PR 合并 / trunk 推进)= monitor 拉 + provenance 戳;关系(ahead/behind)= 现算。** `RepoContext` 是 display/注入的观测视图;gate(写)与读-freshness 都绕开它取真相。同一条原则:*会被拿去推理或 gate 的事实,必须带它的 freshness*。
