"""Shared CLI surface for devloop scripts — the canonical repo-target argument.

Centralizes the one thing every repo-acting script was hand-rolling differently: the
"which repo?" argument. `repo_model`'s own error messages already prescribe `--repo`
(`pass --repo <name|path>`), yet most scripts only accepted a bare positional — so an
agent that followed that advice hit `no subproject matches '--repo'` (the flag was
swallowed as the repo name, the real path dropped). This module makes `--repo` real and
uniform, keeping the positional as an equivalent for the bare `script.py <repo>` calls in
the skill markdown.

The `ArgParser` below — devloop's argument parser — is the other half: it turns the opaque
"unrecognized arguments" failure into actionable guidance, so a mistyped flag teaches the
caller instead of being silently misparsed.
"""
from __future__ import annotations

import argparse
import sys

from domain import repo as repo_model

# Single source of truth for the help text, deliberately worded like repo_model's failure
# messages so `--help` and a resolution error describe the same contract.
REPO_ARG_HELP = (
    "repo to operate on: a path or a workspace subproject name; "
    "default = cwd's repo, falling back to the workspace's last-active repo"
)


class ArgParser(argparse.ArgumentParser):
    """devloop's argument parser — used by every devloop script (reference it as
    `cli.ArgParser`). The single place cross-script CLI conventions live; behaviors
    accumulate here as the script family grows.

    Current behavior: error() appends `extra_hints` on "unrecognized arguments" — the opaque
    failure where a mistyped flag, or a value whose quoting broke shell parsing so its tail
    leaks as stray argv, surfaces as a cryptic message. Pass `extra_hints` for domain guidance;
    they print only on that failure (other errors already name the offending argument).
    """

    def __init__(self, *args, extra_hints=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_hints = list(extra_hints or [])

    def error(self, message: str):  # noqa: A003 — argparse API name
        hint = ""
        if self._extra_hints and "unrecognized arguments" in message:
            hint = "\n" + "\n".join(self._extra_hints)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}{hint}\n")


def add_repo_arg(parser: argparse.ArgumentParser, *, positional: bool = True) -> None:
    """Add the canonical repo-target argument to `parser`.

    `--repo/-r` is the canonical form (the one repo_model's errors tell callers to use).
    By default an equivalent optional trailing positional is added too, so the historical
    `script.py <repo>` calls keep working. Read the chosen value with `repo_target(ns)`.
    Call this AFTER the script's own required positionals so the optional repo trails them
    (two optional positionals would be ambiguous — keep the script's leading ones required).
    """
    parser.add_argument("--repo", "-r", dest="repo", default=None, help=REPO_ARG_HELP)
    if positional:
        parser.add_argument(
            "repo_arg", nargs="?", default=None, help="same as --repo (positional form)",
        )


def repo_target(ns: argparse.Namespace) -> str | None:
    """The repo target from a namespace built via `add_repo_arg`: the flag wins over the
    positional (both spell the same arg; the flag is the explicit one)."""
    return getattr(ns, "repo", None) or getattr(ns, "repo_arg", None)


def resolve_repo_or_exit(
    ns: argparse.Namespace, prog: str, cwd: str = ".",
) -> tuple[repo_model.Repo, str]:
    """Resolve the repo-target arg to a Repo, or print the reason and exit(1).

    The single failure path for repo resolution, so every script reports it identically
    (prefixed with `prog`). Returns (Repo, how) on success.
    """
    resolved, how = repo_model.resolve_repo_dir(repo_target(ns), cwd)
    if not resolved:
        print(f"{prog}: {how}", file=sys.stderr)
        raise SystemExit(1)
    return resolved, how
