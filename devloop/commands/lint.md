---
name: lint
description: Run make fix + the repo's lint target (lint-ci preferred) in its code dir, and stamp lint validation.
argument-hint: "[<repo name|path>]"
allowed-tools: [Bash]
---

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run_fixlint.py" $ARGUMENTS
```

The script resolves the repo (no `cd` needed — cwd's repo, falling back to the
workspace's last-active repo; pass a subproject name/path to target another), finds
its code dir, runs `make fix` then the lint target (`make lint-ci` preferred over
`make lint` for CI parity), and stamps `.devloop` validation on success (skips
cleanly if there's no lint target). Report the outcome. **Only `make fix` may modify files** — never hand-edit code just
to satisfy the linter.
