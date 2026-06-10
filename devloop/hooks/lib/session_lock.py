"""Per-checkout owner lock — detect a concurrent devloop session sharing this
working tree, so a *guest* session's branch switches can be refused (and it's
told to use a worktree instead). This is the fix for the recurring failure where
a second session switches the shared checkout's branch and scrambles the first
session's uncommitted work.

Mechanism (a "pid lock", not a heartbeat registry):
- One small file `<repo>/.devloop/owner.lock` records `{session_id, pid, branch,
  acquired_at}`. First session to acquire owns the checkout; later sessions are
  guests.
- **Liveness is primarily the owner process being alive** (`os.kill(pid, 0)`),
  with a ts-TTL fallback for when the recorded pid is a transient shell rather
  than the CLI process. So an active owner never expires (pid alive); a crashed
  owner expires (pid dead) within the TTL at worst.
- No shared heartbeat registry and no atomic rewrite of context.json — just this
  dedicated file, written rarely (on acquire) via tmp + os.replace.
- Release has two layers: SessionEnd (`hooks/sessionend_release.py`) unlinks the
  session's own locks on normal exit; pid-death liveness covers crashes.

Each linked git worktree has its own `.devloop/`, so the lock is naturally
per-checkout: two sessions already in separate worktrees never see each other's
lock (they are already isolated — the goal).

Caveat: only another *devloop session's* branch switch (a Bash tool call) is
guardable. A human switching branches in their own terminal is outside any hook.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lib import git_state
from lib.context import base

OWNER_TTL_SEC = 30 * 60  # staleness fallback used only when pid liveness is unavailable


def _lock_file(repo: str | Path) -> Path:
    return base.state_dir(repo) / "owner.lock"


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
