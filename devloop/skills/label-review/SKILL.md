---
name: label-review
description: 逐条评价 code-review findings 并打 ccr:label 标注（真问题/误报双向积累 ground truth）。Use when the user asks to 看/处理/评价 review comments or findings, when a "Review: N finding(s)" context line surfaces, or when the user says 打标 / label findings.
---

对当前 PR/分支的每条 review finding **逐条求证并打标**——不只处理，还要留下判定。标注是
review 质量评估的 ground truth，双向积累：真问题和误报一样有价值，只标一侧数据就偏了。

## 步骤

1. **取 findings**：优先 PR 线程（`gh api repos/<owner>/<repo>/pulls/<pr>/comments`，
   找带 "devloop code-review" 头的评论）；没有 PR 评论时读 `.devloop/review.json` 或
   `.devloop/branches/<branch>/review.json`（`comments[]` 带 `fingerprint`）。
2. **逐条求证**（判定纪律，不可省）：
   - 必须对照真实 diff/代码求证，不能顺着 finding 文本信；
   - 论证扎实但在实际执行路径上不成立的判 wrong（"教科书事实错套"是最常见误报形态）；
   - pre-existing（diff 没碰的行为）不算本单有效 finding；
   - 拿不准判 debatable，不要硬判。
3. **打标**：对该 finding 的 PR 评论**回复**一行，词表四档：
   - `ccr:label=important — <理由>`（实质缺陷，采纳修复）
   - `ccr:label=minor — <理由>`（真但小/润色，采纳）
   - `ccr:label=debatable — <理由>`（见仁见智/防御性建议，不采纳）
   - `ccr:label=wrong — <理由>`（误报，附反证）
   理由里可带病因 tag：`#textbook` `#padding` `#pre-existing` `#stale` `#cross-file`。
   发现 review 漏掉的问题，对该 diff 行**直接评论** `ccr:missed — <一句描述>`。
4. **无 PR 线程的 finding**（本地 review.json）：把标注手工追加进 ccr 仓
   `eval/labels/<owner>-<repo>.jsonl`（一行 JSON：fingerprint/label/note/tags/path/line/
   `source: "local:<repo>#<pr>"`），fingerprint 取自 review.json 的 comments[]。
5. **回收**（有 PR 线程时）：在 ccr 仓运行
   `python3 eval/labels.py github <owner>/<repo> <pr> --out eval/labels/<owner>-<repo>.jsonl`，
   标注资产提交入库（ccr 仓 github.com/qiankunli/case-code-review）。

## 纪律

- **每条都标**，处理完某条（修复/驳回/跳过）就立刻标，不要攒；
- 修复了 finding 指出的问题 → important/minor；驳回要附可验证的反证；
- 完整约定见 ccr 仓 `eval/README.md` §8.5。
