# devloop plugin — 设计与开发指南

面向二次开发者。使用向（装 / 配 / 跑）见 [`README.md`](./README.md)；共享术语见 [`CONCEPTS.md`](./CONCEPTS.md)。

---

## 项目定位与边界

**面向场景（本质）**：devloop 服务于**聚合工作区 + 多 subproject**、且**可能有多个 CLI / session（claude / codex / opencode …）同时在操作同一个工作区**的开发。一个 workspace 文件夹挂着某项目的全部代码仓（subproject）；workspace 与每个 subproject 各有自己的 AGENTS.md（项目边界 / 子模块 / 关键约定，跨文件细节下沉到 `docs/` 由 AGENTS.md refer）。AGENTS.md 是**文字知识源**，面向人和 AI 说明"这里是什么、边界在哪、该读什么"；`.devloop/*.json` 是**结构化运行态**，由 hooks / scripts / monitors 从 git、forge（GitHub/GitLab）、验证命令和 AGENTS.md 解析结果派生，面向注入和 guard 做快速决策。

开发者在这个 workspace 里转一个循环：

```
enter 某子模块 → 提需求(可能跨多个 subproject) → 开发 → commit / 建 PR(含 lint/test) → 人工 merge → 下一轮
```

排查问题也在同一 workspace：用 as-ops / as-web-ops / infra-ops 等发现问题后，带着上下文切到对应 subproject 开发。**workspace 是开发与排查的共同根，subproject 是动手的落点**——devloop 的全部能力，都是为提高这个循环里 AI 的首次成功率而生。一轮循环的端到端时序（每拍触发什么事件、哪些 hook/script 参与、三条贯穿流程线）见 [`docs/loop.md`](./docs/loop.md)。

**两个杠杆**（怎么提高首次成功率）：

1. **状态总线消除信息滞后**：`.devloop/` 状态总线把当前 subproject 的 branch / 工作区 / 近期 PR / validation，加上 workspace 级的子项目清单 + AGENTS.md References 的解析结果，实时注入每轮 prompt——AI 改第一行前就掌握现状。
2. **硬拦截把软约定变成执行级边界**：PreToolUse `deny`，AI 绕不过（保护分支、过期分支改文件、误带文件…）。

**实现取向**：native-first——承载这两个杠杆的机制全部坐到 Claude Code 原生事件（CwdChanged / PostCompact / FileChanged / monitors）+ 两个统一 facade（gitcmd / forge）上，代码自证、文档薄。独立 `.devloop/` 命名空间，状态与其它工具互不干扰。

**边界**：
- **两级模型**：聚合工作区（Mode A，多 subproject）与单仓库（Mode B）都支持，按全局配置（`~/.devloop/config.json` 的 `workspaces`）自动判定，workspace 可选。配置**不放 plugin 目录**——那是版本化 cache，`/plugin update` 即清零；workspace 根（非 git 仓 + AGENTS.md + 至少一个子项目，子项目存在性由文件系统自发现或 AGENTS.md 表声明）在 SessionStart / cd / resolve 时自动注册，手工 init 不是前置。
- **跨 subproject 需求是常态，但 0.1 的多 repo 协同（fanout / 发包依赖顺序）仍是占位**——当前以"逐个 enter subproject 开发"承载。
- 只管循环里"在 subproject 内动手开发"这一段的 git / 工作区 / 验证效率；**不做**问题发现与 trace（as-ops / infra-ops 等）、部署、通用 git 教学。
- 当前 **Claude-only**（CLI-agnostic by construction：hook 不读 plugin-root env、payload 只用公共子集、`${CLAUDE_PLUGIN_ROOT}` 占位；Codex 跟上加个 manifest 即可，0.1 不投入）。

---

## 代码地图与核心模块

```
devloop/
├── .claude-plugin/plugin.json     # Claude manifest（0.0.1；靠目录约定自动发现）
├── hooks/
│   ├── hooks.json                 # 事件注册
│   ├── lib/                        # CLI-agnostic 纯逻辑（无协议依赖，sys.path 自定位）
│   │   ├── hook_io.py             #   ★hook harness：guard / inject / observe / run 四个 runner（CC 原生 event 侧）
│   │   ├── notify/                 #   ★notify 端口：Notification + Notifier（base）；channel（ChannelNotifier + run_channel）是第一种投递
│   │   ├── gitcmd.py              #   ★统一 git runner（超时 + failure-safe，唯一 git 入口）
│   │   ├── forge/                 #   ★统一 forge facade（GitHub/GitLab，按 repo 分发）
│   │   │   ├── base.py            #     中立领域：PullRequest / Comment / Forge port（仅原语）/ build_window 策略 / pr_label / parse_pr_number
│   │   │   ├── _rest.py           #     唯一 HTTP 传输（urllib，可配 base/auth）
│   │   │   ├── gitlab.py github.py#     两个 adapter（iid↔number / state 归一 / 路径差异）
│   │   │   └── __init__.py        #     resolve_forge（origin+config+env 一处合一）+ forge_for_repo 分发
│   │   ├── cmdtree/               #   ★命令解析子系统（可插拔后端）：base（中立命令树 IR + Parser 接口）+ parable（Parable AST→IR 后端）+ cmdparse（在 IR 上走 commands/git_invocations/cd-scope 的 walker+facade）；换 parser=改 cmdparse 一行 import
│   │   ├── _vendor/               #   ★第三方原样 vendor（parable.py MIT + LICENSE/PROVENANCE）；永不手改
│   │   ├── lifecycle.py           #   ★devops 生命周期 hook facade（pre_commit/post_commit/pre_mr/post_mr）：并发 join + 聚合
│   │   ├── checks.py              #     lint/test 内置 inline-gate handler（与 /lint /test 共用，单一事实源）
│   │   ├── repo_resolve.py        #   ★脚本的 cwd 无关 repo 解析（--repo 名/路径 → cwd 仓 → last-active）
│   │   ├── git_state.py  parsers.py  repo_layout.py  workspace.py
│   │   └── context/               #   .devloop/ 状态总线，按 owner 粒度分模块：base / session / repo / workspace
│   │                              #     session.py = session 运行态（active 绑定 + checkout owner 锁）
│   ├── cwdchanged_enter.py        # CwdChanged：自动 enter
│   ├── sessionstart_init.py       # SessionStart：References(additionalContext) + watchPaths + 预热
│   ├── userprompt_inject.py       # UserPromptSubmit：turn + session 注入
│   ├── postcompact_reinject.py    # PostCompact：清注入 dedup
│   ├── filechanged_refs.py        # FileChanged：AGENTS.md 变更重注入
│   ├── posttool_git_refresh.py / posttool_track_edits.py   # PostToolUse 状态写入
│   ├── sessionend_release.py      # SessionEnd：释放本 session 的 owner 锁（正常退出路径）
│   └── pretool_*.py               # 10 个硬拦截（guard harness；含 checkout/edit owner 锁）
├── scripts/                        # smart_git_ops + smart_*.sh / pr.py（show/list/update/close；create 归 gcampr）/ run_fixlint / run_tests / poll_pr_status / init_*
├── monitors/monitors.json          # ★PR-sweep 后台轮询（替代 hook 心跳 scheduler）
├── commands/                       # slash：enter / gcam / gcamp / gcampr / lint / test
├── skills/                         # git-ops / gcam / gcamp / gcampr / fix-lint / run-test
└── config/                         # config.example.json 模板；全局配置在 ~/.devloop/config.json，repo/workspace 可在 .devloop/config.json 就近覆盖
```

---

## 关键约定

### 1. hook harness + notify 端口（两个 producer 侧）
- **hook 侧（CC 原生 event）→ `hooks/lib/hook_io.py`**：每个 hook = 一个函数 + 一个 runner：`guard(decide)`（PreToolUse，返回 deny 理由或 None，异常→放行 fail-open）、`inject(produce, event)`（返回注入文本）、`observe(handle)`（副作用，恒输出 `{}`）、`run(build, event)`（富 payload，如 SessionStart 的 additionalContext+watchPaths）。runner 保证 hook 永不打断用户工具调用。
- **非 hook 侧（外部系统）→ monitor（拉）+ `hooks/lib/notify`（推）**：forge / deploy / verdict 这类外部状态没有 CC 原生 event。**拉**：monitor 轮询写状态总线（`poll_pr_status.py` 写 `.devloop/pr.json`，**persist-only**，喂 guard / inject）。**推**：走 notify 端口——`base` 定义 `Notification` + `Notifier`，`channel` 的 `ChannelNotifier`（push 成 Claude Code channel 事件 → 唤醒会话、内容 inline）是第一种投递，`run_channel` 是复用壳；producer（`scripts/forge_channel.py`）盯状态总线的变化、build `Notification` 交给 `Notifier`。deploy/verdict 源照此加一个 producer 即可。channel 是 research preview / opt-in（见 References）。

一个变化同时走"喂状态总线（拉）"与"推给 agent"两条路：

```
hook    变化 ──────────────────────────▶ 状态总线 ─▶ 消费(inject 每轮 / guard 用工具时)   ← 拉
monitor 变化 ─▶ persist ──────────────▶ 状态总线 ─▶ 消费                                ← 拉
channel 变化 ─▶ Notifier.deliver ─────▶ push 进会话（唤醒 agent + 内容 inline）          ← 推
```

hook 的后果通常是写一个段、直接写状态总线；非 hook 外部源走两条：monitor persist 喂状态总线（拉），notify 端口（channel 第一种实现）推给 agent。

### 2. 三个统一 seam（集中、规范、可替换）
- **git → `gitcmd`**：所有 git 子进程的唯一入口，超时 + failure-safe（rc=-1 不抛）。
- **forge → `lib/forge`**：所有代码评审平台（GitHub / GitLab）访问的唯一入口，**缝在 provider 层而非 transport 层**。`base.py` 是中立领域核心：领域对象 `PullRequest`（`number`/`state` 归一，不带任一家行话、**不带 provider**——provider 是 repo 级)+ `Forge` port（**只暴露 `create/get/update/prs_for_branch/recent/comments` 取数原语**）+ 跨家一致的窗口**策略** `build_window`（组合在 `recent`+`get` 上，不让 adapter 各写一份)+ 展示 `pr_label`/`vocab`；`gitlab.py` / `github.py` 是平级 adapter，只实现差异（iid↔number、state 归一、API 路径），原生 JSON→`PullRequest` 的映射全困在各自 adapter；`_rest.py` 是唯一 HTTP 缝（urllib）。`resolve_forge(repo)` 把 origin/config/env 三源一处合一、`forge_for_repo` 据此分发——**聚合工作区里一个子项目 GitHub、一个 GitLab 可共存**。脚本一律是 facade 的薄包装，**不散写 urllib、不写死 provider**。
- **用户配置 → `lib/config`**：所有对外部依赖的配置（`forges`：按 host 索引的 token/type/api_host）+ workspace 注册表 + precommit 门禁的唯一入口。**分层读取**：`默认值 < 全局 ~/.devloop/config.json < 上层 .devloop/config.json（离 repo 最近的赢，可只含部分配置）`；写入只落全局（`/plugin update` 不清；`DEVLOOP_CONFIG_DIR` 可覆写全局目录）。本地覆盖靠 `load(repo_dir)` 从 repo_dir 向上收集 `.devloop/config.json`，故 `forge_*`/precommit 访问器都带 `repo_dir` 参数；`workspaces` 是全局发现态、不参与就近覆盖。token 读取按 provider 的约定 env（`GITHUB_TOKEN`/`GH_TOKEN` / `GITLAB_TOKEN`）优先于 config；provider 由 host 推断、`forges[host].type` 可覆写。`workspace` / `forge` / precommit gate 都委托它，**不各自读文件**。

### 3. native-first 事件映射（本插件的设计本质）
每个能力坐在最原生的原语上；旧的"绕路"被原生事件取代：

| 能力 | 旧机制（hook 绕路） | devloop 原生 |
|------|--------------|-------------|
| 进项目感知 | 正则解析 `cd` | **`CwdChanged`** 自动 enter |
| 防 compaction 丢状态 | TTL 安全网（定时猜） | **`PostCompact`** 清 dedup → 重注入 |
| AGENTS.md 变更 | mtime 轮询 | **`FileChanged`** + SessionStart `watchPaths` |
| session 注入（References） | 每轮 UserPromptSubmit 捎带 | **SessionStart `additionalContext`**（落 first prompt 前，位置稳） |
| PR 感知 / 分支失活 | hook 心跳 scheduler + tasks | **`monitors`** 后台轮询写 context |

### 4. 状态总线 `.devloop/`（文字源 → 结构化态）
两级（workspace / repo）。写入者：PostToolUse / SessionStart / CwdChanged / FileChanged / monitor。读取者：UserPromptSubmit（软注入）+ PreToolUse guards（硬决策）。
- **文字源与结构化态分层**：AGENTS.md（workspace 级 / repo 级各自的边界、清单、References）是文字知识源；`.devloop/` 只保存解析后的结构化结果与运行态，不复刻正文。文件布局见 CONCEPTS.md〈状态文件〉。
- **subproject 存在性 = 文件系统自发现**（不靠手写表格）：workspace 直接子项里「是 / 指向 git 仓」的即为 subproject（`discover_subproject_names`，判据是子目录含 `.git`），`docs` / `worktrees` / 隐藏目录走黑名单排除。AGENTS.md 子项目表降级为**可选润色**——按目录名 join 补 `aliases` / `role`，`language` 缺省自动探测、表格显式值可覆盖。加一个 subproject ≈ 建个 symlink，无需手编表格。为什么：手维护的表格易过时 / 表头不被识别就整片丢失，而文件系统是不会撒谎的事实源。
- **分段状态总线（为什么 repo 级不是单文件）**：repo 级状态按 writer-owner 拆成段文件、`load` 合并成视图——写入角色分散在不同进程，单文件逼出"读-改-写"、并发丢更新；一段一 owner 让每次写只碰不相交的文件，**多写者丢更新在结构上不可能、无需锁**。原子写 / fail-open 读的原语在 `context/base.py`。
- **脚本与 cwd 解耦**：session cwd 在聚合工作区常驻 workspace 根，smart 脚本一律不依赖 cwd——repo 按"显式 `--repo` → cwd 仓库 → 本 session 绑定的最近活跃仓"解析并在 PLAN 里自述来源（active 一 session 一文件，互不劫持；无本 session 绑定即拒绝兜底，他人绑定仅作提示）。解析链见 CONCEPTS.md〈脚本的 repo 解析〉，生命周期见〈Session 运行态〉。
- **PR 模型**：中立 `PullRequest`（provider 随对象走）；单 anchor（`branch.pr_number`）+ 近期窗口（`prs`）**派生**失活 / 在途，不存 bool；`pr.json` 由 monitor 独占且按 branch 归属——切分支即自动失效、无人去清（gcampr 建 PR 后也只触发 monitor poll，不自己写）。字段语义与窗口规则见 CONCEPTS.md〈PR 模型〉。
- **注入两 cadence**：turn（branch/dirty/validation/PR 摘要，每轮，内容哈希 dedup + TTL 兜底）、session（References，SessionStart 发一次、变更才重发）。`PostCompact` 清两者。
- 字段 schema / TTL / cap 数值、段读写原语（`load/save_segment` / `_write_atomic`）都在 `hooks/lib/context/base.py`，文档不复述。

**成本原则（token 是第一约束，"非必要不注入"）**——prompt 注入是跟 AI 沟通最直接也最贵的通道，加任何东西进 prompt 前先过这几条：
1. **只注入当前 subproject + workspace 级**的 References / 状态；**绝不**注入其它 subproject 的 AGENTS.md 正文（多 subproject 场景下这是最大的省 token 点）。
2. **`watchPaths` / `monitor` 输出是给 harness 的指令 / 通知，不进模型 prompt**——所以"注册全部 subproject 的 AGENTS.md 监听""轮询全部 subproject 的 MR"都是 token-free 的；真正进 prompt 的只有当前 repo 的那点状态。
3. **列表一律 cap**（子项目清单、PR 窗口都有定长上限，数值在 `hooks/lib/context/base.py`），长描述压成一行。
4. **只在内容变化时才注入**（最大的省 token 手段之一）：每段注入文本按内容哈希比上次，**一样就不发**（`Cadence.should_emit`）——RepoContext 也是这样，branch/dirty/validation/MR 没变的轮次零增量；session 段一会话发一次。仅 TTL 到期或 `PostCompact` 才强制重发兜底。
5. 新增任何注入前自问：这真得**每轮**进 prompt 吗？hook 内部读 context 决策不行吗？（硬规则 deny 通常不需要进 prompt。）

成本原则约束的是**怎么做**，不是**做不做**：极致的省 token 是什么都不干，那不是目标——devloop 长期方向恰恰是纳入外部通知自动做事、减少人的步骤级干预（见 event-driven resume）。该省的是做事过程里的浪费，不是把事省掉。

### 5. 不走原生通道的硬规矩
- AI **绝不**直接 `git commit/push`（guard 会拦）或散调 forge——commit/push/PR 一律走 `scripts/smart_*.sh`（内部用 gitcmd + facade，自陈 `PLAN:` banner）。保护分支（main/master/release*）判定见 CONCEPTS.md。
- **新分支基点由意图定，不由 HEAD 当前态定**：`--branch`（开新工作）一律 cut 自 `origin/<target>`（`--base` 显式栈式），与当前停在哪条分支无关——否则上一轮留下的 in-flight 分支会被当成基底、新 PR 夹带其提交（夹带在 push/PR 前由 `smart_git_ops` 外来提交自检拦下）。当前分支在循环里的四态流转（protected / healthy / in-flight / inactive）见 CONCEPTS.md〈分支状态流转〉。
- **Owner 锁（owner / guest session）**：多 CLI / session 并发操作同一 checkout 时，第一笔**变更动作**的 session 占有它，guest 的切分支与编辑被硬拦、引导去 worktree——锁保护 checkout 的可变面，防止两个 session 的改动混进同一工作树；enter / 只读不占有，避免假冲突。占有点、豁免与逃逸口见 CONCEPTS.md〈Owner / guest session〉，实现见 `hooks/lib/context/session.py`。

---

## References

- 一轮循环端到端流程（事件 → hook/script → 状态）：[`docs/loop.md`](./docs/loop.md)
- 外部事件驱动的会话续跑（感知 → 唤醒 → 按 auto-mode 决策，含设计/实现分层）：[`docs/event-driven-resume.md`](./docs/event-driven-resume.md)
- devops 生命周期 hook（pre_commit/post_commit/pre_mr/post_mr，统一 lint/test/review 等的触发；hook 皆阻塞，异步=发信号+既有 wake）：[`docs/lifecycle-hooks.md`](./docs/lifecycle-hooks.md)
- 使用 / 安装 / 配置：[`README.md`](./README.md)
- 共享术语（repo_dir / repo_code_dir / 保护分支 / PR 模型 / `<PLUGIN_ROOT>`）：[`CONCEPTS.md`](./CONCEPTS.md)
- 仓库级（marketplace / 多 CLI）：[`../AGENTS.md`](../AGENTS.md)
- 完整方案与设计决策：plan 文档（开发者本地 `~/.claude/plans/devloop-plugin-0.1.md`）
