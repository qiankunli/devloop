#!/usr/bin/env python3
"""Register a directory as an aggregate workspace + build its `.devloop/context.json`.

Usage: init_workspace.py [path]   (defaults to cwd)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib import workspace  # noqa: E402
from lib.context import WorkspaceContext  # noqa: E402


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    root = str(Path(target).resolve())
    workspace.register_workspace(root)
    ctx = WorkspaceContext.refresh(root)
    print(f"devloop workspace registered: {root}")
    print(f"  agents_md    = {ctx.agents_md.path} ({len(ctx.agents_md.references)} references)")
    print(f"  subprojects  = {len(ctx.subprojects)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
