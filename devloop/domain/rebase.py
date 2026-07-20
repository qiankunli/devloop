"""Safe, resumable rebase transaction for an existing remote branch.

The force-with-lease expectation is captured *before* history is rewritten and kept in
checkout-local Git metadata.  Keeping capture and publish in one transaction matters: a
bare ``git push --force-with-lease`` that derives its expectation after the rebase can
silently bless a colleague's intervening push and overwrite it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from lib import git_state, gitcmd

from .context import RepoContext, gate, prstate


class RebaseError(Exception):
    """The transaction cannot safely advance."""


@dataclass(frozen=True)
class RebaseState:
    branch: str
    target: str
    remote_sha: str
    target_sha: str
    started_head: str
    started_at: float
    version: int = 1

    @classmethod
    def from_dict(cls, raw: dict) -> "RebaseState":
        required = ("branch", "target", "remote_sha", "target_sha", "started_head")
        if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
            raise RebaseError("saved rebase state is corrupt; inspect and remove it before retrying")
        return cls(
            branch=raw["branch"],
            target=raw["target"],
            remote_sha=raw["remote_sha"],
            target_sha=raw["target_sha"],
            started_head=raw["started_head"],
            started_at=float(raw.get("started_at", 0) or 0),
            version=int(raw.get("version", 1) or 1),
        )


def _git_path(repo: str, name: str) -> Path:
    result = gitcmd.git(repo, "rev-parse", "--git-path", name)
    if not result.ok or not result.out:
        raise RebaseError(f"cannot resolve checkout-local Git metadata path: {result.err or result.out}")
    path = Path(result.out)
    return path if path.is_absolute() else Path(repo) / path


def _state_path(repo: str) -> Path:
    # `--git-path` resolves inside the linked worktree's own git-dir, so parallel worktrees
    # never share a transaction and detached HEAD during a conflict can still find its state.
    return _git_path(repo, "devloop-rebase.json")


def load_state(repo: str) -> RebaseState | None:
    path = _state_path(repo)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RebaseError(f"cannot read saved rebase state at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RebaseError(f"saved rebase state at {path} is not a JSON object")
    return RebaseState.from_dict(raw)


def _save_state(repo: str, state: RebaseState) -> None:
    """Persist the lease strictly: losing this file would make a safe finish impossible."""
    path = _state_path(repo)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise RebaseError(f"cannot save rebase lease at {path}: {exc}") from exc


def _clear_state(repo: str) -> None:
    try:
        _state_path(repo).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RebaseError(f"rebase completed but its saved state could not be removed: {exc}") from exc


def _in_progress(repo: str) -> bool:
    return any(_git_path(repo, name).exists() for name in ("rebase-merge", "rebase-apply"))


def _require_state(repo: str) -> RebaseState:
    state = load_state(repo)
    if state is None:
        raise RebaseError("no devloop rebase transaction; run `smart_rebase.sh start` first")
    return state


def _require_clean(repo: str, action: str) -> None:
    result = gitcmd.git(repo, "status", "--porcelain")
    if not result.ok:
        raise RebaseError(f"cannot inspect the working tree before {action}: {result.err or result.out}")
    if result.out:
        raise RebaseError(
            f"working tree is dirty; commit/stash the listed changes before {action}:\n{result.out}"
        )


def _check_branch_name(repo: str, branch: str, label: str) -> None:
    result = gitcmd.git(repo, "check-ref-format", "--branch", branch)
    if not result.ok:
        raise RebaseError(f"invalid {label} branch {branch!r}: {result.err or result.out}")


def _refresh(repo: str) -> None:
    RepoContext.refresh_branch(repo)
    prstate.refresh_pr(repo)


def start(repo: str, target: str | None = None) -> list[str]:
    """Capture the source lease, fetch source+target, and begin the rebase.

    A conflict is an expected paused state: the function returns a PLAN with guidance while
    retaining the transaction. Non-conflict failures remove the lease because there is no
    resumable Git operation.
    """
    if load_state(repo) is not None:
        raise RebaseError("a devloop rebase transaction already exists; use status/continue/finish/abort")
    if _in_progress(repo):
        raise RebaseError("Git already has a rebase in progress that devloop did not start")
    _require_clean(repo, "starting rebase")

    view = gate.evaluate(repo, live_refresh=True)
    branch = view.branch
    if not branch:
        raise RebaseError("current checkout is detached; start from the source branch")
    if view.protected():
        raise RebaseError(f"refusing to rewrite protected branch {branch!r}")
    if view.inactive():
        raise RebaseError(f"refusing to rewrite {branch!r}: its PR/MR is merged or closed")

    pr_target = view.active_pr.target_branch if view.active_pr else ""
    selected_target = target or pr_target or view.target
    if target and pr_target and target != pr_target:
        raise RebaseError(
            f"--target {target!r} differs from the open PR/MR target {pr_target!r}; "
            "update the PR/MR target first or omit --target"
        )
    if selected_target == branch:
        raise RebaseError("source and target branch are the same")
    _check_branch_name(repo, branch, "source")
    _check_branch_name(repo, selected_target, "target")

    tips = git_state.ls_remote_tips(repo, branch, selected_target, timeout=15)
    remote_sha = tips.get(branch, "")
    if not remote_sha:
        raise RebaseError(f"origin/{branch} does not exist or the remote is unavailable")
    if not tips.get(selected_target):
        raise RebaseError(f"origin/{selected_target} does not exist or the remote is unavailable")

    # Explicit destinations make both tracking refs authoritative even when the configured
    # fetch refspec is narrow. '+' is local-only: a rewritten remote branch must still be
    # observable so the ancestry preflight can reject it safely.
    fetched = gitcmd.git(
        repo,
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        f"+refs/heads/{selected_target}:refs/remotes/origin/{selected_target}",
        timeout=60,
    )
    if not fetched.ok:
        raise RebaseError(f"fetching origin failed: {fetched.err or fetched.out}")

    observed_source = git_state.rev_parse(repo, f"origin/{branch}")
    if observed_source != remote_sha:
        raise RebaseError(
            f"origin/{branch} moved while preparing the rebase "
            f"({remote_sha[:9]} → {observed_source[:9]}); restart from fresh state"
        )
    if not git_state.is_ancestor(repo, remote_sha, view.head_sha):
        raise RebaseError(
            f"local {branch!r} does not contain current origin/{branch} {remote_sha[:9]}; "
            "reconcile the remote update before rebasing"
        )

    target_sha = git_state.rev_parse(repo, f"origin/{selected_target}")
    if not target_sha:
        raise RebaseError(f"could not resolve fetched origin/{selected_target}")
    state = RebaseState(
        branch=branch,
        target=selected_target,
        remote_sha=remote_sha,
        target_sha=target_sha,
        started_head=view.head_sha,
        started_at=time.time(),
    )
    _save_state(repo, state)
    plan = [
        f"captured lease origin/{branch} @ {remote_sha[:9]}",
        f"fetched origin/{branch} and origin/{selected_target} @ {target_sha[:9]}",
    ]

    result = gitcmd.git(repo, "rebase", f"origin/{selected_target}", timeout=120)
    if result.ok:
        plan.append(f"rebased {branch} onto origin/{selected_target}")
        plan.append("run relevant tests, then `smart_rebase.sh finish` (no --message needed)")
        _refresh(repo)
        return plan
    if _in_progress(repo):
        detail = (result.err or result.out).splitlines()
        plan.append(f"rebase paused on conflicts: {detail[-1] if detail else 'resolve conflicted files'}")
        plan.append("resolve + git add the files, then run `smart_rebase.sh continue`")
        _refresh(repo)
        return plan

    _clear_state(repo)
    raise RebaseError(f"rebase could not start: {result.err or result.out}")


def continue_rebase(repo: str) -> list[str]:
    state = _require_state(repo)
    if not _in_progress(repo):
        branch = git_state.get_current_branch(repo)
        if branch == state.branch:
            return ["rebase is already complete", "run relevant tests, then `smart_rebase.sh finish`"]
        raise RebaseError("saved transaction exists, but Git has no rebase in progress on its source branch")

    result = gitcmd.git(repo, "-c", "core.editor=true", "rebase", "--continue", timeout=120)
    if result.ok:
        _refresh(repo)
        return [
            f"rebase of {state.branch} onto origin/{state.target} is complete",
            "run relevant tests, then `smart_rebase.sh finish`",
        ]
    if _in_progress(repo):
        detail = (result.err or result.out).splitlines()
        return [
            f"rebase remains paused: {detail[-1] if detail else 'resolve the remaining conflicts'}",
            "resolve + git add the files, then run `smart_rebase.sh continue` again",
        ]
    raise RebaseError(f"rebase --continue failed: {result.err or result.out}")


def finish(repo: str) -> list[str]:
    state = _require_state(repo)
    if _in_progress(repo):
        raise RebaseError("rebase still has unresolved steps; run continue or abort before finish")
    branch = git_state.get_current_branch(repo)
    if branch != state.branch:
        raise RebaseError(
            f"transaction belongs to {state.branch!r}, but the checkout is on {branch or 'detached HEAD'!r}"
        )
    _require_clean(repo, "publishing rebased history")

    head = git_state.get_head_sha(repo)
    if not git_state.is_ancestor(repo, state.target_sha, head):
        raise RebaseError(
            f"HEAD no longer contains the rebased target {state.target_sha[:9]}; "
            "inspect the branch before publishing"
        )

    current_tip = git_state.ls_remote_tips(repo, state.branch, timeout=15).get(state.branch, "")
    if current_tip != state.remote_sha:
        shown = current_tip[:9] if current_tip else "missing/unavailable"
        raise RebaseError(
            f"origin/{state.branch} moved since start ({state.remote_sha[:9]} → {shown}); "
            "the lease was not used and no remote history was overwritten"
        )
    if head == state.remote_sha:
        _clear_state(repo)
        _refresh(repo)
        return [f"origin/{state.branch} already matches HEAD; cleared the no-op transaction"]

    lease = f"--force-with-lease=refs/heads/{state.branch}:{state.remote_sha}"
    refspec = f"refs/heads/{state.branch}:refs/heads/{state.branch}"
    result = gitcmd.git(repo, "push", lease, "-u", "origin", refspec, timeout=60)
    if not result.ok:
        raise RebaseError(
            f"lease-protected push failed; transaction retained for inspection: {result.err or result.out}"
        )

    _clear_state(repo)
    _refresh(repo)
    return [
        f"rewrote origin/{state.branch} only if it was still at {state.remote_sha[:9]}",
        f"published rebased HEAD {head[:9]}",
    ]


def abort(repo: str) -> list[str]:
    state = _require_state(repo)
    if not _in_progress(repo):
        raise RebaseError(
            f"rebase of {state.branch!r} is already complete; automatic abort is no longer safe. "
            f"Inspect HEAD and the saved start {state.started_head[:9]} before resetting manually"
        )
    result = gitcmd.git(repo, "rebase", "--abort", timeout=30)
    if not result.ok:
        raise RebaseError(f"git rebase --abort failed: {result.err or result.out}")
    _clear_state(repo)
    _refresh(repo)
    return [f"aborted rebase of {state.branch} and cleared its saved lease"]


def status(repo: str) -> list[str]:
    state = load_state(repo)
    if state is None:
        return ["no devloop rebase transaction"]
    current = git_state.get_current_branch(repo) or "detached HEAD"
    head = git_state.get_head_sha(repo)
    phase = "conflict/continue" if _in_progress(repo) else "ready for tests/finish"
    return [
        f"phase={phase} branch={state.branch} target={state.target}",
        f"checkout={current} HEAD={head[:9]}",
        f"lease expects origin/{state.branch} @ {state.remote_sha[:9]}",
    ]
