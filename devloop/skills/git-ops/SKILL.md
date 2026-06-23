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
`<repo>/.devloop/commit_msg` (gitignored scratch) and pass `--message-file <path>` (alias `-F`;
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

## 提交后的后台 code-review（`ARMED:` 行）

若 PLAN 里出现 `ARMED: <cmd>` 行（lifecycle 的 review signal hook 在 commit 期排了一个后台
code-review），且这次**确实 committed**：把每条 `<cmd>` 用 **`run_in_background: true`** 起
（不阻塞主窗口），告诉用户 review 在后台跑。它跑完会**自动唤醒会话**——届时读
`.devloop/review.json`，按优先级（High/Medium/Low）汇报，**只有用户要求或处于 auto 模式才
动手修**；review 是 advisory，从不代替人 merge。完整契约见 [`docs/code-review.md`](../../docs/code-review.md)。
没有 `ARMED:` 行就什么都不用做（该 repo 没把 `review` 加进 `lifecycle.pre_commit`）。

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

## Branch / PR awareness

The injected `.devloop` context already carries the current branch's state, whether it's
protected, and a **Recent PRs** digest (the monitor keeps `prs` fresh; GitHub repos show
`PR #`, GitLab repos `MR !`). Read that before acting instead of re-querying git. The
branch's own PR is marked `*` in the digest; "INACTIVE" means its PR/MR is merged/closed —
cut a new branch before more edits.
