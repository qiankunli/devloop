---
name: gcamp
description: Commit + push (no PR/MR).
argument-hint: "<commit message> [--repo <name>] [--branch <name>] [--target <branch>] [--files a,b]"
allowed-tools: [Bash]
---

The user wants to commit + push (explicitly NOT opening a PR/MR — typically to add a commit to an existing PR/MR's branch). Construct and run:

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/smart_gcamp.sh" --message "<commit message>" [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>]
```

Same rules as `/gcampr` (no `cd` prefix needed — `--repo` targets a subproject; preflight `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only staging; never `git add -A`). Trust the `PLAN:` banner. This does **not** create a PR/MR.
