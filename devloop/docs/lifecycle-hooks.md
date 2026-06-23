# devops 生命周期 hook（lifecycle hooks）

把「在某个 git 生命周期相位触发的验证 / 动作」收成一个统一的 facade：`pre_commit` /
`post_commit` / `pre_mr` / `post_mr`。lint、test、（后续）code-review、e2e·eval·perf
verdict 形状相同，过去各自 ad-hoc 接线——本机制让它们都退化成挂在相位上的 hook。

本文分三段：概念（这是什么、为什么有）、流程（一次 commit 怎么走）、关键设计（几个
「为什么这么做」）。易变的字段 / 阈值不进本文，看代码。

---

## 一、概念

**为什么需要一个新缝。** devloop 是 native-first：能坐 Claude Code 原生事件就不自造。
但 CC 原生事件只到**工具层**（`PreToolUse(Bash)` 看命令字符串），**git 生命周期这个
altitude 没有原生事件**。所以 lifecycle 是一个正当的「缺失 facade」——与 `lib/notify`
（推端口）、`lib/forge`（评审平台 facade）同性质，不是重造原生事件。

**hook 只有一种，都是阻塞的。** dispatch 并发起一个相位上的全部 hook、join 等全部返回，
再聚合。「并发」让 `lint ‖ test` 同跑，墙钟 = 最慢那个。

**「非阻塞」不是 hook 的属性，而是某个 hook 体只做「发信号」这件快事。** 一个本质异步的
hook（如 code-review，跑得久、不该挡 commit）实现成：hook 体瞬时返回一个 `relay`
（`BackgroundSpec`），把真正的异步下游交给**唯一能造 wake 的 agent/harness** 去起。
dispatch 自身永远同步——它**不能**起一个「跑完唤醒 session」的后台任务（subprocess 派生
的子进程 harness 不跟踪、跑完不会 re-invoke 会话），所以只**收集** relay、交还调用方。

**veto 能力与同步性同源。** inline 干活的 hook 返回 `ok=False` 可挡（gate，如 lint 失败
中止 commit）；只发信号的 hook 恒 `ok=True` 且带 `relay`——信号发成功就是过，真活还没
跑、无可 veto。所以「能不能挡」「是不是异步」由 hook 体「inline 干活 vs 发信号」一个区分
同时决定，不需要单独的 mode 字段。

涉及的对象（锚点）：

| 对象 | 位置 | 职责 |
|---|---|---|
| `dispatch` / `HookResult` / `BackgroundSpec` | `hooks/lib/lifecycle.py` | facade：并发 join + 聚合；纯机制 |
| `lint` / `test` handler | `hooks/lib/checks.py` | 内置 inline gate handler（与 `/lint` `/test` 共用同一段逻辑） |
| `run_lifecycle_gate` | `scripts/smart_git_ops.py` | 在 commit/mr 流水线里的 dispatch 插点 |
| `PrecommitGateRule` | `hooks/lib/rules/command/precommit_gate.py` | 裸 `git commit` 的兜底守卫（查戳，不跑） |
| `config.lifecycle()` | `hooks/lib/config.py` | 「哪个相位挂哪些 hook」的数据（opt-in，默认空） |

---

## 二、流程

「哪个相位挂哪些 hook」是 `config.lifecycle(repo)` 的数据，**opt-in，默认全空 = 每相位
no-op、零行为变化**。配置形如（`default` 叠 `repos[<abs>]`，分层同 `arch`）：

```jsonc
"lifecycle": {
  "default": { "pre_commit": [], "post_commit": [], "pre_mr": [], "post_mr": [] },
  "repos":   { "/abs/repo": { "pre_commit": ["lint", "test"] } }
}
```

一次 `/gcam`（或 gcamp / gcampr）的同步主链路（`smart_git_ops.main`）：

```
resolve_intent → prepare_branch
              → run_lifecycle_gate(pre_commit)   ← 跑 lint‖test，盖戳，失败即中止
              → stage_and_commit                  ← lint 的 `make fix` 改的文件在此被收进 commit
              → run_lifecycle_gate(pre_mr)         ← 仅 mr 模式；默认空
              → publish (push / 建 MR)
```

- `pre_commit` 故意排在 staging **之前**：lint 的 `make fix` 会改文件，这些改动要被随后的
  stage 收进同一个 commit。
- gate 失败（lint/test 返回 `ok=False`）→ 抛 `SmartError`、中止、PLAN 显示
  `pre_commit: lint ✗`、commit 不发生。
- 配置为空 → `run_lifecycle_gate` 静默 no-op，PLAN 无 lifecycle 行。

**两个执行点，一份策略。** 正常 commit 走 `/gcam`→smart_git_ops，那里 dispatch 真跑
lint/test 并盖 `.devloop` validation 戳。`PrecommitGateRule` 是裸 `git commit`（AI 绕过
smart_* 直接敲）的**兜底守卫**——它不跑 lint（PreToolUse 5s 超时、fail-open，跑不了），
只查戳：lint 在 `pre_commit` 且戳过期/从未盖 → deny。两者由同一份 `lifecycle` 配置驱动
（旧 `precommit.commit_gate_lint=true` 兼容等价为「pre_commit 含 lint」）。

---

## 三、关键设计

### 为什么 dispatch 不能起「跑完唤醒」的后台任务

「跑完唤醒 session」这条回路是 **harness 在跟踪 agent 起的后台任务**才有的。devloop 的
hook / 脚本是子进程，它 `Popen` 出去的子进程 harness 不认识、跑完不会 re-invoke 会话。
所以 signal hook 的下游不能在 dispatch 里直接起——只能作为 `relay`（`BackgroundSpec`）
返回，由调用方渡给 agent（`/gcam` skill 用 `run_in_background` 起），或走
`lib/notify` 端口（channel / waiter，见 [event-driven-resume](./event-driven-resume.md)）。
这与「wake ⊥ execute、唤醒只能由 agent/harness 发起」是同一条约束。

### 为什么 gate fail-closed，而 signal hook「永不挡」是 handler 的契约

dispatch 对 handler 抛异常一律收敛成 `ok=False`（fail-closed：把关出错按未通过处理，宁可
挡不可漏）。但 code-review 这类 signal hook **不该**因内部出错而挡住 commit——这通过
**handler 自己 catch 内部异常、恒返回 `ok=True`（必要时带告警 summary、不带 relay）**
来保证，是 handler 的契约，不是 dispatcher 的特例。dispatcher 因此保持极简：一种行为。

### 为什么 lint/test 逻辑下沉到 `lib/checks`

`/lint` `/test` 命令与 lifecycle 的 pre_commit gate 必须跑**同一段**逻辑、在**同一处**盖
戳，否则两条路会漂移（命令绿、gate 红或反之）。故 target 选择（`lint-ci` 优先于 `lint`
对齐 CI）、warm-cache 清理、盖戳都在 `lib/checks`；`scripts/run_fixlint.py` /
`run_tests.py` 退化为薄 CLI 包装（只做 repo 解析 + 实时输出 + 退出码）。

### 相位 × hook 的非重复约定

`gcampr` 一次走 commit+push+MR，会触发 `pre_commit` 和 `pre_mr` 两个相位。把重活
（lint/test）放 `pre_commit`、`pre_mr` 留空，避免一次 gcampr 把 lint/test 跑两遍。
`pre_mr` 留给「MR 专属」检查（如 MR 描述完整性、或后续 e2e verdict）。

### 后续（MR2：code-review）

code-review 作为第一个 **signal hook** 接入：在 `lib/lifecycle._BUILTIN` 注册
`"review"`，handler 返回带 `relay` 的 `HookResult`（不在 subprocess 里跑 ocr）；
`run_lifecycle_gate` 把 `res.to_launch` emit 成 PLAN 的 `ARMED:` 行，`/gcam` skill 读到后
用 `run_in_background` 起 `run_review.py`，跑完 harness 唤醒会话汇报。本机制为它预留了
`relay` / `to_launch` 通道，MR2 是纯增量。
