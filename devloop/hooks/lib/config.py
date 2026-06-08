"""Unified user-level config — the single `~/.config/devloop/config.json`.

One file holds everything devloop depends on from the user / its environment, so
the external dependencies (which GitLab, which token) are explicit in one place:

    {
      "workspaces": ["/abs/workspace/root", ...],   # Mode-A aggregate-workspace registry
      "gitlab": {
        "token": "glpat-...",   # canonical token source; env GITLAB_TOKEN overrides it
        "host":  ""             # optional: override the host derived from the origin remote
      },
      "precommit": {            # per-repo lint commit-gate (default off)
        "default": {"commit_gate_lint": false},
        "repos":   {"/abs/repo": {"commit_gate_lint": true}}
      }
    }

Lives at a USER-LEVEL path (override via `DEVLOOP_CONFIG_DIR`), never the versioned
plugin dir — a `/plugin update` swaps that dir and would silently drop user config.
The file is optional: every section has a default, so a fresh install just works and
the file is created on first write (e.g. when a workspace auto-registers).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Section defaults — `load()` deep-merges the on-disk file over these, so a partial
# config.json (e.g. only `workspaces`) still yields sane `gitlab` / `precommit`.
_DEFAULTS: dict = {
    "workspaces": [],
    "gitlab": {"token": "", "host": ""},
    "precommit": {"default": {}, "repos": {}},
}


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def config_dir() -> Path:
    """User-level config dir — survives plugin version switches."""
    env = os.environ.get("DEVLOOP_CONFIG_DIR")
    if env:
        return Path(_expand(env))
    return Path.home() / ".config" / "devloop"


def config_file() -> Path:
    return config_dir() / "config.json"


def plugin_root() -> Path:
    """Resolve plugin root from `${CLAUDE_PLUGIN_ROOT}` or relative fallback.

    CLI-agnostic: any CLI exporting a plugin-root alias works; the relative
    fallback (this file at `<plugin_root>/hooks/lib/config.py`) covers the rest.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parent.parent.parent


# ── read / write ─────────────────────────────────────────────────────────────
def load() -> dict:
    """Full config dict — on-disk file deep-merged over `_DEFAULTS` (missing file → defaults)."""
    return _deep_merge(_DEFAULTS, _read_json(config_file()) or {})


def save(data: dict) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def update(mutate) -> dict:
    """Read-modify-write that preserves all sections. `mutate(d)` edits in place."""
    data = load()
    mutate(data)
    save(data)
    return data


# ── section accessors ────────────────────────────────────────────────────────
def workspaces() -> list[str]:
    return [_expand(p) for p in load().get("workspaces", []) if isinstance(p, str)]


def set_workspaces(ws: list[str]) -> None:
    update(lambda d: d.__setitem__("workspaces", list(ws)))


def gitlab_token() -> str | None:
    """Canonical token: env `GITLAB_TOKEN` wins (CI-friendly), else `gitlab.token`."""
    env = os.environ.get("GITLAB_TOKEN")
    if env and env.strip():
        return env.strip()
    tok = ((load().get("gitlab") or {}).get("token") or "").strip()
    return tok or None


def gitlab_host() -> str | None:
    """Optional host override; None → derive from each repo's origin remote."""
    host = ((load().get("gitlab") or {}).get("host") or "").strip()
    return host or None


def precommit() -> dict:
    return load().get("precommit") or {}


# ── internals ────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) else None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
