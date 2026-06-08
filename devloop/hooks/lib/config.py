"""Unified config — `~/.devloop/config.json` plus optional local overrides.

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

Layering (low → high precedence), each layer may be PARTIAL:

    _DEFAULTS  <  global (~/.devloop/config.json)  <  ancestor .devloop/config.json (closest wins)

A repo or workspace can drop a `.devloop/config.json` next to its runtime state to
override just a few keys (e.g. a different `gitlab.token`/`host` for that repo); the
nearest one to `repo_dir` wins, everything else falls through to the global file.

Global lives at a USER-LEVEL path (override the dir via `DEVLOOP_CONFIG_DIR`), never
the versioned plugin dir — a `/plugin update` swaps that dir and would drop user
config. Optional: every section has a default, so a fresh install just works; the
global file is created on first write (e.g. when a workspace auto-registers). Writes
always target the global file — local overrides are read-only, hand-authored.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Section defaults — every load() deep-merges real layers over these, so a partial
# config (e.g. only `workspaces`) still yields sane `gitlab` / `precommit`.
_DEFAULTS: dict = {
    "workspaces": [],
    "gitlab": {"token": "", "host": ""},
    "precommit": {"default": {}, "repos": {}},
}

_LOCAL_NAME = ".devloop"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def config_dir() -> Path:
    """Global config dir — survives plugin version switches."""
    env = os.environ.get("DEVLOOP_CONFIG_DIR")
    if env:
        return Path(_expand(env))
    return Path.home() / ".devloop"


def config_file() -> Path:
    """The global, writable config file."""
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
def load(repo_dir: str | Path | None = None) -> dict:
    """Merged config: `_DEFAULTS < global < ancestor .devloop/config.json (closest wins)`.

    `repo_dir` enables the local layers (the repo's / workspace's `.devloop/config.json`
    and any in between); without it only the global file is consulted.
    """
    out: dict = {}
    for layer in [_DEFAULTS, _read_global(), *(_read_json(f) or {} for f in _local_files(repo_dir))]:
        out = _deep_merge(out, layer)
    return out


def save(data: dict) -> None:
    """Persist to the GLOBAL file. Local overrides are hand-authored, never written here."""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def update(mutate) -> dict:
    """Read-modify-write the GLOBAL config, preserving all sections. `mutate(d)` edits in place."""
    data = _deep_merge(_DEFAULTS, _read_global())
    mutate(data)
    save(data)
    return data


# ── section accessors ────────────────────────────────────────────────────────
def workspaces() -> list[str]:
    # The workspace registry is a global-only discovery concern — not subject to
    # per-repo override (a repo declaring "which dirs are workspaces" is nonsensical).
    return [_expand(p) for p in (_deep_merge(_DEFAULTS, _read_global())).get("workspaces", []) if isinstance(p, str)]


def set_workspaces(ws: list[str]) -> None:
    update(lambda d: d.__setitem__("workspaces", list(ws)))


def gitlab_token(repo_dir: str | Path | None = None) -> str | None:
    """Canonical token: env `GITLAB_TOKEN` wins (CI-friendly), else `gitlab.token`
    from the config closest to `repo_dir`."""
    env = os.environ.get("GITLAB_TOKEN")
    if env and env.strip():
        return env.strip()
    tok = ((load(repo_dir).get("gitlab") or {}).get("token") or "").strip()
    return tok or None


def gitlab_host(repo_dir: str | Path | None = None) -> str | None:
    """Optional host override (config closest to `repo_dir`); None → derive from origin."""
    host = ((load(repo_dir).get("gitlab") or {}).get("host") or "").strip()
    return host or None


def precommit(repo_dir: str | Path | None = None) -> dict:
    return load(repo_dir).get("precommit") or {}


# ── internals ────────────────────────────────────────────────────────────────
def _read_global() -> dict:
    """Global layer: `~/.devloop/config.json` (or `$DEVLOOP_CONFIG_DIR`). `{}` if absent."""
    return _read_json(config_file()) or {}


def _local_files(repo_dir: str | Path | None) -> list[Path]:
    """Ancestor `.devloop/config.json` files from `repo_dir` upward, shallow→deep so the
    closest (deepest) wins when deep-merged last. Excludes the global file; bounded at $HOME."""
    if not repo_dir:
        return []
    glob = config_file()
    home = Path.home()
    found: list[Path] = []
    start = Path(os.path.abspath(_expand(str(repo_dir))))
    for anc in [start, *start.parents]:
        f = anc / _LOCAL_NAME / "config.json"
        if f != glob and f.is_file():
            found.append(f)
        if anc == home:
            break
    found.reverse()
    return found


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
