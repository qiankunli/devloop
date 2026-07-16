---
name: run-test
description: Run the repo's component tests and report results. Use when the user wants to run tests, or to verify changes before pushing.
---

Run:

```
<PLUGIN_ROOT>/scripts/python <PLUGIN_ROOT>/scripts/run_tests.py [<repo name|path>] [-- <extra args to narrow scope>]
```

Selects components from the explicit target / changed files / clean repo-wide fallback, then runs each
component's canonical test command (`make test*`, or `go test ./...` for a Go module without a Makefile target)
and stamps `.devloop` validation on success. Skips with guidance if a component has no test command. No `cd` prefix needed — defaults
to cwd's repo, then the workspace's last-active repo; pass a subproject name/path to
target another. Pass `-- <args>` (e.g. a path or `-k` filter) to narrow scope. Trust the output; report pass/fail. Fix only test code
that broke due to the change — never weaken assertions to force a pass.

`<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` on Claude Code; `${PLUGIN_ROOT}` on Codex.
