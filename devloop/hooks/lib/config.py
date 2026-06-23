"""Unified config — `~/.devloop/config.json` plus optional local overrides.

One file holds everything devloop depends on from the user / its environment, so
the external dependencies (which forge, which token) are explicit in one place:

    {
      "workspaces": ["/abs/workspace/root", ...],   # Mode-A aggregate-workspace registry
      "forges": {               # code-review hosts, keyed by the repo's origin host
        "github.com": {
          "token": "ghp_...",   # canonical token; env GITHUB_TOKEN/GH_TOKEN overrides it
          "type":  "github"     # optional: inferred from the host when omitted
        },
        "gitlab.example.com": {
          "token": "glpat-...",  # env GITLAB_TOKEN overrides it
          "type":  "gitlab",
          "api_host": ""         # optional: real API host when origin is an SSH alias / mirror
        }
      },
      "precommit": {            # per-repo lint commit-gate (default off)
        "default": {"commit_gate_lint": false},
        "repos":   {"/abs/repo": {"commit_gate_lint": true}}
      }
    }

Layering (low → high precedence), each layer may be PARTIAL:

    _DEFAULTS  <  global (~/.devloop/config.json)  <  ancestor .devloop/config.json (closest wins)

A repo or workspace can drop a `.devloop/config.json` next to its runtime state to
override just a few keys (e.g. a different `forges` token for that repo); the
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
# config (e.g. only `workspaces`) still yields sane `forges` / `precommit`.
_DEFAULTS: dict = {
    "workspaces": [],
    "forges": {},
    "precommit": {"default": {}, "repos": {}},   # deprecated：lint commit-gate，已收编进 lifecycle
    # devops 生命周期 hook：相位 → [hook 名]。opt-in，默认全空 = dispatch 每相位 no-op、零行为
    # 变化。lib.lifecycle.dispatch 读它；旧 precommit.commit_gate_lint 由 precommit_gate 守卫兼容读。
    "lifecycle": {
        "default": {"pre_commit": [], "post_commit": [], "pre_mr": [], "post_mr": []},
        "repos": {},
    },
    # 代码策略引擎的架构/层级规则。enabled 默认 False：opt-in per repo，装上不按猜的层映射误拦。
    "arch": {
        "default": {
            "enabled": False,
            "layers": {"/api/": "api", "/service/": "service", "/dao/": "dao", "/model/": "model"},
            "order": ["api", "service", "dao", "model"],
        },
        "repos": {},
    },
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


def forges(repo_dir: str | Path | None = None) -> dict:
    """The host-keyed forge registry from the config closest to `repo_dir`."""
    return load(repo_dir).get("forges") or {}


def forge_entry(host: str, repo_dir: str | Path | None = None) -> dict:
    """Config entry for one origin host (`{token, type?, api_host?}`); `{}` if none."""
    e = forges(repo_dir).get(host)
    return e if isinstance(e, dict) else {}


# Provider → the conventional env var names each ecosystem already uses. Env wins over
# config (CI-friendly) and is keyed by provider, not host, since that's the standard.
_TOKEN_ENV = {"github": ("GITHUB_TOKEN", "GH_TOKEN"), "gitlab": ("GITLAB_TOKEN",)}


def forge_token(host: str, provider: str, repo_dir: str | Path | None = None) -> str | None:
    """Token for `host`: the provider's conventional env var wins, else `forges[host].token`
    from the config closest to `repo_dir`. None if absent."""
    for var in _TOKEN_ENV.get(provider, ()):
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    tok = (forge_entry(host, repo_dir).get("token") or "").strip()
    return tok or None


def precommit(repo_dir: str | Path | None = None) -> dict:
    return load(repo_dir).get("precommit") or {}


def lifecycle(repo_dir: str | Path | None = None) -> dict:
    """已解析的 devops 生命周期 hook 配置：section 的 `default` 叠上 `repos[<repo_dir 绝对路径>]`，
    返回 `phase → [hook 名]`。`lib.lifecycle.dispatch` 读它决定每个相位跑哪些 hook。
    opt-in：默认全空 → 每相位 no-op、零行为变化。"""
    section = load(repo_dir).get("lifecycle") or {}
    merged = dict(section.get("default") or {})
    if repo_dir:
        key = os.path.abspath(_expand(str(repo_dir)))
        repo_over = (section.get("repos") or {}).get(key)
        if isinstance(repo_over, dict):
            merged = _deep_merge(merged, repo_over)
    return merged


def arch(repo_dir: str | Path | None = None) -> dict:
    """已解析的架构规则配置：section 的 `default` 叠上 `repos[<repo_dir 绝对路径>]`。
    代码策略引擎的层级规则读它（layer 映射 + 方向序 + 开关）。"""
    section = load(repo_dir).get("arch") or {}
    merged = dict(section.get("default") or {})
    if repo_dir:
        key = os.path.abspath(_expand(str(repo_dir)))
        repo_over = (section.get("repos") or {}).get(key)
        if isinstance(repo_over, dict):
            merged = _deep_merge(merged, repo_over)
    return merged


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
