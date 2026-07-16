#!/usr/bin/env python3
"""Resolve an `/enter` target and optionally enter a managed worktree.

Output protocol (first line):
  MATCH<TAB><absolute_path>
  CANDIDATES<TAB><name1>\\t<path1>\\t<name2>\\t<path2>...
  NONE<TAB><reason>
Exit codes: 0 single match; 2 multiple candidates; 1 no match / error.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from domain import repo as repo_model, worktree  # noqa: E402


def emit(line: str, code: int) -> int:
    print(line)
    return code


def parse_args(argv: list[str]) -> tuple[str | None, str | None]:
    """Return ``(query, worktree_tag)`` after extracting ``--worktree <tag>``."""
    tag = None
    args = argv[1:]
    query_parts: list[str] = []
    while args:
        arg = args.pop(0)
        if arg == "--worktree":
            tag = args.pop(0) if args else None
        else:
            query_parts.append(arg)
    query = " ".join(query_parts).strip() or None
    return query, tag


def resolve_base(query: str) -> tuple[str | None, int, str]:
    """Adapt the shared resolver result to `/enter`'s stable line protocol."""
    result = repo_model.resolve_enter_target(query)
    if result.path:
        return result.path, 0, ""
    if result.candidates:
        parts = [value for candidate in result.candidates for value in candidate]
        return None, 2, "CANDIDATES\t" + "\t".join(parts)
    return None, 1, f"NONE\t{result.reason}"


def main(argv: list[str]) -> int:
    query, tag = parse_args(argv)
    if not query:
        return emit("NONE\tno argument given", 1)
    path, code, line = resolve_base(query)
    if path is None:
        return emit(line, code)
    if tag:
        managed_path, message = worktree.create_or_reuse(path, tag)
        if managed_path is None:
            return emit(f"NONE\t{message}", 1)
        print(f"MATCH\t{managed_path}")
        print(f"INFO\t{message}")
        return 0
    return emit(f"MATCH\t{path}", 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
