"""lint gate 开启且 lint 已过时（陈旧或从未跑）时拦 `git commit`。

由 `~/.devloop/config.json` 的 `precommit` 段驱动（默认关）。每条 commit 按它自己的 repo 判定
（`-C <dir>` 或 cwd），故从 workspace 根对 `git -C subrepo commit` 也命中。
"""
from __future__ import annotations

from pathlib import Path

from lib import config, repo_layout
from lib.context import RepoContext
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _repo_config(git_root: str) -> dict:
    cfg = config.precommit(git_root)
    default = cfg.get("default") or {}
    repos = cfg.get("repos") or {}
    repo_abs = str(Path(git_root).resolve())
    for key, val in repos.items():
        if str(Path(key).expanduser().resolve()) == repo_abs:
            return {**default, **(val or {})}
    return dict(default)


class PrecommitGateRule(Rule):
    name = "precommit-gate"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "commit"

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        if not _repo_config(git_root).get("commit_gate_lint", False):
            return []
        rc = RepoContext.load(git_root)
        stale = rc.validation.edits_since_lint if rc else 0
        last = rc.validation.last_lint_at if rc else None
        if stale == 0 and last:
            return []
        parts = ["⚠️  Refusing `git commit`: precommit gate enabled and lint is stale."]
        if not last:
            parts.append("Lint has never run for this branch.")
        if stale:
            parts.append(f"{stale} edit(s) since last lint pass.")
        parts.append("Run /lint (and /test if your repo requires it), then retry commit.")
        parts.append("Disable this gate for the repo under `precommit` in ~/.devloop/config.json.")
        return [Finding(rule=self.name, severity=Severity.DENY, message="\n".join(parts), locator=" ".join(target.argv))]
