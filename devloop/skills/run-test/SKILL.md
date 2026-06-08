---
name: run-test
description: Run the repo's tests (make test) and report results. Use when the user wants to run tests, or to verify changes before pushing.
---

Run:

```
python3 <PLUGIN_ROOT>/scripts/run_tests.py [<repo name|path>] [-- <extra args to narrow scope>]
```

Runs `make test` in the repo's code dir and stamps `.devloop` validation on success
(skips with guidance if there's no `make test` target). No `cd` prefix needed — defaults
to cwd's repo, then the workspace's last-active repo; pass a subproject name/path to
target another. Pass `-- <args>` (e.g. a path or `-k` filter) to narrow scope. Trust the output; report pass/fail. Fix only test code
that broke due to the change — never weaken assertions to force a pass.

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code.
