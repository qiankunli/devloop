#!/usr/bin/env python3
"""commit_flow 编排：staging 过滤、分支决策/切分、CLI 入参（message/title/repo）、PR 描述同步。

Standalone: `python3 devloop/tests/test_git_ops.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _FakeForge, _git, _git_out, _load_script, run_main  # noqa: E402  (bootstrap first)
from domain.context import PullRequest  # noqa: E402
from domain.forge import ForgeError  # noqa: E402


def test_sensitive_filter():
    is_sensitive = _load_script("commit_flow")._is_sensitive
    assert is_sensitive(".env") and is_sensitive("sub/.env.local")
    assert is_sensitive("a/.idea/x") and is_sensitive("pkg/__pycache__/m.pyc")
    assert not is_sensitive("src/main.py") and not is_sensitive("README.md")

def test_gitlink_guard_exempts_registered_submodule():
    """160000 守卫只拦**未注册**的嵌套仓（误 add 的 accident）；`.gitmodules` 注册过的
    submodule 指针 bump 是合法提交（super-repo 的本职就是 bump 指针），放行。"""
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_gitlink"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/README").write_text("x"); _git(R, "add", "README"); _git(R, "commit", "-qm", "init")
    # 嵌套一个独立 git 仓：git add 会把它作为 160000 gitlink 收进 index
    S = f"{R}/sub"; os.makedirs(S)
    _git(S, "init", "-q"); _git(S, "config", "user.email", "t@t.t"); _git(S, "config", "user.name", "t")
    Path(f"{S}/f").write_text("1"); _git(S, "add", "f"); _git(S, "commit", "-qm", "s")
    # 未注册 → 拦，且 index 已回滚
    try:
        sgo.stage(R, [], [])
        assert False, "expected SmartError for unregistered gitlink"
    except sgo.SmartError as e:
        assert "gitlink" in str(e)
    assert _git_out(R, "diff", "--cached", "--name-only") == ""
    # 注册进 .gitmodules → 放行，gitlink 留在 index
    Path(f"{R}/.gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n')
    sgo.stage(R, [], [])
    assert "sub" in _git_out(R, "diff", "--cached", "--name-only").splitlines()

def test_decide_branch_is_intent_driven():
    """--branch 一律基于 base(默认 origin/<target>),与当前停在哪条分支无关——避免新 MR
    夹带上一条未合 feature 分支的提交。"""
    decide = _load_script("commit_flow").decide_branch
    B = "origin/release"
    # 新 --branch:即便当前停在另一条未合 feature 分支,也切自 base 而非当前 HEAD
    assert decide("chat-fix", "bump-x", protected=False, stale=False, base=B) == ("cut", B)
    # 显式 --base → 故意栈式,切自该 base
    assert decide("release", "feat2", protected=True, stale=False, base="feat1") == ("cut", "feat1")
    # 无 --branch + 健康分支 → 续写当前(往开着的 MR 加提交)
    assert decide("feat1", None, protected=False, stale=False, base=B) == ("continue", None)
    # 无 --branch + protected → 报错,要 --branch
    act, why = decide("release", None, protected=True, stale=False, base=B)
    assert act == "error" and "protected" in why
    # 无 --branch + MR 已 merged/closed(stale) → 报错
    act, why = decide("old", None, protected=False, stale=True, base=B)
    assert act == "error" and "merged" in why
    # --branch == 当前分支(已在该分支)→ 续写,不重切
    assert decide("feat1", "feat1", protected=False, stale=False, base=B) == ("continue", None)

def test_refusal_detail_quotes_pr_evidence():
    """A stale-branch refusal embeds the live-polled PR evidence (number/state/sha/url) so the
    caller trusts the verdict instead of re-querying the forge; protected branches (no PR) and an
    open PR (not inactive) fall back to the plain reason."""
    sgo = _load_script("commit_flow")
    from domain.context import gate
    from domain.forge import PullRequest

    def gv(pr):
        return gate.GateView(git_root="/x", branch="feat/a", head_sha="h", target="main",
                             provider="gitlab", active_pr=pr)

    merged = PullRequest(number=129, state="merged", source_branch="feat/a",
                         sha="541268f2481b", web_url="https://code.byted.org/x/merge_requests/129",
                         updated_at="2026-06-18T10:05:05+08:00")
    detail = sgo.refusal_detail(gv(merged), "current branch's MR is merged/closed")
    assert "MR !129 merged" in detail and "541268f24" in detail
    assert "merge_requests/129" in detail
    # no PR (protected) and open PR (not inactive) → plain fallback, no fabricated evidence
    assert sgo.refusal_detail(gv(None), "protected branch") == "protected branch"
    open_pr = PullRequest(number=7, state="open", source_branch="feat/a")
    assert sgo.refusal_detail(gv(open_pr), "fallback") == "fallback"

def test_cut_new_branch_carries_dirty_tree():
    """cut_new_branch stashes a dirty tree before `checkout -b` and pops after, so uncommitted
    work done before the branch was decided (e.g. a version bump) follows you onto the fresh
    branch instead of `checkout -b` refusing with 'would be overwritten'."""
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_cut"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q", "-b", "main"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    v = Path(f"{R}/v")
    # Mirror the session: feat lags main on the file the dirty edit touches. main advances v
    # "83"→"84"; feat (cut earlier) still has "83"; the uncommitted bump on feat also reaches "84".
    v.write_text("83"); _git(R, "add", "v"); _git(R, "commit", "-qm", "v83")
    _git(R, "checkout", "-q", "-b", "feat"); Path(f"{R}/x").write_text("1"); _git(R, "add", "x"); _git(R, "commit", "-qm", "feat")
    _git(R, "checkout", "-q", "main"); v.write_text("84"); _git(R, "add", "v"); _git(R, "commit", "-qm", "v84")
    _git(R, "checkout", "-q", "feat"); v.write_text("84")   # uncommitted bump; main's committed v also "84"
    plan = []
    sgo.cut_new_branch(R, "newb", "main", plan)   # would fail without stash (v overwrite on checkout)
    assert _git_out(R, "rev-parse", "--abbrev-ref", "HEAD") == "newb"
    assert v.read_text() == "84"   # the dirty bump was carried over, not lost
    assert any("carried over" in line for line in plan)

def test_prepare_branch_reads_gate_pr_state():
    """prepare_branch decides on gate truth (GateView), not the cached ctx. decide_branch's
    unit test bypasses this call, so an end-to-end run guards the wiring (and that gcampr reads
    the LIVE-branch / SHA-validated PR state, not ctx.branch_pr_inactive)."""
    sgo = _load_script("commit_flow")
    from domain.context import RepoContext, gate, prstate
    R = "/tmp/dlut_prep"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    intent = sgo.GitIntent(mode="mr", message="m", title="m", requested_branch=None,
                           target="main", base="origin/main", explicit_base=False,
                           files=[], repo=R, source="test", invoke_cwd=R)
    # healthy branch, no PR segment → gate finds no PR → continue on the current branch
    res = sgo.prepare_branch(intent, gate.evaluate(R), [])
    assert res.branch == "feat/a" and res.cut is False

    # in-flight: an OPEN PR for feat/a in the monitor-owned pr segment → gate picks it (open
    # wins, no SHA check) → continue + a self-narrating line built with the repo-level provider
    prstate.persist_pr(R, {"branch": "feat/a", "provider": "github", "pr_number": 7,
                           "prs": [{"number": 7, "state": "open", "source_branch": "feat/a"}]})
    plan = []
    res = sgo.prepare_branch(intent, gate.evaluate(R), plan)
    assert res.branch == "feat/a" and res.cut is False
    assert any("continuing in-flight PR #7" in line for line in plan)

def test_normalize_files_rebase():
    """--files 自动 rebase 到 repo-root 相对路径——调用方从 workspace 根 / server 子目录
    传来的路径不再死于裸 `git add` 报错;删除文件等不存在路径保持原样。"""
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_nf"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/repo/server", exist_ok=True)
    Path(f"{R}/repo/server/a.py").write_text("x")
    plan: list[str] = []
    assert sgo.normalize_files(f"{R}/repo", [f"{R}/repo/server/a.py"], "/", plan) == ["server/a.py"]
    assert any("rebased" in line for line in plan)
    assert sgo.normalize_files(f"{R}/repo", ["a.py"], f"{R}/repo/server", []) == ["server/a.py"]
    assert sgo.normalize_files(f"{R}/repo", ["server/a.py"], "/", []) == ["server/a.py"]   # 已正确 → 不动
    assert sgo.normalize_files(f"{R}/repo", ["gone.py"], "/", []) == ["gone.py"]           # 不存在 → 不动

def test_version_bump_mix_hint():
    """版本 bump 与功能文件混在同一 commit → PLAN 软提示(不拦);单独 bump 不提示。"""
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_vb"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/pyproject.toml").write_text('version = "0.0.1"\n')
    Path(f"{R}/a.py").write_text("x = 1\n")
    _git(R, "add", "."); _git(R, "commit", "-qm", "i")
    Path(f"{R}/pyproject.toml").write_text('version = "0.0.2"\n')
    Path(f"{R}/a.py").write_text("x = 2\n")
    _git(R, "add", ".")
    plan: list[str] = []
    sgo.warn_mixed_version_bump(R, plan)
    assert any("version bump" in line for line in plan)
    _git(R, "reset", "-q"); _git(R, "add", "pyproject.toml")
    plan = []
    sgo.warn_mixed_version_bump(R, plan)
    assert plan == []

def test_component_lint_target():
    """lint 戳记对齐 CI 入口:有 lint-ci(通常 uv sync 锁定工具链)优先于 lint,
    消灭'本地 lint 绿、CI lint-ci 红'的版本漂移。目标选择是 component 自己的事实（Component.lint_target）。"""
    from domain.repo_layout import Component
    D = "/tmp/dlut_lint"
    shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
    Path(f"{D}/Makefile").write_text("lint:\n\ttrue\n")
    assert Component.at(D, D).lint_target() == "lint"
    Path(f"{D}/Makefile").write_text("lint:\n\ttrue\nlint-ci:\n\ttrue\n")
    assert Component.at(D, D).lint_target() == "lint-ci"
    Path(f"{D}/Makefile").write_text("test:\n\ttrue\n")
    assert Component.at(D, D).lint_target() is None

def test_message_file_and_stdin_input():
    """Commit message via --message-file (path) or -F - (stdin) — the shell-escaping-free path
    for multi-line / quote-heavy messages (mirrors git -F / gh --body-file). Content round-trips
    exactly, including the chars that break inline shell quoting."""
    import io
    sgo = _load_script("commit_flow")
    ap = sgo._build_parser()
    msg = 'feat(x): subj\n\nbody "dq" (paren) $VAR `bt` \'apos\'.'
    p = "/tmp/dlut_msgfile.txt"
    Path(p).write_text(msg, encoding="utf-8")
    assert sgo._resolve_message(ap.parse_args(["mr", "--message-file", p]), ap) == msg
    # -F - reads stdin
    old = sys.stdin
    sys.stdin = io.StringIO("from stdin\nbody")
    try:
        assert sgo._resolve_message(ap.parse_args(["mr", "-F", "-"]), ap) == "from stdin\nbody"
    finally:
        sys.stdin = old

def test_inline_message_still_supported():
    """Back-compat: inline --message / -m is unchanged (just no longer the only option)."""
    sgo = _load_script("commit_flow")
    ap = sgo._build_parser()
    assert sgo._resolve_message(ap.parse_args(["mr", "--message", "fix: x"]), ap) == "fix: x"
    assert sgo._resolve_message(ap.parse_args(["mr", "-m", "fix: y"]), ap) == "fix: y"

def test_message_required_with_hint():
    """Neither --message nor --message-file → exits with an actionable hint, not a bare usage dump."""
    import contextlib
    import io
    sgo = _load_script("commit_flow")
    ap = sgo._build_parser()
    err = io.StringIO()
    raised = False
    try:
        with contextlib.redirect_stderr(err):
            sgo._resolve_message(ap.parse_args(["mr"]), ap)
    except SystemExit:
        raised = True
    assert raised and "--message-file" in err.getvalue()

def test_cli_repo_arg_flag_and_positional_equivalent():
    """The shared repo-target arg (lib.cli): --repo and the bare positional are equivalent
    spellings, the flag wins when both appear, and --repo is no longer swallowed as a
    positional — the original bug that made `run_fixlint.py --repo /x` die with
    "no subproject matches '--repo'"."""
    from lib import cli
    ap = cli.ArgParser(prog="t")
    cli.add_repo_arg(ap)
    assert cli.repo_target(ap.parse_args([])) is None
    assert cli.repo_target(ap.parse_args(["/some/path"])) == "/some/path"            # positional
    assert cli.repo_target(ap.parse_args(["--repo", "/some/path"])) == "/some/path"  # flag, not swallowed
    assert cli.repo_target(ap.parse_args(["-r", "nb"])) == "nb"
    assert cli.repo_target(ap.parse_args(["pos", "--repo", "flag"])) == "flag"       # flag wins
    # positional=False (gcampr shape): only the flag, no bare positional repo
    ap2 = cli.ArgParser(prog="t2")
    cli.add_repo_arg(ap2, positional=False)
    assert cli.repo_target(ap2.parse_args(["--repo", "x"])) == "x"

def test_cli_argparser_hint_only_on_unrecognized():
    """cli.ArgParser appends extra_hints on 'unrecognized arguments' (the silent-misparse
    failure), but not on other errors (which already name the offending argument)."""
    import contextlib
    import io
    from lib import cli
    ap = cli.ArgParser(prog="t", extra_hints=["USE --message-file"])
    ap.add_argument("mode", choices=["a", "b"])
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.suppress(SystemExit):
        ap.parse_args(["a", "--nope"])                 # unrecognized → hint shown
    assert "USE --message-file" in err.getvalue()
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2), contextlib.suppress(SystemExit):
        ap.parse_args(["zzz"])                         # bad choice → no hint
    assert "USE --message-file" not in err2.getvalue()

def test_title_defaults_to_message_first_line():
    """--title omitted → PR title is the message's FIRST line, so a multi-line body can't yield a
    multi-line (invalid) PR title — the gcampr 422 that bit us."""
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_title"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    ap = sgo._build_parser()
    ns = ap.parse_args(["mr", "--message", "feat: subject\n\nlong body line", "--repo", R])
    ns.message = sgo._resolve_message(ns, ap)
    intent = sgo.resolve_intent(ns, R)
    assert intent.title == "feat: subject" and intent.message.endswith("long body line")
    # the body becomes the PR/MR description — the outlet that keeps titles one short line
    assert intent.description == "long body line"
    # single-line message → no description (forge gets body="", not a phantom paragraph)
    ns1 = ap.parse_args(["mr", "--message", "feat: subject only", "--repo", R])
    ns1.message = sgo._resolve_message(ns1, ap)
    assert sgo.resolve_intent(ns1, R).description == ""

def test_sync_pr_description_append_only():
    """sync_pr_description: sets an empty body, appends to a non-empty one (human edits
    survive), no-ops when the paragraph is already present (retry-safe), and a forge
    failure degrades to a PLAN note — never an exception (commit/push already landed)."""
    sgo = _load_script("commit_flow")
    pr = PullRequest(number=7, state="open", source_branch="feat/x")

    f = _FakeForge([pr])
    plan = []
    sgo.sync_pr_description(f, pr, "para one", plan)
    assert f.description(7) == "para one" and any("description" in line for line in plan)
    sgo.sync_pr_description(f, pr, "para two", [])           # append, not overwrite
    assert f.description(7) == "para one\n\npara two"
    sgo.sync_pr_description(f, pr, "para one", [])           # already present → no dup
    assert f.description(7) == "para one\n\npara two"
    sgo.sync_pr_description(f, pr, "", [])                   # nothing to sync → no-op
    assert f.description(7) == "para one\n\npara two"

    class _Broken(_FakeForge):
        def description(self, number):
            raise ForgeError("boom")
    plan = []
    sgo.sync_pr_description(_Broken([pr]), pr, "para", plan)  # non-fatal
    assert any("non-fatal" in line for line in plan)

    # mr-mode reuse path appends through the same helper
    orig = sgo.forge_for_repo
    try:
        f2 = _FakeForge([PullRequest(number=3, state="open", source_branch="feat/x", web_url="u/3")],
                        bodies={3: "original"})
        sgo.forge_for_repo = lambda repo: f2
        sgo.reuse_or_create_pr("/repo", "feat/x", "main", "t", "follow-up body", [])
        assert f2.description(3) == "original\n\nfollow-up body"
    finally:
        sgo.forge_for_repo = orig


def test_ensure_requirement_wiring():
    """gcampr 侧接线（loop-state slice3 + #62 F7）：ensure_requirement 在 cut 与 continue 两路都生效——
    cut 无参 → 新开（id=该分支）；--requirement → 续接，**手工切的分支（continue 路径）也不得静默丢弃**；
    continue 无参 → 不动；重复调用幂等。"""
    from domain.context.loopstate import requirement
    sgo = _load_script("commit_flow")
    R = "/tmp/dlut_req_wire"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q", "-b", "main"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")

    def intent(branch, req):
        return sgo.GitIntent(mode="mr", message="m", title="m", requested_branch=branch,
                             target="main", base="main", explicit_base=False, files=[],
                             repo=R, source="test", invoke_cwd=R, requirement=req)

    BR = sgo.BranchResult
    # cut + 无 --requirement → 新开需求（id = 该分支）
    plan = []
    sgo.ensure_requirement(intent("feat/a", None), BR(branch="feat/a", cut=True), plan)
    assert requirement.resolve(R, "feat/a") == "feat/a"
    assert any("opened 'feat/a'" in line for line in plan)

    # cut + --requirement → 续接
    plan = []
    sgo.ensure_requirement(intent("fix/a-2", "feat/a"), BR(branch="fix/a-2", cut=True), plan)
    assert requirement.resolve(R, "fix/a-2") == "feat/a"
    assert any("continues 'feat/a'" in line for line in plan)

    # F7（狗粮发现）：continue 路径（手工切的分支）+ --requirement → 仍要 attach
    plan = []
    sgo.ensure_requirement(intent(None, "feat/a"), BR(branch="fix/a-3", cut=False), plan)
    assert requirement.resolve(R, "fix/a-3") == "feat/a"
    assert any("continues 'feat/a'" in line for line in plan)
    # 幂等：已 attach 再跑不重复
    plan = []
    sgo.ensure_requirement(intent(None, "feat/a"), BR(branch="fix/a-3", cut=False), plan)
    assert plan == []

    # continue + 无 --requirement → 不动（不新开）
    sgo.ensure_requirement(intent(None, None), BR(branch="feat/other", cut=False), [])
    assert requirement.resolve(R, "feat/other") is None


if __name__ == "__main__":
    run_main(globals())
