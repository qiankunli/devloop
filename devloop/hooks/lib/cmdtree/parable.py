"""Parsing backend: GNU-bash AST (vendored Parable, MIT) → `cmdtree` neutral nodes.

The ONE module that knows Parable's node shapes. To swap parsers, write a sibling backend
(e.g. `cmdtree.bashlex`) exposing a `parser` that satisfies `cmdtree.base.Parser` and point
`cmdparse` at it — nothing else changes. (bashlex is GPL-3.0, so vendoring it would
relicense devloop; Parable is MIT, hence the default.)
"""
from __future__ import annotations

from lib.cmdtree import base as cmdtree
from lib._vendor import parable


class _ParableBackend:
    """Parsing backend over the vendored Parable bash grammar (MIT) — a `cmdtree.Parser`."""

    def parse(self, command: str) -> cmdtree.Node:
        """Parse `command` (raising on parse failure — `cmdparse` wraps it) and convert the
        Parable AST to the backend-neutral tree."""
        return cmdtree.Seq([_conv(n) for n in parable.parse(command)])


parser: cmdtree.Parser = _ParableBackend()


def _word_text(w: parable.Word) -> str:
    """Word text with one layer of surrounding matching quotes stripped — the guards match
    bare tokens; inner/partial quoting never affects the command/flag tokens they inspect."""
    v = w.value
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _word_subs(words: list[parable.Word]) -> list[cmdtree.Node]:
    """Command/process substitutions (`$(…)` / `<(…)`) embedded in a command's words —
    each becomes a nested tree the walker descends into (a fresh subshell)."""
    out: list[cmdtree.Node] = []
    for w in words:
        for part in getattr(w, "parts", []):
            if isinstance(part, parable.Node):
                out.append(_conv(part))
    return out


def _child_nodes(node: parable.Node):
    """Direct child Nodes via attribute introspection — generic over every compound
    (for/while/if/case/function/…) and a substitution's body."""
    for v in vars(node).values():
        if isinstance(v, parable.Node):
            yield v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, parable.Node):
                    yield x


def _conv(n: parable.Node) -> cmdtree.Node:
    if isinstance(n, parable.Command):
        return cmdtree.Command([_word_text(w) for w in n.words], _word_subs(n.words))
    if isinstance(n, parable.List):
        return cmdtree.Seq([_conv(p) for p in n.parts if not isinstance(p, parable.Operator)])
    if isinstance(n, parable.Subshell):
        return cmdtree.Subshell(_conv(n.body))
    if isinstance(n, parable.BraceGroup):
        return cmdtree.Group(_conv(n.body))
    if isinstance(n, parable.Pipeline):
        return cmdtree.Pipeline([_conv(c) for c in n.commands])
    # compound (for/while/until/if/case/function/…) or a substitution wrapper
    return cmdtree.Compound([_conv(c) for c in _child_nodes(n)])
