#!/usr/bin/env python3
"""pr — the CLI for inspecting / managing an EXISTING pull/merge request, over the forge facade.

One discoverable surface for the verbs below, so an agent reaches for `pr <verb>` instead of
hand-rolling curl against some other tool's credential file (the failure this consolidates
away). Provider-neutral (GitHub PR / GitLab MR) and config-driven: the token comes from
`lib.config.forge_token` — env < `~/.devloop/config.json` < nearest `.devloop/config.json` —
never a forge-specific credentials path.

  pr show   <n|url> [--repo R]                               state / branches / merge-readiness / comments
  pr list   [--limit N] [--branch B] [--repo R]              recent MRs, or just this branch's
  pr update <n> [--title|--description|--target-branch] [--repo R]
  pr close  <n> [--repo R]                                   close without merging

Deliberately NO `create`: opening an MR is a commit+push transaction under the branch/staging
gates, which lives in gcampr (`smart_gcampr.sh`). `pr` never touches your working tree — that
boundary is the point; don't dissolve it by adding a create verb that would only forward.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.forge import (  # noqa: E402
    ForgeError,
    MergeReadiness,
    forge_for_repo,
    parse_pr_number,
    pr_label,
)

_READINESS_LABEL = {
    MergeReadiness.READY: "✓ ready",
    MergeReadiness.CONFLICT: "✗ conflict",
    MergeReadiness.DISCUSSIONS_UNRESOLVED: "✗ unresolved discussions",
    MergeReadiness.CI_BLOCKED: "✗ CI blocked",
    MergeReadiness.NEEDS_APPROVAL: "✗ needs approval",
    MergeReadiness.DRAFT: "✗ draft",
    MergeReadiness.UNKNOWN: "? unknown (still checking?)",
}


def _forge_or_exit(ns, prog):
    """Resolve the repo target → a forge, or exit. `None` (no token / unsupported remote) is a
    clean exit(0): the same not-an-error treatment the per-verb scripts had — devloop runs in
    repos with no forge and callers shouldn't see a failure for asking."""
    resolved, _ = cli.resolve_repo_or_exit(ns, prog)
    forge = forge_for_repo(resolved.git_root)
    if forge is None:
        print(f"{prog}: no token or unsupported remote", file=sys.stderr)
        raise SystemExit(0)
    return forge


def _number_or_exit(raw, prog):
    n = parse_pr_number(raw)
    if n is None:
        print(f"{prog}: cannot parse PR/MR number from {raw!r}", file=sys.stderr)
        raise SystemExit(1)
    return n


def cmd_show(ns) -> int:
    forge = _forge_or_exit(ns, "pr show")
    number = _number_or_exit(ns.number, "pr show")
    try:
        pr = forge.get(number)
        comments = forge.comments(number)
        readiness = forge.merge_readiness(number)
    except ForgeError as e:
        print(f"pr show: {e}", file=sys.stderr)
        return 1
    print(f"{pr_label(forge.provider, pr.number)}: {pr.title}  [{pr.state}]")
    print(f"  {pr.source_branch} → {pr.target_branch}")
    print(f"  merge: {_READINESS_LABEL.get(readiness, readiness.value)}")
    print(f"  {pr.web_url}")
    if comments:
        print(f"  comments ({len(comments)}):")
        for c in comments[:20]:
            body = (c.body or "").strip().replace("\n", " ")
            print(f"    - {c.author}: {body[:120]}")
    return 0


def cmd_list(ns) -> int:
    forge = _forge_or_exit(ns, "pr list")
    try:
        prs = forge.prs_for_branch(ns.branch) if ns.branch else forge.recent(ns.limit)
    except ForgeError as e:
        print(f"pr list: {e}", file=sys.stderr)
        return 1
    if not prs:
        print("(none)")
        return 0
    for pr in prs:
        print(f"{pr_label(forge.provider, pr.number)} [{pr.state}]  "
              f"{pr.source_branch} → {pr.target_branch}  {pr.title}")
    return 0


def cmd_update(ns) -> int:
    forge = _forge_or_exit(ns, "pr update")
    number = _number_or_exit(ns.number, "pr update")
    # Neutral field names; the adapter maps them per forge (description→body for GitHub).
    fields = {k: v for k, v in (("title", ns.title), ("body", ns.description),
                                ("target_branch", ns.target_branch)) if v is not None}
    if not fields:
        print("pr update: nothing to update (pass --title/--description/--target-branch)", file=sys.stderr)
        return 1
    try:
        pr = forge.update(number, **fields)
    except ForgeError as e:
        print(f"pr update: {e}", file=sys.stderr)
        return 1
    print(f"updated {pr_label(forge.provider, pr.number)}: {pr.title} [{pr.state}] → {pr.web_url}")
    return 0


def cmd_close(ns) -> int:
    forge = _forge_or_exit(ns, "pr close")
    number = _number_or_exit(ns.number, "pr close")
    try:
        pr = forge.close(number)
    except ForgeError as e:
        print(f"pr close: {e}", file=sys.stderr)
        return 1
    print(f"closed {pr_label(forge.provider, pr.number)}: {pr.title} [{pr.state}] → {pr.web_url}")
    return 0


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(
        prog="pr",
        description="Unified pull/merge-request CLI (config.json-driven, provider-neutral).",
    )
    sub = ap.add_subparsers(dest="verb", required=True)

    p_show = sub.add_parser("show", help="view a PR/MR")
    p_show.add_argument("number", metavar="number|url")
    cli.add_repo_arg(p_show)
    p_show.set_defaults(fn=cmd_show)

    p_list = sub.add_parser("list", help="recent PRs/MRs, or this branch's")
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--branch", default=None, help="only this source branch (else recent)")
    cli.add_repo_arg(p_list)
    p_list.set_defaults(fn=cmd_list)

    p_upd = sub.add_parser("update", help="edit title/description/target")
    p_upd.add_argument("number")
    p_upd.add_argument("--title")
    p_upd.add_argument("--description")
    p_upd.add_argument("--target-branch", dest="target_branch")
    cli.add_repo_arg(p_upd)
    p_upd.set_defaults(fn=cmd_update)

    p_close = sub.add_parser("close", help="close without merging")
    p_close.add_argument("number")
    cli.add_repo_arg(p_close)
    p_close.set_defaults(fn=cmd_close)

    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
