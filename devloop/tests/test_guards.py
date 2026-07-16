#!/usr/bin/env python3
"""hook guards 与 gate：owner lock、edit/cwd guard、live-branch gate 判定、remote baseline。

Standalone: `python3 devloop/tests/test_guards.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import HOOKS, SCRIPTS, _git, _hook_input, _load_hook, run_main  # noqa: E402  (bootstrap first)


def test_protocol_files_schema():
    """平台协议文件(plugin.json/hooks.json/monitors.json)由 CLI 直接解析,写错 key
    只能等到运行时才暴露(如 monitors 带非法 key 时整个 monitor 静默不跑)——发布前由本测试
    锁住:必填键、合法键集、脚本路径必须经 ${CLAUDE_PLUGIN_ROOT}(裸路径会随
    版本化 cache 目录失效)。新增合法键时有意识地更新这里,这正是协议变更的关卡。"""
    import json
    import re as _re
    import shlex
    P = Path(__file__).resolve().parent.parent  # devloop/

    for manifest in [P / ".claude-plugin/plugin.json", P / ".codex-plugin/plugin.json"]:
        plugin = json.loads(manifest.read_text())
        assert {"name", "version", "description"} <= set(plugin)
        assert _re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
    codex_plugin = json.loads((P / ".codex-plugin/plugin.json").read_text())
    assert codex_plugin.get("skills") == "./skills/"
    assert codex_plugin.get("hooks") == "./hooks/hooks.codex.json"
    codex_hooks = json.loads((P / "hooks/hooks.codex.json").read_text())["hooks"]
    assert any("exec" in (group.get("matcher") or "").split("|")
               for group in codex_hooks["PreToolUse"])
    assert any("exec" in (group.get("matcher") or "").split("|")
               for group in codex_hooks["PostToolUse"])

    def assert_hooks(path, known_events):
        hooks = json.loads(path.read_text())["hooks"]
        assert set(hooks) <= known_events, f"{path.name} unknown hook event: {set(hooks) - known_events}"
        for groups in hooks.values():
            for g in groups:
                assert set(g) <= {"matcher", "hooks"}
                for h in g["hooks"]:
                    assert {"type", "command"} <= set(h)
                    assert set(h) <= {"type", "command", "timeout", "statusMessage"}, f"unknown hook key: {set(h)}"
                    assert h["type"] == "command" and "${CLAUDE_PLUGIN_ROOT}" in h["command"]
                    # command 是一条 shell 行，现在形如 `"<root>/scripts/python" "<root>/hooks/x.py"`
                    # ——**两个** plugin-root 路径，都要校验。用 shlex 分词，别拿字符串裁：按
                    # `${CLAUDE_PLUGIN_ROOT}/` 裸切再 `.split()[0]` 会把闭引号一起带走（切出
                    # `scripts/python"`），断言于是恒假红，且只够着解释器、够不着真正的 hook 脚本。
                    refs = [t.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1]
                            for t in shlex.split(h["command"]) if t.startswith("${CLAUDE_PLUGIN_ROOT}/")]
                    assert refs, f"no ${{CLAUDE_PLUGIN_ROOT}} path in hook command: {h['command']}"
                    for rel in refs:
                        assert (P / rel).exists(), f"hook command points to missing script: {rel}"

    assert_hooks(
        P / "hooks/hooks.json",
        {"PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit",
         "PostCompact", "PreCompact", "FileChanged", "CwdChanged", "Stop", "SubagentStop"},
    )
    assert_hooks(
        P / "hooks/hooks.codex.json",
        {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "PostCompact", "PreCompact",
         "PermissionRequest", "Stop", "SubagentStart", "SubagentStop"},
    )

    monitors = json.loads((P / "monitors/monitors.json").read_text())
    assert isinstance(monitors, list) and monitors
    for m in monitors:
        assert {"name", "command"} <= set(m)
        assert set(m) <= {"name", "command", "description", "interval"}, f"unknown monitor key: {set(m)}"
        assert "${CLAUDE_PLUGIN_ROOT}" in m["command"]

def test_enter_does_not_acquire_owner():
    """enter 只选中上下文,不占资源:占有由第一笔变更动作建立(edit/checkout guard、
    posttool git 变更)。否则只是 /enter 看代码的 session 会把真正要编辑的 session
    拦成 guest——锁保护的是可变面,只读进入不污染它(与 gitignored 豁免同一判据)。"""
    ce = _load_hook("cwdchanged_enter")
    from domain.context import session as session_lock
    R = "/tmp/dlut_enter_noacq"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    inp = _hook_input("", {"session_id": "sess-reader", "cwd": R})
    ce.handle(inp)
    assert session_lock.read(R) is None

def test_codex_sessionstart_drops_watchpaths():
    """Codex SessionStart 不接受 Claude 的 watchPaths 字段；Codex wrapper 复用
    sessionstart_init 的内容生成，但只把 Codex 支持的 additionalContext 发回去。"""
    h = _load_hook("sessionstart_codex_init")
    orig = h.sessionstart_init.build
    try:
        h.sessionstart_init.build = lambda inp: {"additionalContext": "refs", "watchPaths": ["/x/AGENTS.md"]}
        out = h.build(_hook_input("", {"cwd": "/tmp", "session_id": "s"}))
    finally:
        h.sessionstart_init.build = orig
    assert out == {"additionalContext": "refs"}

def test_owner_lock_acquire_atomic():
    """acquire 的 first-actor-wins 必须原子:check-then-replace 的 TOCTOU 窗口里两个
    session 同时首次 acquire 会都\"成功\"、后写覆盖先写。O_EXCL 化后:输掉 create race
    收敛到 deny;stale/corrupt 锁可被接管;锁文件 I/O 错误保持 fail-open。"""
    import time as _t
    from domain.context import session as session_lock
    R = "/tmp/dlut_lockrace"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")

    # TOCTOU 模拟:read 第一次谎报\"无锁\"(检查窗口),实际 A 活跃持锁 → B 不得覆盖
    assert session_lock.acquire(R, "A", "b", pid=os.getpid())
    orig_read, calls = session_lock.read, {"n": 0}
    def flaky_read(repo):
        calls["n"] += 1
        return None if calls["n"] == 1 else orig_read(repo)
    session_lock.read = flaky_read
    try:
        assert session_lock.acquire(R, "B", "b", pid=os.getpid()) is False
    finally:
        session_lock.read = orig_read
    assert session_lock.read(R)["session_id"] == "A"

    # stale 接管:owner pid 已死且 TTL 过期 → guest 可接管
    session_lock.acquire(R, "A", "b", pid=99999999, now=_t.time() - session_lock.OWNER_TTL_SEC - 1)
    assert session_lock.acquire(R, "B", "b", pid=os.getpid()) is True
    assert session_lock.read(R)["session_id"] == "B"

    # corrupt 锁文件不卡死:可被重建
    session_lock._lock_file(R).write_text("{not json")
    assert session_lock.acquire(R, "C", "b", pid=os.getpid()) is True
    assert session_lock.read(R)["session_id"] == "C"

def test_workspace_cwd_guard_cd_scope():
    """cmdtree cd-scope 让守卫变 sound:在 workspace 根直接跑子项目命令 → 拦;同 shell `cd <sub>`
    进了真仓 → 放行;而 cd 在子 shell `(cd sub); uv`(对 uv 无效)→ 仍拦——粗判"有没有 cd"放过了它。"""
    guard = _load_hook("pretool_policy_bash")
    from hooks.rules.command import workspace_cwd as wc
    root = "/tmp/dlut_wsg"; os.makedirs(root, exist_ok=True)
    wc.workspace = type("W", (), {"load_workspaces": staticmethod(lambda: [root])})
    wc.WorkspaceContext = type("WC", (), {"load": staticmethod(lambda p: None)})
    wc.load_active_repo = lambda p, sid=None: None

    def at_root(cmd):
        return guard.decide(_hook_input("Bash", {"cwd": root, "tool_input": {"command": cmd}}))

    assert at_root("uv run pytest")                     # 裸命令在根 → 拦
    assert at_root("uv sync")
    assert at_root("make build")
    assert at_root("make")
    assert at_root("npm install")                       # 项目依赖安装在根 → 拦
    assert at_root("npm run build")
    assert at_root("npm install -g @larksuite/cli@latest") is None
    assert at_root("npm view @larksuite/cli version") is None
    assert at_root("npm help install") is None
    assert at_root("npm init vite") is None              # 未知/脚手架类命令默认放行，避免误拦
    assert at_root("pnpm install")
    assert at_root("pnpm run build")
    assert at_root("pnpm add -g @scope/pkg") is None
    assert at_root("pnpm dlx create-vite") is None
    assert at_root("yarn install")
    assert at_root("yarn run build")
    assert at_root("yarn global add eslint") is None
    assert at_root("yarn npm info eslint") is None
    assert at_root("uv tool install ruff") is None
    assert at_root("uv cache clean") is None
    assert at_root("go env") is None
    assert at_root("go version") is None
    assert at_root("cargo install ripgrep") is None
    assert at_root("cargo search tokio") is None
    assert at_root("cd sub && uv run pytest") is None   # cd 进子项目 → 放行
    assert at_root("(cd sub); uv run pytest")           # 子 shell cd 不外泄 → 仍拦(修复点)
    assert at_root("git status") is None                # 非子项目命令 → 放行
    # go/make 的 `-C <dir>` 自身就 chdir 到真仓,不在根上跑 → 放行(此前误拦,只认 git -C)
    assert at_root("go -C /repo build ./...") is None
    assert at_root("make -C sub build") is None
    assert at_root("go build ./...")                    # 无 -C 的裸 go 在根 → 仍拦
    # Codex hook 的 cwd 可能是 session 根；Bash 工具自己的 workdir 才是命令真实运行处。
    assert guard.decide(_hook_input("Bash", {
        "cwd": root,
        "tool_input": {"command": "uv run pytest", "workdir": "/tmp"},
    })) is None
    # 不在 workspace 根 → 与本守卫无关
    assert guard.decide(_hook_input("Bash", {"cwd": "/tmp", "tool_input": {"command": "uv run x"}})) is None

def test_edit_owner_guard():
    """并发 session 防线的补全:owner 锁随'第一笔编辑'建立(acquire-follows-activity),
    guest 直接改 owner 工作树的文件被硬拦并引导 worktree——此前只有 git switch 被拦,
    第二个 session 直接 Edit 同一 checkout 畅通无阻。"""
    guard = _load_hook("pretool_policy_edit")
    from domain.context import session as session_lock
    R = "/tmp/dlut_eog"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(f"{R}/repo/server", exist_ok=True)
    _git(f"{R}/repo", "init", "-q")
    fp = f"{R}/repo/server/a.py"

    # session A 第一笔编辑 → 放行并成为 owner(锁文件落盘)
    inp_a = _hook_input("Edit", {"session_id": "sess-A", "cwd": R, "tool_input": {"file_path": fp}})
    assert guard.decide(inp_a) is None
    owner = session_lock.read(f"{R}/repo")
    assert owner and owner["session_id"] == "sess-A"

    # 把 owner 的 pid 钉成本进程(活着) → session B 编辑被拦,信息含 worktree 指引
    session_lock.acquire(f"{R}/repo", "sess-A", "feat/x", pid=os.getpid())
    inp_b = _hook_input("Edit", {"session_id": "sess-B", "cwd": R, "tool_input": {"file_path": fp}})
    reason = guard.decide(inp_b)
    assert reason and "worktree" in reason and "owner.lock" in reason

    # gitignored 文件不进 owner 的 status/diff,guest 写它无混入风险 → 放行,
    # 且不抢锁(owner 仍是 sess-A)
    Path(f"{R}/repo/.gitignore").write_text("runs/\n")
    ign = _hook_input("Write", {"session_id": "sess-B", "cwd": R,
                                "tool_input": {"file_path": f"{R}/repo/runs/report.md"}})
    assert guard.decide(ign) is None
    assert session_lock.read(f"{R}/repo")["session_id"] == "sess-A"

    # notebook_path(NotebookEdit)同样解析;owner 自己编辑不受影响
    inp_nb = _hook_input("NotebookEdit", {"session_id": "sess-A", "cwd": R, "tool_input": {"notebook_path": fp}})
    assert guard.decide(inp_nb) is None
    # repo 之外的编辑不 gate
    outside = _hook_input("Edit", {"session_id": "sess-B", "cwd": R, "tool_input": {"file_path": f"{R}/x.py"}})
    assert guard.decide(outside) is None

def test_apply_patch_owner_guard_uses_target_path():
    """Codex ``apply_patch`` must enter the edit policy and anchor owner lookup to the patched
    file. Its session cwd commonly remains at the aggregate workspace root, which is not a repo.
    """
    guard = _load_hook("pretool_policy_edit")
    from domain.context import session as session_lock
    R = "/tmp/dlut_patch_owner"
    repo = f"{R}/repo"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    fp = f"{repo}/a.py"
    Path(fp).write_text("old\n")
    patch = f"*** Begin Patch\n*** Update File: {fp}\n@@\n-old\n+new\n*** End Patch\n"

    # The hook's freeform-tool normalization stores the patch under ``input``.
    inp_a = _hook_input("apply_patch", {
        "session_id": "sess-A", "cwd": R, "tool_input": {"input": patch},
    })
    assert guard.decide(inp_a) is None
    assert session_lock.read(repo)["session_id"] == "sess-A"

    session_lock.acquire(repo, "sess-A", "feat/x", pid=os.getpid())
    inp_b = _hook_input("apply_patch", {
        "session_id": "sess-B", "cwd": R, "tool_input": {"input": patch},
    })
    reason = guard.decide(inp_b)
    assert reason and "worktree" in reason and "owner.lock" in reason

def test_codex_exec_envelope_runs_edit_and_command_guards():
    """Codex unified tools expose only top-level ``exec`` to hooks; nested mutations must still
    acquire owner and nested shell commands must still pass through command policy.
    """
    import json
    guard = _load_hook("pretool_policy_edit")
    from domain.context import session as session_lock
    R = "/tmp/dlut_exec_owner"
    repo = f"{R}/repo"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    fp = f"{repo}/a.py"
    Path(fp).write_text("old\n")
    patch = f"*** Begin Patch\n*** Update File: {fp}\n@@\n-old\n+new\n*** End Patch\n"
    source = f"const patch = {json.dumps(patch)};\ntext(await tools.apply_patch(patch));\n"

    inp = _hook_input("exec", {
        "session_id": "sess-A", "cwd": R, "tool_input": {"input": source},
    })
    assert guard.decide(inp) is None
    assert session_lock.read(repo)["session_id"] == "sess-A"

    command = f'const r = await tools.exec_command({{"cmd":"git add -A","workdir":{json.dumps(repo)}}});'
    reason = guard.decide(_hook_input("exec", {
        "session_id": "sess-A", "cwd": R, "tool_input": {"input": command},
    }))
    assert reason and "git add" in reason

    # PostToolUse sees the same envelope and refreshes the repo/session binding after the edit.
    post = _load_hook("posttool_codex_refresh")
    from domain.context import load_active_repo
    from domain import workspace
    previous = workspace.load_workspaces()
    try:
        workspace.register_workspace(R)
        post.handle(inp)
        assert load_active_repo(R, "sess-A") == str(Path(repo).resolve())
    finally:
        workspace.save_workspaces(previous)

def test_branch_merged_guard_uses_file_path():
    """INACTIVE 分支编辑拦截按 file_path 解析 repo——session cwd 在 workspace 根时
    cwd-based 查找为 None,guard 此前静默失效。Also exercises the gate's SHA validation: the
    merged PR's source sha is reachable from the LIVE HEAD, so it's genuinely this branch's PR."""
    from lib import git_state
    from domain.context import RepoContext, prstate
    guard = _load_hook("pretool_policy_edit")
    R = "/tmp/dlut_bmg"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(f"{R}/repo", exist_ok=True)
    _git(f"{R}/repo", "init", "-q"); _git(f"{R}/repo", "config", "user.email", "t@t.t")
    _git(f"{R}/repo", "config", "user.name", "t"); _git(f"{R}/repo", "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/repo/f").write_text("x"); _git(f"{R}/repo", "add", "f"); _git(f"{R}/repo", "commit", "-qm", "i")
    RepoContext.refresh_all(f"{R}/repo")
    head = git_state.get_head_sha(f"{R}/repo")
    prstate.persist_pr(f"{R}/repo", {"branch": "feat/a", "provider": "github", "pr_number": 9,
                                     "prs": [{"number": 9, "state": "merged", "source_branch": "feat/a", "sha": head}]})
    # cwd 在 workspace 根(R,非 git repo),编辑文件在 repo 内 → 仍要拦
    inp = _hook_input("Edit", {"session_id": "s", "cwd": R, "tool_input": {"file_path": f"{R}/repo/f"}})
    reason = guard.decide(inp)
    assert reason and "no longer active" in reason

def test_gate_uses_live_branch_after_unobserved_checkout():
    """The incident: an unobserved checkout (subshell `cd "$var" && git checkout`, make, another
    terminal) leaves branch.json pinned to the OLD branch whose PR merged. The cached display
    path stays fooled; gate.evaluate reads the LIVE branch so it does NOT falsely block the new
    branch's edits."""
    from lib import git_state
    from domain.context import RepoContext, gate, prstate
    guard = _load_hook("pretool_policy_edit")
    R = "/tmp/dlut_incident"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/old")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)                       # branches/feat/old/branch.json written
    head = git_state.get_head_sha(R)
    prstate.persist_pr(R, {"branch": "feat/old", "provider": "github", "pr_number": 1,
                           "prs": [{"number": 1, "state": "merged", "source_branch": "feat/old", "sha": head}]})
    # unobserved checkout: HEAD moves to feat/new; nothing refreshed
    _git(R, "checkout", "-q", "-b", "feat/new")
    # branch-domain segments are LIVE-keyed (branches/<live>/…): the display now reads feat/new's
    # (empty) segment, not feat/old's stale cache — the fooled-display failure mode is structurally
    # gone, not just tolerated by the gate.
    assert RepoContext.load(R).branch_pr_inactive() is False
    # gate reads the LIVE branch (feat/new); the merged PR is feat/old's → NOT inactive
    assert gate.evaluate(R).inactive() is False
    # …so the edit guard does NOT block an edit on feat/new
    inp = _hook_input("Edit", {"session_id": "s", "cwd": R, "tool_input": {"file_path": f"{R}/f"}})
    assert guard.decide(inp) is None

def test_gate_protect_uses_live_branch():
    """Protect-guard fail-open regression: branch.json cached says a feature branch, but HEAD is
    LIVE on a protected branch (unobserved checkout). The guard must still refuse commit/push —
    a stale cache must never let a push to a protected branch slip through."""
    from domain.context import RepoContext, base, store
    guard = _load_hook("pretool_policy_bash")
    R = "/tmp/dlut_protect_live"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "release")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    # forge the CURRENT branch's segment to a non-protected name (a corrupt/poisoned cache —
    # live keying makes stale-by-switch impossible, but the file content itself is still a cache)
    seg_name = store.branch_segment("release", "branch")
    seg = store.load_segment(R, seg_name); seg["local"]["name"] = "feat/safe"
    store.save_segment(R, seg_name, seg)
    assert RepoContext.load(R).branch.local.is_protected() is False     # cache fooled
    inp = _hook_input("Bash", {"cwd": R, "tool_input": {"command": "git commit -m x"}})
    reason = guard.decide(inp)
    assert reason and "protected branch 'release'" in reason            # gate read LIVE → blocked

def test_protect_branch_push_exemptions():
    """保护分支 push 的两类豁免：tag-only push（发版打 tag）与空仓首推（远端零分支，
    无历史可保护）放行；分支 push、证明不了是 tag 的 bare name、远端删除仍拦（fail-closed）。"""
    from domain.context import RepoContext
    guard = _load_hook("pretool_policy_bash")
    R = "/tmp/dlut_tagpush"; B = "/tmp/dlut_tagpush_remote"
    for d in (R, B):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(R)
    _git("/tmp", "init", "-q", "--bare", B)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "main")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    _git(R, "remote", "add", "origin", B)
    _git(R, "tag", "v0.1.0")
    RepoContext.refresh_all(R)

    def decide(cmd):
        return guard.decide(_hook_input("Bash", {"cwd": R, "tool_input": {"command": cmd}}))

    # 空仓首推：远端一个分支都没有 → 放行
    assert decide("git push -u origin main") is None
    # tag-only push 各形态放行（bare tag name / 显式 refs/tags / --tags）
    assert decide("git push origin v0.1.0") is None
    assert decide("git push origin refs/tags/v0.1.0") is None
    assert decide("git push --tags") is None
    # 远端有分支后：分支 push 恢复拦截，tag push 仍放行
    _git(R, "push", "-qu", "origin", "main")
    assert "protected branch 'main'" in (decide("git push origin main") or "")
    assert decide("git push") is not None
    assert decide("git push origin v0.1.0") is None
    # fail-closed：bare name 本地证明不了是 tag / 远端删除 / --delete
    assert decide("git push origin nosuchref") is not None
    assert decide("git push origin :refs/tags/v0.1.0") is not None
    assert decide("git push --delete origin v0.1.0") is not None
    # commit 无豁免
    assert "protected branch 'main'" in (decide("git commit -m x") or "")

def test_gate_branch_name_reuse_not_falsely_inactive():
    """finding-3 defense: a rebuilt branch reusing an OLD name whose merged PR points at an
    unreachable sha must NOT be marked inactive — the merged PR is not this HEAD's PR. A
    name-only join (the old load path) would wrongly block here."""
    from lib import git_state
    from domain.context import RepoContext, gate, prstate
    R = "/tmp/dlut_reuse"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/x")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    sha1 = git_state.get_head_sha(R)
    _git(R, "commit", "--amend", "-qm", "rebuilt")     # HEAD → sha2; sha1 now unreachable
    RepoContext.refresh_all(R)
    prstate.persist_pr(R, {"branch": "feat/x", "provider": "github", "pr_number": 1,
                           "prs": [{"number": 1, "state": "merged", "source_branch": "feat/x", "sha": sha1}]})
    assert gate.evaluate(R).inactive() is False         # sha1 not an ancestor of HEAD → not ours

def test_source_tree_reflects_dependency_direction():
    """domain 拥有业务规则、lib 提供技术能力；二者都不得反向依赖入口 adapter。"""
    root = HOOKS.parent
    domain = root / "domain"
    shared = root / "lib"
    assert (domain / "repo.py").is_file() and (domain / "workspace.py").is_file()
    assert (domain / "forge.py").is_file() and not (shared / "forge" / "base.py").exists()
    assert (domain / "context").is_dir() and (domain / "lifecycle").is_dir()
    for misplaced in ("repo.py", "repo_layout.py", "workspace.py", "worktree.py", "context", "lifecycle"):
        assert not (shared / misplaced).exists(), f"domain owner must not regrow in lib: {misplaced}"
    assert not list((HOOKS / "lib").glob("*.py")), "hook-only code must not regrow hooks/lib"
    for layer in (domain, shared):
        for path in layer.rglob("*.py"):
            source = path.read_text()
            assert "from hooks" not in source and "import hooks" not in source, (
                f"{layer.name} must not depend on hook adapters: {path.relative_to(layer)}"
            )
            assert "from scripts" not in source and "import scripts" not in source, (
                f"{layer.name} must not depend on script adapters: {path.relative_to(layer)}"
            )


def test_gates_use_gate_seam_not_cached_identity():
    """CI invariant: the hard gates resolve branch facts through domain.context.gate (LIVE), never
    the cached RepoContext identity. Prevents a future guard from silently regressing to the
    stale-cache fail-open / false-block the gate seam exists to kill."""
    for rel in ("rules/command/protect_branch.py", "rules/edit/branch_merged.py"):
        src = (HOOKS / rel).read_text()
        assert "gate.evaluate" in src, f"{rel} must read gate truth"
        for forbidden in ("branch_pr_inactive", ".branch.current", ".branch.local"):
            assert forbidden not in src, f"{rel} must not read cached branch identity ({forbidden})"
    sgo = (SCRIPTS / "commit_flow.py").read_text()
    assert "def prepare_branch(intent: GitIntent, gv: gate.GateView" in sgo
    assert "ctx.branch_pr_inactive" not in sgo

def test_fork_from_sticky_across_refresh():
    """fork_from is git-unrecorded → set at cut, PRESERVED across a refresh while the branch is
    unchanged, DROPPED on a switch (the old branch's fork point doesn't apply to the new one)."""
    from domain.context import RepoContext
    R = "/tmp/dlut_fork"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    RepoContext.load(R).set_fork_from("release")
    assert RepoContext.load(R).branch.local.fork_from == "release"
    RepoContext.refresh_branch(R)                       # same branch → preserved
    assert RepoContext.load(R).branch.local.fork_from == "release"
    _git(R, "checkout", "-q", "-b", "feat/b")
    RepoContext.refresh_branch(R)                       # switch → dropped
    assert RepoContext.load(R).branch.local.fork_from is None

def test_remote_branches_segment_is_monitor_owned():
    """remote_branches.json is the monitor's: load merges it into the topology (with its
    fetched_at provenance), and a refresh (refresh-owned branch.json) does NOT clobber it —
    the owner-disjoint segment property that makes lost updates structurally impossible."""
    from domain.context import RepoContext, prstate
    R = "/tmp/dlut_remotes"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    prstate.persist_remote_branches(R, {"fetched_at": 123.0, "remotes": [{"name": "main", "commit": "abc"}]})
    topo = RepoContext.load(R).branch
    assert topo.remotes_fetched_at == 123.0 and topo.remote_tip("main").commit == "abc"
    RepoContext.refresh_branch(R)                       # refresh writes branch.json only
    assert RepoContext.load(R).branch.remote_tip("main").commit == "abc"

def test_remote_baseline_includes_target_and_fork_from():
    """Remote-tip polling tracks the repo's actual baseline (target + fork_from), not just the
    conventional trunks — so a develop/staging baseline gets a 'trunk moved' signal (Codex P2)."""
    from domain.context import base, store, prstate
    R = "/tmp/dlut_baseline"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(f"{R}/.devloop")
    store.save_segment(R, "meta", {"repo": {"default_branch": "staging"}})   # target is now meta.default_branch
    store.save_segment(R, "branch", {"local": {"name": "feat/x", "fork_from": "develop"}})
    bases = prstate._baseline_branches(R)
    assert "develop" in bases and "staging" in bases
    assert bases[:len(prstate.TRUNK_CANDIDATES)] == prstate.TRUNK_CANDIDATES   # conventional trunks first


if __name__ == "__main__":
    run_main(globals())
