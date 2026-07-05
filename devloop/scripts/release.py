#!/usr/bin/env python3
"""release — cut a versioned release over the forge facade (GitHub Release / GitLab Release).

The tag is created SERVER-SIDE by the forge (`create_release`), so releasing needs no working
tree, no `git push --tags`, and trips no push guard — the failure this replaces is hand-rolling
`gh release create --target <sha>` (a mistyped sha shipped a broken release in practice) or a
raw tag push that the protected-branch guard then blocks. Provider-neutral and config-driven:
the token comes from `lib.config.forge_token` (env < ~/.devloop/config.json < nearest .devloop),
same as `pr`.

  release create <version> [--target ref] [--title T] [--notes … | --notes-file F] [--repo R]
  release latest  [--repo R]                        the current published release

`create` defaults `--target` to the repo's trunk BRANCH NAME (not a sha): the forge tags that
branch's remote tip, so there is no sha to mistype. Version must be semver (vX.Y.Z) and greater
than the last release. With no `--notes`, a plain changelog is auto-drafted from PRs/MRs merged
since the last release — a fallback; hand-written notes (`--notes-file`) read better.

Deliberately NOT in gcampr: releasing is a low-frequency, working-tree-free action with its own
preconditions (increment check, notes), not a commit+push+MR transaction — folding it in would
tangle two unrelated flows. It's a peer of `pr` (forge-only), hence its own script.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent / "hooks"))

from lib import cli, git_state  # noqa: E402
from lib.forge import ForgeError, forge_for_repo, pr_label  # noqa: E402

# Release version must be semver (optional leading v): the tag the forge cuts and the key the
# increment check compares on. Anything else is rejected before a call is made.
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_version(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match((tag or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _forge_or_exit(ns, prog):
    """Resolve repo target → (forge, git_root), or exit. No token / unsupported remote is a
    clean exit(0) — the same not-an-error treatment `pr` gives (devloop runs in repos with no
    forge, and asking about a release there isn't a failure)."""
    resolved, _ = cli.resolve_repo_or_exit(ns, prog)
    forge = forge_for_repo(resolved.git_root)
    if forge is None:
        print(f"{prog}: no token or unsupported remote", file=sys.stderr)
        raise SystemExit(0)
    return forge, resolved.git_root


def _read_notes(ns, prog) -> str:
    """Notes body from --notes-file (a path, or '-' for stdin) or inline --notes; '' if neither
    (create then auto-drafts). File/stdin is the shell-escaping-free path for long notes."""
    if ns.notes_file:
        try:
            return (sys.stdin.read() if ns.notes_file == "-"
                    else Path(ns.notes_file).read_text(encoding="utf-8")).strip("\n")
        except OSError as e:
            print(f"{prog}: --notes-file unreadable: {e}", file=sys.stderr)
            raise SystemExit(1)
    return ns.notes or ""


def _draft_notes(forge, since: str | None) -> str:
    """A plain changelog from PRs/MRs merged since the last release (best-effort fallback).

    `since` is the last release's timestamp; with no last release every merged PR/MR is in
    scope. String-compares ISO-8601 `updated_at` against it — coarse (updated_at isn't merge
    time) but good enough for a draft the caller is expected to refine. Empty when nothing
    qualifies, so `create` ships empty notes rather than a misleading heading."""
    try:
        prs = forge.recent(30)
    except ForgeError:
        return ""
    merged = [
        p for p in prs
        if p.state == "merged" and (since is None or (p.updated_at or "") > since)
    ]
    if not merged:
        return ""
    lines = [f"- {pr_label(forge.provider, p.number)} {p.title}".rstrip() for p in merged]
    return "## Changes\n" + "\n".join(lines)


def cmd_create(ns) -> int:
    forge, git_root = _forge_or_exit(ns, "release create")
    version = ns.version.strip()
    if _parse_version(version) is None:
        print(f"release create: {version!r} is not semver (expected vX.Y.Z)", file=sys.stderr)
        return 1

    try:
        last = forge.latest_release()
    except ForgeError as e:
        print(f"release create: could not read the last release: {e}", file=sys.stderr)
        return 1

    # Increment guard: refuse a version that isn't strictly greater than the last release —
    # the mis-order footgun (re-tagging an old number). Skipped only when the last tag isn't
    # semver-parseable (can't compare) — then warn and proceed.
    if last and last.tag:
        prev = _parse_version(last.tag)
        if prev is None:
            print(f"release create: warning — last release '{last.tag}' isn't semver, "
                  "skipping the increment check", file=sys.stderr)
        elif _parse_version(version) <= prev:
            print(f"release create: {version} is not greater than the last release "
                  f"{last.tag} — pick a higher version", file=sys.stderr)
            return 1

    target = ns.target or git_state.local_default_target(git_root)
    notes = _read_notes(ns, "release create")
    drafted = False
    if not notes:
        notes = _draft_notes(forge, last.created_at if last else None)
        drafted = bool(notes)

    try:
        rel = forge.create_release(tag=version, target=target,
                                   name=ns.title or version, notes=notes)
    except ForgeError as e:
        print(f"release create: {e}", file=sys.stderr)
        return 1

    prev_str = f" (was {last.tag})" if last and last.tag else " (first release)"
    print(f"released {rel.tag}{prev_str} @ {target}")
    if drafted:
        print("  notes auto-drafted from merged PRs/MRs since the last release "
              "(refine with --notes-file if needed)")
    if rel.web_url:
        print(f"  {rel.web_url}")
    return 0


def cmd_latest(ns) -> int:
    forge, _ = _forge_or_exit(ns, "release latest")
    try:
        rel = forge.latest_release()
    except ForgeError as e:
        print(f"release latest: {e}", file=sys.stderr)
        return 1
    if rel is None:
        print("(no releases yet)")
        return 0
    print(f"{rel.tag}  {rel.name}")
    if rel.created_at:
        print(f"  released {rel.created_at}")
    if rel.web_url:
        print(f"  {rel.web_url}")
    return 0


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(
        prog="release",
        description="Cut / inspect a forge release (config.json-driven, provider-neutral).",
    )
    sub = ap.add_subparsers(dest="verb", required=True)

    p_create = sub.add_parser("create", help="publish a release (creates the tag server-side)")
    p_create.add_argument("version", help="semver tag, e.g. v1.8.0")
    p_create.add_argument("--target", default=None,
                          help="branch name or sha to tag (default: the repo's trunk branch)")
    p_create.add_argument("--title", default=None, help="release name (default: the version)")
    p_create.add_argument("--notes", default=None, help="inline release notes (single-quote it)")
    p_create.add_argument("--notes-file", dest="notes_file", default=None,
                          help="read notes from a file ('-' = stdin); no shell escaping")
    cli.add_repo_arg(p_create)
    p_create.set_defaults(fn=cmd_create)

    p_latest = sub.add_parser("latest", help="show the current published release")
    cli.add_repo_arg(p_latest)
    p_latest.set_defaults(fn=cmd_latest)

    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
