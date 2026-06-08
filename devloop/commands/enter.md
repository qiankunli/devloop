---
name: enter
description: Jump into a subproject by fuzzy name (or path), optionally in a git worktree. Context (branch / state / AGENTS.md References) loads automatically on cd via devloop's CwdChanged hook — use this only to resolve a name or open a worktree.
argument-hint: "<subproject name (fuzzy) or path> [--worktree <tag>]"
allowed-tools: [Bash, AskUserQuestion]
---

Resolve a subproject and cd into it. devloop's `CwdChanged` hook auto-refreshes
context and surfaces AGENTS.md References right after the cd — so this command does
**not** bundle `git` / `cat` calls; it only resolves the target.

1. Run the resolver:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resolve_subproject.py" $ARGUMENTS
   ```

   First line of stdout:
   - `MATCH\t<absolute_path>` — go to step 2. (With `--worktree <tag>` the path IS the worktree, created/reused for you.)
   - `CANDIDATES\t<name1>\t<path1>\t<name2>\t<path2>...` — multiple fuzzy hits. Use **AskUserQuestion** with one option per candidate (label = `<name>`, description = `<path>`); re-run the resolver with the chosen path.
   - `NONE\t<reason>` — print the reason and stop.

2. `cd "<resolved-path>"` — **bare cd only**, do not bundle `git branch` / `git status` / etc. The CwdChanged hook handles context refresh.

3. Briefly tell the user where they landed. Branch / workspace / validation / recent-MR state is in the auto-injected `.devloop/context.json` segment — no extra git commands needed.
