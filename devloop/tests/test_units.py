#!/usr/bin/env python3
"""devloop unit tests — the pure logic worth pinning down.

Covers the bits most prone to silent breakage: guard command parsing (quoted-text
false positives + `git -C` false negatives), the PR window number-math, origin parsing
+ provider detection, GitHub/GitLab → PullRequest mapping, the staging sensitive-filter,
and branch-PR selection.

Run standalone: `python3 devloop/tests/test_units.py`  (also pytest-collectable).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HOOKS))

from lib.cmdtree import cmdparse  # noqa: E402
from lib.context import Cadence, PullRequest  # noqa: E402
from lib.forge import detect_provider, parse_origin  # noqa: E402
from lib.forge.base import (  # noqa: E402
    Forge,
    Comment,
    ForgeNotFound,
    build_window,
    parse_pr_number,
    pr_label,
)
from lib.forge.github import GitHubForge  # noqa: E402
from lib.forge.gitlab import GitLabForge  # noqa: E402


class _FakeForge(Forge):
    """In-memory Forge for testing the domain composition (build_window) + orchestration
    (reuse_or_create_pr) without HTTP — the port is small enough that this is trivial."""
    provider = "github"

    def __init__(self, prs):
        self._prs = {p.number: p for p in prs}
        self.created = None

    def create(self, *, source_branch, target_branch, title, body=""):
        n = max(self._prs, default=0) + 1
        pr = PullRequest(number=n, state="open", source_branch=source_branch,
                         target_branch=target_branch, title=title, web_url=f"u/{n}")
        self._prs[n] = pr
        self.created = pr
        return pr

    def get(self, number):
        if number not in self._prs:
            raise ForgeNotFound(str(number))
        return self._prs[number]

    def update(self, number, **fields):
        return self._prs[number]

    def prs_for_branch(self, branch):
        return sorted((p for p in self._prs.values() if p.source_branch == branch),
                      key=lambda p: p.number, reverse=True)

    def recent(self, limit):
        return sorted(self._prs.values(), key=lambda p: p.number, reverse=True)[:limit]

    def comments(self, number):
        return [Comment(author="x", body="y")]


def _load_from(base, name):
    spec = importlib.util.spec_from_file_location(name, str(base / f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules 再 exec:dataclass(及其它按 __module__ 反查注解的机制)
    # 需要 sys.modules[m.__name__] 存在,否则被测模块里定义 @dataclass 直接炸
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _load_script(name):
    return _load_from(SCRIPTS, name)


def _load_hook(name):
    return _load_from(HOOKS, name)


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)


def test_cmdparse_git_invocations():
    gi = cmdparse.git_invocations
    assert [i.subcommand for i in gi("git commit -m x")] == ["commit"]
    # false negative fixed: global options before the subcommand
    assert gi("git -C /repo commit")[0].subcommand == "commit"
    assert gi("git -c user.name=x push")[0].subcommand == "push"
    assert gi("GIT_DIR=.git git commit")[0].subcommand == "commit"
    assert gi("/usr/bin/git push")[0].subcommand == "push"
    # false positive fixed: pattern inside quoted text is NOT a git invocation
    assert gi('echo "git add -A"') == []
    assert gi("grep -r 'git commit' .") == []
    # operator inside quotes must not split the command
    assert [i.subcommand for i in gi('git commit -m "a && b"')] == ["commit"]
    # chained commands
    assert [i.subcommand for i in gi("cd r && git push")] == ["push"]
    # add -A detection (incl. -C form)
    assert gi("git add -A")[0].args == ["-A"]
    assert gi("git -C r add -A")[0].subcommand == "add"
    # -C target captured so guards can judge the right repo
    assert gi("git -C /repo commit")[0].cwd == "/repo"
    assert gi("git commit")[0].cwd is None


def test_protect_branch_checks_dash_c_target():
    """Codex #4: protect guard must judge the `-C` target repo, not the caller's cwd."""
    pb = _load_hook("pretool_protect_branch")
    from lib import hook_io
    from lib.context import RepoContext
    R = "/tmp/dlut_prot"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "master")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)

    def hi(cmd, cwd):
        return hook_io.HookInput(event="PreToolUse", tool_name="Bash",
                                 tool_input={"command": cmd}, cwd=cwd, raw={})

    # the hole: `git -C <master repo> commit` from a NON-repo cwd (e.g. workspace root)
    assert pb.decide(hi(f"git -C {R} commit -m x", "/tmp"))
    assert pb.decide(hi("git commit -m x", R))                      # plain, on master
    _git(R, "checkout", "-q", "-b", "feat/x"); RepoContext.refresh_all(R)
    assert pb.decide(hi("git commit -m x", R)) is None              # feature branch → allow
    assert pb.decide(hi(f"git -C {R} commit -m x", "/tmp")) is None  # -C feature repo → allow


def test_cmdparse_commands():
    assert cmdparse.commands("PYTHONPATH=. pytest x")[0][0] == "pytest"   # env stripped
    assert cmdparse.first_token_is("make test", "make") is True
    assert cmdparse.first_token_is('echo "make test"', "make") is False


def test_build_window():
    """Provider-agnostic window policy over the port's recent+get: newest `cap`, with the
    anchor PR always present (fetched via get if it fell off the recent list)."""
    prs = [PullRequest(number=n, state="open", source_branch=f"b{n}") for n in range(1, 21)]
    f = _FakeForge(prs)
    # anchor near latest → just the newest cap
    nums = [p.number for p in build_window(f, 20, cap=5)]
    assert nums == [20, 19, 18, 17, 16]
    # anchor older than the newest cap → newest cap-1 + the anchor (anchor always present)
    nums = [p.number for p in build_window(f, 3, cap=5)]
    assert 3 in nums and nums[:4] == [20, 19, 18, 17] and len(nums) == 5
    # no anchor → newest cap
    assert [p.number for p in build_window(f, None, cap=5)] == [20, 19, 18, 17, 16]
    # anchor that doesn't exist (404 on get) → silently dropped, still returns newest cap
    nums = [p.number for p in build_window(f, 999, cap=5)]
    assert nums == [20, 19, 18, 17, 16]


def test_parse_pr_number():
    assert parse_pr_number("https://github.com/o/r/pull/12") == 12
    assert parse_pr_number("https://gitlab.com/g/p/-/merge_requests/7") == 7
    assert parse_pr_number("#5") == 5 and parse_pr_number("!9") == 9 and parse_pr_number("42") == 42
    assert parse_pr_number("nope") is None


def test_pr_label():
    assert pr_label("github", 3) == "PR #3"
    assert pr_label("gitlab", 3) == "MR !3"
    assert pr_label("", 3) == "PR #3"   # unknown → PR/#


def test_parse_origin_and_detect_provider():
    R = "/tmp/dlut_repo"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    subprocess.run(["git", "init", "-q"], cwd=R, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:owner/repo.git"],
                   cwd=R, check=True)
    host, path = parse_origin(R)
    assert host == "github.com" and path == "owner/repo"
    subprocess.run(["git", "remote", "set-url", "origin", "https://gitlab.com/g/s/proj.git"], cwd=R, check=True)
    host, path = parse_origin(R)
    assert host == "gitlab.com" and path == "g/s/proj"
    # provider inference from host, with explicit config override winning
    assert detect_provider("github.com", None) == "github"
    assert detect_provider("gitlab.example.com", None) == "gitlab"
    assert detect_provider("git.acme.com", None) == "gitlab"            # default
    assert detect_provider("git.acme.com", "github") == "github"        # GHE on custom host


def test_forge_pr_mapping():
    """Each adapter normalizes its native JSON → the neutral PullRequest: iid/number,
    GitHub's open/closed + merged flag → open|merged|closed, head/base → source/target.
    No provider on the PR — that's repo-level."""
    gl = GitLabForge.__new__(GitLabForge)        # bypass __init__ (no HTTP needed for mapping)
    pr = gl._to_pr({"iid": 7, "state": "opened", "source_branch": "f", "target_branch": "m",
                    "web_url": "u", "sha": "abc"})
    assert (pr.number, pr.state, pr.source_branch, pr.sha) == (7, "open", "f", "abc")
    assert not hasattr(pr, "provider")
    assert gl._to_pr({"iid": 8, "state": "merged"}).state == "merged"
    assert gl._to_pr({"iid": 9, "state": "locked"}).state == "closed"

    gh = GitHubForge.__new__(GitHubForge)
    gh.owner, gh.name = "o", "r"
    pr = gh._to_pr({"number": 12, "state": "open", "head": {"ref": "f", "sha": "abc"},
                    "base": {"ref": "main"}, "html_url": "u"})
    assert (pr.number, pr.state, pr.source_branch, pr.target_branch) == (12, "open", "f", "main")
    # closed + merged_at → merged; closed without merge → closed
    assert gh._to_pr({"number": 13, "state": "closed", "merged_at": "2026-01-01"}).state == "merged"
    assert gh._to_pr({"number": 14, "state": "closed"}).state == "closed"
    assert gh._to_pr({"number": 15, "state": "closed", "merged": True}).state == "merged"


def test_sensitive_filter():
    is_sensitive = _load_script("smart_git_ops")._is_sensitive
    assert is_sensitive(".env") and is_sensitive("sub/.env.local")
    assert is_sensitive("a/.idea/x") and is_sensitive("pkg/__pycache__/m.pyc")
    assert not is_sensitive("src/main.py") and not is_sensitive("README.md")


def test_decide_branch_is_intent_driven():
    """--branch 一律基于 base(默认 origin/<target>),与当前停在哪条分支无关——避免新 MR
    夹带上一条未合 feature 分支的提交。"""
    decide = _load_script("smart_git_ops").decide_branch
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


def test_prepare_branch_reads_ctx_pr_state():
    """prepare_branch 用真实 ctx 调 branch_pr_inactive()——decide_branch 的单测绕过了这条
    调用,所以脚本里把 ctx 方法名写错(如沿用旧的 branch_mr_inactive)会从单测漏过。这条端到端
    跑一遍 prepare_branch 兜住该回归。"""
    sgo = _load_script("smart_git_ops")
    from lib.context import RepoContext
    R = "/tmp/dlut_prep"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    ctx = RepoContext.load(R)
    intent = sgo.GitIntent(mode="mr", message="m", title="m", requested_branch=None,
                           target="main", base="origin/main", explicit_base=False,
                           files=[], repo=R, source="test", invoke_cwd=R)
    # healthy branch, no PR → continue on the current branch (and crucially: no AttributeError)
    res = sgo.prepare_branch(intent, ctx, "feat/a", [])
    assert res.branch == "feat/a" and res.cut is False

    # in-flight branch → continue + a self-narrating line built with the repo-level provider
    ctx.prs = [PullRequest(number=7, state="open", source_branch="feat/a")]
    ctx.branch.pr_number = 7; ctx.provider = "github"
    plan = []
    res = sgo.prepare_branch(intent, ctx, "feat/a", plan)
    assert res.branch == "feat/a" and res.cut is False
    assert any("continuing in-flight PR #7" in line for line in plan)


def test_reuse_or_create_pr_over_narrowed_port():
    """reuse_or_create_pr: reuse the branch's OPEN pr if present (via prs_for_branch),
    else create. Over the narrowed port + a fake forge — no HTTP; label is repo-level."""
    sgo = _load_script("smart_git_ops")
    orig = sgo.forge_for_repo
    try:
        # reuse: an open PR exists for the branch
        f = _FakeForge([PullRequest(number=3, state="open", source_branch="feat/x", web_url="u/3")])
        sgo.forge_for_repo = lambda repo: f
        plan = []
        pr = sgo.reuse_or_create_pr("/repo", "feat/x", "main", "t", plan)
        assert pr.number == 3 and f.created is None
        assert any("reused open PR #3" in line for line in plan)
        # create: only a finished PR for the branch → open a new one
        f2 = _FakeForge([PullRequest(number=3, state="merged", source_branch="feat/x")])
        sgo.forge_for_repo = lambda repo: f2
        plan = []
        pr = sgo.reuse_or_create_pr("/repo", "feat/x", "main", "t", plan)
        assert f2.created is not None and pr.number == 4
        assert any("created PR #4" in line for line in plan)
    finally:
        sgo.forge_for_repo = orig


def test_refresh_pr_state_failopen():
    """refresh_pr_state (the live-state preflight for push/mr) is best-effort: a repo with no
    forge remote/token just no-ops rather than raising — the gate falls back to cache."""
    sgo = _load_script("smart_git_ops")
    R = "/tmp/dlut_refresh"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    sgo.refresh_pr_state(R)   # no origin/token → no-op, no exception


def test_pick_branch_pr():
    poll = _load_script("poll_pr_status")
    P = lambda **kw: PullRequest(**kw)  # noqa: E731
    poll._is_ancestor = lambda repo, sha, head: True
    assert poll.pick_branch_pr([P(number=5, state="open", sha="a"),
                                P(number=4, state="merged", sha="b")], "r", "h").number == 5
    poll._is_ancestor = lambda repo, sha, head: sha == "b"
    assert poll.pick_branch_pr([P(number=4, state="merged", sha="b"),
                                P(number=3, state="closed", sha="c")], "r", "h").number == 4
    poll._is_ancestor = lambda repo, sha, head: False
    assert poll.pick_branch_pr([P(number=4, state="merged", sha="dead")], "r", "h") is None


def test_events_dispatch_isolates_failures():
    """dispatch fans out in order and isolates failures — one handler raising never
    blocks the rest (fail-safe, like hook_io on the hook side)."""
    from lib import events
    seen: list = []
    ev = events.Event(source="forge", type="pr.update", subject="r")
    events.dispatch(ev, [
        lambda e: seen.append(("a", e.type)),
        lambda e: (_ for _ in ()).throw(RuntimeError("boom")),   # raises
        lambda e: seen.append(("b", e.subject)),
    ])
    assert seen == [("a", "pr.update"), ("b", "r")]              # b ran despite boom


def test_poll_handlers_persist_and_notify(capsys):
    """forge handlers: persist always writes pr.json; notify (the wake) emits only on a
    real change."""
    from lib import events
    from lib.context import base
    poll = _load_script("poll_pr_status")
    R = "/tmp/dlut_pollh"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    payload = {"branch": "f", "provider": "github", "pr_number": 7, "prs": []}

    changed = events.Event(source="forge", type="pr.update", subject=R, payload=payload,
                           summary="PR #7 merged (f) · 1 recent PR(s) tracked", changed=True)
    poll.persist(changed)
    assert base.load_segment(R, "pr")["pr_number"] == 7          # persisted
    poll.notify(changed)
    assert "devloop: PR #7 merged" in capsys.readouterr().out    # changed → wake

    noop = events.Event(source="forge", type="pr.update", subject=R, payload=payload, changed=False)
    poll.persist(noop)                                           # still writes
    poll.notify(noop)
    assert capsys.readouterr().out == ""                         # no change → silent


def test_pullrequest_and_cadence():
    pr = PullRequest.from_dict({"number": 7, "state": "merged", "source_branch": "f", "target_branch": "m"})
    assert pr.inactive and PullRequest.from_dict({"number": 8, "state": "open"}).inactive is False
    # in-flight(open)与 inactive(merged/closed)互斥——循环"轮次之间"的第四态
    assert PullRequest.from_dict({"number": 9, "state": "open"}).is_open
    assert not PullRequest.from_dict({"number": 10, "state": "merged"}).is_open and not pr.is_open
    c = Cadence()
    assert c.should_emit("x", now=100, ttl=1800)
    c.mark("x", now=100)
    assert not c.should_emit("x", now=200, ttl=1800)        # same → skip
    assert c.should_emit("y", now=200, ttl=1800)            # changed → emit
    assert c.should_emit("x", now=100 + 1800, ttl=1800)     # TTL → emit
    c.clear()
    assert c.should_emit("x", now=200, ttl=1800)            # PostCompact clear → emit


def test_context_segments():
    """Per-owner segment files: each writer touches a disjoint file (no lost update),
    and pr.json is branch-keyed so a branch switch self-invalidates pr_number with no writer."""
    from lib.context import PullRequest, RepoContext, base
    R = "/tmp/dlut_seg"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")

    # refresh_all writes ONLY the refresher-owned segments
    RepoContext.refresh_all(R)
    seg = set(os.listdir(f"{R}/.devloop"))
    assert {"meta.json", "branch.json"} <= seg and "validation.json" not in seg and "pr.json" not in seg

    ctx = RepoContext.load(R)
    assert ctx.branch.current == "feat/a" and ctx.branch.pr_number is None and ctx.prs == []

    # a validation mark writes only validation.json
    ctx.mark_lint_passed()
    assert (Path(R) / ".devloop" / "validation.json").exists()
    assert RepoContext.load(R).validation.edits_since_lint == 0

    # monitor-owned pr write, branch-keyed; provider is repo-level (header, not per-PR)
    ctx = RepoContext.load(R)
    ctx.prs = [PullRequest(number=51, state="open", source_branch="feat/a")]
    ctx.branch.pr_number = 51
    ctx.provider = "github"
    ctx._save_pr()
    assert base.load_segment(R, "pr")["branch"] == "feat/a"
    assert base.load_segment(R, "pr")["provider"] == "github"
    loaded = RepoContext.load(R)
    assert loaded.branch.pr_number == 51 and loaded.provider == "github"

    # branch switch → stale number drops at load with nobody clearing pr.json
    _git(R, "checkout", "-q", "-b", "feat/b")
    RepoContext.refresh_branch(R)
    assert RepoContext.load(R).branch.pr_number is None
    assert base.load_segment(R, "pr")["branch"] == "feat/a"   # monitor file untouched by refresh

    # disjoint writers (refresh ↔ monitor) don't clobber each other
    RepoContext.refresh_branch(R)
    c = RepoContext.load(R); c.prs = [PullRequest(number=60, state="open", source_branch="feat/b")]
    c.branch.pr_number = 60; c.provider = "github"; c._save_pr()
    merged = RepoContext.load(R)
    assert merged.branch.current == "feat/b" and merged.branch.pr_number == 60


def test_branch_pr_in_flight():
    """in-flight = 当前分支有 open PR(循环的"人工 merge 前 / 轮次之间"态);
    与 inactive(merged/closed)互斥。orchestrator 据此提示"在续写在途 PR"。"""
    from lib.context import PullRequest, RepoContext
    R = "/tmp/dlut_inflight"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)

    ctx = RepoContext.load(R)
    ctx.prs = [PullRequest(number=51, state="open", source_branch="feat/a")]; ctx.branch.pr_number = 51
    assert ctx.branch_pr_in_flight() and not ctx.branch_pr_inactive()
    # 合入后转 inactive、不再 in-flight
    ctx.prs = [PullRequest(number=51, state="merged", source_branch="feat/a")]
    assert ctx.branch_pr_inactive() and not ctx.branch_pr_in_flight()
    # 无 PR(pr_number=None)两者皆 False
    ctx.branch.pr_number = None
    assert not ctx.branch_pr_in_flight() and not ctx.branch_pr_inactive()


def test_in_flight_turn_hint():
    """in-flight 是软提示(不硬拦):turn 注入出现 IN-FLIGHT + 引导新工作切新分支;
    inactive 仍是 INACTIVE;healthy 两者都不出现。vocab 按 provider 贴词(GitHub→PR)。"""
    from lib.context import PullRequest, RepoContext
    R = "/tmp/dlut_if_hint"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    ctx = RepoContext.load(R)

    # in-flight(open)→ 软提示出现 + actionable + 按 repo-level provider 出 PR # 词汇
    ctx.prs = [PullRequest(number=51, state="open", source_branch="feat/a")]; ctx.branch.pr_number = 51
    ctx.provider = "github"
    txt = ctx.turn_text()
    assert "IN-FLIGHT" in txt and "fresh branch" in txt and "INACTIVE" not in txt
    assert "PR #51" in txt and "Recent PRs" in txt

    # GitLab provider → MR ! 词汇(同一组 PR,只换 repo-level provider)
    ctx.provider = "gitlab"
    assert "MR !51" in ctx.turn_text() and "Recent MRs" in ctx.turn_text()

    # inactive(merged)→ 仍是 INACTIVE,不误报 IN-FLIGHT
    ctx.provider = "github"
    ctx.prs = [PullRequest(number=51, state="merged", source_branch="feat/a")]
    txt = ctx.turn_text()
    assert "INACTIVE" in txt and "IN-FLIGHT" not in txt

    # healthy(无 PR)→ 两者都没有
    ctx.branch.pr_number = None; ctx.prs = []
    txt = ctx.turn_text()
    assert "IN-FLIGHT" not in txt and "INACTIVE" not in txt


def test_atomic_segment_write():
    """save_segment is atomic: a reader never sees a torn write, and a corrupt segment
    degrades to its default rather than nuking the whole context."""
    from lib.context import base
    R = "/tmp/dlut_atomic"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    base.save_segment(R, "meta", {"repo": {"repo_dir": R}, "updated_at": 1.0})
    base.save_segment(R, "branch", {"current": "x"})
    # no .tmp residue left behind after an atomic replace
    assert not any(n.endswith(".tmp") for n in os.listdir(f"{R}/.devloop"))
    # a corrupt segment reads as None (caller falls back to default), siblings still load
    (Path(R) / ".devloop" / "branch.json").write_text("{ not json")
    assert base.load_segment(R, "branch") is None
    assert base.load_segment(R, "meta")["updated_at"] == 1.0


def test_git_invocation_cd_prefix():
    """git_invocations 按位置跟踪 cd 前缀(取代 last_cd_target)——聚合工作区里 session
    cwd 停在 workspace 根,inp.cwd 不是命令真正触达的仓库;相对 cd 链按 shell 语义组合
    (`cd a && cd b` → a/b,旧 last-cd-wins 会错算成 b)。"""
    def cds(cmd):
        return [inv.cd for inv in cmdparse.git_invocations(cmd)]
    assert cds("cd /a/b && git commit -m 'x'") == ["/a/b"]
    assert cds("cd a && make && cd b && git push") == [os.path.join("a", "b")]
    assert cds("git commit -m 'cd /tmp'") == [None]    # 引号内不算
    assert cds("echo cd /x; git fetch") == [None]      # cd 不是命令词
    assert cds("git fetch && cd /x && git push") == [None, "/x"]  # 位置感知


def test_normalize_files_rebase():
    """--files 自动 rebase 到 repo-root 相对路径——调用方从 workspace 根 / server 子目录
    传来的路径不再死于裸 `git add` 报错;删除文件等不存在路径保持原样。"""
    sgo = _load_script("smart_git_ops")
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
    sgo = _load_script("smart_git_ops")
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


def test_pick_lint_target():
    """lint 戳记对齐 CI 入口:有 lint-ci(通常 uv sync 锁定工具链)优先于 lint,
    消灭'本地 lint 绿、CI lint-ci 红'的版本漂移。"""
    rf = _load_script("run_fixlint")
    D = "/tmp/dlut_lint"
    shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
    Path(f"{D}/Makefile").write_text("lint:\n\ttrue\n")
    assert rf.pick_lint_target(D) == "lint"
    Path(f"{D}/Makefile").write_text("lint:\n\ttrue\nlint-ci:\n\ttrue\n")
    assert rf.pick_lint_target(D) == "lint-ci"
    Path(f"{D}/Makefile").write_text("test:\n\ttrue\n")
    assert rf.pick_lint_target(D) is None


def test_subproject_canonical():
    """symlink 农场:子项目条目本身是 symlink → 注入文本携带 canonical 映射,
    git 输出的真实路径不再被当成另一个仓库;普通子目录不带箭头。"""
    from lib.context import Subproject, WorkspaceContext
    from lib.context import workspace as wsctx
    W = "/tmp/dlut_canon"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/ws/plain"); os.makedirs(f"{W}/real/nb")
    os.symlink(f"{W}/real/nb", f"{W}/ws/nb")
    sub = wsctx._build_subproject(Path(f"{W}/ws"), {"name": "nb", "path": "nb"})
    assert sub.canonical and sub.canonical.endswith("real/nb")
    assert wsctx._build_subproject(Path(f"{W}/ws"), {"name": "plain", "path": "plain"}).canonical is None
    txt = WorkspaceContext(workspace_root="/ws", subprojects=[sub]).session_text()
    assert f"→ {sub.canonical}" in txt
    txt2 = WorkspaceContext(workspace_root="/ws",
                            subprojects=[Subproject(name="plain", path="plain")]).session_text()
    assert "→" not in txt2


def test_subproject_autodiscovery():
    """文件系统是 subproject 存在性的事实来源:workspace 直接子项里"是/指向 git 仓"的
    才算;docs/隐藏目录/非 git 子目录被排除(目录黑名单 + git 判据)。AGENTS.md 表格降级为
    可选润色——按 name 补 aliases/role,language 缺省自动探测、表格显式值可覆盖。"""
    from lib.context import workspace as wsctx
    W = "/tmp/dlut_autodisc"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/ws/docs")                         # 非仓子目录 → 黑名单排除
    os.makedirs(f"{W}/ws/plaindir")                     # 非 git 普通目录 → git 判据排除
    os.makedirs(f"{W}/ws/.hidden")                      # 隐藏目录 → 点前缀排除
    os.makedirs(f"{W}/ws/svc"); _git(f"{W}/ws/svc", "init", "-q")  # 直接 git 子目录 → 命中
    Path(f"{W}/ws/svc/go.mod").write_text("module x\n")     # svc language 自动探测 = go
    os.makedirs(f"{W}/real/nb"); _git(f"{W}/real/nb", "init", "-q")
    os.symlink(f"{W}/real/nb", f"{W}/ws/nb")            # 指向 git 仓的 symlink → 命中
    Path(f"{W}/real/nb/go.mod").write_text("module nb\n")   # nb 探测=go,表格写 python → 验证覆盖

    names = wsctx.discover_subproject_names(f"{W}/ws")
    assert names == ["nb", "svc"]                       # 排序;docs/plaindir/.hidden 不在内

    # 表格只给 nb 一行润色(别名 + role + 显式 language 覆盖);svc 表格里没有但文件系统有
    Path(f"{W}/ws/AGENTS.md").write_text(
        "# ws\n\n## 子项目清单\n\n| 目录 | 简称 | 语言 | 备注 |\n"
        "|------|------|------|------|\n| `nb` | notebook | python | 笔记服务 |\n",
        encoding="utf-8")
    ctx = wsctx.WorkspaceContext.refresh(f"{W}/ws")
    by = {s.name: s for s in ctx.subprojects}
    assert set(by) == {"nb", "svc"}
    assert "notebook" in by["nb"].aliases and by["nb"].role == "笔记服务"
    assert by["nb"].language == "python"               # 表格显式值覆盖自动探测(go)
    assert by["svc"].language == "go" and by["svc"].role is None  # 自发现 + 自动探测,无表格润色


def test_resolve_repo_dir():
    """脚本 repo 解析与 cwd 解耦:显式路径 / 子项目名(模糊)/ cwd 所在仓库 /
    workspace last-active 四级;workspace 根 + 无活动记录 → 明确报错而非瞎猜。"""
    from lib import repo_resolve, workspace as registry
    from lib.context import Subproject, WorkspaceContext, load_active_repo, record_active_repo
    W = "/tmp/dlut_rr"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/ws"); os.makedirs(f"{W}/real/nb")
    _git(f"{W}/real/nb", "init", "-q")
    os.symlink(f"{W}/real/nb", f"{W}/ws/nb")
    WorkspaceContext(workspace_root=f"{W}/ws",
                     subprojects=[Subproject(name="nb", path="nb")]).save()
    real_nb = Path(f"{W}/real/nb").resolve()
    orig = registry.load_workspaces
    registry.load_workspaces = lambda: [f"{W}/ws"]
    try:
        r, how = repo_resolve.resolve_repo_dir(f"{W}/real/nb", "/")           # 显式路径
        assert r and Path(r.git_root).resolve() == real_nb
        # 四个路径身份在解析边界一次算清(ResolvedRepo),消费方不再各自 re-derive
        assert Path(r.real_git_root) == real_nb and r.code_dir and r.source == how
        r, how = repo_resolve.resolve_repo_dir("nb", "/")                     # 子项目名 → canonical 仓库
        assert r and Path(r.git_root).resolve() == real_nb and "subproject" in how
        # symlink farm 下 canonical git_root 在 workspace 树外,containment-only 会得
        # None(Mode B 误判);必须经 subproject realpath 匹配归属到注册 workspace
        assert r.workspace_root and Path(r.workspace_root).resolve() == Path(f"{W}/ws").resolve()
        r, how = repo_resolve.resolve_repo_dir(None, f"{W}/real/nb")          # cwd 在仓库内
        assert r and how == "cwd"
        r, how = repo_resolve.resolve_repo_dir(None, f"{W}/ws")               # workspace 根、无活动 → 明确报错
        assert r is None and "--repo" in how
        record_active_repo(f"{W}/ws/nb")                                      # canonical 不在 ws 下也能归属
        active = load_active_repo(f"{W}/ws")
        assert active and Path(active).resolve() == real_nb
        r, how = repo_resolve.resolve_repo_dir(None, f"{W}/ws")               # last-active 兜底
        assert r and Path(r.git_root).resolve() == real_nb and "last-active" in how
        r, how = repo_resolve.resolve_repo_dir("zzz", "/")                    # 无匹配
        assert r is None
    finally:
        registry.load_workspaces = orig


def test_active_repo_first_entry_symlink_workspace():
    """P1 回归:首次进入(尚无 context.json)+ symlink 子仓,record_active_repo 也要落
    active.json——workspace_for_repo 缺 context 时自刷新(解析 AGENTS.md 子项目表)。"""
    from lib import workspace as registry
    from lib.context import load_active_repo, record_active_repo
    W = "/tmp/dlut_first"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/ws"); os.makedirs(f"{W}/real/nb")
    _git(f"{W}/real/nb", "init", "-q")
    os.symlink(f"{W}/real/nb", f"{W}/ws/nb")
    Path(f"{W}/ws/AGENTS.md").write_text(
        "# ws\n\n## Subprojects\n\n| 名称 | 说明 |\n|------|------|\n| `nb` | python 服务 |\n",
        encoding="utf-8")
    orig = registry.load_workspaces
    registry.load_workspaces = lambda: [f"{W}/ws"]
    try:
        record_active_repo(f"{W}/real/nb")   # canonical 路径,不在 ws 目录树内
        active = load_active_repo(f"{W}/ws")
        assert active and Path(active).resolve() == Path(f"{W}/real/nb").resolve()
    finally:
        registry.load_workspaces = orig


def test_affected_roots_parsed_not_regex():
    """PostToolUse 刷新改 parsed 判定:`git -C repo commit` / `cd repo && git push`
    都解析到正确的 effective repo;引号内文本与非状态子命令不触发。"""
    pgr = _load_hook("posttool_git_refresh")
    W = "/tmp/dlut_ar"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/repo")
    _git(f"{W}/repo", "init", "-q")
    expected = {str(Path(f"{W}/repo").resolve())}
    def roots(cmd, cwd=W):
        return {str(Path(r).resolve()) for r in pgr.affected_roots(cmd, cwd)}
    assert roots(f"git -C {W}/repo commit -m x") == expected          # -C,cwd 非仓库
    assert roots(f"cd {W}/repo && git push") == expected              # cd 前缀
    assert roots("git -C repo fetch", cwd=W) == expected              # -C 相对路径
    assert roots('echo "git commit"') == set()                        # 引号内不算
    assert roots(f"cd {W}/repo && git status") == set()               # 非状态子命令
    assert roots("git commit -m x") == set()                          # cwd 不是仓库


def test_cmdparse_contract_table():
    """guard 协议层契约表:cmdparse 是全部硬拦截的共同地基,把真实踩过的 shell 形态
    固化成表——语义回退会让 guard 集体误判(误拦 kubectl+uv)或漏判(cd 前缀绕过)。"""
    # (command, 期望的段头序列)
    HEADS = [
        ("git push && cd other", ["git", "cd"]),                                  # 后置 cd 独立成段
        ("cd a && git commit -m x && cd b", ["cd", "git", "cd"]),                 # cd 夹击
        ("kubectl -o jsonpath='{range .items[*]}{\"\\n\"}{end}'; git status", ["kubectl", "git"]),  # 引号紧贴 ;
        ('echo "git add -A"', ["echo"]),                                          # 引号内不是调用
        ("FOO=1 BAR=2 git -C /tmp/r fetch", ["git"]),                             # env 前缀剥离
        ("make&&go test", ["make", "go"]),                                        # 胶连运算符
    ]
    for cmd, heads in HEADS:
        got = [os.path.basename(s[0]) for s in cmdparse.commands(cmd)]
        assert got == heads, f"{cmd!r}: {got} != {heads}"
    # git 调用归属:-C 绝对优先 / -C 相对叠在 cd 前缀上 / 后置 cd 不偷归属
    inv = cmdparse.git_invocations("FOO=1 git -C /tmp/r fetch")[0]
    assert inv.subcommand == "fetch" and inv.run_dir("/base") == "/tmp/r"
    inv = cmdparse.git_invocations("cd sub && git -C nested commit -m x")[0]
    assert inv.run_dir("/base") == "/base/sub/nested"
    inv = cmdparse.git_invocations("git push && cd /elsewhere")[0]
    assert inv.run_dir("/base") == "/base"


def test_protocol_files_schema():
    """平台协议文件(plugin.json/hooks.json/monitors.json)由 CLI 直接解析,写错 key
    只能等到运行时才暴露(如 monitors 带非法 key 时整个 monitor 静默不跑)——发布前由本测试
    锁住:必填键、合法键集、脚本路径必须经 ${CLAUDE_PLUGIN_ROOT}(裸路径会随
    版本化 cache 目录失效)。新增合法键时有意识地更新这里,这正是协议变更的关卡。"""
    import json
    import re as _re
    P = Path(__file__).resolve().parent.parent  # devloop/

    plugin = json.loads((P / ".claude-plugin/plugin.json").read_text())
    assert {"name", "version", "description"} <= set(plugin)
    assert _re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])

    hooks = json.loads((P / "hooks/hooks.json").read_text())["hooks"]
    KNOWN_EVENTS = {"PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit",
                    "PostCompact", "PreCompact", "FileChanged", "CwdChanged", "Stop", "SubagentStop"}
    assert set(hooks) <= KNOWN_EVENTS, f"unknown hook event: {set(hooks) - KNOWN_EVENTS}"
    for groups in hooks.values():
        for g in groups:
            assert set(g) <= {"matcher", "hooks"}
            for h in g["hooks"]:
                assert {"type", "command"} <= set(h)
                assert set(h) <= {"type", "command", "timeout", "statusMessage"}, f"unknown hook key: {set(h)}"
                assert h["type"] == "command" and "${CLAUDE_PLUGIN_ROOT}" in h["command"]

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
    from lib import session_lock
    R = "/tmp/dlut_enter_noacq"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    inp = _hook_input("", {"session_id": "sess-reader", "cwd": R})
    ce.handle(inp)
    assert session_lock.read(R) is None


def test_owner_lock_acquire_atomic():
    """acquire 的 first-actor-wins 必须原子:check-then-replace 的 TOCTOU 窗口里两个
    session 同时首次 acquire 会都\"成功\"、后写覆盖先写。O_EXCL 化后:输掉 create race
    收敛到 deny;stale/corrupt 锁可被接管;锁文件 I/O 错误保持 fail-open。"""
    import time as _t
    from lib import session_lock
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


def test_cd_position_aware_attribution():
    """cd 前缀按位置生效,不是 last-cd-wins:`git checkout x && cd <非仓库>` 曾把
    checkout 归到 cd 目标,branch.json 不刷新、注入滞留已删分支;
    `cd subrepo && git commit` 的前缀语义保持不变(guards 也经 run_dir 受益)。"""
    pgr = _load_hook("posttool_git_refresh")
    W = "/tmp/dlut_cdpos"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/repo"); os.makedirs(f"{W}/notrepo")
    _git(f"{W}/repo", "init", "-q")
    expected = {str(Path(f"{W}/repo").resolve())}
    def roots(cmd, cwd):
        return {str(Path(r).resolve()) for r in pgr.affected_roots(cmd, cwd)}
    # cd 在 git 之后:归属仍是发起时的 cwd(修复点)
    assert roots(f"git checkout -q master && cd {W}/notrepo && python3 x.py", cwd=f"{W}/repo") == expected
    # cd 前缀在前:照旧解析到目标仓库
    assert roots(f"cd {W}/repo && git push && cd {W}/notrepo", cwd=W) == expected
    # 相对 cd 链组合
    assert roots(f"cd {W} && cd repo && git fetch", cwd="/") == expected


def test_cmdparse_glued_operators():
    """运算符紧贴词尾时也要断句:shlex.split 会把 `jsonpath='...';` 的 `;` 吞进
    token,segments() 断不开句——cd 落到段中而非段头,workspace guard 的 cd 豁免
    失效,kubectl+cd+uv 串被误拦。punctuation_chars 化后修复。"""
    from lib.cmdtree import cmdparse
    cmd = ("kubectl -o jsonpath='{range .items[*]}{\"\\n\"}{end}'; "
           "cd /tmp/sub && uv run x.py")
    assert [s[0] for s in cmdparse.commands(cmd)] == ["kubectl", "cd", "uv"]
    assert [s[0] for s in cmdparse.commands("make&&go test")] == ["make", "go"]
    # 引号内的运算符不断句(既有语义不回退)
    assert [s[0] for s in cmdparse.commands('echo "a; b" && make x')] == ["echo", "make"]


def test_cmdparse_subshell_scope():
    """AST 解析(Parable)拿到扁平模型拿不到的结构:子 shell 的 `(` 不再掩盖命令词,
    子 shell 的 cd 不外泄,命令替换里的 git 也被看见。"""
    from lib.cmdtree import cmdparse
    # `(` 不再掩码命令词:workspace guard 能同时看到 cd 与 uv(原误拦的 case)
    assert [s[0] for s in cmdparse.commands("(cd repo && uv run pytest)")] == ["cd", "uv"]
    # cd 在子 shell 内对同 shell 的命令生效……
    assert [i.cd for i in cmdparse.git_invocations("(cd x && git push)")] == ["x"]
    # ……但不外泄给子 shell 之后的兄弟命令(扁平模型做不到的 soundness)
    assert [i.cd for i in cmdparse.git_invocations("(cd x); git push")] == [None]
    # brace group 的 cd 留在本 shell → 会外泄(与子 shell 相反)
    assert [i.cd for i in cmdparse.git_invocations("{ cd y; git status; }")] == ["y"]
    # 命令替换 `$(…)` 里的 git 也要被看见(否则 protect 守卫漏判),且其 cd 隔离
    assert [i.subcommand for i in cmdparse.git_invocations("echo $(git push)")] == ["push"]
    assert [i.cd for i in cmdparse.git_invocations("echo $(cd z && git push)")] == ["z"]
    assert cmdparse.git_invocations('echo "git push"') == []   # 引号内仍不算


def test_cmdtree_parser_protocol():
    """解析后端符合 cmdtree.base.Parser 接口(具名 Protocol)——这正是"可随时替换"的契约:
    换 parser 只要再写一个暴露 `parser`(带 `parse(str)->Node`)的后端模块。"""
    from lib.cmdtree import base
    from lib.cmdtree import parable as parable_backend
    assert isinstance(parable_backend.parser, base.Parser)        # runtime_checkable
    assert isinstance(parable_backend.parser.parse("git push"), base.Seq)


def test_cmdparse_command_invocations():
    """每个命令是一个 Invocation(argv + 作用域感知 cd),run_dir(base) 算出有效目录——守卫据此
    判某命令实际在哪执行,而非只看"有没有 cd token"。"""
    Inv = cmdparse.Invocation
    ci = cmdparse.command_invocations
    assert ci("cd x && uv run pytest") == [
        Inv(argv=["cd", "x"], cd=None),
        Inv(argv=["uv", "run", "pytest"], cd="x"),
    ]
    # 子 shell 的 cd 不归属其后的兄弟命令
    uv = [v for v in ci("(cd sub); uv run pytest") if v.argv[0] == "uv"][0]
    assert uv.cd is None
    assert ci("PYTHONPATH=. pytest x")[0].argv[0] == "pytest"   # env 同 commands() 剥离
    # run_dir 把 cd 叠在 base 上
    assert Inv(argv=["uv"], cd="sub").run_dir("/ws") == "/ws/sub"
    assert Inv(argv=["uv"], cd=None).run_dir("/ws") == "/ws"


def test_workspace_cwd_guard_cd_scope():
    """cmdtree cd-scope 让守卫变 sound:在 workspace 根直接跑子项目命令 → 拦;同 shell `cd <sub>`
    进了真仓 → 放行;而 cd 在子 shell `(cd sub); uv`(对 uv 无效)→ 仍拦——粗判"有没有 cd"放过了它。"""
    guard = _load_hook("pretool_workspace_cwd_guard")
    root = "/tmp/dlut_wsg"; os.makedirs(root, exist_ok=True)
    guard.workspace = type("W", (), {"load_workspaces": staticmethod(lambda: [root])})
    guard.WorkspaceContext = type("WC", (), {"load": staticmethod(lambda p: None)})
    guard.load_active_repo = lambda p: None

    def at_root(cmd):
        return guard.decide(_hook_input("Bash", {"cwd": root, "tool_input": {"command": cmd}}))

    assert at_root("uv run pytest")                     # 裸命令在根 → 拦
    assert at_root("make build")
    assert at_root("cd sub && uv run pytest") is None   # cd 进子项目 → 放行
    assert at_root("(cd sub); uv run pytest")           # 子 shell cd 不外泄 → 仍拦(修复点)
    assert at_root("git status") is None                # 非子项目命令 → 放行
    # 不在 workspace 根 → 与本守卫无关
    assert guard.decide(_hook_input("Bash", {"cwd": "/tmp", "tool_input": {"command": "uv run x"}})) is None


def _hook_input(tool: str, raw: dict):
    from lib import hook_io
    return hook_io.HookInput(event="PreToolUse", tool_name=tool,
                             tool_input=raw.get("tool_input") or {},
                             cwd=raw.get("cwd", "/"), raw=raw)


def test_edit_owner_guard():
    """并发 session 防线的补全:owner 锁随'第一笔编辑'建立(acquire-follows-activity),
    guest 直接改 owner 工作树的文件被硬拦并引导 worktree——此前只有 git switch 被拦,
    第二个 session 直接 Edit 同一 checkout 畅通无阻。"""
    import importlib.util as _il
    guard = _load_hook("pretool_edit_owner_guard")
    from lib import session_lock
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


def test_branch_merged_guard_uses_file_path():
    """INACTIVE 分支编辑拦截按 file_path 解析 repo——session cwd 在 workspace 根时
    cwd-based 查找为 None,guard 此前静默失效。"""
    from lib.context import PullRequest, RepoContext
    guard = _load_hook("pretool_branch_merged_guard")
    R = "/tmp/dlut_bmg"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(f"{R}/repo", exist_ok=True)
    _git(f"{R}/repo", "init", "-q"); _git(f"{R}/repo", "config", "user.email", "t@t.t")
    _git(f"{R}/repo", "config", "user.name", "t"); _git(f"{R}/repo", "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/repo/f").write_text("x"); _git(f"{R}/repo", "add", "f"); _git(f"{R}/repo", "commit", "-qm", "i")
    RepoContext.refresh_all(f"{R}/repo")
    ctx = RepoContext.load(f"{R}/repo")
    ctx.prs = [PullRequest(number=9, state="merged", source_branch="feat/a")]
    ctx.branch.pr_number = 9; ctx.provider = "github"; ctx._save_pr()
    # cwd 在 workspace 根(R,非 git repo),编辑文件在 repo 内 → 仍要拦
    inp = _hook_input("Edit", {"session_id": "s", "cwd": R, "tool_input": {"file_path": f"{R}/repo/f"}})
    reason = guard.decide(inp)
    assert reason and "no longer active" in reason


def test_workspace_registry_user_level():
    """注册表住用户级 config.json(DEVLOOP_CONFIG_DIR 可覆写),不随 /plugin update 的
    版本化 cache 重置。"""
    from lib import config, workspace as registry
    W = "/tmp/dlut_reg"
    shutil.rmtree(W, ignore_errors=True); os.makedirs(f"{W}/cfg")
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    try:
        registry.register_workspace(f"{W}/ws1")
        assert config.config_file() == Path(f"{W}/cfg/config.json")
        assert Path(f"{W}/cfg/config.json").exists()   # 落在用户级 config.json
        assert any(p.endswith("ws1") for p in registry.load_workspaces())
    finally:
        if old_env is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old_env


def test_unified_config_forges_and_precommit():
    """config.json 统一承载 workspaces / forges(host→token/type) / precommit;token 按
    provider 的约定 env 覆写 config,update 保留其它段。"""
    from lib import config
    W = "/tmp/dlut_cfg"
    shutil.rmtree(W, ignore_errors=True); os.makedirs(f"{W}/cfg")
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    old_gh = os.environ.get("GITHUB_TOKEN")
    old_gl = os.environ.get("GITLAB_TOKEN")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    os.environ.pop("GITHUB_TOKEN", None); os.environ.pop("GH_TOKEN", None); os.environ.pop("GITLAB_TOKEN", None)
    try:
        Path(f"{W}/cfg/config.json").write_text(
            '{"workspaces": ["/tmp/ws"],'
            ' "forges": {"github.com": {"type": "github", "token": "gh-config"},'
            '            "gitlab.example.com": {"type": "gitlab", "token": "gl-config"}},'
            ' "precommit": {"default": {"commit_gate_lint": true}, "repos": {}}}'
        )
        assert config.forge_entry("github.com")["type"] == "github"
        assert config.forge_token("github.com", "github") == "gh-config"
        assert config.forge_token("gitlab.example.com", "gitlab") == "gl-config"
        assert config.precommit()["default"]["commit_gate_lint"] is True
        # provider 约定 env 覆写 config 里的 token
        os.environ["GITHUB_TOKEN"] = "gh-env"
        assert config.forge_token("github.com", "github") == "gh-env"
        os.environ["GITLAB_TOKEN"] = "gl-env"
        assert config.forge_token("gitlab.example.com", "gitlab") == "gl-env"
        # update 改 workspaces 不丢 forges/precommit
        config.set_workspaces(["/tmp/ws-new"])
        assert config.forge_entry("gitlab.example.com")["type"] == "gitlab"
        assert config.precommit()["default"]["commit_gate_lint"] is True
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
        for k, v in (("DEVLOOP_CONFIG_DIR", old_env), ("GITLAB_TOKEN", old_gl), ("GITHUB_TOKEN", old_gh)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_local_config_overrides_global():
    """repo / workspace 的 .devloop/config.json 覆盖全局,离 repo 近的赢;只含部分配置时
    缺的段落落回全局。写仍只落全局。"""
    from lib import config
    W = "/tmp/dlut_local"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/cfg")                      # 全局(DEVLOOP_CONFIG_DIR)
    os.makedirs(f"{W}/ws/repo/sub/.devloop")     # repo 级(最近)
    os.makedirs(f"{W}/ws/.devloop")              # workspace 级(较远)
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    old_tok = os.environ.get("GITHUB_TOKEN")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    os.environ.pop("GITHUB_TOKEN", None); os.environ.pop("GH_TOKEN", None)
    try:
        Path(f"{W}/cfg/config.json").write_text(
            '{"forges": {"github.com": {"type": "github", "token": "GLOBAL"}}}')
        Path(f"{W}/ws/.devloop/config.json").write_text(
            '{"forges": {"github.com": {"type": "github", "token": "WS", "api_host": "ws.example.com"}}}')
        # repo 级只含 token(部分配置)→ api_host 落回更外层
        Path(f"{W}/ws/repo/sub/.devloop/config.json").write_text(
            '{"forges": {"github.com": {"token": "REPO"}}}')
        repo = f"{W}/ws/repo/sub"
        assert config.forge_token("github.com", "github", repo) == "REPO"          # 最近的赢
        assert config.forge_entry("github.com", repo).get("api_host") == "ws.example.com"  # repo 没配 → 落 workspace 层
        assert config.forge_token("github.com", "github", f"{W}/ws") == "WS"        # 在 workspace 根 → workspace 层赢
        assert config.forge_token("github.com", "github", None) == "GLOBAL"         # 无 repo_dir → 仅全局
        # env 仍最高优先
        os.environ["GITHUB_TOKEN"] = "ENV"
        assert config.forge_token("github.com", "github", repo) == "ENV"
        os.environ.pop("GITHUB_TOKEN", None)
        # 写只落全局,不碰本地
        config.set_workspaces(["/tmp/ws-x"])
        assert "WS" in Path(f"{W}/ws/.devloop/config.json").read_text()   # 本地未被改写
        assert config.forge_token("github.com", "github", repo) == "REPO"  # 本地覆盖仍生效
    finally:
        if old_env is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old_env
        if old_tok is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = old_tok


def test_maybe_register_workspace():
    """workspace 自动注册:非 git 目录 + AGENTS.md 带子项目表 → 注册;普通 git 仓 /
    无 AGENTS.md 的目录绝不误判。手工 init_workspace 不再是主路径的前置条件。"""
    from lib import workspace as registry
    W = "/tmp/dlut_auto"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/cfg"); os.makedirs(f"{W}/ws/nb"); os.makedirs(f"{W}/plain"); os.makedirs(f"{W}/repo")
    _git(f"{W}/repo", "init", "-q")
    Path(f"{W}/ws/AGENTS.md").write_text(
        "# ws\n\n## Subprojects\n\n| 名称 | 说明 |\n|------|------|\n| `nb` | python |\n",
        encoding="utf-8")
    Path(f"{W}/repo/AGENTS.md").write_text("# repo\n", encoding="utf-8")
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    try:
        assert registry.maybe_register_workspace(f"{W}/ws") == str(Path(f"{W}/ws").resolve())
        assert registry.find_containing_workspace(f"{W}/ws") is not None   # 注册即生效
        assert registry.maybe_register_workspace(f"{W}/repo") is None      # git 仓不算
        assert registry.maybe_register_workspace(f"{W}/plain") is None     # 无 AGENTS.md 不算
    finally:
        if old_env is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old_env


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception as e:
            print(f"  ✗ FAIL {t.__name__}: {e}")
            failed.append(t.__name__)
    print("RESULT:", "FAIL" if failed else f"ALL PASS ({len(tests)} tests)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
