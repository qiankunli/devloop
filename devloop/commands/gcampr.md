---
name: gcampr
description: Commit + push + create/reuse a pull/merge request (GitHub PR or GitLab MR; full release flow).
argument-hint: "<commit message> [--repo <name>] [--branch <name>] [--target <branch>] [--files a,b]"
allowed-tools: [Bash]
---

The user wants to commit + push + open/reuse a pull/merge request. Construct and run the orchestrator:

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/smart_gcampr.sh" --message "<commit message>" [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>] [--title "<PR title>"]
```

- Derive `--message` from the user's request and the actual diff (`git diff --stat`, `git status`).
- No `cd` prefix needed — the script resolves the repo itself (cwd's repo → workspace's last-active repo); pass `--repo <subproject name or path>` to target another.
- Pass `--branch <name>` when the injected context shows the current branch is **PROTECTED** or **INACTIVE** (PR/MR merged/closed) — the script refuses to commit there otherwise and tells you. Pick a short kebab-case feature name.
- Pass `--files a,b` to stage explicit files; omit to stage tracked modifications. **Never** `git add -A`.
- The script self-narrates a `PLAN:` banner and does preflight branch-cutting, staging, commit, push, and PR/MR create/reuse (the forge is picked from the repo's origin). Trust the banner; surface the PR/MR URL to the user.
- On a `✗ ...` line, fix per the message (usually: add `--branch`) and retry. Do not fall back to raw `git push`.
