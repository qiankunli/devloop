"""lint 被纳入 pre_commit 且 lint 已过时（陈旧或从未跑）时，拦裸 `git commit`。

这是 lifecycle pre_commit gate 的**兜底守卫**：正常 commit 走 `/gcam` → smart_git_ops，
那里 `lifecycle.dispatch("pre_commit")` 会真跑 lint/test 并盖戳；本守卫只防 AI 绕过 smart_*
直接敲 `git commit`（smart_git_ops 内部用 gitcmd 跑的 commit 是子进程，不触发 PreToolUse，
故不会被自己拦）。它**不跑** lint，只查戳——PreToolUse 有 5s 超时、fail-open，跑不了 lint。

是否把关由 `lifecycle.pre_commit` 是否含 `lint` 决定（旧 `precommit.commit_gate_lint=true`
兼容等价为「含 lint」）。每条 commit 按它自己的 repo 判定（`-C <dir>` 或 cwd），故从
workspace 根对 `git -C subrepo commit` 也命中。
"""
from __future__ import annotations

from pathlib import Path

from lib import config, repo_layout
from lib.context import RepoContext
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule


def _legacy_commit_gate_lint(git_root: str) -> bool:
    """旧 `precommit` 段的 commit_gate_lint（default 叠 repos[<abs>]）——兼容保留。"""
    cfg = config.precommit(git_root)
    default = cfg.get("default") or {}
    repos = cfg.get("repos") or {}
    repo_abs = str(Path(git_root).resolve())
    for key, val in repos.items():
        if str(Path(key).expanduser().resolve()) == repo_abs:
            return bool({**default, **(val or {})}.get("commit_gate_lint", False))
    return bool(default.get("commit_gate_lint", False))


def _lint_gated(git_root: str) -> bool:
    """lint 是否在 pre_commit 把关：lifecycle.pre_commit 含 `lint`，或旧配置 commit_gate_lint。"""
    pre = config.lifecycle(git_root).get("pre_commit") or []
    return "lint" in pre or _legacy_commit_gate_lint(git_root)


class PrecommitGateRule(Rule):
    name = "precommit-gate"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "commit"

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        if not _lint_gated(git_root):
            return []
        rc = RepoContext.load(git_root)
        stale = rc.validation.edits_since_lint if rc else 0
        last = rc.validation.last_lint_at if rc else None
        if stale == 0 and last:
            return []
        parts = ["⚠️  Refusing `git commit`: lint is in the pre_commit gate and is stale."]
        if not last:
            parts.append("Lint has never run for this branch.")
        if stale:
            parts.append(f"{stale} edit(s) since last lint pass.")
        parts.append("Run /lint (and /test if your repo requires it), then retry commit.")
        parts.append("Adjust the gate under `lifecycle` in ~/.devloop/config.json.")
        return [Finding(rule=self.name, severity=Severity.DENY, message="\n".join(parts), locator=" ".join(target.argv))]
