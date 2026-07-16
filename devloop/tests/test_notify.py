#!/usr/bin/env python3
"""notify 端口：Source/Notifier/transports（channel/waiter）、composite `all`、should-arm、monitor persist。

Standalone: `python3 devloop/tests/test_notify.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys

from _testkit import _git, _load_script, run_main  # noqa: E402  (bootstrap first)


def test_poll_persist():
    """The monitor is persist-only: prstate writes the `pr` segment (sole writer of
    .devloop/pr.json, for the PR guard / injection). Waking on a change is the forge
    channel's job, not the monitor's."""
    from domain.context import base, store, prstate
    R = "/tmp/dlut_pollh"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    prstate.persist_pr(R, {"branch": "f", "provider": "github", "pr_number": 7, "prs": []})
    assert store.load_segment(R, "pr")["pr_number"] == 7

def test_notify_port():
    """The port shape: a Notification says only WHAT to surface (kind/meta default); both delivery
    backends — ChannelNotifier (channel push) and StdoutNotifier (one-shot stdout) — satisfy the
    Notifier protocol. mcp is imported lazily so this loads without it."""
    from lib.notify.base import Notification, Notifier
    from lib.notify.channel import ChannelNotifier
    from lib.notify.waiter import StdoutNotifier
    assert Notification(content="x").kind == "info" and Notification(content="x").meta == {}
    assert isinstance(ChannelNotifier(None), Notifier)      # push backend
    assert isinstance(StdoutNotifier(), Notifier)       # one-shot-stdout backend

def test_sources_registry():
    """SOURCES maps a name to a Source with the (name, instructions, seed, step) shape both runners
    drive. forge + review are leaves; `all` (CompositeSource) fans over them; a deploy/verdict
    source is one more leaf entry + one module, auto-covered by `all`."""
    from lib.notify.sources import SOURCES
    assert set(SOURCES) == {"forge", "review", "all"}
    for name, src in SOURCES.items():
        assert src.name == name and isinstance(src.instructions, str)
        assert callable(src.seed) and callable(src.step)

def test_review_source():
    """ReviewSource fires `review_done` only for an ACTIONABLE terminal review (findings / failures /
    error), stays silent for running / skipped / clean, and carries the findings inline. wake_key is
    the single 'wake-worthy' definition both transports share; seed() ignores the startup edge;
    generated_at in the key makes a same-sha re-review wake again."""
    from domain.context import base, store
    from lib.notify.sources.review import ReviewSource, wake_key
    assert wake_key(None) is None
    assert wake_key({"status": "running", "reviewed_sha": "a"}) is None
    assert wake_key({"status": "skipped", "reviewed_sha": "a"}) is None
    assert wake_key({"status": "success", "count": 0, "failed": 0, "reviewed_sha": "a"}) is None  # clean
    assert wake_key({"status": "completed_with_errors", "count": 0, "failed": 1, "reviewed_sha": "b"}) is not None
    assert wake_key({"status": "error", "count": 0, "failed": 0, "reviewed_sha": "c"}) is not None
    src = ReviewSource(); assert src.name == "review"
    R = "/tmp/dlut_rsrc"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _rseg = store.branch_segment(None, "review")   # R 无 git → source 同样解析到 @detached 桶
    store.save_segment(R, _rseg, {"status": "running", "reviewed_sha": "a", "count": 0})
    carry = src.seed(R); assert carry is None               # armed mid-run → no startup fire
    store.save_segment(R, _rseg, {"status": "success", "count": 1, "reviewed_sha": "abcdef123456",
                                    "reviewed_range": "origin/main..HEAD", "generated_at": 100.0,
                                    "comments": [{"path": "a.py", "start_line": 3, "end_line": 5,
                                                  "alias": "ds-v4", "content": "bug here"}]})
    carry, notes = src.step(R, carry)
    assert carry == ("abcdef123456", "success", 100.0) and len(notes) == 1
    assert notes[0].kind == "review_done"
    assert notes[0].content.splitlines()[0] == "review[dlut_rsrc]: 1 finding(s) on abcdef123 [origin/main..HEAD]"
    assert notes[0].content.splitlines()[1] == "  - a.py:3-5 (ds-v4) — bug here"
    # same sha re-reviewed → new generated_at → new key → fires again; then a clean review → silent
    store.save_segment(R, _rseg, {"status": "success", "count": 1, "reviewed_sha": "abcdef123456",
                                    "generated_at": 200.0, "comments": [{"path": "a.py", "content": "x"}]})
    carry, notes = src.step(R, carry); assert len(notes) == 1
    store.save_segment(R, _rseg, {"status": "success", "count": 0, "failed": 0, "reviewed_sha": "z"})
    _, notes = src.step(R, carry); assert notes == []       # clean terminal review → no wake

def test_forge_source():
    """ForgeSource fires two INDEPENDENT signals over pr.json: `pr_change` on a lifecycle delta
    (level key, names each transitioned PR) and `merge_blocked` on ENTERING a blocker (edge +
    hysteresis so the async 'checking'/unknown window can't double-fire). seg_key is lifecycle-only."""
    from domain.context import base, store
    from lib.notify.sources.forge import ForgeSource, merge_block_event, seg_key
    assert seg_key(None) is None
    assert seg_key({"pr_number": 12, "prs": [{"number": 12, "state": "open"}]}) == (12, ((12, "open"),))
    src = ForgeSource(); assert src.name == "forge"
    R = "/tmp/dlut_fsrc"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    store.save_segment(R, "pr", {"branch": "b", "pr_number": 12, "prs": [{"number": 12, "state": "open"}]})
    carry = src.seed(R)
    store.save_segment(R, "pr", {"branch": "b", "pr_number": 12,
                                "prs": [{"number": 12, "state": "merged", "title": "docs"}]})
    carry, notes = src.step(R, carry)
    assert len(notes) == 1 and notes[0].kind == "pr_change"
    assert notes[0].content == "forge[dlut_fsrc]: PR #12 open→merged (docs) [branch=b]"
    # merge-block edge+hysteresis (pure): enter wakes; checking HOLDs; same holds; type-change wakes; leave clears
    last, wake = merge_block_event(None, "ready");                  assert last is None and wake is None
    last, wake = merge_block_event(last, "conflict");               assert last == "conflict" and wake == "conflict"
    last, wake = merge_block_event(last, "unknown");                assert last == "conflict" and wake is None
    last, wake = merge_block_event(last, "conflict");               assert wake is None
    last, wake = merge_block_event(last, "discussions_unresolved"); assert wake == "discussions_unresolved"
    last, wake = merge_block_event(last, "ready");                  assert last is None and wake is None
    assert merge_block_event(None, None) == (None, None)

def test_run_waiter():
    """run_waiter steps ANY Source until the first fire — deliver via the notifier, return
    ('changed', note) — or times out → ('timeout', None). Source-agnostic: the wake condition lives
    in the Source, the runner just stops on the first fire (its process exit IS the wake). asyncio
    loop; sleep/clock injected so the test never really waits."""
    import asyncio
    from lib.notify.base import Notification
    from lib.notify.waiter import run_waiter

    class FireOnSecond:
        name = "fake"; instructions = ""
        def __init__(self): self.n = 0
        def seed(self, repo): return None
        def step(self, repo, carry):
            self.n += 1
            return ("k", [Notification(content="fired", kind="fake")]) if self.n == 2 else (carry, [])

    class Capture:
        def __init__(self): self.got = []
        async def deliver(self, n): self.got.append(n)

    clk = {"t": 0.0}
    async def adv(dt): clk["t"] += dt

    cap = Capture()
    reason, note = asyncio.run(run_waiter(FireOnSecond(), "/x", interval=1, timeout=100,
                                          notifier=cap, sleep=adv, clock=lambda: clk["t"]))
    assert reason == "changed" and note.content == "fired" and [n.content for n in cap.got] == ["fired"]

    clk["t"] = 0.0
    class NeverFires(FireOnSecond):
        def step(self, repo, carry): return carry, []
    reason2, note2 = asyncio.run(run_waiter(NeverFires(), "/x", interval=1, timeout=3,
                                            notifier=Capture(), sleep=adv, clock=lambda: clk["t"]))
    assert reason2 == "timeout" and note2 is None

def test_composite_source():
    """CompositeSource fans over the leaves and merges their fires into ONE stream; carry is a
    per-leaf dict, seed ignores each leaf's startup edge, and a fire from ANY leaf propagates.
    Aggregation is in code over the real segments — no separate notify file."""
    from lib.notify.base import Notification
    from lib.notify.sources.composite import CompositeSource

    class Leaf:
        def __init__(self, name, fire_on):
            self.name = name; self.instructions = f"brief-{name}"; self._fire_on = fire_on; self.n = 0
        def seed(self, repo): return None
        def step(self, repo, carry):
            self.n += 1
            if self.n == self._fire_on:
                return self.n, [Notification(content=f"{self.name}!", kind=self.name)]
            return carry, []

    src = CompositeSource({"a": Leaf("a", 1), "b": Leaf("b", 2)})
    assert src.name == "all" and "brief-a" in src.instructions and "brief-b" in src.instructions
    carry = src.seed("/x"); assert carry == {"a": None, "b": None}     # union seed, no startup fire
    carry, notes = src.step("/x", carry)                              # tick 1: a fires, b silent
    assert [n.content for n in notes] == ["a!"] and carry["a"] == 1
    carry, notes = src.step("/x", carry)                              # tick 2: b fires
    assert [n.content for n in notes] == ["b!"] and carry["b"] == 2

def test_should_arm_gate():
    """`notify should-arm` is the synchronous, non-waking decision run BEFORE arming: exit 0 → the
    caller arms a `waiter`; exit 1 → a standing `channel all` covers the session, skip (so a channel
    costs zero arming — a backgrounded decider would itself wake on exit). Signal is explicit: env
    DEVLOOP_NOTIFY_CHANNELS wins, else config.notify().channels (default → arm). Unknown source → 2.
    (Bootstrap points DEVLOOP_CONFIG_DIR at an empty dir, so the no-env case is deterministic.)"""
    notify = _load_script("notify")
    R = "/tmp/dlut_shouldarm"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    saved_env = os.environ.pop("DEVLOOP_NOTIFY_CHANNELS", None)
    try:
        assert notify.main(["should-arm", "all", R]) == 0       # no channel → arm (the floor)
        os.environ["DEVLOOP_NOTIFY_CHANNELS"] = "1"
        assert notify.main(["should-arm", "all", R]) == 1       # channel covers → skip
        os.environ["DEVLOOP_NOTIFY_CHANNELS"] = "0"
        assert notify.main(["should-arm", "all", R]) == 0       # explicit off → arm
        assert notify.main(["should-arm", "bogus", R]) == 2     # unknown source → usage
    finally:
        os.environ.pop("DEVLOOP_NOTIFY_CHANNELS", None)
        if saved_env is not None: os.environ["DEVLOOP_NOTIFY_CHANNELS"] = saved_env


if __name__ == "__main__":
    run_main(globals())
