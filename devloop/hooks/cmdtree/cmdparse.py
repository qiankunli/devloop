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
`git_invocations` (→ `Invocation` / `GitInvocation`, carrying `.env` / `.cd` / `.dash_c` and
`.run_dir(base)`) / `first_token_is`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from hooks.cmdtree import base as cmdtree

# ── parsing backend (the swap point — any `cmdtree.Parser`) ─────────────────────
from hooks.cmdtree.parable import parser as _parser
# from hooks.cmdtree.bashlex import parser as _parser   # alt: bashlex (GPL-3.0; relicenses)

_GIT_GLOBAL_WITH_ARG = {
    "-C", "--git-dir", "--work-tree", "--namespace", "-c", "--exec-path", "--super-prefix",
}


@dataclass
class Invocation:
    """One command invocation: its `argv` (leading `VAR=` env assignments stripped), the
    `env` assignments that were stripped off it, the `cd` prefix in effect — a scope-aware
    path *fragment*, not a resolved dir — and an optional `dash_c`, the tool's own
    `-C <dir>` (git/go/make all chdir before running). Call `run_dir(base)` to layer them
    over a base. A git call is the `GitInvocation` subtype, enriched with its subcommand/args.

    `env` is kept rather than discarded because「这条调用带没带 env 前缀」是调用自身的事实,
    有规则要据此判断(naked-pytest: `PYTHONPATH=. pytest` 不算裸)。以前它只能靠另一个**不追踪
    cd** 的 walker 拿原始 token 才看得到,那个 walker 于是把 cd scope 丢了——规则想同时要 env 和
    run_dir 就无解。一个 walker、一个对象,两个事实都在。"""

    argv: list[str]
    env: list[str] = field(default_factory=list)   # 被剥掉的前缀 `VAR=val`(有则非裸调用)
    cd: str | None = None
    dash_c: str | None = None  # `-C <dir>` target (git/go/make)

    def run_dir(self, base: str | Path | None) -> Path | None:
        """Effective directory for this invocation, or None when no absolute base is known.

        An absolute cd/-C can recover an exact directory even when the tool-level base is
        unknown; relative fragments stay unknown instead of being anchored to session cwd.
        """
        return _layer(base, self.cd, self.dash_c)  # `-C` over (cd over base)


@dataclass
class GitInvocation(Invocation):
    """A git call enriched with the resolved `subcommand` and its `args` (the `git -C <dir>`
    target lives in the base `dash_c`)."""

    subcommand: str | None = None
    args: list[str] = field(default_factory=list)


def _layer(base: str | Path | None, *parts: str | None) -> Path | None:
    """Layer path fragments over `base` (each relative part composes, each absolute resets),
    returning a normalized `Path` so callers needn't `..`-collapse it themselves."""
    d = Path(base) if base is not None else None
    for part in parts:
        if part:
            p = Path(os.path.expanduser(os.path.expandvars(part)))
            if p.is_absolute():
                d = p
            elif d is not None:
                d = d / p
    return Path(os.path.normpath(d)) if d is not None else None


def _split_env(tokens: list[str]) -> tuple[list[str], list[str]]:
    """切开前缀 `VAR=val` env 赋值与其后的 argv(如 `PYTHONPATH=. pytest`)→ `(env, argv)`。
    一处判定产出两半,调用方不必再自己数一遍前缀。"""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if "=" in t and not t.startswith("-") and "/" not in t.split("=", 1)[0]:
            i += 1
        else:
            break
    return tokens[:i], tokens[i:]


def _compose_cd(prefix: str | None, target: str) -> str:
    """cd target over the prefix in effect: relative composes (`cd a && cd b` → a/b),
    absolute resets."""
    if prefix and not os.path.isabs(os.path.expanduser(target)):
        return os.path.join(prefix, target)
    return target


def _git_inv(toks: list[str], cd_prefix: str | None, env: list[str]) -> GitInvocation:
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
    return GitInvocation(argv=toks, env=env, cd=cd_prefix, dash_c=cdir,
                         subcommand=toks[j] if j < len(toks) else None, args=toks[j + 1:])


# Non-git commands whose `-C <dir>` chdirs before running, exactly like `git -C` — so a
# `go -C repo build` / `make -C repo` issued at a workspace root really runs in `repo`, not
# the root. Without this the cwd guard false-positives on them (it only saw git's `-C`).
_DASH_C_CMDS = {"go", "make"}


def _dash_c_target(toks: list[str]) -> str | None:
    """First `-C <dir>` (separate token) or glued `-Cdir` in `toks` — the chdir target for a
    go/make call. go requires `-C` right after the command, make accepts it anywhere; we scan
    all args so either form is caught. Returns None if absent."""
    for j in range(1, len(toks)):
        t = toks[j]
        if t == "-C" and j + 1 < len(toks):
            return toks[j + 1]
        if len(t) > 2 and t.startswith("-C"):  # make's glued `-Crepo`
            return t[2:]
    return None


def _walk(node: cmdtree.Node, cd: str | None, invs: list[Invocation]) -> str | None:
    """Append each command to `invs` (a git call as the `GitInvocation` subtype), tracking the
    cd prefix with correct shell scope. Returns the cd prefix AFTER `node` (for sequential
    composition in a Seq); a subshell / pipeline / compound never leaks its cd out."""
    if isinstance(node, cmdtree.Command):
        env, toks = _split_env(node.words)
        result = cd
        if toks:
            base = os.path.basename(toks[0])
            if base == "git":
                invs.append(_git_inv(toks, cd, env))
            elif base in _DASH_C_CMDS:
                invs.append(Invocation(argv=toks, env=env, cd=cd, dash_c=_dash_c_target(toks)))
            else:
                invs.append(Invocation(argv=toks, env=env, cd=cd))
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
