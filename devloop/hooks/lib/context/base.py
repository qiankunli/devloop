"""Context state layer — shared leaves, constants, persistence primitives.

The `.devloop/` state bus spans two levels. **Repo** state is split into per-owner
*segment* files (`meta`/`branch`/`mr`/`validation`/`injection`.json) so independent
writer-roles never share a file — see `repo.py`. **Workspace** state is a single
`context.json` (one writer-role: the refresh). This module holds what both share:
leaf dataclasses (`Reference`, `AgentsMd`) plus the re-exported forge
domain (`PullRequest`), the injection
`Cadence` (content-hash dedup with a TTL safety net), tunable constants, and the JSON
read/write primitives. All writes go through `_write_atomic` (tmp + os.replace) so a
reader sees old-or-new, never a torn half-write.

The composite `RepoContext` / `WorkspaceContext` (with their git/AGENTS.md refresh
logic) live in `repo.py` / `workspace.py` and build on these.

Design notes that matter:
- **Cadence dedup** is how prompt injection stays cheap: re-emit only when the
  text changed, or when the TTL elapsed. `PostCompact` calls `Cadence.clear()` so
  state is re-injected right after compaction drops it — replacing a
  guess-with-a-timer TTL net with a native trigger (TTL stays as a backstop).
- All tunables (TTLs, MR cap/poll) are **constants here**, never in docs — they
  drift with experience and belong next to the code that reads them.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# The neutral code-review proposal lives in the forge domain (provider-agnostic). The
# state layer persists it and joins it by number; it does not redefine it.
from lib.forge.base import PRS_CAP, Comment, MergeReadiness, PullRequest, pr_label, vocab  # noqa: F401

# ── tunables (seconds unless noted) ──────────────────────────────────────────
REPO_STALE_SEC = 300          # repo context older than this → refresh_all on next cd/prompt
WORKSPACE_STALE_SEC = 600     # workspace context staleness
TURN_TTL_SEC = 1800           # turn-cadence injection re-emit backstop (~30 min)
SESSION_TTL_SEC = 14400       # session-cadence (References) re-emit backstop (~4 h)
PR_POLL_INTERVAL_SEC = 90     # monitor poll cadence for PR/MR status
REMOTE_VIEW_STALE_SEC = 120   # remote_branches snapshot older than this → re-pull trunk tips on enter
ACTIVE_REPO_TTL_SEC = 21600   # workspace last-active repo validity (~6 h); stale → don't guess
REVIEW_STALE_SEC = 1800       # review.json stuck at "running" longer than this (~30 min) → run_review
                              # almost certainly died mid-flight (sleep/OOM/kill); surface as stale, not running
DEFAULT_BRANCH_TTL_SEC = 86400  # repo default branch is near-immutable → only re-fetch from the forge
                                # once a day (refresh_all runs far more often; this gates the network call)

STATE_DIRNAME = ".devloop"
STATE_FILENAME = "context.json"   # workspace-level state (single owner: the refresh)

# Repo state is split into per-owner segment files under .devloop/ (see plan §state-bus).
# Each segment has exactly one writer-role, so a writer overwrites only its own file —
# the cross-writer lost-update class is designed out, not guarded against. A missing /
# corrupt segment degrades to its default (fail-open) without touching the others.
REPO_SEGMENTS = ("meta", "branch", "pr", "validation", "injection")


# ── leaf dataclasses ─────────────────────────────────────────────────────────
@dataclass
class Reference:
    """One entry from an AGENTS.md References section."""
    title: str
    path: str
    hook: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        return cls(title=d.get("title", ""), path=d.get("path", ""), hook=d.get("hook"))


@dataclass
class AgentsMd:
    path: str | None = None
    references: list[Reference] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentsMd":
        return cls(
            path=d.get("path"),
            references=[Reference.from_dict(r) for r in (d.get("references") or [])],
        )


@dataclass
class Cadence:
    """One injection cadence's dedup stamp (turn or session).

    `should_emit` returns True when the content changed OR the TTL backstop elapsed.
    `clear` (called by the PostCompact hook) forces a re-emit next turn by dropping
    the stamp — so compaction can't silently strip injected state.
    """
    last_hash: str | None = None
    last_emit_at: float | None = None

    def should_emit(self, text: str, *, now: float, ttl: float) -> bool:
        if not text:
            return False
        if _content_hash(text) != self.last_hash:
            return True
        if self.last_emit_at is None or (now - self.last_emit_at) >= ttl:
            return True
        return False

    def mark(self, text: str, *, now: float) -> None:
        self.last_hash = _content_hash(text)
        self.last_emit_at = now

    def clear(self) -> None:
        self.last_hash = None
        self.last_emit_at = None

    @classmethod
    def from_dict(cls, d: dict) -> "Cadence":
        return cls(last_hash=d.get("last_hash"), last_emit_at=d.get("last_emit_at"))


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ── time helpers ─────────────────────────────────────────────────────────────
# All persisted timestamps are float epoch seconds (one representation, no ISO
# parsing / timezone bugs). `fmt_ts` renders them for prompt display only.
def now() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "never"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "never"


def is_stale(ts: float | None, ttl: float, *, now_: float | None = None) -> bool:
    """True if `ts` is missing or older than `ttl` seconds."""
    if ts is None:
        return True
    return ((now_ if now_ is not None else now()) - ts) >= ttl


# ── persistence primitives ───────────────────────────────────────────────────
def state_dir(root: str | Path) -> Path:
    return Path(root) / STATE_DIRNAME


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


def to_dict(obj) -> dict:
    """Serialize a context dataclass to a plain dict (for save_raw)."""
    return asdict(obj)
