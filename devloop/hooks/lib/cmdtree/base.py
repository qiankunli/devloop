"""Backend-neutral command tree — the parsing seam between a bash parser and the guards.

`cmdparse` projects everything the guards need (command token lists, git invocations, cd
scope) from THIS tree, never from a specific parser's AST. A backend (e.g.
`cmdtree_parable`) converts its parser's AST into these nodes; swapping the parser is a
one-line backend swap in `cmdparse`, leaving the walker and every guard untouched.

Only what cd-scoping + command detection need is modelled — operator *types* (`&&`/`;`/`||`)
are dropped (no guard inspects them), but subshell vs brace-group IS kept because their cd
scope differs (`( cd x )` doesn't escape; `{ cd x; }` does).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Command:
    """A simple command: its token list, plus any command/process substitutions found
    inside its words (each a nested tree that runs in a fresh subshell)."""

    words: list[str]
    subs: list[Node] = field(default_factory=list)


@dataclass
class Seq:
    """Operator-joined sequence; cd threads left→right across items (`cd a && git`)."""

    items: list[Node] = field(default_factory=list)


@dataclass
class Subshell:
    """`( … )` — runs in a child shell; its cd does NOT escape to siblings."""

    body: Node


@dataclass
class Group:
    """`{ …; }` — runs in THIS shell; its cd DOES escape."""

    body: Node


@dataclass
class Pipeline:
    """`a | b` — each stage is its own shell; cd does not escape a stage."""

    stages: list[Node] = field(default_factory=list)


@dataclass
class Compound:
    """for/while/if/case/function/… — its child commands run in the current cwd; the
    compound's internal cd is not propagated to its siblings (best-effort)."""

    children: list[Node] = field(default_factory=list)


Node = Command | Seq | Subshell | Group | Pipeline | Compound


@runtime_checkable
class Parser(Protocol):
    """The parsing-backend interface: turn a bash command string into a neutral `Node` tree.

    Structural (a Protocol) — a backend conforms just by exposing `parse`, with no
    inheritance or registration. `cmdparse` selects one backend (its sole backend import);
    a new parser is a module exposing a `Parser`, e.g. a `bashlex` backend alongside the
    default `cmdtree.parable.parser`. `parse` may raise on a parse failure; `cmdparse`
    wraps the call.
    """

    def parse(self, command: str) -> Node: ...
