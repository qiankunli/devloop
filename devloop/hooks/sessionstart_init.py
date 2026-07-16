#!/usr/bin/env python3
"""SessionStart: prewarm context + emit session-cadence References once, register
AGENTS.md for FileChanged watching.

Why emit References HERE (additionalContext lands once, before the first prompt —
a stable position) rather than riding every UserPromptSubmit: it's the closest
devloop gets to cache-stable injection for session-grained content (plan §4/§6).
We `mark_session_emitted` so UserPromptSubmit dedups it until it actually changes
(FileChanged / PostCompact clear the stamp to force a re-emit). `reset_turn_injection`
makes the first prompt re-emit the volatile turn block (new session lost history).
(MR-target freshness is handled inside `refresh_all` — TTL-gated, forge-first.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import hook_io
from domain import repo_layout, workspace  # noqa: E402
from domain.context import RepoContext, WorkspaceContext  # noqa: E402


def build(inp: hook_io.HookInput) -> dict | None:
    parts: list[str] = []
    watch: list[str] = []

    # auto-register: starting a session at an unregistered workspace root is the
    # main path (manual init_workspace never happens) — discover it here.
    ws_root = workspace.find_containing_workspace(inp.cwd) or workspace.maybe_register_workspace(inp.cwd)
    if ws_root:
        ws = WorkspaceContext.refresh(ws_root)
        if ws.agents_md.path:
            watch.append(ws.agents_md.path)
        # Watch EVERY subproject's AGENTS.md, not just the startup repo's — the session
        # cd's between subprojects, and CwdChanged can't register new watchPaths, so
        # registering them all up front keeps FileChanged covering whichever repo is edited.
        for sub in ws.subprojects:
            sub_dir = str((Path(ws_root) / (sub.path or sub.name)).resolve())
            amd = repo_layout.find_agents_md(sub_dir, repo_layout.find_repo_code_dir(sub_dir))
            if amd and amd not in watch:
                watch.append(amd)
        s = ws.session_text()
        if s:
            parts.append(s)
            ws.mark_session_emitted(s)

    git_root = repo_layout.find_git_root(inp.cwd)
    if git_root:
        # default-branch freshness is handled (TTL-gated, forge-first) inside refresh_all below;
        # no separate unconditional set-head call here.
        # deliberately NO owner acquire: starting here only selects context, it doesn't
        # touch the checkout's mutable surface (working tree / index / branch position)。
        # 占有由第一笔变更动作建立(edit/checkout guard、posttool git refresh)——与
        # gitignored 豁免同一判据:是否污染 owner 的 diff。并读 session 不互斥。
        ctx = RepoContext.refresh_all(git_root)
        ctx.reset_turn_injection()
        if ctx.agents_md.path:
            watch.append(ctx.agents_md.path)
        s = ctx.session_text()
        if s:
            parts.append(s)
            ctx.mark_session_emitted(s)

    out: dict = {}
    if parts:
        out["additionalContext"] = "\n\n".join(parts)
    if watch:
        out["watchPaths"] = watch
    return out or None


if __name__ == "__main__":
    raise SystemExit(hook_io.run(build, event="SessionStart"))
