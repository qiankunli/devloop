---
name: git-ops
description: Commit, push, create/read/update a GitLab merge request, cut a feature branch, or view recent MRs in the current repo. Triggers — gcam / gcamp / gcampr / 提 MR / merge request / 看 MR / 切新分支 / 起新分支 / 发版.
---

The umbrella for devloop's git + GitLab workflow. All git goes through one runner
(`hooks/lib/gitcmd.py`); all GitLab through one facade (`hooks/lib/gitlab/`). You
call the scripts below — never raw `git commit/push` (the guards intercept those,
and the scripts encode the case logic + self-narrate a `PLAN:` banner you can trust).

Paths use `<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.

## Commit / push / MR

| Intent | Script |
|--------|--------|
| commit only | `bash <PLUGIN_ROOT>/scripts/smart_gcam.sh --message "<msg>" [...]` |
| commit + push | `bash <PLUGIN_ROOT>/scripts/smart_gcamp.sh --message "<msg>" [...]` |
| commit + push + MR | `bash <PLUGIN_ROOT>/scripts/smart_gcampr.sh --message "<msg>" [...]` |

Shared flags: `--repo <name|path>` (target repo; no `cd` prefix needed — default is
cwd's repo, falling back to the workspace's last-active repo), `--branch <name>`
(required when the context shows **PROTECTED** / **INACTIVE** — the script cuts a
fresh branch off `origin/<target>`), `--target <branch>`, `--files a,b` (explicit
staging, auto-rebased onto the repo root; else tracked modifications — never
`git add -A`), `--title "<MR title>"` (gcampr only). Trust the `PLAN:` banner; on
`✗`, fix per the message (usually add `--branch`) and retry.

## View / update an MR

```
python3 <PLUGIN_ROOT>/scripts/read_mr.py <iid|url>          # title/state/branches/comments
python3 <PLUGIN_ROOT>/scripts/update_mr.py <iid> --title "..." --description "..." --target-branch <b>
```

## Branch / MR awareness

The injected `.devloop` context already carries the current branch's state, whether it's
protected, and a **Recent MRs** digest (the monitor keeps `mrs` fresh). Read that before
acting instead of re-querying git. The branch's own MR is marked `*` in the digest;
"INACTIVE" means its MR is merged/closed — cut a new branch before more edits.
