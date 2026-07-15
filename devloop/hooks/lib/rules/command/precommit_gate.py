"""lint 被纳入 pre_commit 且 lint 已过时（陈旧或从未跑）时，拦裸 `git commit`。

这是 lifecycle pre_commit gate 的**兜底守卫**：正常 commit 走 `/gcam` → smart_git_ops，那里
`lifecycle.dispatch("pre_commit")` 会真跑 lint/test 并盖戳；本守卫只防 AI 绕过 smart_* 直接敲
`git commit`（smart_git_ops 内部用 gitcmd 跑的 commit 是子进程，不触发 PreToolUse）。它**不跑**
lint，只查戳——PreToolUse 有 5s 超时、fail-open，跑不了 lint。

是否把关由 `lifecycle.pre_commit` 是否含 `lint` 决定。每条 commit 按它自己的 repo 判定
（`-C <dir>` 或 cwd），故从 workspace 根对 `git -C subrepo commit` 也命中。
"""
from __future__ import annotations

from lib import config, repo_layout, repo_resolve
from lib.context import RepoContext, Validation
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule


class PrecommitGateRule(Rule):
    name = "precommit-gate"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "commit"

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        if "lint" not in (config.lifecycle(git_root).get("pre_commit") or []):
            return []
        rc = RepoContext.load(git_root)
        val = rc.validation if rc else Validation()   # 无 context = 没验过 → 走下面逐 unit 判（fail-closed）
        # 问 dispatch 的 lint 会跑哪些 unit（同一个 WorkSet），再逐个查**该 unit 自己**的戳。
        # 从前这里问 `default_unit`（server > backend > 根）+ repo 级单戳，两处都错：改了 cli 却去
        # 看 server 有没有 lint target、去看一个 repo 级戳有没有置位。于是「server 无 lint」会把改
        # cli 的 commit 整体放行；更糟的是「cli 过、server 挂」时 cli 盖的戳让这里以为全仓已验——
        # gate 挡住了 gcampr，却给裸 `git commit` 发了通行证。正常路径与这条防绕过守卫必须是同一
        # 份策略，否则守卫拦不住它唯一要拦的东西。
        ws = repo_resolve.select_units(git_root)
        required = [repo_layout.unit_id(u, git_root) for u in ws.units if u.lint_target() is not None]
        if not required:
            # 本轮没有任何带 lint target 的 unit：dispatch 的 lint 对它们本来就是干净跳过、永远
            # 盖不出戳，硬要戳等于把裸 commit 锁死（跑 fix-lint 也解不开）。与 checks.lint 对齐。
            return []
        unverified = [(uid, val.unit(uid)) for uid in required]
        unverified = [(uid, v) for uid, v in unverified if not v.last_lint_at or v.edits_since_lint]
        if not unverified:
            return []
        parts = ["⚠️  Refusing `git commit`: lint is in the pre_commit gate and is stale."]
        for uid, v in unverified:
            if not v.last_lint_at:
                parts.append(f"  {uid}: lint has never run for this branch.")
            else:
                parts.append(f"  {uid}: {v.edits_since_lint} edit(s) since last lint pass.")
        parts.append("Commit via gcam/gcampr instead (the gate runs lint inline), or run the fix-lint skill, then retry.")
        parts.append("Adjust the gate under `lifecycle` in ~/.devloop/config.json.")
        return [Finding(rule=self.name, severity=Severity.DENY, message="\n".join(parts), locator=" ".join(target.argv))]
