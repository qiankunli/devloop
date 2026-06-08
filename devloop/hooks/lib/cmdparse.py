"""Shell-command parsing for the PreToolUse guards.

A raw regex over the Bash command string has two failure modes the hard guards
must avoid: (a) **false positives** — `echo "git add -A"` or a grep probe matches
the pattern inside quoted text; (b) **false negatives** — `git -C repo commit` /
`git -c k=v push` slip past `\\bgit\\s+(commit|push)\\b` because the subcommand
isn't adjacent to `git`. This module tokenizes (respecting quotes) and is aware of
git's global options so guards check the real command token, not a substring.

Best-effort by design: full shell semantics (`$(...)`, process substitution,
operator-without-spaces) are out of scope — the primary path is the smart_*.sh
scripts; guards are the secondary net (see AGENTS.md §3). Tokenize-first then split
on operator *tokens* so operators inside quotes (e.g. a commit message) don't split.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

_OPS = {"&&", "||", ";", "|", "&"}
_GIT_GLOBAL_WITH_ARG = {
    "-C", "--git-dir", "--work-tree", "--namespace", "-c", "--exec-path", "--super-prefix",
}


def _tokenize(command: str) -> list[str]:
    # shlex.shlex with punctuation_chars (NOT shlex.split): split 只认空白，会把紧贴
    # 引号/词尾的 `;`/`&&` 吞进前一个 token（如 kubectl -o jsonpath='...';），导致
    # segments() 断不开句、cd 豁免和段头判定全部失真（曾把 kubectl ...; cd sub && uv 串误拦）。
    # punctuation_chars 把运算符即使贴着词也拆成独立 token，且引号内不受影响。
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return command.split()


def segments(command: str) -> list[list[str]]:
    """Token lists per shell segment, split on operator tokens (quote-aware)."""
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in _tokenize(command):
        if t in _OPS:
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


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


def commands(command: str) -> list[list[str]]:
    """Each segment's tokens with leading env assignments stripped (empty dropped)."""
    return [c for c in (_strip_env(seg) for seg in segments(command)) if c]


def git_invocations(command: str) -> list[dict]:
    """git calls in the command. Each: {'subcommand': str|None, 'args': [...], 'cwd': str|None, 'cd': str|None}.

    Global options (-C/-c/--git-dir/...) are skipped so the real subcommand is found
    even in `git -C repo commit`. `cwd` captures the `-C <dir>` target so guards can
    judge the RIGHT repo (a commit/push on `git -C subprojectB` must check subprojectB's
    branch, not the caller's cwd). `/usr/bin/git` matches via basename.

    `cd` is the cd-prefix in effect AT THAT POINT in the command — position-aware,
    not last-cd-wins: in `git push && cd /elsewhere` the push must NOT be attributed
    to /elsewhere（实测让 posttool 刷新落空、branch.json 滞留旧分支）。Relative
    chains compose（`cd a && cd b` → a/b）；`cd -`/option forms are ignored best-effort.
    """
    out: list[dict] = []
    cd_prefix: str | None = None
    for toks in commands(command):
        if not toks:
            continue
        if os.path.basename(toks[0]) == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
            t = toks[1]
            if cd_prefix and not os.path.isabs(os.path.expanduser(t)):
                cd_prefix = os.path.join(cd_prefix, t)
            else:
                cd_prefix = t
            continue
        if os.path.basename(toks[0]) != "git":
            continue
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
        out.append({"subcommand": toks[j] if j < len(toks) else None, "args": toks[j + 1:],
                    "cwd": cdir, "cd": cd_prefix})
    return out


def first_token_is(command: str, *names: str) -> bool:
    """True if any segment's command (env stripped) has basename in `names`."""
    return any(os.path.basename(c[0]) in names for c in commands(command))


def invocation_dir(inv: dict, base: str | Path) -> str:
    """Effective dir of one `git_invocations` entry: `-C` target, layered over the
    cd-prefix in effect at that point, layered over `base`. Guards/refreshers judge
    each git call against THIS dir, not the session cwd — in an aggregate workspace
    the session cwd snaps back to the workspace root between calls, so both
    `git -C subrepo commit` and `cd subrepo && git commit` must resolve to subrepo."""
    d = str(base)
    for part in (inv.get("cd"), inv.get("cwd")):
        if part:
            p = Path(os.path.expanduser(os.path.expandvars(part)))
            d = str(p if p.is_absolute() else Path(d) / p)
    return d
