#!/usr/bin/env python3
"""context 状态层与 workspace：segment 读写、in-flight 提示、subproject 解析、config 分层、active repo。

Standalone: `python3 devloop/tests/test_context.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from _testkit import _git, _hook_input, _load_hook, run_main  # noqa: E402  (bootstrap first)
from domain.context import PullRequest  # noqa: E402


def test_python_launcher_skips_old_name_and_uses_supported_fallback():
    """PATH 上第一个名字可能是旧 Python；launcher 必须继续找，而不是只认 python3。"""
    root = Path("/tmp/dlut_python_launcher")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    old = root / "python3"
    old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    old.chmod(0o755)
    good = root / "python"
    good.write_text(
        "#!/bin/sh\ncase \"$2\" in *version_info*) exit 0;; esac\nprintf python-fallback\n",
        encoding="utf-8",
    )
    good.chmod(0o755)
    launcher = Path(__file__).parents[1] / "scripts" / "python"
    env = {**os.environ, "PATH": str(root)}
    out = subprocess.run([launcher, "-c", "ignored"], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == "python-fallback"

    # A versioned-only installation is discovered from PATH, including future names that
    # were not known when this launcher was written.
    good.unlink()
    versioned = root / "python3.99"
    versioned.write_text(
        "#!/bin/sh\ncase \"$2\" in *version_info*) exit 0;; esac\nprintf versioned-fallback\n",
        encoding="utf-8",
    )
    versioned.chmod(0o755)
    out = subprocess.run([launcher, "-c", "ignored"], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == "versioned-fallback"

    # Explicit override is authoritative: a bad requested interpreter reports itself instead
    # of silently switching to another binary and hiding a configuration mistake.
    env["DEVLOOP_PYTHON"] = "python3"
    bad = subprocess.run([launcher, "-c", "ignored"], env=env, capture_output=True, text=True)
    assert bad.returncode == 127 and "DEVLOOP_PYTHON" in bad.stderr


def test_turn_block_stable_across_clock_when_state_unchanged():
    """整块 hash 去重（Cadence）赖以成立的前提：状态没变时 `turn_text()` 必须**逐字**相同。

    block 的去重粒度被它**最吵的一行**支配——只要有任何一行渲染相对时间（"3 分钟前"）或每轮
    自增的计数，整块 hash 就每轮都变，于是**所有**行每轮重发，Cadence 静默失效。那种退化不会
    让任何别的测试变红（每行内容都还是对的），所以这条测试就是那个前提的守卫。
    `base.fmt_ts` 故意渲染绝对时间戳而非"N 分钟前"，正是为此。

    时钟**允许**驱动的只有阈值跃迁（running→stale @REVIEW_STALE_SEC、requirement idle
    @REQUIREMENT_STALE_SEC）——那是真状态变了，本就该重发。跨不过任何阈值的时间流逝必须零变化。
    """
    from domain.context import RepoContext, base, store
    R = "/tmp/dlut_blockstable"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    Path(f"{R}/dirty").write_text("y")            # 让 Workspace 行也有内容
    ctx = RepoContext.refresh_all(R)
    G, branch = ctx.repo.repo_dir, ctx.branch.local.name

    # 把带时间语义的行都摆上：review(running,未到 stale 阈值) + 待打标 nudge + validation
    store.save_segment(G, store.branch_segment(branch, "review"),
                       {"status": "running", "count": 0, "reviewed_sha": "abcdef1234567",
                        "generated_at": base.now(), "comments": []})
    store.save_segment(G, "pr", {"branch": branch, "provider": "github",
                                 "label_pending": 2, "label_pending_key": "setA"})
    RepoContext.load(G).mark_lint_passed(".", "fp1")   # Validation: <component>: lint=<绝对时间戳>

    t1 = RepoContext.load(G).turn_text()
    assert "Review: running" in t1 and "待打标" in t1 and "lint=" in t1   # 别测了个空 block

    orig_now = base.now
    base.now = lambda: orig_now() + 600           # 10 分钟后，什么都没做，跨不过任何阈值
    try:
        t2 = RepoContext.load(G).turn_text()
    finally:
        base.now = orig_now
    assert t2 == t1, f"时间流逝改变了 turn block —— 整块去重已失效:\n---\n{t1}\n---\n{t2}\n---"


def test_turn_text_merge_blocked_hint():
    """An open MR with an actionable readiness blocker surfaces a MERGE-BLOCKED nag in the turn
    banner; READY / the async UNKNOWN stay quiet (no clutter while still checking)."""
    from domain.context import PullRequest, RepoContext
    R = "/tmp/dlut_mblock"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    ctx = RepoContext.load(R)
    ctx.prs = [PullRequest(number=7, state="open", source_branch="feat/a")]; ctx.branch.pr_number = 7
    ctx.merge_readiness = "conflict"
    assert "MERGE-BLOCKED" in ctx.turn_text() and "conflict" in ctx.turn_text()
    ctx.merge_readiness = "discussions_unresolved"
    assert "MERGE-BLOCKED" in ctx.turn_text() and "discussions" in ctx.turn_text()
    ctx.merge_readiness = "ready"
    assert "MERGE-BLOCKED" not in ctx.turn_text()
    ctx.merge_readiness = "unknown"      # async 'still checking' → must not nag
    assert "MERGE-BLOCKED" not in ctx.turn_text()
    # a blocker on an INACTIVE (merged/closed) PR isn't surfaced — nothing to act on
    ctx.prs = [PullRequest(number=7, state="merged", source_branch="feat/a")]
    ctx.merge_readiness = "conflict"
    assert "MERGE-BLOCKED" not in ctx.turn_text()

def test_concurrent_lint_and_test_marks_dont_lose_each_other():
    """lint 与 test 在一次 `lifecycle.dispatch` 里**并发**盖戳，谁都不许覆盖谁。

    红过的样子：两者共用 `branches/<b>/validation.json`，而 segment 的纪律是「single-writer
    whole-file overwrite」——两个 writer 各自 load→mutate→save 整个文件，后写的把先写的抹掉。
    实测丢的是 **test** 戳：状态说「没测过」而其实测过，是**记录失真**，不是保守的 fail-closed。

    修法用 store 自己的既定答案（拆到一段一 writer-role），不是「写前重读合并」——那只是把
    store 说的「结构上不可能」降级成窗口更窄的 race。

    barrier 把交错对齐成**必现**：两个线程读到同一个旧视图，再各自写。
    """
    import threading

    from domain.context import RepoContext
    R = "/tmp/dlut_concurrent_marks"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)

    barrier = threading.Barrier(2)
    def mark(kind):
        ctx = RepoContext.load(R)      # 两边读到同一个旧视图
        barrier.wait()                  # 对齐，放大成必现
        if kind == "lint":
            ctx.mark_lint_passed(".", "fp-abc")
        else:
            ctx.mark_test_passed(".")

    ts = [threading.Thread(target=mark, args=(k,)) for k in ("lint", "test")]
    [t.start() for t in ts]
    [t.join() for t in ts]

    v = RepoContext.load(R).validation.component(".")
    assert v.last_lint_at and v.lint_fingerprint == "fp-abc", "lint 戳被 test 覆盖了"
    assert v.last_test_at, "test 戳被 lint 覆盖了 —— 状态会说「没测过」而其实测过"


def test_context_segments():
    """Per-owner segment files: each writer touches a disjoint file (no lost update),
    and pr.json is branch-keyed so a branch switch self-invalidates pr_number with no writer."""
    from domain.context import PullRequest, RepoContext, base, store
    R = "/tmp/dlut_seg"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")

    # refresh_all writes ONLY the refresher-owned segments; branch-domain ones land under
    # branches/<branch>/ (三域布局), repo-domain ones at the .devloop root
    RepoContext.refresh_all(R)
    D = Path(R) / ".devloop"
    assert (D / "meta.json").exists() and (D / "branches/feat/a/branch.json").exists()
    assert not (D / "branches/feat/a/lint.json").exists() and not (D / "pr.json").exists()

    ctx = RepoContext.load(R)
    assert ctx.branch.local.name == "feat/a" and ctx.branch.pr_number is None and ctx.prs == []

    # 验证戳按 **check** 分段（lint / test 各一个 writer-role，dispatch 里并发跑）：
    # 盖 lint 戳只碰 lint.json，绝不碰 test.json——碰了就把「一段一 writer」破掉，lost update 回来。
    ctx.mark_lint_passed(".", "fp1")
    assert (D / "branches/feat/a/lint.json").exists()
    assert not (D / "branches/feat/a/test.json").exists(), "盖 lint 戳不该碰 test 段"
    assert RepoContext.load(R).validation.component(".").lint_fingerprint == "fp1"
    RepoContext.load(R).mark_test_passed(".")
    assert (D / "branches/feat/a/test.json").exists()
    # 两段合并成一个内存视图（消费方看不到拆分）
    v = RepoContext.load(R).validation.component(".")
    assert v.lint_fingerprint == "fp1" and v.last_lint_at and v.last_test_at

    # monitor-owned pr write, branch-keyed; provider is repo-level (header, not per-PR)
    ctx = RepoContext.load(R)
    ctx.prs = [PullRequest(number=51, state="open", source_branch="feat/a")]
    ctx.branch.pr_number = 51
    ctx.provider = "github"
    ctx._save_pr()
    assert store.load_segment(R, "pr")["branch"] == "feat/a"
    assert store.load_segment(R, "pr")["provider"] == "github"
    loaded = RepoContext.load(R)
    assert loaded.branch.pr_number == 51 and loaded.provider == "github"

    # branch switch → stale number drops at load with nobody clearing pr.json
    _git(R, "checkout", "-q", "-b", "feat/b")
    RepoContext.refresh_branch(R)
    assert RepoContext.load(R).branch.pr_number is None
    assert store.load_segment(R, "pr")["branch"] == "feat/a"   # monitor file untouched by refresh

    # disjoint writers (refresh ↔ monitor) don't clobber each other
    RepoContext.refresh_branch(R)
    c = RepoContext.load(R); c.prs = [PullRequest(number=60, state="open", source_branch="feat/b")]
    c.branch.pr_number = 60; c.provider = "github"; c._save_pr()
    merged = RepoContext.load(R)
    assert merged.branch.local.name == "feat/b" and merged.branch.pr_number == 60

def test_branch_pr_in_flight():
    """in-flight = 当前分支有 open PR(循环的"人工 merge 前 / 轮次之间"态);
    与 inactive(merged/closed)互斥。orchestrator 据此提示"在续写在途 PR"。"""
    from domain.context import PullRequest, RepoContext
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
    from domain.context import PullRequest, RepoContext
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
    assert "PR #51" in txt and "Recent PRs" not in txt

    # GitLab provider → MR ! 词汇(同一组 PR,只换 repo-level provider)
    ctx.provider = "gitlab"
    assert "MR !51" in ctx.turn_text() and "Recent MRs" not in ctx.turn_text()

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
    from domain.context import base, store
    R = "/tmp/dlut_atomic"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    store.save_segment(R, "meta", {"repo": {"repo_dir": R}, "updated_at": 1.0})
    store.save_segment(R, "branch", {"current": "x"})
    # no .tmp residue left behind after an atomic replace
    assert not any(n.endswith(".tmp") for n in os.listdir(f"{R}/.devloop"))
    # a corrupt segment reads as None (caller falls back to default), siblings still load
    (Path(R) / ".devloop" / "branch.json").write_text("{ not json")
    assert store.load_segment(R, "branch") is None
    assert store.load_segment(R, "meta")["updated_at"] == 1.0

def test_subproject_canonical():
    """symlink 农场:子项目条目本身是 symlink → 注入文本携带 canonical 映射,
    git 输出的真实路径不再被当成另一个仓库;普通子目录不带箭头。"""
    from domain.context import Subproject, WorkspaceContext
    from domain.context import workspace as wsctx
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
    from domain.context import workspace as wsctx
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
    from domain import repo as repo_model, workspace as registry
    from domain.context import Subproject, WorkspaceContext, load_active_repo, record_active_repo
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
        r, how = repo_model.resolve_repo_dir(f"{W}/real/nb", "/")           # 显式路径
        assert r and Path(r.git_root).resolve() == real_nb
        # 路径身份在解析边界一次算清(Repo),消费方不再各自 re-derive；显式路径解析
        # 带出 target_path（喂 select_components 当 explicit 信号），component 不再挂在解析结果上
        assert Path(r.real_git_root) == real_nb and r.target_path and r.source == how
        r, how = repo_model.resolve_repo_dir("nb", "/")                     # 子项目名 → canonical 仓库
        assert r and Path(r.git_root).resolve() == real_nb and "subproject" in how
        # symlink farm 下 canonical git_root 在 workspace 树外,containment-only 会得
        # None(Mode B 误判);必须经 subproject realpath 匹配归属到注册 workspace
        assert r.workspace_root and Path(r.workspace_root).resolve() == Path(f"{W}/ws").resolve()
        r, how = repo_model.resolve_repo_dir(None, f"{W}/real/nb")          # cwd 在仓库内
        assert r and how == "cwd"
        r, how = repo_model.resolve_repo_dir(None, f"{W}/ws")               # workspace 根、无活动 → 明确报错
        assert r is None and "--repo" in how
        record_active_repo(f"{W}/ws/nb")                                      # canonical 不在 ws 下也能归属
        active = load_active_repo(f"{W}/ws")
        assert active and Path(active).resolve() == real_nb
        r, how = repo_model.resolve_repo_dir(None, f"{W}/ws")               # last-active 兜底
        assert r and Path(r.git_root).resolve() == real_nb and "last-active" in how
        r, how = repo_model.resolve_repo_dir("zzz", "/")                    # 无匹配
        assert r is None
    finally:
        registry.load_workspaces = orig

def test_resolve_repo_dir_deduplicates_canonical_matches():
    """同一 canonical repo 被多个 workspace symlink 注册时仍是一个候选，不误报 ambiguous。"""
    from domain import repo as repo_model, workspace as registry
    from domain.context import Subproject, WorkspaceContext
    R = "/tmp/dlut_rr_dedup"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/real/repo")
    _git(f"{R}/real/repo", "init", "-q")
    for name in ("ws1", "ws2"):
        os.makedirs(f"{R}/{name}")
        os.symlink(f"{R}/real/repo", f"{R}/{name}/repo")
        WorkspaceContext(workspace_root=f"{R}/{name}",
                         subprojects=[Subproject(name="repo", path="repo")]).save()
    orig = registry.load_workspaces
    registry.load_workspaces = lambda: [f"{R}/ws1", f"{R}/ws2"]
    try:
        resolved, how = repo_model.resolve_repo_dir("repo", "/")
        assert resolved and Path(resolved.git_root).resolve() == Path(f"{R}/real/repo").resolve()
        assert "subproject" in how
    finally:
        registry.load_workspaces = orig


def test_is_workspace_root():
    from domain import workspace as registry

    root = "/tmp/dlut_workspace_root"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(f"{root}/ws/sub")

    original = registry.load_workspaces
    registry.load_workspaces = lambda: [f"{root}/ws"]
    try:
        assert registry.is_workspace_root(f"{root}/ws")
        assert not registry.is_workspace_root(f"{root}/ws/sub")
        assert not registry.is_workspace_root(f"{root}/elsewhere")
    finally:
        registry.load_workspaces = original

def test_component_multi_dir():
    """多代码目录仓（server/ + cli/）：component 由**操作目标路径**决定，不是 repo 单值属性。
    显式点名 cli 命中 cli；指向仓根 / 深层子目录归属到对应 component；仓根回落默认 component。"""
    from domain import repo as repo_model, repo_layout
    R = "/tmp/dlut_unit"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/repo/server/internal", exist_ok=True)
    os.makedirs(f"{R}/repo/cli", exist_ok=True)
    _git(f"{R}/repo", "init", "-q")
    Path(f"{R}/repo/server/pyproject.toml").write_text("[project]\n")
    Path(f"{R}/repo/cli/package.json").write_text('{"devDependencies":{"typescript":"5"}}')
    Path(f"{R}/repo/cli/Makefile").write_text("test:\n\techo ok\n")

    # 向上归属：cli 目标 → cli（语言 ts）；server 深层 → server（语言 py）
    u_cli = repo_layout.enclosing_component(f"{R}/repo/cli", f"{R}/repo")
    assert Path(u_cli.path).name == "cli" and u_cli.language == "typescript"
    u_srv = repo_layout.enclosing_component(f"{R}/repo/server/internal", f"{R}/repo")
    assert Path(u_srv.path).name == "server" and u_srv.language == "python"
    # 目标就是仓根 → 默认 component（探测 server/ 优先），不被根级无 marker 抢成 repo 根
    assert Path(repo_layout.enclosing_component(f"{R}/repo", f"{R}/repo").path).name == "server"
    assert Path(repo_layout.default_component(f"{R}/repo").path).name == "server"

    # 归属 vs 站位是**两个问题**，别混（`owning_` vs `enclosing_`）：
    # 仓根的 README 在「根不是 component」的仓里确实不属于任何 component —— 归属答 None 才是事实。
    # 硬派一个只能派 default_component（server > backend > 根的**选择**启发式），得到的是
    # 「改 README → 跑 server 的 lint」这种没理由的结论（为什么是 server 不是 cli？）。
    assert repo_layout.owning_component(f"{R}/repo/README.md", f"{R}/repo") is None
    assert repo_layout.owning_component(f"{R}/repo/.github/workflows/ci.yml", f"{R}/repo") is None
    assert repo_layout.owning_component(f"{R}/repo/cli/app.ts", f"{R}/repo").id == "cli"
    # 站位仍必给答案：站在仓根跑命令，总得有个 component 判 uv / make test（guard 用的就是这一支）
    assert repo_layout.enclosing_component(f"{R}/repo/README.md", f"{R}/repo").id == "server"

    # 解析边界不再挂 component：显式路径带出 target_path，选哪个 component 交给 select_components（explicit 信号）
    r, _ = repo_model.resolve_repo_dir(f"{R}/repo/cli", "/")
    ws = repo_model.select_components(r.git_root, explicit=r.target_path)
    assert [Path(u.path).name for u in ws.components] == ["cli"] and "explicit" in ws.reason
    assert ws.components[0].language == "typescript"
    r, _ = repo_model.resolve_repo_dir(None, f"{R}/repo/cli")   # cwd 在 cli 下
    ws = repo_model.select_components(r.git_root, explicit=r.target_path)
    assert [Path(u.path).name for u in ws.components] == ["cli"]

def test_select_components_by_change():
    """WorkSet 契约：验证目标由**本次改动**决定，不由解析来源猜——把「改 cli 不得跑 server」
    从约定升级成可执行约束。clean 从仓根 repo-wide 全选；显式=仓根不静默回落默认 server。"""
    from domain import repo as repo_model, repo_layout
    R = "/tmp/dlut_select"
    shutil.rmtree(R, ignore_errors=True)
    repo = f"{R}/repo"
    os.makedirs(f"{repo}/server", exist_ok=True)
    os.makedirs(f"{repo}/cli", exist_ok=True)
    _git(repo, "init", "-q")
    Path(f"{repo}/server/pyproject.toml").write_text("[project]\n")
    Path(f"{repo}/cli/package.json").write_text('{"devDependencies":{"typescript":"5"}}')
    Path(f"{repo}/cli/Makefile").write_text("test:\n\techo ok\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    names = lambda ws: sorted(Path(u.path).name for u in ws.components)

    # discover：两个 component 都在
    assert sorted(Path(u.path).name for u in repo_layout.discover_components(repo)) == ["cli", "server"]
    # clean tree：repo-wide 全选（绝不静默 server-only）
    ws = repo_model.select_components(repo)
    assert names(ws) == ["cli", "server"] and "all components" in ws.reason
    # 开发根里常有自指软链 + linked worktrees：它们不是本仓 component，也不是
    # 当前代码改动；不得污染 discover，更不得把 dirty WorkSet 拉回无语言的仓根。
    os.symlink(repo, f"{repo}/repo-link")
    os.makedirs(f"{repo}/worktrees/branch")
    _git(f"{repo}/worktrees/branch", "init", "-q")
    Path(f"{repo}/worktrees/branch/go.mod").write_text("module nested\n")
    assert names(repo_model.select_components(repo)) == ["cli", "server"]
    assert sorted(Path(u.path).name for u in repo_layout.discover_components(repo)) == ["cli", "server"]
    # 只改 cli → dirty 只投影 cli（核心：改 cli 不跑 server）
    Path(f"{repo}/cli/app.ts").write_text("export const x = 1\n")
    ws = repo_model.select_components(repo)
    assert names(ws) == ["cli"] and "changed files" in ws.reason
    # explicit == 仓根：不静默回 server，落回 dirty(cli)
    assert names(repo_model.select_components(repo, explicit=repo)) == ["cli"]
    # 两个 component 都改 → 都进 WorkSet
    Path(f"{repo}/server/mod.py").write_text("x = 1\n")
    assert names(repo_model.select_components(repo)) == ["cli", "server"]

def test_discover_root_and_sub_units():
    """component 的身份 = **语言项目清单**，且仓根不是特例。两条都红过：

    1. 补根 component 曾用 `default_component`（`server/` > `backend/` > 根的**选择**启发式）来探——
       `server/` 存在时它返回 `server/`，而 `server/` 早被 walk 收过，于是「补根」永远补不进、
       根的 go.mod 从 catalog 里消失。旧 fixture 只造了「无 server/」那支，所以一直绿。
    2. `Makefile` 曾算 marker，于是 `docs/` 里一个 sphinx Makefile 就成了 component。Makefile
       是 component 的动作入口（怎么 lint/test），不是身份。
    """
    from domain import repo_layout
    R = "/tmp/dlut_discover"
    shutil.rmtree(R, ignore_errors=True)
    repo = f"{R}/repo"
    os.makedirs(f"{repo}/tools", exist_ok=True)
    _git(repo, "init", "-q")
    Path(f"{repo}/go.mod").write_text("module x\n")
    Path(f"{repo}/tools/pyproject.toml").write_text("[project]\n")
    ids = lambda p: sorted(u.id for u in repo_layout.discover_components(p))
    assert ids(repo) == [".", "tools"]

    # 关键回归：`server/` 一出现，「补根」那支就哑火了——根必须仍在 catalog 里
    os.makedirs(f"{repo}/server", exist_ok=True)
    Path(f"{repo}/server/pyproject.toml").write_text("[project]\n")
    assert ids(repo) == [".", "server", "tools"]
    # catalog 与改动投影必须给同一个答案：根的文件归根，不归 server
    assert repo_layout.enclosing_component(f"{repo}/main.go", repo).id == "."
    assert repo_layout.enclosing_component(f"{repo}/server/app.py", repo).id == "server"
    # 而「没有具体目标该选谁」仍是 server：选择启发式不变，与身份判据各答各的问题
    assert repo_layout.default_component(repo).id == "server"

    # 身份不是 Makefile：docs/ 的 sphinx Makefile 不让 docs 变成 component
    os.makedirs(f"{repo}/docs", exist_ok=True)
    Path(f"{repo}/docs/Makefile").write_text("html:\n\tsphinx-build . _build\n")
    assert ids(repo) == [".", "server", "tools"]

    # 逐语言的项目清单——TS 与 JS 同为 package.json（tsconfig.json 只是 TS 的编译配置，
    # 一个 package 里可以有多份，不定义项目边界，故不算身份）
    for manifest in ("go.mod", "pyproject.toml", "setup.py", "package.json"):
        d = Path(f"{R}/probe/{manifest}"); d.mkdir(parents=True)
        (d / manifest).write_text("{}" if manifest == "package.json" else "")
        assert repo_layout._is_component(d), f"{manifest} 应当是项目清单"
    for weak in ("Makefile", "requirements.txt", "tsconfig.json"):
        d = Path(f"{R}/weak/{weak}"); d.mkdir(parents=True)
        (d / weak).write_text("{}" if weak.endswith(".json") else "")
        assert not repo_layout._is_component(d), f"{weak} 不是项目边界，不该独自构成 component"

def test_active_repo_first_entry_symlink_workspace():
    """P1 回归:首次进入(尚无 context.json)+ symlink 子仓,record_active_repo 也要落
    active.json——workspace_for_repo 缺 context 时自刷新(解析 AGENTS.md 子项目表)。"""
    from domain import workspace as registry
    from domain.context import load_active_repo, record_active_repo
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

def test_active_repo_is_per_session():
    """session 运行态:active 绑定一 session 一文件(`.devloop/active/<sid>.json`,owner=session,
    铁律零例外)。并发 session 各干各的仓,兜底各回各家——B 的活动不劫持 A 的无参 /lint /gcam;
    绝不读别人的绑定当答案(candidates 仅作报错提示);SessionEnd 清掉本 session 的绑定。"""
    from domain import workspace as registry
    from domain.context import clear_active_repo, load_active_repo, record_active_repo
    from domain.context.session import active_repo_candidates
    W = "/tmp/dlut_active_sess"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/ws/nb"); os.makedirs(f"{W}/ws/svc")
    _git(f"{W}/ws/nb", "init", "-q"); _git(f"{W}/ws/svc", "init", "-q")
    orig = registry.load_workspaces
    registry.load_workspaces = lambda: [f"{W}/ws"]
    try:
        record_active_repo(f"{W}/ws/nb", "sess-A")
        record_active_repo(f"{W}/ws/svc", "sess-B")
        assert (Path(f"{W}/ws") / ".devloop" / "active" / "sess-A.json").exists()
        assert load_active_repo(f"{W}/ws", "sess-A").endswith("/nb")
        assert load_active_repo(f"{W}/ws", "sess-B").endswith("/svc")
        # 无绑定的 session → None,哪怕别人的绑定全指向同一个仓也不借用
        assert load_active_repo(f"{W}/ws", "sess-C") is None
        record_active_repo(f"{W}/ws/nb", "sess-B")
        assert load_active_repo(f"{W}/ws", "sess-C") is None
        # candidates 只做解析器报错里的提示
        assert sorted(Path(c).name for c in active_repo_candidates(f"{W}/ws")) == ["nb"]
        # SessionEnd 释放:清掉本 session 的绑定,不碰别人的
        clear_active_repo(f"{W}/ws", "sess-A")
        assert load_active_repo(f"{W}/ws", "sess-A") is None
        assert load_active_repo(f"{W}/ws", "sess-B").endswith("/nb")
    finally:
        registry.load_workspaces = orig

def test_workspace_registry_user_level():
    """注册表住用户级 config.json(DEVLOOP_CONFIG_DIR 可覆写),不随 /plugin update 的
    版本化 cache 重置。"""
    from domain import workspace as registry
    from lib import config
    W = "/tmp/dlut_reg"
    shutil.rmtree(W, ignore_errors=True); os.makedirs(f"{W}/cfg")
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    old_codex = os.environ.get("CODEX_HOME")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    try:
        registry.register_workspace(f"{W}/ws1")
        os.environ["CODEX_HOME"] = f"{W}/codex-home"
        registry.register_workspace(f"{W}/codex-home")
        assert config.config_file() == Path(f"{W}/cfg/config.json")
        assert Path(f"{W}/cfg/config.json").exists()   # 落在用户级 config.json
        assert any(p.endswith("ws1") for p in registry.load_workspaces())
        assert not any(p.endswith("codex-home") for p in registry.load_workspaces())
        assert "codex-home" not in Path(f"{W}/cfg/config.json").read_text()
    finally:
        if old_codex is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex
        if old_env is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old_env

def test_unified_config_forges_and_lifecycle():
    """config.json 统一承载 workspaces / forges(host→token/type) / lifecycle;token 按
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
            ' "lifecycle": {"default": {"pre_commit": ["lint"]}, "repos": {}}}'
        )
        assert config.forge_entry("github.com")["type"] == "github"
        assert config.forge_token("github.com", "github") == "gh-config"
        assert config.forge_token("gitlab.example.com", "gitlab") == "gl-config"
        assert config.lifecycle()["pre_commit"] == ["lint"]
        # provider 约定 env 覆写 config 里的 token
        os.environ["GITHUB_TOKEN"] = "gh-env"
        assert config.forge_token("github.com", "github") == "gh-env"
        os.environ["GITLAB_TOKEN"] = "gl-env"
        assert config.forge_token("gitlab.example.com", "gitlab") == "gl-env"
        # update 改 workspaces 不丢 forges/lifecycle
        config.set_workspaces(["/tmp/ws-new"])
        assert config.forge_entry("gitlab.example.com")["type"] == "gitlab"
        assert config.lifecycle()["pre_commit"] == ["lint"]
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
    from domain import workspace as registry
    W = "/tmp/dlut_auto"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/cfg"); os.makedirs(f"{W}/ws/nb"); os.makedirs(f"{W}/plain"); os.makedirs(f"{W}/repo")
    _git(f"{W}/repo", "init", "-q")
    Path(f"{W}/ws/AGENTS.md").write_text(
        "# ws\n\n## Subprojects\n\n| 名称 | 说明 |\n|------|------|\n| `nb` | python |\n",
        encoding="utf-8")
    Path(f"{W}/repo/AGENTS.md").write_text("# repo\n", encoding="utf-8")
    old_env = os.environ.get("DEVLOOP_CONFIG_DIR")
    old_codex = os.environ.get("CODEX_HOME")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"
    try:
        os.environ["CODEX_HOME"] = f"{W}/codex-home"
        os.makedirs(f"{W}/codex-home/tool-repo")
        _git(f"{W}/codex-home/tool-repo", "init", "-q")
        Path(f"{W}/codex-home/AGENTS.md").write_text("# tool home\n", encoding="utf-8")
        assert registry.maybe_register_workspace(f"{W}/codex-home") is None
        assert registry.maybe_register_workspace(f"{W}/ws") == str(Path(f"{W}/ws").resolve())
        assert registry.find_containing_workspace(f"{W}/ws") is not None   # 注册即生效
        assert registry.maybe_register_workspace(f"{W}/repo") is None      # git 仓不算
        assert registry.maybe_register_workspace(f"{W}/plain") is None     # 无 AGENTS.md 不算
    finally:
        if old_codex is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex
        if old_env is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old_env

def test_inject_at_workspace_root_uses_active_repo():
    """At the aggregate-workspace root (cwd not a git repo), inject falls back to the workspace's
    last-active repo so the turn context (branch topology / freshness / hints) still reaches the
    prompt — the most common usage, where a naive cwd-only lookup injects nothing (Codex P1)."""
    ui = _load_hook("userprompt_inject")

    class _Board:
        repo = None
        def deliver_prompt(self): return "Branch: feat/x (ahead 0, behind 0 vs main, as of 1)"

    saved = ui.BoardRuntime
    seen = {}
    try:
        ui.BoardRuntime = type("M", (), {"resolve": staticmethod(
            lambda cwd, sid=None: seen.setdefault("args", (cwd, sid)) and _Board())})
        out = ui.produce(_hook_input("UserPromptSubmit", {"cwd": "/ws"}))
    finally:
        ui.BoardRuntime = saved
    assert seen.get("args", (None,))[0] == "/ws"
    assert out and "Branch: feat/x" in out                 # active repo's turn context reached the prompt


def test_session_log_is_append_only_and_kind_discriminated():
    """`<repo>/.devloop/sessions/<sid>.jsonl` 是 devloop 自己的 append-only 日志，`kind` 区分
    记录类型——新类型直接落**同一个文件**，读者按 kind 过滤，不破坏既有行、也不用另开文件。
    `inject` 只是第一种。纯 exhaust：devloop 从不读回，删了不影响任何行为。"""
    import json as _json
    from domain.context import record_session_event
    R = "/tmp/dlut_seslog"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")

    record_session_event(R, "s1", "inject", text="block A")
    record_session_event(R, "s1", "gate_blocked", gate="precommit", reason="lint")   # 未来的某种记录
    rows = [_json.loads(ln) for ln in
            (Path(R) / ".devloop/sessions/s1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows] == ["inject", "gate_blocked"]      # 共存，不互相破坏
    assert rows[0]["text"] == "block A" and rows[1]["gate"] == "precommit"
    assert all(r["ts"] > 0 for r in rows)
    # 只 append，不覆写（对照 save_segment）
    record_session_event(R, "s1", "inject", text="block B")
    assert len((Path(R) / ".devloop/sessions/s1.jsonl").read_text().splitlines()) == 3


def test_inject_recorded_to_session_log():
    """注入本来是 write-only 的——每轮拼好、在模型 context 里花掉、就没了；能读到构造它的代码，
    读不到某一轮它实际说了什么，而后者才是判断「这行值不值它的 token」的依据。
    只记 devloop 注入的文本，不记用户 prompt（那在 CLI transcript 里，再存一份等于多一处没人
    审的留存）。"""
    import json as _json
    from domain.context import RepoContext
    ui = _load_hook("userprompt_inject")
    R = "/tmp/dlut_injlog"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/a")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)

    out = ui.produce(_hook_input("UserPromptSubmit", {"cwd": R, "session_id": "sess-A"}))
    assert out and "Branch: feat/a" in out
    p = Path(R) / ".devloop" / "sessions" / "sess-A.jsonl"
    rows = [_json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "inject"
    assert rows[0]["text"] == out and rows[0]["ts"] > 0      # 记的正是发出去的那一份

    # cadence 压掉这轮（状态没变）→ 什么都没注入 → 不记：ledger 是「模型看见了什么」的账，
    # 不是「hook 跑了几次」的账，记空行只会稀释它。
    assert ui.produce(_hook_input("UserPromptSubmit", {"cwd": R, "session_id": "sess-A"})) is None
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1

    # 另一个 session 各记各的；session id 里的路径分隔符必须消毒掉，不能写穿出目录
    Path(f"{R}/dirty").write_text("y")     # 让 turn block 变化，否则会被 cadence 压掉
    out2 = ui.produce(_hook_input("UserPromptSubmit", {"cwd": R, "session_id": "a/../../evil"}))
    assert out2
    sess_dir = Path(R) / ".devloop" / "sessions"
    assert (sess_dir / "a-..-..-evil.jsonl").exists()             # `/` 消毒成 `-`，写不穿出去
    assert sorted(x.name for x in sess_dir.iterdir()) == ["a-..-..-evil.jsonl", "sess-A.jsonl"]
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1   # sess-A 的账没被串写


def test_append_jsonl_ledger():
    """store.append_jsonl 是 ledger 原语：多次追加成多行、每行独立 json、不覆写（对照 save_segment
    的整体覆写）；写失败 best-effort 不抛。"""
    import json

    from domain.context import base, store
    R = "/tmp/dlut_ledger"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    store.append_jsonl(R, "friction", {"a": 1})
    store.append_jsonl(R, "friction", {"a": 2})
    lines = (Path(R) / ".devloop" / "friction.jsonl").read_text().splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]          # append-only，两行不覆写
    store.append_jsonl("/dev/null/nope", "x", {"a": 1})           # 不可写路径 → 吞掉，不抛

def test_friction_records_deny():
    """blocked Decision → 追加一条 friction 记录（kind/source/tool + 逐 finding 的 rule/locator +
    live branch）；allow → 不写文件。best-effort：append 抛错也不影响 guard（record_deny 不抛）。"""
    import json

    from domain.context import store
    from hooks import friction
    from hooks.core.domain import Decision, Finding, Severity
    R = "/tmp/dlut_friction"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "checkout", "-q", "-b", "feat/x")

    deny = Decision.of([Finding(rule="protect-branch", severity=Severity.DENY,
                                message="nope", locator="git push origin main")])
    friction.record_deny(deny, tool="Bash", cwd=R, session_id="s-1")
    rec = json.loads((Path(R) / ".devloop" / "friction.jsonl").read_text().splitlines()[-1])
    assert rec["kind"] == "friction" and rec["source"] == "guard" and rec["tool"] == "Bash"
    assert rec["branch"] == "feat/x"                              # live branch，供后续归属 requirement
    assert rec["session_id"] == "s-1"                             # 下钻 harness transcript 的 join 键
    assert rec["findings"] == [{"rule": "protect-branch", "locator": "git push origin main"}]

    # allow decision → no-op（不建文件）
    R2 = "/tmp/dlut_friction2"
    shutil.rmtree(R2, ignore_errors=True); os.makedirs(R2)
    _git(R2, "init", "-q")
    friction.record_deny(Decision.allow(), tool="Bash", cwd=R2)
    assert not (Path(R2) / ".devloop" / "friction.jsonl").exists()

    # best-effort：底层 append 抛错，record_deny 仍不抛（guard 判决不受影响）
    orig = store.append_jsonl
    store.append_jsonl = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    try:
        friction.record_deny(deny, tool="Bash", cwd=R)            # 不得抛
    finally:
        store.append_jsonl = orig

def test_friction_sink_wired_into_bash_guard():
    """集成：真被 protect-branch 拦下的 Bash push，除了返回 deny 文案，还落一条 friction 记录——
    即"引擎已算出的 Decision 不再发射后即弃"。"""
    import json

    from domain.context import RepoContext
    guard = _load_hook("pretool_policy_bash")
    R = "/tmp/dlut_friction_wire"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "main")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)
    inp = _hook_input("Bash", {"cwd": R, "session_id": "sess-w",
                               "tool_input": {"command": "git push origin main"}})
    assert guard.decide(inp)                                       # 被拦（返回 deny 文案）
    rec = json.loads((Path(R) / ".devloop" / "friction.jsonl").read_text().splitlines()[-1])
    assert rec["source"] == "guard" and rec["branch"] == "main"
    assert rec["session_id"] == "sess-w"                           # hook payload 的 session_id 被留住
    assert any(f["rule"] == "protect-branch" for f in rec["findings"])


def test_requirement_open_attach_resolve():
    """requirement scope（loop-state slice3）：open 建索引 + session_start；attach 续接 + branch_cut；
    resolve 反查；open 幂等（不重复 session_start）；note 对未索引分支惰性 open。"""
    import json

    from domain.context import store
    from domain.context.loopstate import requirement
    R = "/tmp/dlut_req"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    # open：id = 首个分支名；索引段（branches 按 repo 嵌套，键 = realpath）+ session_start（带 repo）
    rid = requirement.open_requirement(R, "feat/x", fork_from="main", fork_sha="abc")
    assert rid == "feat/x"
    RK = str(Path(R).resolve())                  # repo 键 = realpath（symlink 拼写不分裂键）
    idx = store.load_segment(R, "requirements")
    assert idx["branches"] == {RK: {"feat/x": "feat/x"}} and idx["requirements"]["feat/x"]["status"] == "open"
    sess = (Path(R) / ".devloop" / "requirements" / "feat/x" / "session.jsonl").read_text().splitlines()
    e0 = json.loads(sess[0])
    assert e0["kind"] == "session_start" and e0["requirement"] == "feat/x" and e0["fork_sha"] == "abc"
    assert e0["repo"] == RK                      # 事件带 repo：单 spine 跨仓的归因字段

    # open 幂等：不追加第二条 session_start
    requirement.open_requirement(R, "feat/x")
    assert len((Path(R) / ".devloop" / "requirements" / "feat/x" / "session.jsonl").read_text().splitlines()) == 1

    # attach：第二个分支续接同一 requirement → branch_cut{continues:true}，索引指向 req
    requirement.attach_branch(R, "feat/x", "fix/x-followup", fork_sha="def")
    assert requirement.resolve(R, "fix/x-followup") == "feat/x"
    sess = (Path(R) / ".devloop" / "requirements" / "feat/x" / "session.jsonl").read_text().splitlines()
    last = json.loads(sess[-1])
    assert last["kind"] == "branch_cut" and last["branch"] == "fix/x-followup" and last["continues"] is True

    # resolve 未索引分支 → None；note 对它惰性 open（id = 该分支）
    assert requirement.resolve(R, "chore/z") is None
    requirement.note(R, "chore/z", {"kind": "pr_created", "branch": "chore/z", "number": 9})
    assert requirement.resolve(R, "chore/z") == "chore/z"
    zsess = (Path(R) / ".devloop" / "requirements" / "chore/z" / "session.jsonl").read_text().splitlines()
    assert json.loads(zsess[0])["kind"] == "session_start" and json.loads(zsess[-1])["kind"] == "pr_created"


def test_requirement_reconcile_closures():
    """close 半（monitor 侧，loop-state）：merged PR → pr_merged + session_end{done}，幂等不重复；
    不写 requirements.json（保 gcampr 单写者）；open → 不 end；closed-only → abandoned；
    staleness backstop → assumed_done。"""
    import json

    from domain.context import store
    from domain.context.loopstate import requirement
    R = "/tmp/dlut_req_close"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    def kinds(req):
        return [e["kind"] for e in requirement.session_events(R, req)]

    # feat/x：PR merged → pr_merged + session_end done
    requirement.open_requirement(R, "feat/x")
    store.save_segment(R, "pr", {"prs": [{"number": 100, "state": "merged", "source_branch": "feat/x"}]})
    idx_before = store.load_segment(R, "requirements")
    requirement.reconcile_closures(R)
    assert kinds("feat/x") == ["session_start", "pr_merged", "session_end"]
    end = requirement.session_events(R, "feat/x")[-1]
    assert end["result"] == "done"
    assert store.load_segment(R, "requirements") == idx_before   # 不写索引（单写者不变）

    # 幂等：再跑一遍不追加
    requirement.reconcile_closures(R)
    assert kinds("feat/x") == ["session_start", "pr_merged", "session_end"]

    # feat/open：PR 仍 open → 不 end
    requirement.open_requirement(R, "feat/open")
    store.save_segment(R, "pr", {"prs": [
        {"number": 100, "state": "merged", "source_branch": "feat/x"},
        {"number": 101, "state": "open", "source_branch": "feat/open"}]})
    requirement.reconcile_closures(R)
    assert "session_end" not in kinds("feat/open")

    # feat/dead：PR closed（非 merged）→ pr_closed + session_end abandoned
    requirement.open_requirement(R, "feat/dead")
    store.save_segment(R, "pr", {"prs": [{"number": 102, "state": "closed", "source_branch": "feat/dead"}]})
    requirement.reconcile_closures(R)
    ev = requirement.session_events(R, "feat/dead")
    assert ev[-2]["kind"] == "pr_closed" and ev[-1]["result"] == "abandoned"

    # feat/stale：无 PR，idle 超阈值 → assumed_done（backstop）
    requirement.open_requirement(R, "feat/stale")
    store.save_segment(R, "pr", {"prs": []})
    requirement.reconcile_closures(R, stale_after_sec=0)   # 立即判定过期
    assert requirement.session_events(R, "feat/stale")[-1]["result"] == "assumed_done"


def test_requirement_arcs_and_offwindow_closure():
    """review findings F1/F2：① 同名分支在需求关闭后再 open → 追加新 session_start（arc 定界），
    且上一 arc 的 merged PR 不得立刻误关新 arc；② 闭合按 spine 的 pr_created number 事件溯源——
    PR 掉出 5 条窗口时用 forge.get(number) 兜底，从不为未建 PR 的分支查 forge。"""
    import json

    from domain.context import store
    from domain.context.loopstate import requirement
    R = "/tmp/dlut_req_arcs"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    def events():
        return requirement.session_events(R, "feat/x")

    # arc1：open + pr_created(100)，窗口里 100 merged → pr_merged + session_end done
    requirement.open_requirement(R, "feat/x")
    requirement.note(R, "feat/x", {"kind": "pr_created", "branch": "feat/x", "number": 100})
    store.save_segment(R, "pr", {"prs": [{"number": 100, "state": "merged", "source_branch": "feat/x"}]})
    requirement.reconcile_closures(R)
    assert [e["kind"] for e in events()][-2:] == ["pr_merged", "session_end"]

    # F1：需求已关闭，同名分支再 open → 新 session_start（第二段 arc 可见）
    requirement.open_requirement(R, "feat/x", fork_sha="new")
    starts = [e for e in events() if e["kind"] == "session_start"]
    assert len(starts) == 2 and starts[-1]["fork_sha"] == "new"
    # arc 活跃期间再 open → 幂等，不重复
    requirement.open_requirement(R, "feat/x")
    assert len([e for e in events() if e["kind"] == "session_start"]) == 2

    # 旧 arc 的 merged PR(100) 仍在窗口 → 不得误关新 arc（旧实现会立刻 session_end）
    requirement.reconcile_closures(R)
    assert len([e for e in events() if e["kind"] == "session_end"]) == 1

    # F2：arc2 建了 PR 105，窗口里没有它（掉出 PRS_CAP）→ forge.get(105) 兜底 → done
    requirement.note(R, "feat/x", {"kind": "pr_created", "branch": "feat/x", "number": 105})

    class _F:
        def get(self, num):
            assert num == 105                       # 只查 spine 里已知存在的 PR
            from domain.context import PullRequest
            return PullRequest(number=105, state="merged", source_branch="feat/x")
    orig = requirement.forge_for_repo
    requirement.forge_for_repo = lambda repo: _F()
    try:
        requirement.reconcile_closures(R)
    finally:
        requirement.forge_for_repo = orig
    ks = [e["kind"] for e in events()]
    assert ks[-2:] == ["pr_merged", "session_end"] and ks.count("session_end") == 2
    assert json.loads((Path(R) / ".devloop/requirements/feat/x/session.jsonl")
                      .read_text().splitlines()[-1])["result"] == "done"

    # 无 forge（返回 None）+ 窗口空 + 未过期 → pending 保持 open，不误关
    requirement.open_requirement(R, "feat/y")
    requirement.note(R, "feat/y", {"kind": "pr_created", "branch": "feat/y", "number": 200})
    store.save_segment(R, "pr", {"prs": []})
    requirement.reconcile_closures(R)               # 该仓无 origin → forge None → unknown
    assert not any(e["kind"] == "session_end" for e in requirement.session_events(R, "feat/y"))


def test_requirement_attach_guards_arc_invariant():
    """狗粮发现（PR#62 后首用）：attach 必须守住「每段 arc 以 session_start 开头」的定界约定。
    ① `--requirement <未开过的名字>` 走 continue 路径 → 先补 session_start 再 branch_cut
    （否则 spine 首行是 branch_cut，工具靠首行识别原始流的约定被破坏）；
    ② 需求已关闭后 attach 后续分支（merge 后 follow-up）→ 新 arc 的 session_start 先行
    （否则 branch_cut 悬在 session_end 之后，_active_tail 看不见、永远不被 reconcile）。"""
    from domain.context import store
    from domain.context.loopstate import requirement
    R = "/tmp/dlut_req_attach"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    # ① attach 到从未 open 过的 requirement：session_start 先于 branch_cut，索引两条都建
    requirement.attach_branch(R, "feat/new-req", "fix/first-cut", fork_sha="abc")
    ks = [e["kind"] for e in requirement.session_events(R, "feat/new-req")]
    assert ks == ["session_start", "branch_cut"]
    assert requirement.resolve(R, "fix/first-cut") == "feat/new-req"

    # 活跃 arc 上再 attach 其他分支 → 不重复 session_start
    requirement.attach_branch(R, "feat/new-req", "fix/second-cut")
    ks = [e["kind"] for e in requirement.session_events(R, "feat/new-req")]
    assert ks == ["session_start", "branch_cut", "branch_cut"]

    # ② 需求关闭后 attach follow-up 分支 → 新 arc：session_start 先行，branch_cut 可被 tail 看见
    requirement.note(R, "feat/new-req", {"kind": "pr_created", "branch": "fix/first-cut", "number": 7})
    store.save_segment(R, "pr", {"prs": [{"number": 7, "state": "merged", "source_branch": "fix/first-cut"}]})
    requirement.reconcile_closures(R)
    assert [e["kind"] for e in requirement.session_events(R, "feat/new-req")][-1] == "session_end"
    requirement.attach_branch(R, "feat/new-req", "fix/followup")
    tail = requirement._active_tail(requirement.session_events(R, "feat/new-req"))
    assert [e["kind"] for e in tail] == ["session_start", "branch_cut"]
    assert tail[-1]["branch"] == "fix/followup"


def test_requirement_cross_repo_dev_root():
    """requirement-first 目标态：仓属于注册 workspace → requirement 域落 workspace 根（dev root），
    跨仓事件写同一 spine（带 repo 字段）；turn_line 渲染跨仓 PR live 态（多仓带 repo 前缀）；
    reconcile 按 (repo, number) join 各仓 pr.json 收口——同号不同仓不混淆。
    （Mode B——仓不属任何 workspace——退化为 repo 根，即其余 requirement 测试的形态。）"""
    import json

    from domain import workspace as registry
    from domain.context import store
    from domain.context.loopstate import requirement

    W = "/tmp/dlut_req_ws"
    shutil.rmtree(W, ignore_errors=True)
    A, B = f"{W}/repoA", f"{W}/repoB"
    for r in (A, B):
        os.makedirs(f"{r}/.git")                 # .git 目录 → _main_repo_root 返回自身
    Path(f"{W}/AGENTS.md").write_text("# ws\n")
    registry.register_workspace(W)

    # A 开需求，B attach 同一需求 → 单 spine 落 W/.devloop，事件带各自 repo；仓内无 requirement 域
    requirement.open_requirement(A, "feat/cross", fork_from="main")
    requirement.attach_branch(B, "feat/cross", "feat/cross-b")
    spine = Path(W) / ".devloop/requirements/feat/cross/session.jsonl"
    assert spine.exists()
    AK, BK = str(Path(A).resolve()), str(Path(B).resolve())
    evs = [json.loads(x) for x in spine.read_text().splitlines()]
    assert [e["kind"] for e in evs] == ["session_start", "branch_cut"]
    assert evs[0]["repo"] == AK and evs[1]["repo"] == BK
    assert not (Path(A) / ".devloop/requirements").exists()
    assert requirement.resolve(B, "feat/cross-b") == "feat/cross"

    # 双仓各自 note 同号 PR —— (repo, number) 是键，不混淆
    requirement.note(A, "feat/cross", {"kind": "pr_created", "branch": "feat/cross", "number": 7})
    requirement.note(B, "feat/cross-b", {"kind": "pr_created", "branch": "feat/cross-b", "number": 7})

    # turn_line：从任一仓看到同一个任务视图，多仓时 PR 带 repo 短名前缀
    store.save_segment(A, "pr", {"prs": [{"number": 7, "state": "merged", "source_branch": "feat/cross"}]})
    store.save_segment(B, "pr", {"prs": [{"number": 7, "state": "open", "source_branch": "feat/cross-b"}]})
    line = requirement.turn_line(B, "feat/cross-b")
    assert line.startswith("Requirement: feat/cross")
    assert "repoA#7 merged" in line and "repoB#7 open" in line
    assert requirement.turn_line(B, "unrelated-branch") == ""   # 无 requirement → 零注入

    # reconcile（从 A 触发，管整个 dev root）：A#7 merged 记账；B#7 仍 open → 不收口
    requirement.reconcile_closures(A)
    evs = [json.loads(x) for x in spine.read_text().splitlines()]
    kinds = [e["kind"] for e in evs]
    assert kinds.count("pr_merged") == 1 and "session_end" not in kinds
    merged = next(e for e in evs if e["kind"] == "pr_merged")
    assert merged["repo"] == AK and merged["number"] == 7

    # B 的 PR 也 merge → 需求收口 done；收口后 turn_line 归零（任务已完成，不再占 token）
    store.save_segment(B, "pr", {"prs": [{"number": 7, "state": "merged", "source_branch": "feat/cross-b"}]})
    requirement.reconcile_closures(B)
    evs = [json.loads(x) for x in spine.read_text().splitlines()]
    assert evs[-1]["kind"] == "session_end" and evs[-1]["result"] == "done"
    assert requirement.turn_line(B, "feat/cross-b") == ""


def test_state_domains_worktree():
    """三域布局的核心承诺：linked worktree 里产生的 repo 域（friction/requirements）与 branch 域
    （branch/validation）状态全部落**主仓** .devloop（worktree 清理不再丢数据）；owner 锁留在
    worktree 自己的 .devloop（并行 worktree 不被误串行化）；submodule 形态的 .git 文件回落本地
    （绝不往宿主 .git/modules 里写）。"""
    from domain.context import RepoContext, store
    from hooks import friction
    from domain.context.loopstate import requirement
    from domain.context import session as session_lock
    from hooks.core.domain import Decision, Finding, Severity
    M = "/tmp/dlut_domains_main"
    shutil.rmtree(M, ignore_errors=True); os.makedirs(M)
    _git(M, "init", "-q", "-b", "main"); _git(M, "config", "user.email", "t@t.t"); _git(M, "config", "user.name", "t")
    Path(f"{M}/f").write_text("x"); _git(M, "add", "f"); _git(M, "commit", "-qm", "i")
    W = f"{M}/.worktrees/wt"
    _git(M, "worktree", "add", "-q", "-b", "feat/wt", W)

    # 解析原语：主 checkout → 自身；worktree → 主仓（.git 文件里是 canonical 路径，
    # 故与 /tmp→/private/tmp 软链无关地按 resolve 比较）；无 .git → 自身
    assert store._main_repo_root(M) == Path(M)
    assert store._main_repo_root(W).resolve() == Path(M).resolve()
    assert store.state_dir(W).resolve() == (Path(M) / ".devloop").resolve()   # repo 域统一落主仓
    assert store.worktree_state_dir(W) == Path(W) / ".devloop"                # working-tree 域留本地

    # repo 域：worktree 里的 friction / requirement 落主仓
    deny = Decision.of([Finding(rule="protect-branch", severity=Severity.DENY, message="n", locator="x")])
    friction.record_deny(deny, tool="Bash", cwd=W)
    assert (Path(M) / ".devloop/friction.jsonl").exists()
    assert not (Path(W) / ".devloop/friction.jsonl").exists()
    requirement.open_requirement(W, "feat/wt")
    assert (Path(M) / ".devloop/requirements/feat/wt/session.jsonl").exists()

    # branch 域：worktree 的 refresh/validation 落主仓 branches/feat/wt/，与主 checkout 的
    # branches/main/ 并存互不干扰（git 禁止同分支双检出 → 每文件单写者天然成立）
    wt_ctx = RepoContext.refresh_all(W)
    wt_turn = wt_ctx.turn_text()
    header = wt_turn.split("]", 1)[0]
    assert str(Path(M).resolve()) in header
    assert ".worktrees/wt" not in header
    assert "Branch: feat/wt (worktree)" in wt_turn
    # validation 的 key 是 **checkout 相对**的 component id：worktree 的根 component 与主 checkout 的根
    # component 同为 "."。用绝对路径当 key 会写成 <M>/.worktrees/wt——戳落在主仓 branches/feat/wt/ 里，
    # 却带着一个只在那个 worktree 成立的 key：worktree 删掉再在主 checkout 上 feat/wt，戳就查不到，
    # 白跑一遍 lint；且 key 随 worktree 增删无限累积。
    from domain.repo_layout import Component
    assert Component.at(W, W).id == Component.at(M, M).id == "."
    wt_ctx.mark_lint_passed(Component.at(W, W).id, "fp1")
    RepoContext.refresh_all(M)
    assert (Path(M) / ".devloop/branches/feat/wt/lint.json").exists()
    assert (Path(M) / ".devloop/branches/feat/wt/branch.json").exists()
    assert (Path(M) / ".devloop/branches/main/branch.json").exists()
    assert RepoContext.load(W).branch.local.name == "feat/wt"   # load 按 live 分支取段
    assert RepoContext.load(M).branch.local.name == "main"

    # working-tree 域：owner 锁各归各的工作树
    assert session_lock.acquire(W, "sess-wt", "feat/wt", pid=os.getpid())
    assert (Path(W) / ".devloop/owner.lock").exists()
    assert not (Path(M) / ".devloop/owner.lock").exists()

    # submodule 形态：.git 文件指向宿主 .git/modules/... → 回落本地，绝不写进宿主 git dir
    S = "/tmp/dlut_domains_sub"
    shutil.rmtree(S, ignore_errors=True); os.makedirs(S)
    Path(f"{S}/.git").write_text(f"gitdir: {M}/.git/modules/sub\n")
    assert store._main_repo_root(S) == Path(S)


if __name__ == "__main__":
    run_main(globals())
