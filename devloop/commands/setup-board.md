---
description: Configure Claude Code's native devloop Board status line
---

Configure the native Claude Code Board status line using the plugin's installer:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/python" "${CLAUDE_PLUGIN_ROOT}/scripts/setup_claude_board.py" \
  --plugin-root "${CLAUDE_PLUGIN_ROOT}"
```

If the installer exits with `CONFLICT`, an existing non-devloop `statusLine` was found.
Ask the user whether to replace it. Only after explicit confirmation, rerun the same
command with `--replace`. Never edit or overwrite the existing status line manually.

On success, tell the user Claude reloads settings automatically and the Board should
appear after the next interaction. The installer prints a backup path when it changes
an existing settings file.
