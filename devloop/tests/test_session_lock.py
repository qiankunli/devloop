from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

from lib.context import session as session_lock  # noqa: E402


def test_acquire_then_foreign_and_self(tmp_path):
    repo = str(tmp_path)
    # session A claims, with a live pid (this test process)
    assert session_lock.acquire(repo, "sess-A", "feat/x", pid=os.getpid()) is True
    assert (Path(repo) / ".devloop" / "owner.lock").exists()

    # a different live session sees A as a foreign owner and cannot acquire
    owner = session_lock.foreign_owner(repo, "sess-B")
    assert owner is not None and owner["branch"] == "feat/x"
    assert session_lock.acquire(repo, "sess-B", "feat/y", pid=os.getpid()) is False

    # A is never foreign to itself
    assert session_lock.foreign_owner(repo, "sess-A") is None


def test_stale_owner_is_reclaimable(tmp_path):
    repo = str(tmp_path)
    dead_pid = 2**31 - 1  # almost certainly not a live process
    session_lock.acquire(
        repo, "sess-A", "feat/x", pid=dead_pid, now=time.time() - session_lock.OWNER_TTL_SEC - 1
    )
    # dead pid + expired ts → inactive → not blocking, reclaimable
    assert session_lock.foreign_owner(repo, "sess-B") is None
    assert session_lock.acquire(repo, "sess-B", "feat/y", pid=os.getpid()) is True
    assert session_lock.read(repo)["session_id"] == "sess-B"


def test_release_own_lock_frees_checkout_immediately(tmp_path):
    repo = str(tmp_path)
    session_lock.acquire(repo, "sess-A", "feat/x", pid=os.getpid())
    # neither another session nor a blank one can release A's lock
    assert session_lock.release(repo, "sess-B") is False
    assert session_lock.release(repo, "") is False
    assert session_lock.read(repo)["session_id"] == "sess-A"
    # owner's normal exit releases at once — next session needn't wait for liveness
    assert session_lock.release(repo, "sess-A") is True
    assert session_lock.read(repo) is None
    assert session_lock.acquire(repo, "sess-B", "feat/y", pid=os.getpid()) is True


def test_blank_session_never_gates_or_writes(tmp_path):
    repo = str(tmp_path)
    session_lock.acquire(repo, "sess-A", "feat/x", pid=os.getpid())
    # a CLI without a session id is never blocked and never overwrites the lock
    assert session_lock.acquire(repo, "", "feat/y") is True
    assert session_lock.read(repo)["session_id"] == "sess-A"
