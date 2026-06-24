# 提交期 code-review（异步、不阻塞、全自动）

code-review 是 lifecycle 的第一个 **signal hook**：在 commit 期触发，但**不挡 commit**、
**不阻塞主线程**——commit 后由 smart_git_ops 自动 detach 起后台
[open-code-review](https://github.com/alibaba/open-code-review)（`ocr`），结果在下一轮经状态
总线注入浮现（pull）。承接 [`lifecycle-hooks.md`](./lifecycle-hooks.md) 的 signal-hook 模型。

## 为什么是 signal hook + detach + pull

review 跑得久（逐文件 ReAct + 跨文件上下文），且写代码的 AI 与 review 的 AI 同源——**结论
仅供参考，merge 必须人拍**。所以它不能像 lint/test 那样 inline 挡 commit。它实现成 signal
hook：dispatch 里瞬时返回一个 `relay`（要跑的命令），不在 dispatch 里跑 ocr。

**为什么 detach、而非靠 agent 起后台**：dispatch（subprocess）不能起「跑完唤醒 session」的
harness 后台任务。早期让 agent 读 PLAN 的 `ARMED:` 行自己 `run_in_background` 起——实测不可靠
（MR 一建好，agent 当任务已完成、跳过尾部指令，review 被 armed 却没人跑）。改由 **smart_git_ops
在 commit 后 detach 起**（`Popen(start_new_session=True)`，fire-and-forget），不依赖 agent 记性、
也不需 harness wake；结果靠状态总线 **pull**：每轮注入读 `.devloop/review.json`，浮现一行 `Review:`。

## 流程

```
/gcam（或 gcamp/gcampr）→ smart_git_ops
   pre_commit dispatch：review handler 返回 relay（不跑 ocr）
   → commit
   → (已提交) smart_git_ops detach 起 run_review；PLAN 出 `review: launched in background`
run_review（后台、独立进程）：先写 status=running → ocr review --commit HEAD --format json
   → 覆盖写 .devloop/review.json（success / clean / skipped / error）→ 退出
下一轮：userprompt 注入读 review.json → 上下文出现 `Review: running / N finding(s) / clean`
   → agent 见有 findings 时，读 .devloop/review.json 简明通报（advisory）
```

关键对象（锚点）：`lib/lifecycle/review.py`（signal handler，返回 relay）、
`smart_git_ops.launch_background_relays`（commit 后 detach 起）、`scripts/run_review.py`
（后台执行体）、`.devloop/review.json`（结果段，run_review 独占）、`context/repo.py` 的
`Review:` 注入行（pull）。

## 启用

在 `~/.devloop/config.json` 把 `review` 加进某 repo 的 `pre_commit`（opt-in）：

```jsonc
"lifecycle": { "repos": { "/abs/repo": { "pre_commit": ["lint", "test", "review"] } } }
```

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
  "message": "…",         // ocr 的整体消息（如 "No comments generated. Looks good to me."）
  "generated_at": 1.0
}
```

## 结果回流（下一轮）：agent 怎么做

review 跑完后，**下一轮**注入上下文会出现一行 `Review:`（来自 `_format_turn` 读 review.json）。
这是「递信息」，不是「夺取控制权」：**不拦截 session 接下来要做的事**——不强制进入修复流、
不打断用户在做 / 想做的动作。review 端到端 advisory：既不挡 commit，结果回流也只通报、不挟持。

- `Review: running …` → review 还在跑，先不管，下一轮再看。
- `Review: clean …` → 无 findings，无需动作。
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
