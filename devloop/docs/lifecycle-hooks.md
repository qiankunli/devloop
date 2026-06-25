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

**dispatch 永远同步。** 它并发起一个相位上的全部 hook、join 等全部返回再聚合（`lint ‖ test`
同跑，墙钟 = 最慢那个）。它**不能**起「跑完唤醒 session」的后台任务（subprocess 派生的子进程
harness 不跟踪、跑完不会 re-invoke 会话）——异步靠某个 hook 体只做「发信号」这件快事来表达。

**三种 hook 模式，由 `HookResult` 两个字段（`relay`、`advisory`）编码，不需要单独 mode 枚举：**

| 模式 | 字段 | 例 | 失败时 |
|------|------|----|--------|
| **硬拦截**（gate） | `relay=None, advisory=False` | `lint` | `ok=False` → 抛 `SmartError`、中止 commit/MR |
| **软提示**（advisory） | `relay=None, advisory=True` | `test` | `ok=False` → 只通报（PLAN `⚠`），**放行** |
| **异步信号**（signal） | `relay=BackgroundSpec` | `review` | 恒 `ok=True`、永不挡，下游 detach、下轮浮现 |

- **软提示存在的理由**：test 挂常因基线坏测 / 环境，与本次 diff 未必相关；「该不该拦」本质是
  「diff 是否与挂掉的测试相关」，需 baseline-aware 分析（TODO）。在那之前先不硬拦，把判断交给
  CI / 人——符合「硬拦只立在没有合法例外处」。lint 快、确定、几乎总是你的代码，仍硬拦。
- **veto 与同步性的关系**：只有「inline 且非 advisory」才进 `proceed`；advisory 与 signal 都不挡。
  `proceed = all(r.ok for r in results if not r.advisory and r.relay is None)`。

涉及的对象（锚点）：

| 对象 | 位置 | 职责 |
|---|---|---|
| `dispatch` / `HookResult` / `BackgroundSpec` | `hooks/lib/lifecycle/base.py` | facade：并发 join + 聚合；纯机制 |
| `lint` / `test` handler | `hooks/lib/lifecycle/checks.py` | 内置 inline gate handler（与 `/lint` `/test` 共用同一段逻辑） |
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
- **硬拦截** gate 失败（lint `ok=False`）→ 抛 `SmartError`、中止、PLAN 显示 `pre_commit: lint ✗`、
  commit 不发生。**软提示**失败（test `ok=False`）→ PLAN 显示 `test ⚠` + 一行通报、**commit 照常**。
- 配置为空 → `run_lifecycle_gate` 静默 no-op，PLAN 无 lifecycle 行。

**两个执行点，一份策略。** 正常 commit 走 `/gcam`→smart_git_ops，那里 dispatch 真跑
lint/test 并盖 `.devloop` validation 戳。`PrecommitGateRule` 是裸 `git commit`（AI 绕过
smart_* 直接敲）的**兜底守卫**——它不跑 lint（PreToolUse 5s 超时、fail-open，跑不了），
只查戳：lint 在 `pre_commit` 且戳过期/从未盖 → deny。两者由同一份 `lifecycle` 配置驱动。

---

## 三、关键设计

### signal hook 的下游怎么落地（detach + pull，不靠 wake）

dispatch（subprocess）不能起「跑完唤醒 session」的 harness 后台任务——它 `Popen` 的子进程
harness 不认识、不会 re-invoke 会话。所以 signal hook 不在 dispatch 里跑下游，只把它作为
`relay`（`BackgroundSpec`）返回，**由调用方 detach 起**：smart_git_ops 在 git 动作完成后
`Popen(start_new_session=True)` fire-and-forget 起它（`launch_background_relays`），结果落到
状态总线、**下一轮经注入 pull 浮现**——既不靠 agent 记得起后台，也不需 harness wake。
（曾试过 emit `ARMED:` 行让 agent 自己 `run_in_background` 起，实测不可靠：MR 一建好 agent 就
当任务完成、跳过尾部指令，已弃。）

### 为什么 gate fail-closed，而 signal hook「永不挡」是 handler 的契约

dispatch 对 handler 抛异常一律收敛成 `ok=False`（fail-closed：把关出错按未通过处理，宁可
挡不可漏）。但 code-review 这类 signal hook **不该**因内部出错而挡住 commit——这通过
**handler 自己 catch 内部异常、恒返回 `ok=True`（必要时带告警 summary、不带 relay）**
来保证，是 handler 的契约，不是 dispatcher 的特例。dispatcher 因此保持极简：一种行为。

### 为什么 lint/test 逻辑在 `lib/lifecycle/checks`

`/lint` `/test` 命令与 lifecycle 的 pre_commit gate 必须跑**同一段**逻辑、在**同一处**盖
戳，否则两条路会漂移（命令绿、gate 红或反之）。故 target 选择（`lint-ci` 优先于 `lint`
对齐 CI）、warm-cache 清理、盖戳都在 `lib/lifecycle/checks`；`scripts/run_fixlint.py` /
`run_tests.py` 是薄 CLI 包装（只做 repo 解析 + 实时输出 + 退出码）。

### 相位 × hook 的非重复约定

`gcampr` 一次走 commit+push+MR，会触发 `pre_commit` 和 `pre_mr` 两个相位。把重活
（lint/test）放 `pre_commit`、`pre_mr` 留空，避免一次 gcampr 把 lint/test 跑两遍。
`pre_mr` 留给「MR 专属」检查（如 MR 描述完整性、或后续 e2e verdict）。

### signal hook 实例：code-review

code-review 是 signal hook（`lib/lifecycle/review.py` 的 `review`，返回带 `relay` 的 HookResult，
不在 subprocess 里跑 ocr）。**它是一个动作,挂哪个相位由 config 决定**;`smart_git_ops` detach 起
`run_review`,审 `origin/<target>..HEAD`,写 `.devloop/review.json`(通用交付,下一轮注入浮现)。
**机会性**:relay 跑时若分支有开放 MR(典型 `post_mr`,或往在途 MR 追加时),额外经 `forge.comment`
发评论到 MR 做历史。完整契约见 [`code-review.md`](./code-review.md)。

`launch_background_relays` 是通用的:任何相位 gate 产生的 relay 都在它所裹的 git 动作完成后
detach 起——pre/post_commit relays 在 commit 后、pre/post_mr relays 在 publish 后。
