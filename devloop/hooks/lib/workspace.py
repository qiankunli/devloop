"""Workspace registry — which directories are registered aggregate workspaces.

Per-workspace state (`<workspace_root>/.devloop/context.json`) is owned by
`lib.context.workspace`. This module owns only the registry and discovery
(`find_containing_workspace` / `maybe_register_workspace`).

Persistence lives in the unified `lib.config` (`~/.config/devloop/config.json`,
`workspaces` key) — a USER-LEVEL path, NOT inside the plugin directory: the plugin
dir is a versioned cache (`~/.claude/plugins/cache/.../<version>/`), so anything
written there is silently reset on every `/plugin update`. `config_dir` / `plugin_root`
are re-exported from `config` for callers that still reference them through here.
"""
from __future__ import annotations

from pathlib import Path

from . import config

# Back-compat re-exports: other hooks call `workspace.config_dir()` / `workspace.plugin_root()`.
config_dir = config.config_dir
plugin_root = config.plugin_root
_expand = config._expand


def load_workspaces() -> list[str]:
    return config.workspaces()


def save_workspaces(workspaces: list[str]) -> None:
    config.set_workspaces(workspaces)


def register_workspace(path: str | Path) -> None:
    """Append a workspace path to the registry. Idempotent."""
    abs_path = str(Path(_expand(str(path))).resolve())
    ws = load_workspaces()
    if abs_path not in [str(Path(_expand(p)).resolve()) for p in ws]:
        ws.append(abs_path)
        save_workspaces(ws)


def maybe_register_workspace(cwd: str | Path) -> str | None:
    """Auto-register `cwd` as a workspace when it self-evidently is one; return its
    root or None.

    Qualification: NOT a git repo itself, but has an AGENTS.md with a parsed
    Subprojects section. Exists because manual `init_workspace.py` proved too
    fragile a dependency for the main path — nobody runs an optional step, and a
    registration written into the versioned plugin dir didn't survive updates
    anyway. Conservative on purpose: a plain repo or a random dir never qualifies.
    """
    root = Path(cwd).resolve()
    if not root.is_dir() or (root / ".git").exists():
        return None
    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        return None
    from . import parsers  # local import: keep this module's import graph flat
    if not parsers.parse_subprojects_section(agents_md):
        return None
    register_workspace(root)
    return str(root)


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
