---
name: test
description: Run the repo's tests (make test) and stamp test validation.
argument-hint: "[<repo name|path>] [-- <extra test args to narrow scope>]"
allowed-tools: [Bash]
---

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run_tests.py" $ARGUMENTS
```

Runs `make test` in the repo's code dir and stamps `.devloop` validation on success
(skips with guidance if there's no `make test` target). No `cd` needed — defaults to
cwd's repo, then the workspace's last-active repo; pass a subproject name/path to
target another. Pass `-- <args>` to narrow scope (e.g. a specific path / `-k` filter).
Report the outcome.
