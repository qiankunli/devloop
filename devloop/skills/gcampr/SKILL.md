---
name: gcampr
description: Commit current changes, push, and create/reuse a pull/merge request (GitHub PR or GitLab MR). Use when the user says "gcampr" or asks to commit + push + open/raise a PR / MR / pull request / merge request.
---

Run the orchestrator (it handles preflight branch decision, staging, commit, push, and PR/MR create/reuse, then prints a self-narrating `PLAN:` banner):

```
bash <PLUGIN_ROOT>/scripts/smart_gcampr.sh --message-file <repo>/.devloop/commit_msg [--repo <name|path>] [--branch <name>] [--target <branch>] [--files <a,b,c>] [--title "<PR title>"]
```

Rules:
- **Commit message — never hand-escape multi-line text through the shell.** One-line / simple → inline single-quoted: `--message 'fix: …'`. Multi-line, or containing quotes / `$` / backticks → write it with the **Write tool** to `<repo>/.devloop/commit_msg` (gitignored scratch, zero shell escaping), then pass `--message-file <repo>/.devloop/commit_msg` (alias `-F`; `-F -` reads stdin). This is the shell-escaping-free path, mirroring `git commit -F` / `gh --body-file`. `--title` defaults to the message's first line, so a multi-line message still yields a valid one-line PR title.
- No `cd` prefix needed: the script resolves the repo itself (cwd's repo → workspace's last-active repo). From a workspace root, or to target another subproject, pass `--repo <subproject name or path>`.
- Add `--branch <name>` when the injected `.devloop` context shows the branch is **PROTECTED** or **INACTIVE** (PR/MR merged/closed); the script refuses otherwise and tells you why.
- `--files` for explicit staging, else tracked modifications are staged. Never `git add -A`. Paths are auto-rebased onto the repo root.
- Trust the `PLAN:` banner; surface the PR/MR URL. On `✗`, fix per the message and retry — do not fall back to raw git.

The forge (GitHub vs GitLab) is picked automatically from the repo's origin remote.
`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.
