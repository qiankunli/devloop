---
name: gcamp
description: Commit current changes and push, without creating a pull/merge request. Use when the user says "gcamp" or wants to commit + push (e.g. add a commit to an existing PR/MR branch).
---

Run:

```
bash <PLUGIN_ROOT>/scripts/smart_gcamp.sh --message "<commit msg>" [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>]
```

Same preflight / staging rules as the `gcampr` skill (no `cd` prefix needed — `--repo` targets a subproject by name; add `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only; never `git add -A`). Does **not** create a PR/MR. Trust the `PLAN:` banner.

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.
