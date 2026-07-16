#!/usr/bin/env python3
"""pr — the CLI for inspecting / managing an EXISTING pull/merge request, over the forge facade.

One discoverable surface for the verbs below, so an agent reaches for `pr <verb>` instead of
hand-rolling curl against some other tool's credential file (the failure this consolidates
away). Provider-neutral (GitHub PR / GitLab MR) and config-driven: the token comes from
`lib.config.forge_token` — env < `~/.devloop/config.json` < nearest `.devloop/config.json` —
never a forge-specific credentials path.

  pr show     <n|url> [--repo R]                             state / branches / merge-readiness / comments
  pr list     [--limit N] [--branch B] [--repo R]            recent MRs, or just this branch's
  pr update   <n> [--title|--description|--target-branch] [--repo R]
  pr close    <n> [--repo R]                                 close without merging
  pr findings <n> [--pending] [--repo R]                     published findings + ccr:label verdicts
  pr reply    <n> <comment-id> <body> [--repo R]             reply in a comment's thread

Deliberately NO `create`: opening an MR is a commit+push transaction under the branch/staging
gates, which lives in gcampr (`smart_gcampr.sh`). `pr` never touches your working tree — that
boundary is the point; don't dissolve it by adding a create verb that would only forward.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent))

from lib import cli, review_feedback  # noqa: E402
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


def cmd_findings(ns) -> int:
    """List the review findings published to a PR/MR, each with its `ccr:label` verdict or
    `PENDING`. The labeling loop's read half — it exists so the skill has ONE provider-neutral
    command instead of a `gh api` line plus a GitLab one, and so the fp↔verdict join lives in
    `review_feedback`, not re-improvised in prose per agent."""
    forge = _forge_or_exit(ns, "pr findings")
    number = _number_or_exit(ns.number, "pr findings")
    try:
        found = review_feedback.findings(forge.comments(number))
    except ForgeError as e:
        print(f"pr findings: {e}", file=sys.stderr)
        return 1
    if ns.pending:
        found = [f for f in found if f.pending]
    if not found:
        print("no published findings" + (" pending a verdict" if ns.pending else ""))
        return 0
    for f in found:
        loc = f.comment.path + (f":{f.comment.line}" if f.comment.line else "")
        # comment-id is what `pr reply` takes — printed so the two commands chain by copy.
        print(f"{f.comment.id}  [{f.label or 'PENDING'}]  {loc or '?'}  ccr:fp={f.fp}")
        body = (f.comment.body or "").strip().replace("\n", " ")
        print(f"    {body[:200]}")
    return 0


def cmd_reply(ns) -> int:
    """Reply in a comment's thread — how a `ccr:label=<verdict>` verdict lands on the finding
    it judges. Takes a comment id from `pr findings`; the thread is resolved here so callers
    never touch the provider's threading model."""
    forge = _forge_or_exit(ns, "pr reply")
    number = _number_or_exit(ns.number, "pr reply")
    try:
        target = next((c for c in forge.comments(number) if c.id == ns.comment_id), None)
        if target is None:
            print(f"pr reply: no comment {ns.comment_id} on "
                  f"{pr_label(forge.provider, number)}", file=sys.stderr)
            return 1
        forge.reply(number, target, ns.body)
    except ForgeError as e:
        print(f"pr reply: {e}", file=sys.stderr)
        return 1
    print(f"replied to {ns.comment_id} on {pr_label(forge.provider, number)}")
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

    p_find = sub.add_parser("findings", help="published review findings + their ccr:label verdicts")
    p_find.add_argument("number", metavar="number|url")
    p_find.add_argument("--pending", action="store_true", help="only findings without a verdict")
    cli.add_repo_arg(p_find)
    p_find.set_defaults(fn=cmd_findings)

    p_reply = sub.add_parser("reply", help="reply in a comment's thread (e.g. a ccr:label verdict)")
    p_reply.add_argument("number", metavar="number|url")
    p_reply.add_argument("comment_id", metavar="comment-id", help="from `pr findings`")
    p_reply.add_argument("body")
    cli.add_repo_arg(p_reply)
    p_reply.set_defaults(fn=cmd_reply)

    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
