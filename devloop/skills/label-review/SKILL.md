---
name: label-review
description: 逐条评价 review 已发布到 GitLab/GitHub PR/MR 的 finding comments，并通过 devloop plugin 回复 ccr:label 标注，供后续 API 集中采集 ground truth。Use when the user asks to 看/处理/评价 review comments or findings, when a "Review findings: N 条待打标" context line surfaces, or when the user says 打标 / label findings.
---

review 把每条 finding 发布成 PR/MR 上的**锚点 comment**（带 `ccr:fp=` 指纹）；Codex/Claude
对这些 comment **逐条求证并在其线程内回复** `ccr:label=<verdict>`。后续统一通过 GitLab/GitHub
API 采集「原 finding comment + 其 label 回复」产出 ground truth；真问题和误报一样有价值，
只标一侧数据就偏了。

指纹和判定都活在 comment body 里、锚在 forge 上——换机器、换 worktree、换 session 都能接着
标，不依赖任何本地文件。

## 步骤

1. **取待标 finding**：

   ```bash
   pr findings <n|url> --pending      # 只列还没有 verdict 的
   pr findings <n|url>                # 全部，含已标的 verdict
   ```

   每行是 `<comment-id>  [PENDING|verdict]  <path>[:<line>]  ccr:fp=<fp>`，`<comment-id>`
   就是下一步的定位符。GitLab/GitHub 同一套写法，不用分别拼 `gh api` / `glab api`。

   `.devloop/review.json` 只用于辅助求证；**仅存在本地、没发布成 comment 的 finding 不打标**
   ——它没有可回复的对象，采集侧也看不见。

2. **逐条求证**（判定纪律，不可省）：
   - 必须对照真实 diff/代码求证，**不能顺着 finding 文本信**——这是打标的头号失效模式：
     review 是模型写的，你也是模型，附和它会让 ground truth 退化成"模型认同模型"，
     整个评测基准就白做了；
   - 论证扎实但在实际执行路径上不成立的判 wrong（"教科书事实错套"是最常见误报形态）；
   - pre-existing（diff 没碰的行为）不算本单有效 finding；
   - 拿不准判 debatable，不要硬判。

3. **打标**：回到该 finding 的线程里回复，一条一行：

   ```bash
   pr reply <n> <comment-id> 'ccr:label=wrong — 该分支实际走不到，xxx.py:42 已早返回 #textbook'
   ```

   词表四档（**只有这四个词会被采集侧认**，拼错等于没标，`pr findings --pending` 会继续列它）：
   - `ccr:label=important — <理由>`（实质缺陷，采纳修复）
   - `ccr:label=minor — <理由>`（真但小/润色，采纳）
   - `ccr:label=debatable — <理由>`（见仁见智/防御性建议，不采纳）
   - `ccr:label=wrong — <理由>`（误报，附反证）

   理由里可带病因 tag：`#textbook` `#padding` `#pre-existing` `#stale` `#cross-file`。

   发现 review 漏掉的问题，对该 diff 行**直接评论** `ccr:missed — <一句描述>`。

4. **收口**：`pr findings <n> --pending` 返回空即结束。不运行 ccr `eval/labels.py`，不手工修改
   `eval/labels/*.jsonl`，不在当前流程提交 ground truth；ground truth 由后续的集中采集任务
   统一查询 API 产生。

## 纪律

- **每条都标**，处理完某条（修复/驳回/跳过）就立刻标，不要攒；
- forge 上的 finding comment + `ccr:label` 回复是唯一采集源；当前流程不生成、同步或提交
  本地 ground truth；
- 修复了 finding 指出的问题 → important/minor；驳回要附**可验证的**反证（指到具体文件行，
  不是"我认为不会发生"）。

## 边界

- 只有**锚点 comment**（行级或文件级）能打标——它有线程可回复。review 汇总 note 里列出的
  回落 finding 没有线程，`pr findings` 不列、也不该标；这类会越来越少（run_review 先按
  行锚 → 文件锚 降级，尽量不掉进汇总）。
- `pr reply` 只能回锚点 comment 的线程，对普通 note 回复会报错。这是有意的：避免发出一条
  跟被判对象脱钩的游离评论。
