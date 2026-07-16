"""Composite wake source — fan out over the leaf sources and merge their fires into one stream.

Lets a single transport watch the WHOLE state bus, agnostic to which source fired:
`notify.py channel all` (one standing channel, multi-wake) or `notify.py waiter all <repo>`
(one background task, armed after a `should-arm` probe). Aggregation lives here in code, over the
authoritative `.devloop/<seg>.json` segments — NOT in a separate notify file — so there is no
duplicated state to keep in sync: each producer keeps writing its own segment, the leaves read it.

`carry` is `{source_name: that source's carry}`; `step` advances every leaf and concatenates
whatever they fire. A leaf reading a missing segment just doesn't fire, so an `all` watch over a
repo that only has some segments is fine.
"""
from __future__ import annotations

from lib.notify.base import Notification, Source


class CompositeSource:
    """A Source made of Sources. `name` doubles as the registry/CLI token (`all`); `instructions`
    unions the leaves' channel briefs so a `channel all` handshake covers every kind."""

    name = "all"

    def __init__(self, leaves: dict[str, Source]) -> None:
        self._leaves = leaves
        self.instructions = "\n\n".join(
            f"[{n}] {s.instructions}" for n, s in leaves.items() if s.instructions
        )

    def seed(self, repo: str) -> dict:
        return {n: s.seed(repo) for n, s in self._leaves.items()}

    def step(self, repo: str, carry) -> tuple[dict, list[Notification]]:
        carry = carry or {}
        new_carry: dict = {}
        notes: list[Notification] = []
        for n, s in self._leaves.items():
            c, ns = s.step(repo, carry.get(n))
            new_carry[n] = c
            notes.extend(ns)
        return new_carry, notes
