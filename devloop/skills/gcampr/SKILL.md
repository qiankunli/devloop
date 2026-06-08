---
name: gcampr
description: Commit current changes, push, and create/reuse a GitLab merge request. Use when the user says "gcampr" or asks to commit + push + open/raise an MR / merge request.
---

Run the orchestrator (it handles preflight branch decision, staging, commit, push, and MR create/reuse, then prints a self-narrating `PLAN:` banner):

```
bash <PLUGIN_ROOT>/scripts/smart_gcampr.sh --message "<commit msg>" [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>] [--title "<MR title>"]
```

Rules:
- Derive `--message` from the user's intent + the diff.
- No `cd` prefix needed: the script resolves the repo itself (cwd's repo → workspace's last-active repo). From a workspace root, or to target another subproject, pass `--repo <subproject name or path>`.
- Add `--branch <name>` when the injected `.devloop` context shows the branch is **PROTECTED** or **INACTIVE** (MR merged/closed); the script refuses otherwise and tells you why.
- `--files` for explicit staging, else tracked modifications are staged. Never `git add -A`. Paths are auto-rebased onto the repo root.
- Trust the `PLAN:` banner; surface the MR URL. On `✗`, fix per the message and retry — do not fall back to raw git.

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.
