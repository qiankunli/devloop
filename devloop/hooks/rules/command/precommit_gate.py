"""lint 被纳入 pre_commit 且 lint 已过时（陈旧或从未跑）时，拦裸 `git commit`。

这是 lifecycle pre_commit gate 的**兜底守卫**：正常 commit 走 `/gcam` → commit_flow，那里
`lifecycle.dispatch("pre_commit")` 会真跑 lint/test 并盖戳；本守卫只防 AI 绕过 smart_* 直接敲
`git commit`（commit_flow 内部用 gitcmd 跑的 commit 是子进程，不触发 PreToolUse）。它**不跑**
lint，只查戳——PreToolUse 有 5s 超时、fail-open，跑不了 lint。

是否把关由 `lifecycle.pre_commit` 是否含 `lint` 决定。每条 commit 按它自己的 repo 判定
（`-C <dir>` 或 cwd），故从 workspace 根对 `git -C subrepo commit` 也命中。
"""
from __future__ import annotations

from domain import repo as repo_model, repo_layout
from lib import config
from domain.context import RepoContext, Validation
from hooks.core.domain import Command, Finding, Severity, TargetKind
from hooks.core.protocol import Rule


class PrecommitGateRule(Rule):
    name = "precommit-gate"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand == "commit"

    def check(self, target: Command, ctx) -> list[Finding]:
        run_dir = target.working_dir.path
        if run_dir is None:
            return []
        git_root = repo_layout.find_git_root(run_dir)
        if not git_root:
            return []
        if "lint" not in (config.lifecycle(git_root).get("pre_commit") or []):
            return []
        rc = RepoContext.load(git_root)
        val = rc.validation if rc else Validation()   # 无 context = 没验过 → 走下面逐 component 判（fail-closed）
        # 问 dispatch 的 lint 会跑哪些 component（同一个 WorkSet），再逐个查**该 component 自己**的戳。
        # 从前这里问 `default_unit`（server > backend > 根）+ repo 级单戳，两处都错：改了 cli 却去
        # 看 server 有没有 lint target、去看一个 repo 级戳有没有置位。于是「server 无 lint」会把改
        # cli 的 commit 整体放行；更糟的是「cli 过、server 挂」时 cli 盖的戳让这里以为全仓已验——
        # gate 挡住了 gcampr，却给裸 `git commit` 发了通行证。正常路径与这条防绕过守卫必须是同一
        # 份策略，否则守卫拦不住它唯一要拦的东西。
        ws = repo_model.select_components(git_root)
        required = [u for u in ws.components if u.lint_target() is not None]
        if not required:
            # 本轮没有任何带 lint target 的 component：dispatch 的 lint 对它们本来就是干净跳过、永远
            # 盖不出戳，硬要戳等于把裸 commit 锁死（跑 fix-lint 也解不开）。与 checks.lint 对齐。
            return []
        # 通行证 = 「lint 跑过」+「跑的就是现在这份内容」。第二条比指纹，不比编辑计数：计数由
        # PostToolUse 报，而它认不出 apply_patch / MultiEdit / Bash 里的改动——那个 0 的意思是
        # 「没人报告」，不是「没改过」（见 repo_model.component_fingerprint）。指纹算不出（None）
        # 也按未验证：宁可多拦一次，不可拿不准还放行。
        unverified: list[tuple[str, str]] = []
        for u in required:
            v = val.component(u.id)
            if not v.last_lint_at:
                unverified.append((u.id, "lint has never run for this branch."))
                continue
            current = repo_model.component_fingerprint(git_root, u)
            if not current or not v.lint_fingerprint or current != v.lint_fingerprint:
                unverified.append((u.id, "content changed since its last lint pass."))
        if not unverified:
            return []
        parts = ["⚠️  Refusing `git commit`: lint is in the pre_commit gate and is stale."]
        for cid, why in unverified:
            parts.append(f"  {cid}: {why}")
        parts.append("Commit via gcam/gcampr instead (the gate runs lint inline), or run the fix-lint skill, then retry.")
        parts.append("Adjust the gate under `lifecycle` in ~/.devloop/config.json.")
        return [Finding(rule=self.name, severity=Severity.DENY, message="\n".join(parts), locator=" ".join(target.argv))]
