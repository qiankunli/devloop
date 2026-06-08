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
(AGENTS.md §3). Requires Python 3.12+. Public API — `commands` / `git_invocations` /
`first_token_is` / `segments` / `invocation_dir` — unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path

from lib.cmdtree import base as cmdtree

# ── parsing backend (the swap point — any `cmdtree.Parser`) ─────────────────────
from lib.cmdtree.parable import parser as _parser
# from lib.cmdtree.bashlex import parser as _parser   # alt: bashlex (GPL-3.0; relicenses)

_GIT_GLOBAL_WITH_ARG = {
    "-C", "--git-dir", "--work-tree", "--namespace", "-c", "--exec-path", "--super-prefix",
}


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


def _git_inv(toks: list[str], cd_prefix: str | None) -> dict:
    """A git command's tokens → {subcommand, args, cwd(-C target), cd(prefix in effect)}.
    Global options (-C/-c/--git-dir/...) skipped so the real subcommand is found."""
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
    return {"subcommand": toks[j] if j < len(toks) else None, "args": toks[j + 1:],
            "cwd": cdir, "cd": cd_prefix}


def _walk(node: cmdtree.Node, cd: str | None, cmds: list[list[str]], gits: list[dict]) -> str | None:
    """Append each command's tokens to `cmds` and each git call to `gits`, tracking the cd
    prefix with correct shell scope. Returns the cd prefix AFTER `node` (for sequential
    composition in a Seq); a subshell / pipeline / compound never leaks its cd out."""
    if isinstance(node, cmdtree.Command):
        toks = _strip_env(node.words)
        result = cd
        if toks:
            cmds.append(toks)
            base = os.path.basename(toks[0])
            if base == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
                result = _compose_cd(cd, toks[1])
            elif base == "git":
                gits.append(_git_inv(toks, cd))
        for sub in node.subs:  # `$(…)` / `<(…)` run in a fresh subshell → isolated cd
            _walk(sub, None, cmds, gits)
        return result
    if isinstance(node, cmdtree.Seq):
        cur = cd
        for item in node.items:
            cur = _walk(item, cur, cmds, gits)
        return cur  # threads cd to the next statement (`cd a; git push` → push in a)
    if isinstance(node, cmdtree.Subshell):
        _walk(node.body, cd, cmds, gits)
        return cd  # `( … )` cd does NOT escape
    if isinstance(node, cmdtree.Group):
        return _walk(node.body, cd, cmds, gits)  # `{ …; }` cd escapes
    if isinstance(node, cmdtree.Pipeline):
        for stage in node.stages:
            _walk(stage, cd, cmds, gits)  # each stage its own shell → no cd escape
        return cd
    # Compound (for/while/if/case/function/…): find commands in the current cwd, don't propagate
    for child in node.children:
        _walk(child, cd, cmds, gits)
    return cd


def _tree(command: str) -> cmdtree.Node:
    try:
        return _parser.parse(command)
    except Exception:
        return cmdtree.Seq([])


def _walk_all(command: str) -> tuple[list[list[str]], list[dict]]:
    cmds: list[list[str]] = []
    gits: list[dict] = []
    _walk(_tree(command), None, cmds, gits)
    return cmds, gits


def commands(command: str) -> list[list[str]]:
    """Each command's tokens (leading `VAR=` env assignments stripped), in execution order."""
    return _walk_all(command)[0]


def git_invocations(command: str) -> list[dict]:
    """Each git call: ``{'subcommand', 'args', 'cwd' (-C target), 'cd' (prefix in effect)}``.
    `cd` is scope-aware — a subshell `(cd x)` does not attribute later siblings to x."""
    return _walk_all(command)[1]


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


def invocation_dir(inv: dict, base: str | Path) -> str:
    """Effective dir of one `git_invocations` entry: the `-C` target over the cd prefix in
    effect, over `base` — guards judge each git call against THIS dir, not the session cwd."""
    d = str(base)
    for part in (inv.get("cd"), inv.get("cwd")):
        if part:
            p = Path(os.path.expanduser(os.path.expandvars(part)))
            d = str(p if p.is_absolute() else Path(d) / p)
    return d
