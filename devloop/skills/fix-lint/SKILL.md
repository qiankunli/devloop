---
name: fix-lint
description: Run make fix + make lint and report results. Use when the user wants to lint / format / fix lint, or before committing. Only `make fix` may modify code — never hand-edit to satisfy the linter.
---

Run:

```
<PLUGIN_ROOT>/scripts/python <PLUGIN_ROOT>/scripts/run_fixlint.py [<repo name|path>]
```

The script handles repo resolution (no `cd` prefix needed — defaults to cwd's repo, then
the workspace's last-active repo; pass a subproject name/path to target another), code-dir
detection, `make fix` (may edit files) + the lint target (`make lint-ci` preferred over
`make lint` for CI parity), the no-target skip, and stamping `.devloop` validation on
success. Trust its output; report pass/fail. If lint fails, fix the reported issues in source — do **not** hand-edit code
solely to silence the linter (only `make fix` legitimately auto-edits).

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code; `${PLUGIN_ROOT}` on Codex.
