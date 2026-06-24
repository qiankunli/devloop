# 提交期 code-review（异步、不阻塞、全自动）

code-review 是 lifecycle 的 **signal hook**：触发但**不挡 commit**、**不阻塞主线程**——
smart_git_ops 自动 detach 起后台 [open-code-review](https://github.com/alibaba/open-code-review)
（`ocr`），审 `origin/<target>..HEAD`（整条分支 vs target 的全量改动），结果写
`.devloop/review.json`（→ 下一轮注入浮现，**通用交付**）。**机会性地**:relay 跑时若分支已有
开放 MR,就**额外**把结果发一条评论到 MR 上（→ MR 攒出 review 历史，可跟踪对比）。承接
[`lifecycle-hooks.md`](./lifecycle-hooks.md) 的 signal-hook 模型。

## 一个动作、任意相位、机会性评论

- **review 是一个动作,挂哪个相位由 config 决定**（`pre_commit` / `post_commit` / `pre_mr` /
  `post_mr` 都行）。相位只决定**何时触发**;动作本身不变。
- **通用交付 = surface 给 session**:无论哪个相位,都写 review.json → 下一轮经状态总线注入
  浮现 `Review:` 行（pull）。
- **post_mr 的额外能力 = MR 评论**:relay 在 git 动作后跑时,查分支是否有开放 MR;有就
  `forge.comment` 发一条（典型是 `post_mr`——MR 刚建好;或往在途 MR 追加提交时也会命中）。
  没有 MR 就只落 review.json。所以 **MR 评论是机会性的,不是 phase 硬绑**。
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
   → ocr review --from origin/<target> --to HEAD --format json
   → 写 .devloop/review.json（通用）+ 若分支有开放 MR：forge.comment(MR, 格式化结果)
下一轮：userprompt 注入读 review.json → 上下文出现 `Review: …`（含 mr_comment 状态）
```

关键对象（锚点）：`lib/lifecycle/review.py`（`review` handler，返回 relay）、`smart_git_ops`
（`launch_background_relays`，各相位 git 动作后 detach 起）、`scripts/run_review.py`（后台执行体：
审全量 diff + 机会性发评论）、`forge.comment`（写评论原语，gitlab notes / github issue comment）、
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

`ocr` 需自备 LLM（保留它自己的 key/endpoint，devloop 不接管）。未装 ocr 或未配 LLM →
run_review 写 `status=skipped` 退出，**不报错、不挡任何东西**。

## `.devloop/review.json`

run_review 独占写入。`comments` 是 ocr 的原始评论（无优先级——分级是 agent 在 Execute 轮
做的）：

```jsonc
{
  "status": "running | success | completed_with_warnings | completed_with_errors | skipped | error",
  "reviewed_sha": "…",
  "comments": [ { "path", "content", "start_line", "end_line", "suggestion_code?", "existing_code?", "thinking?" } ],
  "count": 3,
  "failed": 0,            // review 失败的文件数（ocr 的 subtask_error warnings）——0 评论但 failed>0 = 出错而非 clean
  "warnings": [ … ],      // ocr 原始 warnings（每文件失败原因），供诊断
  "message": "…",         // ocr 的整体消息（如 "No comments generated. Looks good to me."）
  "reviewed_range": "…",  // 审查范围：HEAD 模式是 sha，--mr 模式是 "origin/<target>..HEAD"
  "mr_comment": "…",      // --mr 模式：发评论到 MR 的结果（"posted to MR !N" / "no open MR…" / 失败原因）
  "generated_at": 1.0
}
```

## 结果回流（下一轮）：agent 怎么做

review 跑完后，**下一轮**注入上下文会出现一行 `Review:`（来自 `_format_turn` 读 review.json）。
这是「递信息」，不是「夺取控制权」：**不拦截 session 接下来要做的事**——不强制进入修复流、
不打断用户在做 / 想做的动作。review 端到端 advisory：既不挡 commit，结果回流也只通报、不挟持。

- `Review: running …` → review 还在跑，先不管，下一轮再看。
- `Review: stale …` → review.json 卡在 `running` 超过 `REVIEW_STALE_SEC`（~30min），detach 的
  run_review 很可能被中途杀掉（休眠 / OOM / kill）没写终态——视为中断，可重跑（再 gcampr 即可）。
- `Review: clean (no findings) …` → 无 findings、无失败，无需动作。
- `Review: … N file(s) failed …` 或 `review errored` → ocr 有文件没 review 成（LLM 超时 / token
  超限等）。告知用户「review 未完整覆盖」，要细节读 `.devloop/review.json` 的 `warnings`；可
  重跑或缩小范围。**不是 clean**——别当没问题。
- `Review: N finding(s) …` → 值得通报时，读 `.devloop/review.json`：
  1. 对每条 `comment` 判优先级（ocr 不给 severity，agent 判）：
     **High**（明确 bug / 安全 / 数据丢失 / 崩溃，或有精确修法）、**Medium**（依赖上下文的顾虑、
     性能 / 可维护性、需人工实现的修复）、**Low**（疑似误报 / 吹毛求疵——静默丢弃）。
  2. 按 High / Medium **简明通报**（`start_line==end_line==0` 表示定位失败，读该文件按 content 定位），
     然后**把控制权交还**——别自动展开一长串修复把会话占住。
  3. **默认只通报、不动手**。仅当用户明确要"review 并修" → 才改 High/Medium。**review 从不代替人
     merge，也不替 session 决定下一步。**

注:`Review:` 注入行有内容哈希 dedup——同一份 review.json 不会每轮重复刷;新 commit 触发新
review 才变。`status=skipped`（ocr/LLM 没配）不进注入,避免噪声。

## 规则积累（让 review 不靠运气）

ocr 按 `--rule` > `<repo>/.opencodereview/rule.json` > `~/.opencodereview/rule.json` > 内置
解析规则。把本项目稳定的业务约定（超时 / 连接池、SQL 可移植、分层、易错点）写进项目级
`rule.json`——它之于 review 就像 AGENTS.md 之于进项目的人。模板见
[`config/opencodereview.rule.example.json`](../config/opencodereview.rule.example.json)。
