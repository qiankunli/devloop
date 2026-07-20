---
name: gcamp
description: Commit current changes and push, without creating a pull/merge request. Use when the user says "gcamp" or wants to commit + push (e.g. add a commit to an existing PR/MR branch).
---

Run:

```
bash <PLUGIN_ROOT>/scripts/smart_gcamp.sh --message "<commit msg>" [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>]
```

Same preflight / staging / message rules as the `gcampr` skill (no `cd` prefix needed — `--repo` targets a subproject by name; add `--branch` on PROTECTED/INACTIVE branches; explicit `--files` or tracked-only; never `git add -A`; multi-line message → fully overwrite `<repo>/.devloop/commit_msg` with the Write tool, without reading/editing its previous contents, then pass `--message-file`). The script removes that canonical one-shot file on success and retains it on failure for retry. Does **not** create a PR/MR — but when the branch already has an in-flight (open) PR/MR, the message's body (lines after the subject) is **appended to its description** automatically, so write a short subject + a detail body as usual. Trust the `PLAN:` banner.

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code; `${PLUGIN_ROOT}` on Codex.
