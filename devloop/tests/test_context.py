#!/usr/bin/env python3
"""context 状态层与 workspace：segment 读写、in-flight 提示、subproject 解析、config 分层、active repo。

Standalone: `python3 devloop/tests/test_context.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _git, _hook_input, _load_hook, run_main  # noqa: E402  (bootstrap first)
from lib.context import PullRequest  # noqa: E402


def test_turn_text_merge_blocked_hint():
    """An open MR with an actionable readiness blocker surfaces a MERGE-BLOCKED nag in the turn
    banner; READY / the async UNKNOWN stay quiet (no clutter while still checking)."""
    from lib.context import PullRequest, RepoContext
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

def test_context_segments():
    """Per-owner segment files: each writer touches a disjoint file (no lost update),
    and pr.json is branch-keyed so a branch switch self-invalidates pr_number with no writer."""
    from lib.context import PullRequest, RepoContext, base
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
    assert not (D / "branches/feat/a/validation.json").exists() and not (D / "pr.json").exists()

    ctx = RepoContext.load(R)
    assert ctx.branch.local.name == "feat/a" and ctx.branch.pr_number is None and ctx.prs == []

    # a validation mark writes only its branch's validation.json
    ctx.mark_lint_passed()
    assert (D / "branches/feat/a/validation.json").exists()
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
    assert merged.branch.local.name == "feat/b" and merged.branch.pr_number == 60

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

def test_active_repo_is_per_session():
    """session 运行态:active 绑定一 session 一文件(`.devloop/active/<sid>.json`,owner=session,
    铁律零例外)。并发 session 各干各的仓,兜底各回各家——B 的活动不劫持 A 的无参 /lint /gcam;
    绝不读别人的绑定当答案(candidates 仅作报错提示);SessionEnd 清掉本 session 的绑定。"""
    from lib import workspace as registry
    from lib.context import clear_active_repo, load_active_repo, record_active_repo
    from lib.context.session import active_repo_candidates
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

def test_inject_at_workspace_root_uses_active_repo():
    """At the aggregate-workspace root (cwd not a git repo), inject falls back to the workspace's
    last-active repo so the turn context (branch topology / freshness / hints) still reaches the
    prompt — the most common usage, where a naive cwd-only lookup injects nothing (Codex P1)."""
    ui = _load_hook("userprompt_inject")

    class _Ctx:
        def emit_session_if_changed(self): return ""
        def mark_session_emitted(self, s): pass
        def emit_turn_if_changed(self): return "Branch: feat/x (ahead 0, behind 0 vs main, as of 1)"
        def mark_turn_emitted(self, s): pass

    saved = (ui.repo_layout, ui.workspace, ui.WorkspaceContext, ui.RepoContext, ui.load_active_repo)
    seen = {}
    try:
        ui.repo_layout = type("M", (), {"find_git_root": staticmethod(lambda p: None)})
        ui.workspace = type("M", (), {"find_containing_workspace": staticmethod(lambda p: "/ws")})
        ui.WorkspaceContext = type("M", (), {"load": staticmethod(lambda r: None)})
        ui.load_active_repo = lambda r, sid=None: seen.setdefault("active_arg", r) or "/active/repo"
        ui.RepoContext = type("M", (), {"load": staticmethod(lambda r: _Ctx())})
        out = ui.produce(_hook_input("UserPromptSubmit", {"cwd": "/ws"}))
    finally:
        ui.repo_layout, ui.workspace, ui.WorkspaceContext, ui.RepoContext, ui.load_active_repo = saved
    assert seen.get("active_arg") == "/ws"                 # fell back via the workspace root
    assert out and "Branch: feat/x" in out                 # active repo's turn context reached the prompt


def test_append_jsonl_ledger():
    """base.append_jsonl 是 ledger 原语：多次追加成多行、每行独立 json、不覆写（对照 save_segment
    的整体覆写）；写失败 best-effort 不抛。"""
    import json

    from lib.context import base
    R = "/tmp/dlut_ledger"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    base.append_jsonl(R, "friction", {"a": 1})
    base.append_jsonl(R, "friction", {"a": 2})
    lines = (Path(R) / ".devloop" / "friction.jsonl").read_text().splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]          # append-only，两行不覆写
    base.append_jsonl("/dev/null/nope", "x", {"a": 1})           # 不可写路径 → 吞掉，不抛

def test_friction_records_deny():
    """blocked Decision → 追加一条 friction 记录（kind/source/tool + 逐 finding 的 rule/locator +
    live branch）；allow → 不写文件。best-effort：append 抛错也不影响 guard（record_deny 不抛）。"""
    import json

    from lib.context import base, friction
    from lib.core.domain import Decision, Finding, Severity
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
    orig = base.append_jsonl
    base.append_jsonl = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    try:
        friction.record_deny(deny, tool="Bash", cwd=R)            # 不得抛
    finally:
        base.append_jsonl = orig

def test_friction_sink_wired_into_bash_guard():
    """集成：真被 protect-branch 拦下的 Bash push，除了返回 deny 文案，还落一条 friction 记录——
    即"引擎已算出的 Decision 不再发射后即弃"。"""
    import json

    from lib.context import RepoContext
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

    from lib.context import base, requirement
    R = "/tmp/dlut_req"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    # open：id = 首个分支名；索引段 + session_start
    rid = requirement.open_requirement(R, "feat/x", fork_from="main", fork_sha="abc")
    assert rid == "feat/x"
    idx = base.load_segment(R, "requirements")
    assert idx["branches"] == {"feat/x": "feat/x"} and idx["requirements"]["feat/x"]["status"] == "open"
    sess = (Path(R) / ".devloop" / "requirements" / "feat/x" / "session.jsonl").read_text().splitlines()
    e0 = json.loads(sess[0])
    assert e0["kind"] == "session_start" and e0["requirement"] == "feat/x" and e0["fork_sha"] == "abc"

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

    from lib.context import base, requirement
    R = "/tmp/dlut_req_close"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    def kinds(req):
        return [e["kind"] for e in requirement.session_events(R, req)]

    # feat/x：PR merged → pr_merged + session_end done
    requirement.open_requirement(R, "feat/x")
    base.save_segment(R, "pr", {"prs": [{"number": 100, "state": "merged", "source_branch": "feat/x"}]})
    idx_before = base.load_segment(R, "requirements")
    requirement.reconcile_closures(R)
    assert kinds("feat/x") == ["session_start", "pr_merged", "session_end"]
    end = requirement.session_events(R, "feat/x")[-1]
    assert end["result"] == "done"
    assert base.load_segment(R, "requirements") == idx_before   # 不写索引（单写者不变）

    # 幂等：再跑一遍不追加
    requirement.reconcile_closures(R)
    assert kinds("feat/x") == ["session_start", "pr_merged", "session_end"]

    # feat/open：PR 仍 open → 不 end
    requirement.open_requirement(R, "feat/open")
    base.save_segment(R, "pr", {"prs": [
        {"number": 100, "state": "merged", "source_branch": "feat/x"},
        {"number": 101, "state": "open", "source_branch": "feat/open"}]})
    requirement.reconcile_closures(R)
    assert "session_end" not in kinds("feat/open")

    # feat/dead：PR closed（非 merged）→ pr_closed + session_end abandoned
    requirement.open_requirement(R, "feat/dead")
    base.save_segment(R, "pr", {"prs": [{"number": 102, "state": "closed", "source_branch": "feat/dead"}]})
    requirement.reconcile_closures(R)
    ev = requirement.session_events(R, "feat/dead")
    assert ev[-2]["kind"] == "pr_closed" and ev[-1]["result"] == "abandoned"

    # feat/stale：无 PR，idle 超阈值 → assumed_done（backstop）
    requirement.open_requirement(R, "feat/stale")
    base.save_segment(R, "pr", {"prs": []})
    requirement.reconcile_closures(R, stale_after_sec=0)   # 立即判定过期
    assert requirement.session_events(R, "feat/stale")[-1]["result"] == "assumed_done"


def test_requirement_arcs_and_offwindow_closure():
    """review findings F1/F2：① 同名分支在需求关闭后再 open → 追加新 session_start（arc 定界），
    且上一 arc 的 merged PR 不得立刻误关新 arc；② 闭合按 spine 的 pr_created number 事件溯源——
    PR 掉出 5 条窗口时用 forge.get(number) 兜底，从不为未建 PR 的分支查 forge。"""
    import json

    from lib.context import base, requirement
    R = "/tmp/dlut_req_arcs"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)

    def events():
        return requirement.session_events(R, "feat/x")

    # arc1：open + pr_created(100)，窗口里 100 merged → pr_merged + session_end done
    requirement.open_requirement(R, "feat/x")
    requirement.note(R, "feat/x", {"kind": "pr_created", "branch": "feat/x", "number": 100})
    base.save_segment(R, "pr", {"prs": [{"number": 100, "state": "merged", "source_branch": "feat/x"}]})
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
            from lib.context import PullRequest
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
    base.save_segment(R, "pr", {"prs": []})
    requirement.reconcile_closures(R)               # 该仓无 origin → forge None → unknown
    assert not any(e["kind"] == "session_end" for e in requirement.session_events(R, "feat/y"))


def test_state_domains_worktree():
    """三域布局的核心承诺：linked worktree 里产生的 repo 域（friction/requirements）与 branch 域
    （branch/validation）状态全部落**主仓** .devloop（worktree 清理不再丢数据）；owner 锁留在
    worktree 自己的 .devloop（并行 worktree 不被误串行化）；submodule 形态的 .git 文件回落本地
    （绝不往宿主 .git/modules 里写）。"""
    from lib.context import RepoContext, base, friction, requirement
    from lib.context import session as session_lock
    from lib.core.domain import Decision, Finding, Severity
    M = "/tmp/dlut_domains_main"
    shutil.rmtree(M, ignore_errors=True); os.makedirs(M)
    _git(M, "init", "-q", "-b", "main"); _git(M, "config", "user.email", "t@t.t"); _git(M, "config", "user.name", "t")
    Path(f"{M}/f").write_text("x"); _git(M, "add", "f"); _git(M, "commit", "-qm", "i")
    W = f"{M}/.worktrees/wt"
    _git(M, "worktree", "add", "-q", "-b", "feat/wt", W)

    # 解析原语：主 checkout → 自身；worktree → 主仓（.git 文件里是 canonical 路径，
    # 故与 /tmp→/private/tmp 软链无关地按 resolve 比较）；无 .git → 自身
    assert base._main_repo_root(M) == Path(M)
    assert base._main_repo_root(W).resolve() == Path(M).resolve()
    assert base.state_dir(W).resolve() == (Path(M) / ".devloop").resolve()   # repo 域统一落主仓
    assert base.worktree_state_dir(W) == Path(W) / ".devloop"                # working-tree 域留本地

    # repo 域：worktree 里的 friction / requirement 落主仓
    deny = Decision.of([Finding(rule="protect-branch", severity=Severity.DENY, message="n", locator="x")])
    friction.record_deny(deny, tool="Bash", cwd=W)
    assert (Path(M) / ".devloop/friction.jsonl").exists()
    assert not (Path(W) / ".devloop/friction.jsonl").exists()
    requirement.open_requirement(W, "feat/wt")
    assert (Path(M) / ".devloop/requirements/feat/wt/session.jsonl").exists()

    # branch 域：worktree 的 refresh/validation 落主仓 branches/feat/wt/，与主 checkout 的
    # branches/main/ 并存互不干扰（git 禁止同分支双检出 → 每文件单写者天然成立）
    RepoContext.refresh_all(W).mark_lint_passed()
    RepoContext.refresh_all(M)
    assert (Path(M) / ".devloop/branches/feat/wt/validation.json").exists()
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
    assert base._main_repo_root(S) == Path(S)


if __name__ == "__main__":
    run_main(globals())
