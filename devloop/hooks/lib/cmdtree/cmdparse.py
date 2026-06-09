"""Shell-command parsing for the PreToolUse guards — over a backend-neutral command tree.

The guards must avoid two failure modes a regex/flat-tokenizer hits: (a) **false
positives** — `echo "git add -A"` or a glued `;` matching inside quoted text; (b) **false
negatives** — `git -C repo commit` slipping past, or a subshell `(cd x)` leaking its cd to
later commands. Both are *structural*, so the command is parsed by a real bash grammar and
projected to the small surface the guards need.

What the tree buys over a flat splitter (no heuristics): quoting (a quoted `&&` never splits
a command), `git -C repo cmd` (globals skipped for the real subcommand), and **cd scope** —
`( cd x && git push )` attributes push to x, but `( cd x ); git push` does NOT, and a
`$(git push)` is still seen.

**Swappable parser.** This walker reads the `cmdtree` neutral nodes a backend produces; the
backend (a `cmdtree.base.Parser`) is the one import below. Default = Parable (MIT, vendored).
To use bashlex (more mature, but GPL-3.0 → would relicense devloop) write a `cmdtree.bashlex`
module exposing a conforming `parser` and change that import — the walker and every guard
stay untouched.

Best-effort by design: guards are the secondary net; the smart_*.sh scripts are primary
(AGENTS.md §3). Requires Python 3.12+. Public API — `commands` / `command_invocations` /
`git_invocations` (→ `Invocation` / `GitInvocation`, with `.run_dir(base)`) / `first_token_is`
/ `segments`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from lib.cmdtree import base as cmdtree

# ── parsing backend (the swap point — any `cmdtree.Parser`) ─────────────────────
from lib.cmdtree.parable import parser as _parser
# from lib.cmdtree.bashlex import parser as _parser   # alt: bashlex (GPL-3.0; relicenses)

_GIT_GLOBAL_WITH_ARG = {
    "-C", "--git-dir", "--work-tree", "--namespace", "-c", "--exec-path", "--super-prefix",
}


@dataclass
class Invocation:
    """One command invocation: its `argv` (leading `VAR=` env assignments stripped) and the
    `cd` prefix in effect — a scope-aware path *fragment*, not a resolved dir; call
    `run_dir(base)` to layer it over a base. A git call is the `GitInvocation` subtype,
    enriched with its subcommand/args and `-C` target."""

    argv: list[str]
    cd: str | None = None

    def run_dir(self, base: str | Path) -> Path:
        """Effective directory this invocation runs in (a normalized `Path`): the cd prefix
        layered over `base` (relative composes, absolute resets). Guards judge each call
        against THIS dir, not the session cwd."""
        return _layer(base, self.cd)


@dataclass
class GitInvocation(Invocation):
    """A git call enriched with the resolved `subcommand`, its `args`, and `dash_c` — the
    `git -C <dir>` target, which overrides the cd prefix for the run dir (None if no `-C`)."""

    subcommand: str | None = None
    args: list[str] = field(default_factory=list)
    dash_c: str | None = None  # `git -C` target

    def run_dir(self, base: str | Path) -> Path:
        return _layer(base, self.cd, self.dash_c)  # `-C` over (cd over base)


def _layer(base: str | Path, *parts: str | None) -> Path:
    """Layer path fragments over `base` (each relative part composes, each absolute resets),
    returning a normalized `Path` so callers needn't `..`-collapse it themselves."""
    d = Path(base)
    for part in parts:
        if part:
            p = Path(os.path.expanduser(os.path.expandvars(part)))
            d = p if p.is_absolute() else d / p
    return Path(os.path.normpath(d))


def _strip_env(tokens: list[str]) -> list[str]:
    """Drop leading `VAR=val` env assignments (e.g. `PYTHONPATH=. pytest`)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if "=" in t and not t.startswith("-") and "/" not in t.split("=", 1)[0]:
            i += 1
        else:
            break
    return tokens[i:]


def _compose_cd(prefix: str | None, target: str) -> str:
    """cd target over the prefix in effect: relative composes (`cd a && cd b` → a/b),
    absolute resets."""
    if prefix and not os.path.isabs(os.path.expanduser(target)):
        return os.path.join(prefix, target)
    return target


def _git_inv(toks: list[str], cd_prefix: str | None) -> GitInvocation:
    """A git command's tokens → a `GitInvocation`. Global options (-C/-c/--git-dir/...) are
    skipped so the real subcommand is found; `-C` is captured as the run-dir override."""
    j = 1
    cdir: str | None = None
    while j < len(toks):
        t = toks[j]
        if t == "-C" and j + 1 < len(toks):
            cdir = toks[j + 1]
            j += 2
            continue
        if t in _GIT_GLOBAL_WITH_ARG:
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    return GitInvocation(argv=toks, cd=cd_prefix, dash_c=cdir,
                         subcommand=toks[j] if j < len(toks) else None, args=toks[j + 1:])


def _walk(node: cmdtree.Node, cd: str | None, invs: list[Invocation]) -> str | None:
    """Append each command to `invs` (a git call as the `GitInvocation` subtype), tracking the
    cd prefix with correct shell scope. Returns the cd prefix AFTER `node` (for sequential
    composition in a Seq); a subshell / pipeline / compound never leaks its cd out."""
    if isinstance(node, cmdtree.Command):
        toks = _strip_env(node.words)
        result = cd
        if toks:
            base = os.path.basename(toks[0])
            invs.append(_git_inv(toks, cd) if base == "git" else Invocation(argv=toks, cd=cd))
            if base == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
                result = _compose_cd(cd, toks[1])
        for sub in node.subs:  # `$(…)` / `<(…)` run in a fresh subshell → isolated cd
            _walk(sub, None, invs)
        return result
    if isinstance(node, cmdtree.Seq):
        cur = cd
        for item in node.items:
            cur = _walk(item, cur, invs)
        return cur  # threads cd to the next statement (`cd a; git push` → push in a)
    if isinstance(node, cmdtree.Subshell):
        _walk(node.body, cd, invs)
        return cd  # `( … )` cd does NOT escape
    if isinstance(node, cmdtree.Group):
        return _walk(node.body, cd, invs)  # `{ …; }` cd escapes
    if isinstance(node, cmdtree.Pipeline):
        for stage in node.stages:
            _walk(stage, cd, invs)  # each stage its own shell → no cd escape
        return cd
    # Compound (for/while/if/case/function/…): find commands in the current cwd, don't propagate
    for child in node.children:
        _walk(child, cd, invs)
    return cd


def _tree(command: str) -> cmdtree.Node:
    try:
        return _parser.parse(command)
    except Exception:
        return cmdtree.Seq([])


def _walk_all(command: str) -> list[Invocation]:
    invs: list[Invocation] = []
    _walk(_tree(command), None, invs)
    return invs


def commands(command: str) -> list[list[str]]:
    """Each command's tokens (leading `VAR=` env assignments stripped), in execution order."""
    return [inv.argv for inv in _walk_all(command)]


def command_invocations(command: str) -> list[Invocation]:
    """Each command as an `Invocation` (git calls as the `GitInvocation` subtype), in execution
    order — `inv.run_dir(base)` resolves where each one runs. `cd` is scope-aware: a subshell
    `(cd x)` does not attribute later siblings to x."""
    return _walk_all(command)


def git_invocations(command: str) -> list[GitInvocation]:
    """The git calls among `command_invocations` — each a `GitInvocation` (`.subcommand`,
    `.args`, `.dash_c` = `-C` target, `.cd` = prefix in effect, scope-aware)."""
    return [inv for inv in _walk_all(command) if isinstance(inv, GitInvocation)]


def first_token_is(command: str, *names: str) -> bool:
    """True if any command's name (basename, env stripped) is in `names`."""
    return any(os.path.basename(c[0]) in names for c in commands(command))


def segments(command: str) -> list[list[str]]:
    """Each command's tokens with env assignments NOT stripped — for callers that need the
    raw leading `VAR=` (the naked-pytest guard distinguishes `PYTHONPATH=. pytest`)."""
    out: list[list[str]] = []

    def collect(node: cmdtree.Node) -> None:
        if isinstance(node, cmdtree.Command):
            if node.words:
                out.append(node.words)
            for sub in node.subs:
                collect(sub)
        elif isinstance(node, cmdtree.Seq):
            for item in node.items:
                collect(item)
        elif isinstance(node, (cmdtree.Subshell, cmdtree.Group)):
            collect(node.body)
        elif isinstance(node, cmdtree.Pipeline):
            for stage in node.stages:
                collect(stage)
        else:  # Compound
            for child in node.children:
                collect(child)

    collect(_tree(command))
    return out
