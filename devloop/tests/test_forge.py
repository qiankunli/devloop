#!/usr/bin/env python3
"""forge port：origin 解析/provider 识别、PR 映射、window 组合、PR 复用/创建、default branch。

Standalone: `python3 devloop/tests/test_forge.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from _testkit import _FakeForge, _git, _load_script, run_main  # noqa: E402  (bootstrap first)
from lib.context import Cadence, PullRequest  # noqa: E402
from lib.forge import detect_provider, parse_origin  # noqa: E402
from lib.forge.base import build_window, parse_pr_number, pr_label  # noqa: E402
from lib.forge.github import GitHubForge  # noqa: E402
from lib.forge.gitlab import GitLabForge  # noqa: E402


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

def test_forge_merge_readiness_mapping():
    """GitLab detailed_merge_status → neutral MergeReadiness, with a has_conflicts fallback and
    UNKNOWN for the async 'checking' window (UNKNOWN must never collapse to READY/CONFLICT).
    GitHub inherits the safe UNKNOWN default since it's not implemented yet."""
    from lib.forge.base import MergeReadiness
    r = GitLabForge._readiness
    assert r({"detailed_merge_status": "mergeable"}) is MergeReadiness.READY
    assert r({"detailed_merge_status": "conflict"}) is MergeReadiness.CONFLICT
    assert r({"detailed_merge_status": "discussions_not_resolved"}) is MergeReadiness.DISCUSSIONS_UNRESOLVED
    assert r({"detailed_merge_status": "ci_still_running"}) is MergeReadiness.CI_BLOCKED
    assert r({"detailed_merge_status": "checking"}) is MergeReadiness.UNKNOWN   # async window, not a verdict
    assert r({}) is MergeReadiness.UNKNOWN
    assert r({"has_conflicts": True}) is MergeReadiness.CONFLICT                # fallback when no detailed status
    # GitHub adapter hasn't implemented it → inherits the safe UNKNOWN default (no HTTP)
    assert GitHubForge.__new__(GitHubForge).merge_readiness(0) is MergeReadiness.UNKNOWN
    # blocks_merge: the shared "worth nagging about" predicate (banner + wake channel use it)
    assert MergeReadiness.CONFLICT.blocks_merge and MergeReadiness.DISCUSSIONS_UNRESOLVED.blocks_merge
    assert not (MergeReadiness.READY.blocks_merge or MergeReadiness.UNKNOWN.blocks_merge
                or MergeReadiness.DRAFT.blocks_merge)

def test_pr_cli_dispatch():
    """The `pr` CLI routes show/list/update/close to the forge facade (config-driven,
    provider-neutral). There is deliberately no `create` verb — opening an MR is gcampr's
    gated transaction, so `pr create` is rejected like any unknown verb."""
    prcli = _load_script("pr")
    fake = _FakeForge([PullRequest(number=5, state="open", source_branch="feat/x",
                                   target_branch="main", title="T", web_url="u/5")])

    class _R:
        git_root = "/x"

    orig_forge = prcli.forge_for_repo
    orig_resolve = prcli.cli.resolve_repo_or_exit
    try:
        prcli.forge_for_repo = lambda repo: fake
        prcli.cli.resolve_repo_or_exit = lambda ns, prog: (_R(), "test")
        assert prcli.main(["show", "5"]) == 0
        assert prcli.main(["list"]) == 0
        assert prcli.main(["list", "--branch", "feat/x"]) == 0
        assert prcli.main(["update", "5", "--title", "New"]) == 0
        assert prcli.main(["close", "5"]) == 0
        assert fake.get(5).state == "closed"   # close flipped state via the facade
        assert prcli.main(["update", "5"]) == 1   # nothing to update → error
        try:                                       # no create verb → argparse "invalid choice" → exit(2)
            prcli.main(["create", "--message", "m"])
            raised = False
        except SystemExit:
            raised = True
        assert raised
    finally:
        prcli.forge_for_repo = orig_forge
        prcli.cli.resolve_repo_or_exit = orig_resolve

def test_forge_release_endpoints():
    """create_release / latest_release hit the right endpoint with the provider's field names
    and map back to the neutral Release: github POST/GET releases (`target_commitish`/`body`,
    `/releases/latest` with 404→None), gitlab POST/GET releases (`ref`/`description`, list→first)."""
    from lib.forge.base import ForgeNotFound
    from lib.forge.github import GitHubForge
    from lib.forge.gitlab import GitLabForge

    class _Cap:
        def __init__(self, get_resp=None, get_exc=None):
            self.calls = []
            self._get, self._get_exc = get_resp, get_exc
            self.gets = []
        def get(self, path, **kw):
            self.gets.append((path, kw))
            if self._get_exc:
                raise self._get_exc
            return self._get
        def post(self, path, body):
            self.calls.append((path, body))
            return {"tag_name": body.get("tag_name"), "name": body.get("name"),
                    "html_url": f"gh/{body.get('tag_name')}", "target_commitish": body.get("target_commitish"),
                    "_links": {"self": f"gl/{body.get('tag_name')}"},
                    "commit": {"id": "deadbeef"}, "published_at": "2026-07-05"}

    gh = GitHubForge("api.github.com", "o", "r", "t"); gh.c = _Cap()
    rel = gh.create_release(tag="v1.8.0", target="main", notes="hi")
    assert gh.c.calls[0] == ("releases", {"tag_name": "v1.8.0", "target_commitish": "main",
                                          "name": "v1.8.0", "body": "hi"})
    assert (rel.tag, rel.target, rel.web_url) == ("v1.8.0", "main", "gh/v1.8.0")

    gl = GitLabForge("h", "o/r", "t"); gl.c = _Cap()
    rel = gl.create_release(tag="v1.8.0", target="release", name="R", notes="hi")
    assert gl.c.calls[0] == ("releases", {"tag_name": "v1.8.0", "ref": "release",
                                          "name": "R", "description": "hi"})
    assert (rel.tag, rel.name, rel.target, rel.web_url) == ("v1.8.0", "R", "deadbeef", "gl/v1.8.0")

    # latest: github 404 → None (first release); a hit → mapped
    gh2 = GitHubForge("api.github.com", "o", "r", "t")
    gh2.c = _Cap(get_exc=ForgeNotFound("404"))
    assert gh2.latest_release() is None and gh2.c.gets[0][0] == "releases/latest"
    gh3 = GitHubForge("api.github.com", "o", "r", "t")
    gh3.c = _Cap(get_resp={"tag_name": "v1.7.2", "html_url": "u"})
    assert gh3.latest_release().tag == "v1.7.2"
    # gitlab: list newest-first → first; empty list → None
    gl2 = GitLabForge("h", "o/r", "t"); gl2.c = _Cap(get_resp=[{"tag_name": "v1.7.2"}])
    assert gl2.latest_release().tag == "v1.7.2" and gl2.c.gets[0] == ("releases", {"per_page": 1})
    gl3 = GitLabForge("h", "o/r", "t"); gl3.c = _Cap(get_resp=[])
    assert gl3.latest_release() is None

def test_release_cli_dispatch():
    """`release` CLI over the facade: create validates semver + strict increment, defaults the
    target to the repo trunk, auto-drafts notes from merged PRs when none given; latest reads."""
    from lib import git_state
    rel = _load_script("release")
    fake = _FakeForge([PullRequest(number=7, state="merged", source_branch="feat/x",
                                   title="did a thing", web_url="u/7", updated_at="2026-07-05")])

    class _R:
        git_root = "/x"

    orig_forge, orig_resolve = rel.forge_for_repo, rel.cli.resolve_repo_or_exit
    orig_trunk = git_state.local_default_target
    try:
        rel.forge_for_repo = lambda repo: fake
        rel.cli.resolve_repo_or_exit = lambda ns, prog: (_R(), "test")
        git_state.local_default_target = lambda repo: "main"

        # first release: no prior → allowed; target defaults to trunk; notes auto-drafted from merged PR
        assert rel.main(["create", "v1.8.0"]) == 0
        assert fake.released.tag == "v1.8.0" and fake.released.target == "main"
        assert "## Changes" in fake.released_notes and "did a thing" in fake.released_notes
        # non-semver rejected before any call
        fake.released = None
        assert rel.main(["create", "1.8"]) == 1 and fake.released is None
        # not-greater-than-last rejected (last is now v1.8.0)
        assert rel.main(["create", "v1.8.0"]) == 1 and fake.released is None
        assert rel.main(["create", "v1.7.0"]) == 1 and fake.released is None
        # higher version accepted; explicit --target and --notes honored (no draft)
        assert rel.main(["create", "v1.9.0", "--target", "abc123", "--notes", "manual"]) == 0
        assert fake.released.tag == "v1.9.0" and fake.released.target == "abc123"
        assert fake.released_notes == "manual"
        # latest reads the newest
        assert rel.main(["latest"]) == 0
    finally:
        rel.forge_for_repo, rel.cli.resolve_repo_or_exit = orig_forge, orig_resolve
        git_state.local_default_target = orig_trunk

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
        pr = sgo.reuse_or_create_pr("/repo", "feat/x", "main", "t", "", plan)
        assert pr.number == 3 and f.created is None
        assert any("reused open PR #3" in line for line in plan)
        # create: only a finished PR for the branch → open a new one, body = description
        f2 = _FakeForge([PullRequest(number=3, state="merged", source_branch="feat/x")])
        sgo.forge_for_repo = lambda repo: f2
        plan = []
        pr = sgo.reuse_or_create_pr("/repo", "feat/x", "main", "t", "why & what", plan)
        assert f2.created is not None and pr.number == 4
        assert any("created PR #4" in line for line in plan)
        assert f2.description(4) == "why & what"
    finally:
        sgo.forge_for_repo = orig

def test_refresh_pr_failopen():
    """prstate.refresh_pr (gcampr's authoritative live-PR preflight, via gate.evaluate
    live_refresh) is best-effort: a repo with no forge remote/token returns False rather than
    raising — and crucially it now PERSISTS its poll (the old refresh_pr_state discarded it)."""
    from lib.context import prstate
    R = "/tmp/dlut_refresh"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    assert prstate.refresh_pr(R) is False   # no forge → no-op, no exception, nothing written

def test_pick_branch_pr():
    """Relocated to lib.context.prstate (so the gate and the monitor share one picker). Open PR
    wins; else the most-recent finished PR whose source sha is an ancestor of HEAD — the
    SHA-ancestry check is git_state.is_ancestor (patched here)."""
    from lib import git_state
    from lib.context import prstate
    P = lambda **kw: PullRequest(**kw)  # noqa: E731
    orig = git_state.is_ancestor
    try:
        git_state.is_ancestor = lambda repo, anc, desc: True
        assert prstate.pick_branch_pr([P(number=5, state="open", sha="a"),
                                       P(number=4, state="merged", sha="b")], "r", "h").number == 5
        git_state.is_ancestor = lambda repo, anc, desc: anc == "b"
        assert prstate.pick_branch_pr([P(number=4, state="merged", sha="b"),
                                       P(number=3, state="closed", sha="c")], "r", "h").number == 4
        git_state.is_ancestor = lambda repo, anc, desc: False
        assert prstate.pick_branch_pr([P(number=4, state="merged", sha="dead")], "r", "h") is None
    finally:
        git_state.is_ancestor = orig

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

def test_forge_comment_endpoint():
    """comment() 发到正确端点：gitlab → merge_requests/{n}/notes；github → issues/{n}/comments。"""
    from lib.forge.github import GitHubForge
    from lib.forge.gitlab import GitLabForge

    class _Cap:
        def __init__(self): self.calls = []
        def post(self, path, body): self.calls.append((path, body)); return {"id": 1}

    gl = GitLabForge("h", "o/r", "t"); gl.c = _Cap(); gl.comment(7, "hi")
    assert gl.c.calls == [("merge_requests/7/notes", {"body": "hi"})]
    gh = GitHubForge("api.github.com", "o", "r", "t"); gh.c = _Cap(); gh.comment(7, "hi")
    assert gh.c.calls == [("issues/7/comments", {"body": "hi"})]


def test_forge_diff_comment_endpoint():
    """diff_comment() 发行锚点评论（原生 outdated 生命周期）：gitlab → positioned discussion
    （diff_refs memo，一轮 N 条只 GET 一次）；github → pulls/{n}/comments 带 head-sha commit_id
    （同样 memo）。端口默认实现 raise ForgeError——不支持的 adapter 让调用方回落汇总 note。"""
    from lib.forge.base import Forge, ForgeError
    from lib.forge.github import GitHubForge
    from lib.forge.gitlab import GitLabForge

    class _Cap:
        def __init__(self, get_resp): self.calls = []; self._get = get_resp; self.gets = 0
        def get(self, path, **kw): self.gets += 1; return self._get
        def post(self, path, body): self.calls.append((path, body)); return {"id": 1}

    gl = GitLabForge("h", "o/r", "t")
    gl.c = _Cap({"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}})
    gl.diff_comment(7, "hi", "a.py", 5); gl.diff_comment(7, "yo", "b.py", 9)
    assert gl.c.gets == 1                                        # diff_refs memoized
    path, body = gl.c.calls[0]
    assert path == "merge_requests/7/discussions" and body["body"] == "hi"
    assert body["position"]["new_path"] == "a.py" and body["position"]["new_line"] == 5
    assert body["position"]["head_sha"] == "h"

    gh = GitHubForge("api.github.com", "o", "r", "t")
    gh.c = _Cap({"head": {"sha": "abc"}})
    gh.diff_comment(7, "hi", "a.py", 5); gh.diff_comment(7, "yo", "b.py", 9)
    assert gh.c.gets == 1                                        # head sha memoized
    assert gh.c.calls[0] == ("pulls/7/comments",
                             {"body": "hi", "commit_id": "abc", "path": "a.py", "line": 5, "side": "RIGHT"})

    try:                                                         # 端口默认：不支持 → raise
        Forge.diff_comment(gl, 7, "x", "a.py", 1)
        raise AssertionError("default diff_comment should raise")
    except ForgeError:
        pass

    gl2 = GitLabForge("h", "o/r", "t")                           # 缺 sha 的 diff_refs → 提前明确报错,
    gl2.c = _Cap({"diff_refs": {"head_sha": "h"}})               # 不让 None 漏进 position 变成盲 400
    try:
        gl2.diff_comment(7, "hi", "a.py", 5)
        raise AssertionError("partial diff_refs should raise")
    except ForgeError as e:
        assert "base_sha" in str(e) and "start_sha" in str(e)

def test_forge_default_branch():
    """default_branch() 读 repo 根对象的 default_branch（gitlab GET /projects/{id}、
    github GET /repos/{o}/{n}，路径为 ""）；缺字段 → ""。"""
    from lib.forge.github import GitHubForge
    from lib.forge.gitlab import GitLabForge

    class _C:
        def __init__(self, d): self.d, self.paths = d, []
        def get(self, path): self.paths.append(path); return self.d

    gl = GitLabForge("h", "o/r", "t"); gl.c = _C({"default_branch": "release"})
    assert gl.default_branch() == "release" and gl.c.paths == [""]   # repo root
    gh = GitHubForge("api.github.com", "o", "r", "t"); gh.c = _C({"default_branch": "main"})
    assert gh.default_branch() == "main"
    gl2 = GitLabForge("h", "o/r", "t"); gl2.c = _C({})
    assert gl2.default_branch() == ""                                # missing field → empty

def test_repo_meta_default_branch_roundtrip():
    """default_branch + default_branch_at 经 asdict/from_dict 往返不丢(meta 段持久化路径)。"""
    from dataclasses import asdict

    from lib.context.repo import RepoMeta
    m = RepoMeta(repo_dir="/r", default_branch="release", default_branch_at=123.0)
    m2 = RepoMeta.from_dict(asdict(m))
    assert m2.default_branch == "release" and m2.default_branch_at == 123.0

def test_resolve_default_branch_ttl():
    """TTL 门控:新鲜缓存零网络(不碰 forge);过期才取 forge 的权威值并打新时间戳。"""
    from lib.context import base as B
    from lib.context import repo as R

    calls = {"forge": 0}

    def _no_forge(d):
        calls["forge"] += 1
        return None

    orig = R.forge_for_repo
    R.forge_for_repo = _no_forge
    try:
        db, at = R._resolve_default_branch("/r", "main", B.now())   # 新鲜
        assert db == "main" and calls["forge"] == 0                 # 命中缓存、未拉

        class _F:
            def default_branch(self): return "release"

        R.forge_for_repo = lambda d: _F()
        db2, at2 = R._resolve_default_branch("/r", "main", 0.0)     # 过期(at=0)
        assert db2 == "release" and at2 > 0                         # forge 权威值 + 新时间戳
    finally:
        R.forge_for_repo = orig


if __name__ == "__main__":
    run_main(globals())
