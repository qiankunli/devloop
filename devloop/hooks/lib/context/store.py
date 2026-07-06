"""Storage primitives for the `.devloop/` state bus — paths, domains, atomic JSON I/O.

The ONE place that knows where state bytes land and which concurrency invariant holds there.
Everything above (views / seams / ledger writers) calls these; nothing else touches paths.

`.devloop/` state is split into three DOMAINS (see devloop/AGENTS.md 状态总线):
  repo domain      → the MAIN repo's .devloop (state_dir): segments + ledgers that describe
                     the repo / a requirement — one copy, survives worktree cleanup.
  branch domain    → main .devloop/branches/<branch>/ (branch_segment): state that describes
                     one BRANCH (topology / validation / review). git forbids checking the
                     same branch out in two worktrees, so per-file single-writer holds free.
  working-tree domain → each worktree's own .devloop (worktree_state_dir): the owner lock —
                     it protects THIS working tree's mutable surface; centralizing it would
                     wrongly serialize parallel worktrees.

Two persistence disciplines, one primitive each:
  segment (`save_segment`) — single-writer whole-file overwrite, atomic (tmp + os.replace);
  ledger  (`append_jsonl`) — many-writer append-only, never rewritten (the loop's event
  stream); single json lines stay under PIPE_BUF so concurrent O_APPEND writes don't
  interleave — no lock needed.
All of it is best-effort: state is a cache, never a hard dependency — a failed write just
means the next writer/refresh recomputes.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

STATE_DIRNAME = ".devloop"
STATE_FILENAME = "context.json"   # workspace-level state (single owner: the refresh)

# Repo state is split into per-owner segment files under .devloop/. Each segment has exactly
# one writer-role, so a writer overwrites only its own file — the cross-writer lost-update
# class is designed out, not guarded against. A missing / corrupt segment degrades to its
# default (fail-open) without touching the others.
REPO_SEGMENTS = ("meta", "branch", "pr", "validation", "injection")


@lru_cache(maxsize=64)
def _main_repo_root(root: str) -> Path:
    """Resolve a working-tree root to the MAIN repo root — pure file parse, no subprocess.

    `.git` is a dir (main checkout) or absent (workspace root / not a repo) → `root` itself.
    `.git` is a FILE → parse its `gitdir:` line: a linked worktree points into
    `<main>/.git/worktrees/<name>` → return `<main>`. Anything else (a SUBMODULE points into
    the superproject's `.git/modules/…` — writing state there would be a data accident) →
    fall back to `root` itself (local, safe). Cached: the mapping is immutable per process."""
    p = Path(root)
    g = p / ".git"
    try:
        if not g.is_file():
            return p
        text = g.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return p
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            gitdir = Path(line[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (p / gitdir).resolve()
            parts = gitdir.parts
            #  <main>/.git/worktrees/<name>  →  <main>
            if len(parts) >= 3 and parts[-3] == ".git" and parts[-2] == "worktrees":
                return Path(*parts[:-3])
            break
    return p


def state_dir(root: str | Path) -> Path:
    """Repo-domain state dir: the MAIN repo's `.devloop` (a linked worktree resolves to its
    main checkout, so ledgers/segments have ONE home and survive worktree cleanup)."""
    return _main_repo_root(str(root)) / STATE_DIRNAME


def worktree_state_dir(root: str | Path) -> Path:
    """Working-tree-domain state dir: THIS working tree's own `.devloop` (no resolution).
    For state scoped to one working tree — today the owner lock."""
    return Path(root) / STATE_DIRNAME


def tmp_dir(root: str | Path) -> Path:
    """Ephemera under the repo-domain dir (`.devloop/tmp/`): inter-process hand-offs and logs
    (commit_msg scratch, review.log, the ccr history feed). The line vs ledgers: mining /
    audit reads ledgers long-term; tmp is consumed once and safe to delete any time."""
    return state_dir(root) / "tmp"


def branch_segment(branch: str | None, name: str) -> str:
    """Segment name for a BRANCH-domain segment: `branches/<branch>/<name>` under the main
    repo's state dir. `None` (detached HEAD) buckets to `@detached` — transient, never a real
    branch name (git forbids `@`-leading refs)."""
    return f"branches/{branch or '@detached'}/{name}"


def state_file(root: str | Path) -> Path:
    return state_dir(root) / STATE_FILENAME


def segment_file(root: str | Path, name: str) -> Path:
    return state_dir(root) / f"{name}.json"


def _write_atomic(path: Path, data: dict) -> None:
    """tmp + os.replace — readers see old-or-new, never a torn half-write.

    POSIX rename is atomic on the same filesystem; the tmp sits in the same dir so
    that holds. Best-effort: any OSError is swallowed (state is a cache, never a
    hard dependency — a failed write just means the next writer/refresh recomputes).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic
    except OSError:
        pass


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def load_raw(root: str | Path) -> dict | None:
    """Read `.devloop/context.json` as a dict. None if missing/unreadable/not a dict."""
    return _read_json(state_file(root))


def save_raw(root: str | Path, data: dict) -> None:
    """Write `.devloop/context.json` atomically (best-effort, creates the dir)."""
    _write_atomic(state_file(root), data)


def load_segment(root: str | Path, name: str) -> dict | None:
    """Read one repo-state segment file. None if missing/unreadable/not a dict."""
    return _read_json(segment_file(root, name))


def save_segment(root: str | Path, name: str, data: dict) -> None:
    """Write one repo-state segment atomically. The caller is its sole writer-role."""
    _write_atomic(segment_file(root, name), data)


def append_jsonl(root: str | Path, name: str, record: dict) -> None:
    """Append one record as a line to `.devloop/<name>.jsonl` — the **ledger** primitive,
    peer of `save_segment` (see module docstring for the two disciplines)."""
    try:
        p = state_dir(root) / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def to_dict(obj) -> dict:
    """Serialize a context dataclass to a plain dict (for save_raw)."""
    return asdict(obj)
