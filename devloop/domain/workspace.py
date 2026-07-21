"""Workspace registry — which directories are registered aggregate workspaces.

Per-workspace state (`<workspace_root>/.devloop/context.json`) is owned by
`domain.context.workspace`. This module owns only the registry and discovery
(`find_containing_workspace` / `maybe_register_workspace`).

Persistence lives in the unified `lib.config` (`~/.devloop/config.json`,
`workspaces` key) — a USER-LEVEL path, NOT inside the plugin directory: the plugin
dir is a versioned cache (`~/.claude/plugins/cache/.../<version>/`), so anything
written there is silently reset on every `/plugin update`. `config_dir` / `plugin_root`
are re-exported from `config` for callers that still reference them through here.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib import config

# Back-compat re-exports: other hooks call `workspace.config_dir()` / `workspace.plugin_root()`.
config_dir = config.config_dir
plugin_root = config.plugin_root
_expand = config._expand


def load_workspaces() -> list[str]:
    return [p for p in config.workspaces() if not _is_reserved_tool_home(p)]


def save_workspaces(workspaces: list[str]) -> None:
    config.set_workspaces(workspaces)


def register_workspace(path: str | Path) -> None:
    """Append a workspace path to the registry. Idempotent."""
    abs_path = str(Path(_expand(str(path))).resolve())
    if _is_reserved_tool_home(abs_path):
        return
    ws = load_workspaces()
    if abs_path not in [str(Path(_expand(p)).resolve()) for p in ws]:
        ws.append(abs_path)
        save_workspaces(ws)


def maybe_register_workspace(cwd: str | Path) -> str | None:
    """Auto-register `cwd` as a workspace when it self-evidently is one; return its
    root or None.

    Qualification: NOT a git repo itself, has an AGENTS.md, AND holds at least one
    subproject — either discovered on the filesystem (a child that is/symlinks to a git
    repo) or declared in an AGENTS.md Subprojects table. Filesystem discovery is the
    primary signal now, so a symlink farm with no table still registers; the table check
    keeps table-only legacy workspaces qualifying. Exists because manual
    `init_workspace.py` proved too fragile for the main path — nobody runs an optional
    step. Conservative on purpose: a plain repo or a random dir never qualifies.
    """
    root = Path(cwd).resolve()
    if _is_reserved_tool_home(root):
        return None
    if not root.is_dir() or (root / ".git").exists():
        return None
    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        return None
    from lib import parsers  # local import: keep this module's import graph flat
    from .context import workspace as wsctx
    if not wsctx.discover_subproject_names(root) and not parsers.parse_subprojects_section(agents_md):
        return None
    register_workspace(root)
    return str(root)


def _is_reserved_tool_home(path: str | Path) -> bool:
    """Tool state dirs can look like aggregate workspaces but are not dev workspaces.

    `~/.codex` commonly has AGENTS.md plus git-backed helper dirs, so auto-discovery used
    to register it and then workspace-root guards blocked global tool maintenance commands.
    """
    root = Path(config._expand(str(path))).resolve()
    candidates = [
        Path(config._expand(os.environ.get("CODEX_HOME", "~/.codex"))),
        Path(config._expand(os.environ.get("CLAUDE_HOME", "~/.claude"))),
    ]
    for c in candidates:
        try:
            if root == c.resolve():
                return True
        except OSError:
            continue
    return False


def find_containing_workspace(cwd: str | Path) -> str | None:
    """If cwd is under any registered workspace, return that workspace root.
    Else None — most repos run in Mode B (no workspace)."""
    cwd_resolved = Path(cwd).resolve()
    for ws in load_workspaces():
        ws_path = Path(_expand(ws)).resolve()
        try:
            cwd_resolved.relative_to(ws_path)
            return str(ws_path)
        except ValueError:
            continue
    return None


def is_workspace_root(path: str | Path) -> bool:
    """Whether `path` is exactly a registered aggregate-workspace root."""
    resolved = Path(path).resolve()
    return any(resolved == Path(_expand(w)).resolve() for w in load_workspaces())
