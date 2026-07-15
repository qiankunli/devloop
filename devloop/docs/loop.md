# devloop 一轮循环 — 端到端流程

把分散在 AGENTS.md（架构）与 CONCEPTS.md（术语）里的三条流程线——repo 解析、分支四态、owner 锁——放回同一条时间轴上看。术语定义一律以 CONCEPTS.md 为准，本文不复述。

---

## 1. 理念

开发者在聚合工作区里转一个循环：

```
enter 某子模块 → 提需求 → 开发 → 验证(lint/test) → commit/建 PR → 人工 merge → 下一轮
```

devloop 的两个杠杆沿循环分布：**状态总线**（软提示）覆盖循环的每一拍——AI 动手前就知道现状；**硬拦截**只立在"没有合法例外"的位置——保护分支、失活分支、guest 共写。workspace 是循环的根（session cwd 常驻于此），subproject 是每一拍动手的落点——所以贯穿全文的一个不变量是：**任何组件都不能信 shell cwd，要按有效落点解析 repo**。

---

## 2. 流程：一轮循环的时序

每拍列出：触发事件 → 参与的 hook / script → 碰了哪类状态。

| # | 拍 | 触发 | 参与者 | 状态效果 |
|---|----|------|--------|---------|
| 0 | session 启动 | `SessionStart` | `sessionstart_init` | References 以 additionalContext 注入；注册全部 subproject 的 AGENTS.md `watchPaths`；workspace 自动注册；状态预热 |
| 1 | enter 子模块 | `cd` / `/enter` → `CwdChanged` | `cwdchanged_enter` | 刷新 repo 段状态；记 `active.json`（不占有 owner 锁——enter 只选上下文） |
| 2 | 每轮对话 | `UserPromptSubmit` | `userprompt_inject` | turn 注入（branch / dirty / validation / PR 摘要，内容哈希 dedup）；`PostCompact` 清 dedup 强制重发；AGENTS.md 被改时 `FileChanged` → `filechanged_refs` 重注入 References |
| 3 | 开发（编辑与命令） | `PreToolUse` | 10 个 guard：编辑面（`edit_owner_guard` / `branch_merged_guard` / `requirements_edit_block`）+ 命令面（`protect_branch` / `checkout_owner_guard` / `block_add_all` / `workspace_cwd_guard` / `pytest_naked` / `pip_install_block` / `precommit_gate`） | deny 或放行，全部 fail-open |
| 3' | 开发后效 | `PostToolUse` | `posttool_git_refresh` | git 状态命令后按**位置感知的有效目录**刷新对应 repo 的 branch 段（**没有**编辑计数 hook：验证是否过期由 gate 现算内容指纹判，不靠工具事件累计——见 CONCEPTS.md〈验证状态〉） |
| 4 | 验证 | `/lint` `/test` | `run_fixlint` / `run_tests` → `lifecycle.checks` | 按 code unit 盖 validation 戳（该 unit 的 lint/test 时间 + lint 通过那刻的内容指纹） |
| 5 | 提交 / PR | `/gcam` `/gcamp` `/gcampr` | `smart_git_ops`（PLAN banner 自陈） | 新分支 cut 自 `origin/<target>`（base 由意图定）；`--files` 收敛 staging；外来提交自检；建 PR 后触发一次 `poll_pr_status` |
| 6 | 等人工 merge | monitors 周期轮询 | `poll_pr_status` | 写 `pr.json` 窗口 → 当前分支派生为 in-flight，turn 注入软提示 |
| 7 | merge 后下一轮 | （人工，AI 范围外） | — | 分支派生为 inactive，编辑被硬拦；回到第 1 拍，从最新 `origin/<target>` 切新分支 |

### 分支一轮的生命线

```
cut off origin/<target> → healthy ──push+PR──▶ in-flight ──人工 merge(AI 范围外)──▶ inactive
         ▲                                                                            │
         └──────────────────  下一轮：再 cut off origin/<target>  ◀───────────────────┘
```

四态定义与软提示 / 硬拦的分档理由见 CONCEPTS.md〈分支状态流转〉。

### 三条贯穿线

- **repo 解析链**（拍 1 / 3 / 4 / 5 都用）：显式 `--repo` → cwd 所在仓库 → `active.json` 最近活跃仓；guard 侧按命令段的有效执行目录（`-C` > cd 前缀 > cwd）归属。见 CONCEPTS.md〈脚本的 repo 解析〉。
- **分支四态**（拍 2 的提示内容、拍 3 / 5 的拦截依据）：protected / healthy / in-flight / inactive，全部由 `pr.json` 窗口派生、不存 bool。见 CONCEPTS.md〈分支状态流转〉。
- **owner 锁**（拍 3 / 3' 第一笔变更动作 acquire + enforce）：第一个动手的 session 占有 checkout，guest 的切分支与编辑被拦、引导 worktree；enter / 只读不占有。见 CONCEPTS.md〈Owner / guest session〉。

---

## 3. 关键设计（流程级 why）

- **为什么 merge 留在 AI 范围外**：merge 是发布权，留给人是边界设计而非能力缺口；devloop 只保证 merge 前的一切（验证戳、PLAN 自陈、外来提交自检）可审计。
- **为什么验证不是 commit 的强制前置**：lint/test 状态走软提示 + validation 戳，强制 gate 是 opt-in（`precommit_gate`，默认关）——"未验证就提交"有合法场景（docs、紧急 hotfix），硬拦会逼出逃逸口。与 in-flight 软提示同一个分档准则：有合法例外的用提示，没有的才硬拦。
- **为什么循环里到处是"派生不存 bool"**：拍 6/7 的状态翻转发生在 AI 范围外（人工 merge），任何落盘的 bool 都会过期；按 `pr.json` 窗口现算，切分支即自动失效、无人去清。
- **为什么 guard 全部 fail-open**：循环的主产出是开发本身，guard 是护栏不是闸门——任何 guard 内部错误都不得打断用户的工具调用（`hook_io` runner 保证）。命令侧守卫同理采用风险黑名单而非白名单：宁可放过少数未知风险，也不因无法穷举命令生态而误拦正常操作。
