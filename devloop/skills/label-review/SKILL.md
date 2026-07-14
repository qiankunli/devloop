---
name: label-review
description: 逐条评价 review 已发布到 GitLab/GitHub PR/MR 的 finding comments，并通过 devloop plugin 回复 ccr:label 标注，供后续 API 集中采集 ground truth。Use when the user asks to 看/处理/评价 review comments or findings, when a "Review: N finding(s)" context line surfaces, or when the user says 打标 / label findings.
---

review 先把 finding 发布为 GitLab/GitHub PR/MR comment；Codex/Claude 通过 devloop plugin
对这些 comments **逐条求证并回复标注**。后续统一通过 GitLab/GitHub API 采集原 finding
comment 及其 `ccr:label` 回复；真问题和误报一样有价值，只标一侧数据就偏了。

## 步骤

1. **取待标 comments**：找当前 GitLab/GitHub PR/MR 上带 "devloop code-review" 头的 finding
   comments（GitHub 可用
   `gh api repos/<owner>/<repo>/pulls/<pr>/comments`，
   返回的 comment id 是打标目标）。`.devloop/review.json` 或
   `.devloop/branches/<branch>/review.json` 只用于辅助求证，仅存在本地、尚未发布为 PR/MR
   comment 的 finding 不打标。
2. **逐条求证**（判定纪律，不可省）：
   - 必须对照真实 diff/代码求证，不能顺着 finding 文本信；
   - 论证扎实但在实际执行路径上不成立的判 wrong（"教科书事实错套"是最常见误报形态）；
   - pre-existing（diff 没碰的行为）不算本单有效 finding；
   - 拿不准判 debatable，不要硬判。
3. **打标**：通过 devloop plugin 对 review 发布的 finding comment **回复**一行，词表四档：
   - `ccr:label=important — <理由>`（实质缺陷，采纳修复）
   - `ccr:label=minor — <理由>`（真但小/润色，采纳）
   - `ccr:label=debatable — <理由>`（见仁见智/防御性建议，不采纳）
   - `ccr:label=wrong — <理由>`（误报，附反证）
   理由里可带病因 tag：`#textbook` `#padding` `#pre-existing` `#stale` `#cross-file`。
   发现 review 漏掉的问题，对该 diff 行**直接评论** `ccr:missed — <一句描述>`。
4. **收口**：所有 `ccr:label` 回复 / `ccr:missed` comment 发到 GitLab/GitHub 后即结束。不运行 ccr
   `eval/labels.py`，不手工修改 `eval/labels/*.jsonl`，不在当前流程提交 ground truth；
   ground truth 由后续的集中采集任务统一查询 GitLab/GitHub API 产生。

## 纪律

- **每条都标**，处理完某条（修复/驳回/跳过）就立刻标，不要攒；
- GitLab/GitHub 上的 finding comment + `ccr:label` 回复是 API 采集源；当前流程不生成、
  同步或提交本地 ground truth；
- 修复了 finding 指出的问题 → important/minor；驳回要附可验证的反证；
