"""Session-grain state — the third granularity of the `.devloop/` state bus.

The bus has one module per fact-owner grain: repo facts (`repo.py`), workspace
facts (`workspace.py`), and THIS — facts owned by one CLI session. Modules are
organized by OWNER, not by where the file happens to sit (the binding lives under
the workspace dir, the lock under each checkout — both are session facts).

Session runtime state follows a single lifecycle (CONCEPTS〈Session 运行态〉):
created on first activity, released by the SessionEnd hook
(`hooks/sessionend_release.py`), pid/TTL liveness as the crash fallback. Two
instances live here:

- **active-repo binding** — `<workspace_root>/.devloop/active/<session_id>.json`:
  "which repo is this session working on", feeding the scripts' cwd-independent
  repo resolution and the workspace-root turn injection.
- **checkout owner lock** — `<git_root>/.devloop/owner.lock`: the first session
  to MUTATE a checkout owns it; a guest's branch switches and edits are denied
  and routed to a worktree.

Identity: hooks pass the payload session_id; scripts self-identify via the CLI's
session id environment when one is exported (Claude Code uses CLAUDE_CODE_SESSION_ID;
Codex may expose CODEX_SESSION_ID in some runtimes).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .. import git_state
from . import base, store
from .base import ACTIVE_REPO_TTL_SEC


def _session_key(session_id: str | None) -> str:
    """Explicit id (hooks, from the payload) wins; scripts fall back to the env."""
    if session_id is not None:
        return session_id
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "") or os.environ.get("CODEX_SESSION_ID", "") or ""


# ── session log ────────────────────────────────────────────────────────────────
def record_session_event(repo_dir: str | Path, session_id: str | None,
                         kind: str, **fields) -> None:
    """Append one record to `<repo>/.devloop/sessions/<sid>.jsonl` — devloop's own append-only
    log of what it did during one CLI session.

    OBSERVABILITY ONLY, read by humans, never by devloop: nothing loads this back, so it can be
    truncated or deleted at any time and no behavior changes. That's the line to hold — the
    moment code reads it, it stops being exhaust and becomes state, and it has none of the
    durability guarantees state needs.

    `kind` discriminates record types so new ones can land in the SAME file without breaking
    readers (filter on kind) — `inject` is merely the first, added because the injected block
    was otherwise write-only: assembled per turn, spent in the model's context, gone. You could
    read the code that builds it but not what it actually said on a given turn, which is what
    tells you whether a line earned its tokens.

    `ts` / `kind` are reserved; a same-named entry in `fields` would clobber them (same shape as
    `run_review._append_history`). Best-effort — `append_jsonl` swallows I/O errors, because an
    observability write must never cost the caller's real work.

    Which ledgers do NOT belong here, and why the `kind` field doesn't make them fit:
    - `requirements/<id>/session.jsonl` — a REQUIREMENT's lifecycle, keyed by its first branch,
      and devloop reads it back.
    - `review-history.jsonl` — a PR's review rounds, read back by `run_review` to feed the next
      review (`ccr --history`). Keyed by pr_number because review→fix→re-review spans SESSIONS
      by nature (review in one, fix in the next); re-keying it per session would cut that join.
      Its writer is also a detached process with no session id at all.
    The test isn't "is it append-only jsonl" — it's WHO OWNS the lifetime, and whether anything
    reads it. A ledger devloop consumes cannot live somewhere callers are told to truncate freely.
    """
    store.append_jsonl(repo_dir, f"sessions/{_session_name(session_id)}",
                       {"ts": round(base.now(), 1), "kind": kind, **fields})


# ── active-repo binding ────────────────────────────────────────────────────────
# One file per session, owner = that session, so the one-file-one-owner rule holds
# with zero exceptions: no cross-session read-modify-write exists at all. A
# workspace hosts several subprojects, and several concurrent sessions each working
# on a different one is its normal shape — per-session files keep one session's
# no-arg /gcam / run_fixlint fallback untouchable by another's activity.
#
# Readers never guess from OTHER sessions' files: a session with no binding of its
# own gets None and the resolver asks for an explicit --repo — foreign bindings are
# surfaced as a hint in that error, never as an answer. Dead files (crashed
# sessions never ran SessionEnd) are pruned opportunistically on write.


def _session_name(session_id: str | None) -> str:
    """A session id as a safe path component. A CLI that provides no session id degrades to
    one shared "anon" slot — sessions merge there rather than the state going nowhere."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", _session_key(session_id)) or "anon"


def _session_file(ws_root: str | Path, session_id: str | None) -> Path:
    return store.state_dir(ws_root) / "active" / f"{_session_name(session_id)}.json"


def _live_binding(path: Path) -> str | None:
    """The file's repo_dir if fresh and still existing, else None."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = d.get("repo_dir") if isinstance(d, dict) else None
    if not repo or base.is_stale(d.get("ts"), ACTIVE_REPO_TTL_SEC):
        return None
    return repo if Path(repo).is_dir() else None


def record_active_repo(repo_dir: str | Path, session_id: str | None = None) -> None:
    """Bind THIS session to `repo_dir` (`.devloop/active/<sid>.json`)."""
    from .workspace import workspace_for_repo  # deferred: workspace joins on this grain too

    ws = workspace_for_repo(repo_dir)
    if not ws:
        return
    f = _session_file(ws, session_id)
    store._write_atomic(f, {"repo_dir": str(Path(repo_dir).resolve()), "ts": base.now()})
    # opportunistic GC: crashed sessions never ran SessionEnd; drop their dead files here
    try:
        for other in f.parent.glob("*.json"):
            if other != f and _live_binding(other) is None:
                other.unlink(missing_ok=True)
    except OSError:
        pass


def load_active_repo(ws_root: str | Path, session_id: str | None = None) -> str | None:
    """THIS session's bound repo dir, or None — never guesses from other sessions."""
    return _live_binding(_session_file(ws_root, session_id))


def load_active_repo_lenient(ws_root: str | Path,
                             session_id: str | None = None) -> tuple[str, float] | None:
    """READ-path binding: `(repo_dir, age_sec)` ignoring the TTL. For turn INJECTION only.

    The TTL exists so WRITE-path fallbacks (/gcam, run_fixlint.py repo resolution) never guess a stale
    target — that semantic stays in `load_active_repo`. But injection sharing it caused silent
    blindness: past the TTL the repo view vanished without a word and the model kept reasoning
    from hours-old context (the cross-repo merge-order incident). The repo STATE itself is
    monitor-fresh; only the "which repo" binding is old — so the read path keeps injecting and
    lets the caller annotate the age instead of going dark."""
    path = _session_file(ws_root, session_id)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = d.get("repo_dir") if isinstance(d, dict) else None
    if not repo or not Path(repo).is_dir():
        return None
    return repo, max(0.0, base.now() - float(d.get("ts") or 0))


def clear_active_repo(ws_root: str | Path, session_id: str | None = None) -> None:
    """SessionEnd path: drop this session's binding."""
    try:
        _session_file(ws_root, session_id).unlink(missing_ok=True)
    except OSError:
        pass


def active_repo_candidates(ws_root: str | Path) -> list[str]:
    """Distinct live repos across all sessions — a HINT for the resolver's error
    message, never an answer."""
    out: list[str] = []
    try:
        files = sorted((store.state_dir(ws_root) / "active").glob("*.json"))
    except OSError:
        return out
    for p in files:
        repo = _live_binding(p)
        if repo and repo not in out:
            out.append(repo)
    return out


# ── checkout owner lock ────────────────────────────────────────────────────────
# Detect a concurrent devloop session sharing a working tree, so a *guest*
# session's branch switches / edits can be refused (and routed to a worktree).
# This is the fix for the recurring failure where a second session switches the
# shared checkout's branch and scrambles the first session's uncommitted work.
#
# Mechanism (a "pid lock", not a heartbeat registry):
# - One small file `<repo>/.devloop/owner.lock` records `{session_id, pid, branch,
#   acquired_at}`. First session to acquire owns the checkout; later sessions are
#   guests.
# - **Liveness is primarily the owner process being alive** (`os.kill(pid, 0)`),
#   with a ts-TTL fallback for when the recorded pid is a transient shell rather
#   than the CLI process. So an active owner never expires (pid alive); a crashed
#   owner expires (pid dead) within the TTL at worst.
# - Release has two layers: SessionEnd unlinks the session's own locks on normal
#   exit; pid-death liveness covers crashes.
# - No shared heartbeat registry and no atomic rewrite of context.json — just this
#   dedicated file, written rarely (on acquire) via tmp + os.replace.
#
# The lock is WORKING-TREE domain BY DESIGN (store.worktree_state_dir, never the main-repo
# state_dir): it protects one working tree's mutable surface, so each linked worktree keeps
# its own lock and two sessions in separate worktrees never see each other's — parallel
# worktrees stay parallel. Centralizing it in the main repo would wrongly serialize them.
#
# Caveat: only another *devloop session's* branch switch (a Bash tool call) is
# guardable. A human switching branches in their own terminal is outside any hook.

OWNER_TTL_SEC = 30 * 60  # staleness fallback used only when pid liveness is unavailable


def _lock_file(repo: str | Path) -> Path:
    return store.worktree_state_dir(repo) / "owner.lock"


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by another uid
    except (OSError, ValueError, TypeError):
        return False
    return True


def read(repo: str | Path) -> dict | None:
    try:
        d = json.loads(_lock_file(repo).read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _active(owner: dict | None, now: float) -> bool:
    if not owner:
        return False
    if owner.get("pid") and _pid_alive(owner["pid"]):
        return True
    return (now - float(owner.get("acquired_at", 0) or 0)) < OWNER_TTL_SEC


def foreign_owner(repo: str | Path, session_id: str, now: float | None = None) -> dict | None:
    """The active owner iff a DIFFERENT session holds the lock, else None (free / stale / mine)."""
    now = time.time() if now is None else now
    owner = read(repo)
    if (
        owner
        and owner.get("session_id")
        and owner["session_id"] != session_id
        and _active(owner, now)
    ):
        return owner
    return None


def release(repo: str | Path, session_id: str) -> bool:
    """Drop ownership iff THIS session holds the lock (the normal-exit path).

    Two release layers, both required: SessionEnd releases immediately on normal
    exit (a guest needn't wait for any liveness check); pid-death liveness in
    `_active` is the fallback for crashes / a SessionEnd hook that never ran
    (ts-TTL only when the recorded pid can't be probed). Never touches a foreign
    or blank lock; a read-then-unlink race is benign here — takeover requires the
    lock to be inactive, and mine is active while this session still runs.
    """
    if not session_id:
        return False
    owner = read(repo)
    if not owner or owner.get("session_id") != session_id:
        return False
    try:
        _lock_file(repo).unlink()
    except OSError:
        return False
    return True


def acquire(
    repo: str | Path,
    session_id: str,
    branch: str,
    *,
    pid: int | None = None,
    now: float | None = None,
) -> bool:
    """Claim/refresh ownership unless an active *foreign* session holds it.

    Returns True if I own the checkout afterwards (the common case). A blank
    ``session_id`` (older CLI without the field) never gates — returns True
    without writing.

    First-actor-wins is enforced ATOMICALLY: the free-lock path creates the file
    with O_CREAT|O_EXCL, so two sessions racing their first acquire can't both
    succeed（check-then-replace 的 TOCTOU 会让后写者覆盖先写者）。Losing the
    create race converges to deny: re-read and only return True if the winner
    turns out to be me. Refresh of my OWN lock keeps tmp+replace（同 session
    重写自己的记录，无竞争语义）。Stale takeover (unlink+EXCL) still has a tiny
    two-guests-race window — acceptable: the loser is denied on its next action
    by foreign_owner, direction stays deny.
    """
    now = time.time() if now is None else now
    if not session_id:
        return True
    rec = {
        "session_id": session_id,
        "pid": int(pid if pid is not None else os.getppid()),
        "branch": branch or "",
        "acquired_at": now,
    }
    f = _lock_file(repo)
    owner = read(repo)
    if owner and owner.get("session_id") == session_id:
        # mine → refresh in place; same-session writers carry identical claims,
        # so plain atomic replace is race-free in the only sense that matters
        try:
            tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, f)
        except OSError:
            pass
        return True
    if owner and owner.get("session_id") and _active(owner, now):
        return False  # active foreign owner — never overwrite
    try:
        # creating the lock would create .devloop/ early (before any context save) —
        # make sure it's git-excluded first so it can never be committed.
        git_state.ensure_gitignore_excluded(repo)
        f.parent.mkdir(parents=True, exist_ok=True)  # self-creates .devloop/ if absent
        if owner is not None:
            # POSITIVELY-read stale record: clear it so O_EXCL arbitrates the takeover.
            # (Never unlink on mere f.exists() — a read that transiently returned None
            # over an ACTIVE lock would then clobber it, re-opening the TOCTOU.)
            try:
                f.unlink()
            except OSError:
                pass
        for _ in range(2):
            try:
                fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                cur = read(repo)
                if cur is not None:
                    # lost the create race to a real claim — the winner's record decides
                    return cur.get("session_id") == session_id
                # exists but unreadable: a persistently corrupt record would wedge
                # O_EXCL forever — clear it and retry once
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(rec))
            return True
        return False
    except OSError:
        return True  # lock is best-effort: never block work on lock-file I/O errors
