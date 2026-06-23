# 提交期 code-review（异步、不阻塞）

code-review 是 lifecycle 的第一个 **signal hook**：在 commit 期触发，但**不挡 commit**、
**不阻塞主线程**——它在后台跑 [open-code-review](https://github.com/alibaba/open-code-review)
（`ocr`），跑完唤醒会话汇报。承接 [`lifecycle-hooks.md`](./lifecycle-hooks.md) 的 signal-hook
模型。

## 为什么是 signal hook 而非 inline gate

review 跑得久（逐文件 ReAct + 跨文件上下文），且写代码的 AI 与 review 的 AI 同源——**结论
仅供参考，merge 必须人拍**。所以它不能像 lint/test 那样 inline 挡 commit。它实现成 signal
hook：`dispatch` 里瞬时返回一个 `relay`（要跑的后台命令），真正的 ocr 跑在 commit 之后的后台。

dispatch 自己**不能**起「跑完唤醒 session」的后台任务（subprocess 派生的子进程 harness 不
跟踪），所以这条 relay 必须由 **agent** 用 `run_in_background` 起——这是整条链路的承重约束。

## 流程

```
/gcam（或 gcamp/gcampr）→ smart_git_ops
   pre_commit dispatch：review handler 返回 relay
   → PLAN 打印  ARMED: python3 .../run_review.py --repo <repo>
   → commit 照常发生（review 不挡）
agent 读 PLAN：若有 ARMED 行且已 committed → run_in_background 起该命令（主窗口立即交还）
   run_review：ocr review --commit HEAD --format json → 写 .devloop/review.json → 退出
   → harness 检测后台退出 → 唤醒会话（wake）
agent（唤醒轮，Execute）：读 .devloop/review.json → 按优先级分级汇报 → 按需修
```

关键对象（锚点）：`lib/lifecycle/review.py`（signal handler）、`scripts/run_review.py`
（后台执行体）、`.devloop/review.json`（结果段，run_review 独占）。

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
  "status": "success | completed_with_warnings | completed_with_errors | skipped | error",
  "reviewed_sha": "…",
  "comments": [ { "path", "content", "start_line", "end_line", "suggestion_code?", "existing_code?", "thinking?" } ],
  "count": 3,
  "message": "…",         // ocr 的整体消息（如 "No comments generated. Looks good to me."）
  "generated_at": 1.0
}
```

## Execute（唤醒轮）：agent 怎么做

后台 run_review 退出会唤醒会话。**这一轮是「递信息」，不是「夺取控制权」**：把结果吐给
session，但**不拦截 session 接下来要做的事**——不强制进入修复流、不打断用户在做 / 想做的
动作。review 端到端都是 advisory：既不挡 commit，wake 回来也只通报、不挟持。

1. 读 `.devloop/review.json`。`status=skipped` → 一句话告知未跑的原因（如 ocr/LLM 没配），结束。
2. 对每条 `comment` 判优先级（ocr 不给 severity，agent 判）：
   - **High**：明确 bug、安全问题、数据丢失、崩溃，或有精确修法的可靠建议。
   - **Medium**：合理但依赖上下文的顾虑、性能 / 可维护性建议、需人工实现的修复。
   - **Low**：疑似误报、上下文不足、吹毛求疵 —— 静默丢弃。
3. 按 High / Medium **简明通报**（`start_line==end_line==0` 表示定位失败，读该文件按 content 定位），
   然后**把控制权交还**——别自动展开一长串修复把会话占住。
4. **默认只通报、不动手**。仅当用户明确要"review 并修" → 才改 High/Medium；其余情况摆建议
   等用户决定。**review 是 advisory，从不代替人 merge，也不替 session 决定下一步。**

## 规则积累（让 review 不靠运气）

ocr 按 `--rule` > `<repo>/.opencodereview/rule.json` > `~/.opencodereview/rule.json` > 内置
解析规则。把本项目稳定的业务约定（超时 / 连接池、SQL 可移植、分层、易错点）写进项目级
`rule.json`——它之于 review 就像 AGENTS.md 之于进项目的人。模板见
[`config/opencodereview.rule.example.json`](../config/opencodereview.rule.example.json)。
