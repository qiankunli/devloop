---
name: gcam
description: Commit current changes (no push).
argument-hint: "<commit message> [--repo <name>] [--branch <name>] [--files a,b]"
allowed-tools: [Bash]
---

The user wants to commit current changes (no push). Construct and run:

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/smart_gcam.sh" --message "<commit message>" [--repo <name|path>] [--branch <name>] [--files <a,b,c>]
```

Same staging/branch rules as `/gcampr` (no `cd` prefix needed — `--repo` targets a subproject; preflight `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only; never `git add -A`). Trust the `PLAN:` banner. After committing, ask the user whether to push (`/gcamp`) or open a PR/MR (`/gcampr`).

If `code-review` is enabled for the repo, it runs **automatically** in the background after the commit (smart_git_ops detaches it — you launch nothing); a `review: launched in background` line appears in the `PLAN:`. Findings surface next turn via the injected `Review:` line; read `.devloop/review.json` for details and report when relevant (advisory — never blocks). See `docs/code-review.md`.
