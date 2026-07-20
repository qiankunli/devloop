---
name: gcam
description: Commit current changes without pushing. Use when the user says "gcam" or wants to commit only.
---

Run:

```
bash <PLUGIN_ROOT>/scripts/smart_gcam.sh --message "<commit msg>" [--repo <name|path>] [--branch <name>] [--files <a,b,c>]
```

Same preflight / staging / message rules as the `gcampr` skill (no `cd` prefix needed — `--repo` targets a subproject by name; add `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only; never `git add -A`; multi-line message → fully overwrite `<repo>/.devloop/commit_msg` with the Write tool, without reading/editing its previous contents, then pass `--message-file`). The script removes that canonical one-shot file on success and retains it on failure for retry. Trust the `PLAN:` banner. After committing, ask whether to push (`gcamp`) or open a PR/MR (`gcampr`).

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code; `${PLUGIN_ROOT}` on Codex.
