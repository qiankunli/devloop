---
name: git-ops
description: Commit, push, create/read/update a pull/merge request (GitHub PR or GitLab MR), cut a feature branch, or view recent PRs in the current repo. Triggers — gcam / gcamp / gcampr / 提 PR / 提 MR / pull request / merge request / 看 PR / 看 MR / 切新分支 / 起新分支 / 发版.
---

The umbrella for devloop's git + code-review workflow. All git goes through one runner
(`hooks/lib/gitcmd.py`); all code-review hosting through one facade (`hooks/lib/forge/`),
which picks GitHub or GitLab per-repo from the origin remote. You call the scripts below —
never raw `git commit/push` (the guards intercept those, and the scripts encode the case
logic + self-narrate a `PLAN:` banner you can trust).

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
`✗`, fix per the message (usually add `--branch`) and retry.

## View / update a PR/MR

```
python3 <PLUGIN_ROOT>/scripts/read_pr.py <number|url>          # title/state/branches/comments
python3 <PLUGIN_ROOT>/scripts/update_pr.py <number> --title "..." --description "..." --target-branch <b>
```

## Branch / PR awareness

The injected `.devloop` context already carries the current branch's state, whether it's
protected, and a **Recent PRs** digest (the monitor keeps `prs` fresh; GitHub repos show
`PR #`, GitLab repos `MR !`). Read that before acting instead of re-querying git. The
branch's own PR is marked `*` in the digest; "INACTIVE" means its PR/MR is merged/closed —
cut a new branch before more edits.
