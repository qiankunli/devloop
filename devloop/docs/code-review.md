# 提交期 code-review（异步、不阻塞、全自动）

code-review 是 lifecycle 的 **signal hook**：触发但**不挡 commit**、**不阻塞主线程**——
smart_git_ops 自动 detach 起后台 **review 引擎**（默认 [`ccr`](https://github.com/qiankunli/case-code-review)，
可切 `ocr`，见 `review.tool` 配置），审 `origin/<target>..HEAD`（整条分支 vs target 的全量改动），结果写
`.devloop/review.json`（→ 下一轮注入浮现，**通用交付**）。**机会性地**:relay 跑时若分支已有
开放 MR,就**额外**把结果发一条评论到 MR 上（→ MR 攒出 review 历史，可跟踪对比）。承接
[`lifecycle-hooks.md`](./lifecycle-hooks.md) 的 signal-hook 模型。

## 一个动作、任意相位、机会性评论

- **review 是一个动作,挂哪个相位由 config 决定**（`pre_commit` / `post_commit` / `pre_mr` /
  `post_mr` 都行）。相位只决定**何时触发**;动作本身不变。
- **通用交付 = surface 给 session**:无论哪个相位,都写 review.json → 下一轮经状态总线注入
  浮现 `Review:` 行（pull）。
- **post_mr 的额外能力 = MR 评论**:relay 在 git 动作后跑时,查分支是否有开放 MR;有就
  发评论（典型是 `post_mr`——MR 刚建好;或往在途 MR 追加提交时也会命中）。
  没有 MR 就只落 review.json。所以 **MR 评论是机会性的,不是 phase 硬绑**。
- **findings 优先 inline（行锚点）,汇总 note 兜底**:每条 finding 先尝试发成 diff 行锚点评论
  （`forge.diff_comment`,GitLab positioned discussion / GitHub review comment）——锚点换来
  forge **原生的 outdated 生命周期**:下一轮 AI 修完再 push,改到的行上的旧 finding 被 forge
  自动折叠成 outdated,不像普通 note 永远悬着（GitLab 项目开 `resolve_outdated_diff_discussions`
  还能 push 时自动 resolve）。锚不上的（无行号 / 行不在当前 diff / forge 不支持）回落到那条
  汇总评论里列出;汇总评论承载 review 级身份（models / cost / 引擎版本）与历史对比,但只在有
  finding 或有文件审失败时才发——clean 不发评论(结论留 review.json / 下轮注入),避免往在途 MR
  反复 push 时攒出一串无信息量的 clean 刷屏。
- **signal hook,不挡 commit**:review 跑得久,且写码 AI 与 review AI 同源——结论仅供参考、
  **merge 必须人拍**。故不像 lint/test 那样 inline 挡。
- **detach、不靠 agent 起后台**:dispatch(subprocess)不能起「跑完唤醒 session」的 harness 后台
  任务（早期让 agent 读 `ARMED:` 行自己起,实测不可靠）。改由 smart_git_ops
  `Popen(start_new_session=True)` fire-and-forget 起;每相位的 relay 在它所裹的 git 动作后起
  （pre/post_commit 在 commit 后、pre/post_mr 在 publish 后）。

## 流程（以 post_mr 为例）

```
gcampr → … → commit → publish（建/复用 MR）→ post_mr relay
   → smart_git_ops detach 起 run_review（PLAN 出 `review: launched in background`）
run_review（后台、独立进程）：先写 status=running
   → 自动拼 --background（业务上下文）：本次提交说明（git log）+ MR 标题/描述（forge）
   → <engine> review --from origin/<target> --to HEAD --background <ctx> --format json   # engine=ccr(默认)/ocr
   → 写 .devloop/review.json（通用）+ 若分支有开放 MR：逐条 findings 尝试 inline（diff_comment）
     → 锚不上的回落进汇总评论（forge.comment）
下一轮：userprompt 注入读 review.json → 上下文出现 `Review: …`（含 mr_comment 状态）
```

**给引擎喂上下文以提准**（`--background`，注入到每个文件的 review prompt）：run_review 自动从
本次提交说明 + MR 标题/描述拼出（detach 进程经 git log / forge 自取，不依赖会话）。每段有上限
（`_BG_CAP`），因为它每文件都注、要控 token。AGENTS.md / 受影响 spec 等更多上下文是后续增量
（往同一个 background 里加）。

关键对象（锚点）：`lib/lifecycle/review.py`（`review` handler，返回 relay）、`smart_git_ops`
（`launch_background_relays`，各相位 git 动作后 detach 起）、`scripts/run_review.py`（后台执行体：
审全量 diff + 机会性发评论，经 `lib/review_engine.py` 协议调引擎）、`lib/review_engine.py`（**review
tool 协议** `ReviewEngine` + `ReviewResult` + ocr/ccr adapter）、`forge.comment`（写评论原语，gitlab notes / github issue comment）、
`.devloop/review.json`（结果段）、`context/repo.py` 的 `Review:` 注入行（pull）。

## 启用

`~/.devloop/config.json`，把 `review` 加进某 repo 想触发的相位（opt-in，任意相位可用）：

```jsonc
"lifecycle": { "repos": { "/abs/repo": {
  "pre_commit": ["lint", "test"],   // 阻塞门禁
  "post_mr":    ["review"]           // code-review：审全量 + （此相位有 MR）发评论到 MR 做历史
} } }
```

放 `post_mr` 是为了拿到 MR 号发评论;若只想要本地 review（不发 MR）放 `pre_commit`/`post_commit`
也行——一样审、一样注入,只是没 MR 可评时不发评论。

**切引擎**：默认 `ccr`，切回 ocr 在 `~/.devloop/config.json` 加 `"review": {"tool": "ocr"}`。
devloop 只依赖 **review tool 协议**（`lib/review_engine.py` 的 `ReviewEngine`：`available()` /
`configured()` / `review() → ReviewResult`）——ocr/ccr 各自独立 adapter（`CcrEngine` / `OcrEngine`，
**刻意不共享基类、接受重复**，好让它们自由演进）。**接一个非 ocr 系列的引擎 = 加个 adapter 实现协议，`run_review` 一行不动**。
引擎需自备 LLM（保留各自 key/endpoint，devloop 不接管）。未装引擎或未配 LLM → run_review 写
`status=skipped` 退出，**不报错、不挡任何东西**。

## `.devloop/review.json`

run_review 独占写入。`comments` 是引擎的原始评论（无优先级——分级是 agent 在 Execute 轮
做的）：

```jsonc
{
  "status": "running | success | completed_with_warnings | completed_with_errors | skipped | error",
  "reviewed_sha": "…",
  "comments": [ { "path", "content", "start_line", "end_line", "suggestion_code?", "existing_code?", "thinking?" } ],
  "count": 3,
  "failed": 0,            // review 失败的文件数（引擎的 subtask_error warnings）——0 评论但 failed>0 = 出错而非 clean
  "warnings": [ … ],      // 引擎原始 warnings（每文件失败原因），供诊断
  "message": "…",         // 引擎的整体消息（如 "No comments generated. Looks good to me."）
  "reviewed_range": "…",  // 审查范围：HEAD 模式是 sha，--mr 模式是 "origin/<target>..HEAD"
  "mr_comment": "…",      // --mr 模式：发评论到 MR 的结果（"posted to MR !N" / "no open MR…" / 失败原因）
  "generated_at": 1.0
}
```

## `.devloop/review-history.jsonl`（运行历史）

review.json 每次覆盖、只留最新一次；要统计"今天跑了几次 / 跟踪历史"需要 append-only 的痕迹。
故每次 review **终态**（success / skipped / error）再追加一行到 `.devloop/review-history.jsonl`：

```jsonc
{"ts": 1719300000.0, "secs": 42.0, "status": "success", "sha": "584d717", "count": 3, "failed": 0, "range": "origin/main..HEAD", "posted": "posted to MR !146"}
```

`ts` + `secs` 支持按天计数与时长统计；per-repo，跨仓聚合即遍历各 repo 此文件。`.devloop` 已
gitignore，不会被提交。

## 结果回流（下一轮）：agent 怎么做

review 跑完后，**下一轮**注入上下文会出现一行 `Review:`（来自 `_format_turn` 读 review.json）。
这是「递信息」，不是「夺取控制权」：**不拦截 session 接下来要做的事**——不强制进入修复流、
不打断用户在做 / 想做的动作。review 端到端 advisory：既不挡 commit，结果回流也只通报、不挟持。

- `Review: running …` → review 还在跑，先不管，下一轮再看。
- `Review: stale …` → review.json 卡在 `running` 超过 `REVIEW_STALE_SEC`（~30min），detach 的
  run_review 很可能被中途杀掉（休眠 / OOM / kill）没写终态——视为中断，可重跑（再 gcampr 即可）。
- `Review: clean (no findings) …` → 无 findings、无失败，无需动作。
- `Review: … N file(s) failed …` 或 `review errored` → 引擎有文件没 review 成（LLM 超时 / token
  超限等）。告知用户「review 未完整覆盖」，要细节读 `.devloop/review.json` 的 `warnings`；可
  重跑或缩小范围。**不是 clean**——别当没问题。
- `Review: N finding(s) …` → 值得通报时，读 `.devloop/review.json`：
  1. 对每条 `comment` 判优先级（引擎不给 severity，agent 判）：
     **High**（明确 bug / 安全 / 数据丢失 / 崩溃，或有精确修法）、**Medium**（依赖上下文的顾虑、
     性能 / 可维护性、需人工实现的修复）、**Low**（疑似误报 / 吹毛求疵——静默丢弃）。
  2. 按 High / Medium **简明通报**（`start_line==end_line==0` 表示定位失败，读该文件按 content 定位），
     然后**把控制权交还**——别自动展开一长串修复把会话占住。
  3. **默认只通报、不动手**。仅当用户明确要"review 并修" → 才改 High/Medium。**review 从不代替人
     merge，也不替 session 决定下一步。**

注:`Review:` 注入行有内容哈希 dedup——同一份 review.json 不会每轮重复刷;新 commit 触发新
review 才变。`status=skipped`（引擎/LLM 没配）不进注入,避免噪声。

## 规则积累（让 review 不靠运气）

引擎按 `--rule` > 项目级 > 全局 > 内置解析规则；**路径随引擎**：`ccr` 读 `<repo>/.casecodereview/rule.json`
（全局 `~/.casecodereview/rule.json`），`ocr` 读 `<repo>/.opencodereview/rule.json`。把本项目稳定的业务约定
（超时 / 连接池、SQL 可移植、分层、易错点）写进项目级 `rule.json`——它之于 review 就像 AGENTS.md
之于进项目的人。模板（schema 通用，按引擎放对应路径）见
[`config/opencodereview.rule.example.json`](../config/opencodereview.rule.example.json)。
