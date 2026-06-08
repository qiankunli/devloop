---
name: gcam
description: Commit current changes without pushing. Use when the user says "gcam" or wants to commit only.
---

Run:

```
bash <PLUGIN_ROOT>/scripts/smart_gcam.sh --message "<commit msg>" [--repo <name|path>] [--branch <name>] [--files <a,b,c>]
```

Same preflight / staging rules as the `gcampr` skill (no `cd` prefix needed — `--repo` targets a subproject by name; add `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only; never `git add -A`). Trust the `PLAN:` banner. After committing, ask whether to push (`gcamp`) or open an MR (`gcampr`).

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.
