---
name: git-ops
description: Commit, push, create/read/update/close a pull/merge request (GitHub PR or GitLab MR), cut a feature branch, or view recent PRs in the current repo. Triggers — gcam / gcamp / gcampr / 提 PR / 提 MR / pull request / merge request / 看 PR / 看 MR / 关 PR / 关 MR / 切新分支 / 起新分支 / 发版.
---

The umbrella for devloop's git + code-review workflow. All git goes through one runner
(`hooks/lib/gitcmd.py`); all code-review hosting through one facade (`hooks/lib/forge/`),
which picks GitHub or GitLab per-repo from the origin remote. You call the scripts below —
never raw `git commit/push` (the guards intercept those, and the scripts encode the case
logic + self-narrate a `PLAN:` banner you can trust), and **never hand-roll `curl`/`glab`/`gh`
against the forge API** — that one facade backs both script surfaces below (gcampr *raises* an
MR; `pr` *inspects/manages* an existing one) and resolves the token from config, so there's no
credentials file to hunt for.

Paths use `<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.

## Commit / push / PR

| Intent | Script |
|--------|--------|
| commit only | `bash <PLUGIN_ROOT>/scripts/smart_gcam.sh --message "<msg>" [...]` |
| commit + push | `bash <PLUGIN_ROOT>/scripts/smart_gcamp.sh --message "<msg>" [...]` |
| commit + push + PR/MR | `bash <PLUGIN_ROOT>/scripts/smart_gcampr.sh --message "<msg>" [...]` |

**Message**: one-line / simple → inline single-quoted (`--message 'fix: …'`). Multi-line, or
containing quotes / `$` / backticks → write it with the **Write tool** to
`<repo>/.devloop/tmp/commit_msg` (gitignored scratch) and pass `--message-file <path>` (alias `-F`;
`-F -` reads stdin) — no shell escaping, mirroring `git commit -F` / `gh --body-file`. `--title`
defaults to the message's first line.

Shared flags: `--repo <name|path>` (target repo; no `cd` prefix needed — default is
cwd's repo, falling back to the workspace's last-active repo), `--branch <name>`
(required when the context shows **PROTECTED** / **INACTIVE** — the script cuts a
fresh branch off `origin/<target>`), `--target <branch>`, `--files a,b` (explicit
staging, auto-rebased onto the repo root; else tracked modifications — never
`git add -A`), `--title "<PR title>"` (gcampr only). Trust the `PLAN:` banner; on
`✗`, fix per the message (usually add `--branch`) and retry. The `✗` for an
**INACTIVE / merged-or-closed** branch is computed from a live, authoritative forge poll and
quotes the MR's number / state / sha — so it's ground truth even right after you created the MR
(a colleague can merge it in seconds); add `--branch` and re-run.

### `--requirement` — 需求 scope（loop-state）

一次需求交付常横跨多个分支（stacked、merge 后 follow-up）。**切新分支时**（即带 `--branch`
的那次 gcampr）用 `--requirement` 声明这条分支属于哪个需求——需求以其**首个分支名**为 ID：

- **新需求**：省略 `--requirement`（默认新开，ID = 这条分支名）。
- **续接已有需求**：`--requirement <首个分支名>`，把这条分支挂到那个需求下。

**你先自判、判不出才问人**：会话上下文多数自明——「下一个 / 新需求」= 省略；「把 review 意见修了 /
接着上一个做」= 续接上一个需求的首分支。真正模糊时才问一句。免问兜底：仓里没有进行中的需求就直接
新开；拿不准就默认新开（误归档比误新开难修）。它只影响 loop-state 的经验沉淀记账，**不影响 git/PR
本身**，写失败降级为一条 PLAN note、绝不挡 commit/push。

**跨 repo 需求用同名分支**：一次需求横跨多个 subproject 时，各仓的分支用**同一个名字**——
requirement id = 首分支名，同名即天然的 workspace 级 join 键（挖掘/分析按名归并，无需跨仓索引）。

## 后台 code-review（自动，无需你操作）

启用了 `review` 的仓,commit 后 smart_git_ops 会**自动 detach 起后台 ocr review**(PLAN 出
`review: launched in background`)——**你不用起任何东西**。它跑完写 `.devloop/review.json`,
结果**下一轮**经注入上下文的 `Review:` 行浮现(`running` / `N finding(s)` / `clean`)。

**要 review 一完就主动唤醒当前会话**(不必等下一轮敲键):看到 `review: launched in
background` 后,分两步——

1. 前台先决议要不要 arm(同步、不唤醒):

   ```
   python3 <PLUGIN_ROOT>/scripts/notify.py should-arm all <repo>
   ```

   exit 0(打印 `arm:`)= 没有常驻 channel,走第 2 步;exit 1(`skip:`)= 已有 `channel all`
   覆盖,**别 arm**(channel 自会唤醒你,多起 waiter 只徒增一次空唤醒)。

2. 仅当第 1 步 exit 0,才用 **run_in_background** 工具**各起一个** waiter——review、forge 两类
   事件各自独立唤醒、互不抢占:

   ```
   python3 <PLUGIN_ROOT>/scripts/notify.py waiter review <repo>
   python3 <PLUGIN_ROOT>/scripts/notify.py waiter forge  <repo>
   ```

每个 waiter 盯自己那段(review.json / pr.json),其源**首个终态事件**即唤醒本会话、内容 inline
带回。这一步只能 agent 起(detach 进程/hook 起不了 harness 后台任务),把它当建完 MR 后的固定
动作;被唤醒那轮仍守 advisory:默认只**简明通报**、不自动改。每个 waiter **单次唤醒**——命中即
退出,30min 内无事件以 `*-watch-timeout` 收尾;想继续盯那一源,就在醒来那轮对它再走一遍 1→2。

看到 `Review: N finding(s)` 时:可读 `.devloop/review.json` 把问题按优先级(High/Medium/Low)
**简明通报**——这是「递信息」,**不打断 / 不挟持 session 的后续动作**,默认只通报不动手,仅
用户明确要才修。review 端到端 advisory(不挡 commit、不夺控制权),从不代替人 merge。完整
契约见 [`docs/code-review.md`](../../docs/code-review.md)。

## Inspect / manage a PR/MR — the `pr` CLI

One provider-neutral, config-driven surface for **inspecting / managing an existing** PR/MR
(token from env < `~/.devloop/config.json` < nearest `.devloop/config.json`). Raising a new one
is gcampr's job, above.

```
python3 <PLUGIN_ROOT>/scripts/pr.py show   <number|url>        # state/branches/merge-readiness/comments
python3 <PLUGIN_ROOT>/scripts/pr.py list   [--limit N] [--branch B]
python3 <PLUGIN_ROOT>/scripts/pr.py update <number> --title "..." --description "..." --target-branch <b>
python3 <PLUGIN_ROOT>/scripts/pr.py close  <number>            # close without merging
```

There is no `pr create`: `pr` only ever operates on an MR that already exists and never touches
your working tree. Opening a new one is a commit+push transaction under the branch/staging gates
— that's gcampr, above.

## 发版 — the `release` CLI

Cut a versioned release over the same forge facade (GitHub Release / GitLab Release). The tag is
created **server-side** — no `git push --tags`, no working tree, no push guard in the way; and no
`--target <sha>` to mistype (a mistyped sha shipped a broken release before this existed).

```
python3 <PLUGIN_ROOT>/scripts/release.py create <version> [--target <ref>] [--title "..."] [--notes "..." | --notes-file <path>]
python3 <PLUGIN_ROOT>/scripts/release.py latest                         # the current published release
```

- `<version>` must be **semver** (`vX.Y.Z`) and strictly greater than the last release — the CLI
  refuses a non-increment before calling the API.
- `--target` defaults to the repo's **trunk branch name**, so the forge tags that branch's current
  remote tip. Pass a branch/sha/tag only to release off something else.
- **Notes**: for anything beyond a one-liner, compose them yourself and pass `--notes-file <path>`
  (write it with the Write tool — no shell escaping; `-` reads stdin). With no `--notes`, a plain
  changelog is auto-drafted from PRs/MRs merged since the last release — a fallback, not a substitute
  for hand-written notes. `--title` defaults to the version.
- Shares `--repo <name|path>` with the scripts above. Since the tag lands on the remote, `git fetch
  --tags` locally afterward if you need it in the working copy.

This is **not** part of gcampr: releasing is a low-frequency, working-tree-free action, so it's a
peer of `pr` (forge-only), not a step in the commit→push→MR transaction.

## Branch / PR awareness

The injected `.devloop` context already carries the current branch's state, whether it's
protected, and a **Recent PRs** digest (the monitor keeps `prs` fresh; GitHub repos show
`PR #`, GitLab repos `MR !`). Read that before acting instead of re-querying git. The
branch's own PR is marked `*` in the digest; "INACTIVE" means its PR/MR is merged/closed —
cut a new branch before more edits.
