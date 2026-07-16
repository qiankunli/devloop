#!/usr/bin/env python3
"""code-review 链路：引擎解析、run_review 成文/回喂 history、注入行、后台启动。

Standalone: `python3 devloop/tests/test_review.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _FakeForge, _git, _git_out, _load_script, run_main  # noqa: E402  (bootstrap first)
from domain.context import PullRequest  # noqa: E402
from domain.forge import ForgeError  # noqa: E402


def test_run_review_skips_without_engine():
    """引擎没装（默认 ccr）→ run_review 写 status=skipped、退出 0（advisory，从不报错/挡事）。"""
    from domain.context import base, store
    rr = _load_script("run_review")
    G = "/tmp/dlut_rr"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    orig = rr.review_engine.shutil.which
    rr.review_engine.shutil.which = lambda name: None   # 假装引擎不在 PATH（which 在协议 adapter 里）
    try:
        rc = rr.main(["--repo", G])
    finally:
        rr.review_engine.shutil.which = orig
    assert rc == 0
    from lib import git_state
    seg = store.load_segment(G, store.branch_segment(git_state.get_current_branch(G), "review"))
    assert seg and seg["status"] == "skipped" and "not installed" in seg["message"] and seg["count"] == 0

def test_review_result_lands_on_the_branch_it_reviewed():
    """review 是 detach 起的**长时**任务：结果必须落到它**启动时**那条分支，而不是写盘那一刻的
    live 分支。

    红过的样子——`_write` 每次现问 `get_current_branch()`（注释还写着「即使 checkout 移动过也
    正确」）：跑 ~6 分钟期间人 / agent 切走分支，于是被审分支的 review.json 永远停在 running
    （注入把它读成 stale），而当前分支凭空拿到一份**别人的** findings。实测过三条 PR，两条如此。

    同一条不变量还管着 `reviewed_sha`：引擎的 to_ref 若传字面量 "HEAD"，ccr 在它自己启动那刻
    才解析——审的是切换后的分支，记录里的 reviewed_sha 却是启动时的，记录直接撒谎。
    """
    from domain.context import store
    rr = _load_script("run_review")

    G = "/tmp/dlut_rr_branch"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q", "-b", "main")
    _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    _git(G, "checkout", "-q", "-b", "feat/reviewed")
    Path(f"{G}/b.txt").write_text("y"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "work")
    reviewed_sha = _git_out(G, "rev-parse", "HEAD")

    seen: dict = {}

    class _FakeEngine:
        name = "fake"
        def available(self): return True
        def configured(self, repo): return True
        def install_hint(self): return ""
        def review(self, repo, from_ref, to_ref, background, history_path=None):
            seen["to_ref"] = to_ref
            # 引擎跑到一半，checkout 被切走——这正是真实场景（人/agent 去开下一条 PR 了）
            _git(repo, "checkout", "-q", "main")
            return rr.review_engine.ReviewResult(
                ok=True, status="success", comments=[], failed=0, warnings=[],
                message="lgtm", cost_sec=1, tool_version="v0", models={})

    orig_resolve, orig_open = rr.review_engine.resolve, rr._open_mr
    rr.review_engine.resolve = lambda name: _FakeEngine()
    rr._open_mr = lambda repo, branch: (None, None)     # 不碰 forge
    try:
        assert rr.main(["--repo", G]) == 0
    finally:
        rr.review_engine.resolve, rr._open_mr = orig_resolve, orig_open

    # 终态落在**被审的**分支，不是写盘时的 live 分支（main）
    seg = store.load_segment(G, store.branch_segment("feat/reviewed", "review"))
    assert seg and seg["status"] == "success", f"结果没落在被审分支：{seg}"
    assert seg["reviewed_sha"] == reviewed_sha
    assert store.load_segment(G, store.branch_segment("main", "review")) is None, \
        "结果落到了切换后的分支名下 —— 正是「写盘时现问 live 分支」的 bug"
    # 引擎审的是冻结的 sha，不是字面量 HEAD（否则 reviewed_sha 会与实际审的对象不符）
    assert seen["to_ref"] == reviewed_sha, f"to_ref 应是冻结的 sha，实际 {seen['to_ref']!r}"


def test_review_engine_resolve():
    """review tool 协议：按名解析引擎，未知 / 空回落默认 ccr；ReviewResult 归一化形状。"""
    re = _load_script("run_review").review_engine
    assert re.resolve(None).name == "ccr"      # 未配 → 默认
    assert re.resolve("ocr").name == "ocr"
    assert re.resolve("nope").name == "ccr"    # 未知 → 回落默认（不报错）
    r = re.ReviewResult(ok=True, comments=[1], failed=2)
    assert r.ok and r.comments == [1] and r.failed == 2 and r.warnings == [] and r.status == "success"

def test_review_injection_line():
    """review.json 经 _format_turn 在下一轮注入浮现（pull）：running / N findings / clean；skipped 不出。"""
    from domain.context import RepoContext, base, store
    G = "/tmp/dlut_revinj"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    ctx = RepoContext.refresh_all(G)
    R = ctx.repo.repo_dir   # save 用与注入侧 load 相同的路径，避开 /tmp→/private/tmp 软链不一致

    _rseg = store.branch_segment(ctx.branch.local.name, "review")   # review 是 branch 域段
    def seg(**kw): store.save_segment(R, _rseg, {"reviewed_sha": "abcdef1234567", "comments": [], "generated_at": 1.0, **kw})
    seg(status="success", count=2); assert "Review: 2 finding(s) on abcdef123" in ctx.turn_text()
    seg(status="success", count=0); assert "Review: clean (no findings) on abcdef123" in ctx.turn_text()
    seg(status="running", count=0, generated_at=base.now()); assert "Review: running on abcdef123" in ctx.turn_text()
    seg(status="running", count=0, generated_at=1.0); assert "Review: stale on abcdef123" in ctx.turn_text()  # 卡死的 running → stale 兜底
    seg(status="skipped", count=0); assert "Review:" not in ctx.turn_text()   # 噪声不进注入
    # 关键：completed_with_errors + 0 评论不再伪装 clean——失败诚实呈现
    seg(status="completed_with_errors", count=0, failed=3); assert "Review: 3 file(s) failed on abcdef123" in ctx.turn_text()
    seg(status="completed_with_errors", count=2, failed=1); t = ctx.turn_text()
    assert "2 finding(s)" in t and "1 file(s) failed" in t
    seg(status="error", count=0); assert "Review: review errored on abcdef123" in ctx.turn_text()


def test_review_line_told_once_per_result():
    """Review 是**事件**不是状态：同一个结果讲一遍就闭嘴（否则 agent 会反复 triage 同一批
    已处理的 findings），重跑出新结果（sha/status/计数变了）才再讲。且 PostCompact 不复活它
    ——compaction 掉的是「说过的话」，state 必须重说，事件重投则是让人重做已做的事。"""
    from domain.context import RepoContext, base, store
    G = "/tmp/dlut_revonce"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    ctx = RepoContext.refresh_all(G)
    R, branch = ctx.repo.repo_dir, ctx.branch.local.name
    _rseg = store.branch_segment(branch, "review")

    def seg(**kw): store.save_segment(R, _rseg, {"comments": [], "generated_at": 1.0, **kw})
    def tell() -> bool:
        c = RepoContext.load(R)
        said = "Review:" in c.turn_text()
        c.mark_turn_emitted(c.turn_text())
        return said

    seg(status="success", count=2, reviewed_sha="abcdef1234567")
    assert tell() is True                       # 第一次：讲
    assert tell() is False and tell() is False  # 同一个结果：不再讲

    seg(status="success", count=2, reviewed_sha="9999999999999")   # 重跑（新 sha）→ 新事件
    assert tell() is True and tell() is False

    seg(status="success", count=5, reviewed_sha="9999999999999")   # 同 sha 但结果不同 → 新事件
    assert tell() is True

    # running → success 是状态推进，也是新事件，各讲一次
    seg(status="running", count=0, reviewed_sha="aaaaaaaaaaaaa", generated_at=base.now())
    assert tell() is True and tell() is False
    seg(status="success", count=1, reviewed_sha="aaaaaaaaaaaaa")
    assert tell() is True

    # PostCompact 清 cadence 让 state 重注入，但不复活已投递的事件
    c = RepoContext.load(R); c.clear_injection_dedup()
    assert tell() is False

    # running → stale 必须还能讲：stale 是 *推导* 的（running 超时），段里的 status 一直是
    # running。若 key 认存的 status，这条「review 半路死了」就永远发不出来——而那正是
    # staleness 兜底存在的唯一理由。
    seg(status="running", count=0, reviewed_sha="bbbbbbbbbbbbb", generated_at=base.now())
    assert tell() is True and tell() is False                  # running 讲一次
    seg(status="running", count=0, reviewed_sha="bbbbbbbbbbbbb", generated_at=1.0)   # 超时 → stale
    c = RepoContext.load(R)
    assert "Review: stale" in c.turn_text()                    # 同一 status，但事件变了
    c.mark_turn_emitted(c.turn_text())
    assert tell() is False                                     # stale 也只讲一次


def test_label_pending_nudge_is_forge_derived():
    """待打标 nudge 的数来自 pr.json（forge 派生的 pending），不是 review.json 的 finding 数。
    两个关键性质：(1) review.json 说有 2 条 finding、但都标完了(pending=0) → 不再喊;
    (2) review.json 整个没了 / 被下轮覆盖 → nudge 照样在,因为 pending 锚在 forge 上。"""
    from domain.context import RepoContext, store
    G = "/tmp/dlut_labelnudge"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    ctx = RepoContext.refresh_all(G)
    R, branch = ctx.repo.repo_dir, ctx.branch.local.name

    def prseg(**kw): store.save_segment(R, "pr", {"branch": branch, "provider": "github", **kw})
    _rseg = store.branch_segment(branch, "review")
    def revseg(**kw): store.save_segment(R, _rseg, {"reviewed_sha": "abcdef1234567", "comments": [],
                                                    "generated_at": 1.0, **kw})

    revseg(status="success", count=2)
    prseg(label_pending=2); assert "2 条待打标" in RepoContext.load(R).turn_text()
    prseg(label_pending=0); assert "待打标" not in RepoContext.load(R).turn_text()   # 标完 → 不喊
    prseg(label_pending=None); assert "待打标" not in RepoContext.load(R).turn_text()  # 无开着的 MR / poll 失败

    # review.json 没了（下轮覆盖 / 换机器 / worktree 删了）→ nudge 仍在：MR 上没标的还挂着
    store.save_segment(R, _rseg, {})
    prseg(label_pending=3); t = RepoContext.load(R).turn_text()
    assert "3 条待打标" in t and "Review:" not in t

    # pr.json 是 branch-keyed：切走后不认（与 pr_number / merge_readiness 同一条约定）
    prseg(label_pending=3); store.save_segment(R, "pr", {"branch": "other", "label_pending": 3})
    assert "待打标" not in RepoContext.load(R).turn_text()


def test_label_nudge_decays_then_reopens_on_new_findings():
    """待打标是**要人干活**的行，不是状态行：同一批 finding 问满 LABEL_NUDGE_CAP 次就闭嘴
    （不理也是一种回答）；来了新的一批（pending set 变了）才重新开口。turn Cadence 顶不了
    这件事——它按整块 hash 去重，随便哪行状态一动就整块重发，chore 行会永远跟着喊。"""
    from domain.context import RepoContext, store
    from domain.context.base import LABEL_NUDGE_CAP
    G = "/tmp/dlut_nudgedecay"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    ctx = RepoContext.refresh_all(G)
    R, branch = ctx.repo.repo_dir, ctx.branch.local.name

    def prseg(**kw): store.save_segment(R, "pr", {"branch": branch, "provider": "github", **kw})

    def ask() -> bool:
        """一个 emit→mark 回合，返回这轮有没有喊。"""
        c = RepoContext.load(R)
        said = "待打标" in c.turn_text()
        c.mark_turn_emitted(c.turn_text())   # 真发出去了才记账（见 userprompt_inject）
        return said

    prseg(label_pending=3, label_pending_key="setA")
    assert [ask() for _ in range(LABEL_NUDGE_CAP)] == [True] * LABEL_NUDGE_CAP   # 问满 cap
    assert ask() is False and ask() is False                                     # 之后闭嘴

    # 新一轮 review 带来新 finding → set 变了 → 重新开口，且计数重来
    prseg(label_pending=5, label_pending_key="setB")
    assert [ask() for _ in range(LABEL_NUDGE_CAP)] == [True] * LABEL_NUDGE_CAP
    assert ask() is False

    # 标掉一条 → set 又变了 → 继续喊：agent 在推进，值得跟；闭嘴只针对「原地不动」
    prseg(label_pending=4, label_pending_key="setC")
    assert ask() is True

def test_launch_background_relays():
    """detach 起后台 relay：不抛、写 PLAN 行；空列表 no-op。"""
    sgo = _load_script("commit_flow")
    from domain.lifecycle import BackgroundSpec
    G = "/tmp/dlut_relay"; shutil.rmtree(G, ignore_errors=True); os.makedirs(f"{G}/.devloop")
    plan: list = []
    sgo.launch_background_relays([BackgroundSpec("review", ["python3", "-c", "pass"])], G, plan)
    assert any("launched in background" in p for p in plan)
    p2: list = []; sgo.launch_background_relays([], G, p2); assert p2 == []

def test_run_review_format_comment():
    """MR 评论格式化：clean / findings+failed。"""
    rr = _load_script("run_review")
    assert "无 findings" in rr._format_comment([], 0, "origin/main..HEAD", "abc1234567")
    out = rr._format_comment([{"path": "a.py", "start_line": 3, "end_line": 5, "content": "bug here"}],
                             2, "origin/main..HEAD", "abc1234567")
    assert "1 finding(s)" in out and "`a.py:3-5`" in out and "bug here" in out and "2 个文件未能 review" in out
    # 空 content 不留悬空破折号（ocr 自审挑出的 bug）
    out2 = rr._format_comment([{"path": "a.py", "start_line": 0, "end_line": 0, "content": ""}], 0, "r", "abc1234567")
    assert "- `a.py`" in out2 and "`a.py` —" not in out2
    # 多 model：alias 显示在 loc 后，便于跨 model 对比
    out3 = rr._format_comment([{"path": "a.py", "start_line": 1, "end_line": 1, "content": "x", "alias": "deepseek-v4-pro"}],
                              0, "r", "abc1234567")
    assert "`a.py:1-1` (deepseek-v4-pro) — x" in out3

def test_run_review_build_background():
    """--background 自动拼装：显式 extra + MR 标题/描述都进 background（commit 段在无 origin 的临时路径下为空）。"""
    rr = _load_script("run_review")

    class _PR:
        number, title = 7, "Add dark mode"

    class _Forge:
        def description(self, n): return "why: users asked for dark mode"

    bg = rr._build_background("/no/such/repo", "main", _Forge(), _PR(), "extra ctx")
    assert "extra ctx" in bg and "Add dark mode" in bg and "users asked for dark mode" in bg
    # 无 forge/pr → 只有 extra（commit 段在该假路径为空）
    assert rr._build_background("/no/such/repo", "main", None, None, "only extra").strip() == "only extra"

def test_run_review_append_history():
    """每次 review 终态追加一行 jsonl（append-only），含 ts/secs/status，且多次运行累积不覆盖。"""
    import json as _json
    import tempfile
    from pathlib import Path as _Path
    rr = _load_script("run_review")
    with tempfile.TemporaryDirectory() as d:
        rr._append_history(d, 100.0, status="success", sha="abc", count=3, failed=0)
        rr._append_history(d, 100.0, status="skipped", sha="def", count=0, failed=0)
        p = _Path(d) / ".devloop" / "review-history.jsonl"
        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2                       # 累积，不覆盖
        r0 = _json.loads(lines[0])
        assert r0["status"] == "success" and r0["count"] == 3 and r0["sha"] == "abc"
        assert "ts" in r0 and "secs" in r0           # 时间戳 + 时长，供按天统计
        assert _json.loads(lines[1])["status"] == "skipped"

def test_findings_for_history_status_from_warnings():
    """history findings 带 symbol_id + 状态：成功文件 ok、失败(超时)文件 failed+reason。"""
    rr = _load_script("run_review")
    comments = [
        {"path": "a.go", "symbol_id": "a.go::F", "content": "missing nil check"},
        {"path": "b.go", "symbol_id": "b.go::G", "content": "garbage from timeout"},
    ]
    warnings = [{"type": "subtask_error", "file": "b.go", "message": "context deadline exceeded"}]
    out = rr._findings_for_history(comments, warnings)
    assert out[0]["symbol_id"] == "a.go::F" and out[0]["status"] == "ok"
    assert out[1]["status"] == "failed" and "deadline" in out[1]["reason"]

def test_build_history_feed_filters_ok_and_keys_by_symbol():
    """回喂只取本 PR 上一轮的 ok findings、按 symbol-id keyed；failed / 无 symbol_id / 别的 PR 都跳过。"""
    import json as _json
    import tempfile
    from pathlib import Path as _Path
    rr = _load_script("run_review")
    with tempfile.TemporaryDirectory() as d:
        sd = _Path(d) / ".devloop"
        sd.mkdir()
        rows = [
            {"sha": "otherpr", "pr_number": 9, "findings": [{"symbol_id": "z.go::Z", "msg": "other", "status": "ok"}]},
            {"sha": "priorsha", "pr_number": 7, "findings": [
                {"symbol_id": "a.go::F", "msg": "missing nil check", "status": "ok"},
                {"symbol_id": "b.go::G", "msg": "garbage", "status": "failed", "reason": "timeout"},
                {"symbol_id": "", "msg": "no unit", "status": "ok"},
            ]},
        ]
        (sd / "review-history.jsonl").write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
        path = rr._build_history_feed(d, 7, "currentsha")
        assert path is not None
        data = _json.loads(_Path(path).read_text())
        assert list(data.keys()) == ["a.go::F"]            # only ok + has symbol_id, only this PR
        assert data["a.go::F"][0]["msg"] == "missing nil check"
        assert data["a.go::F"][0]["sha"] == "priorsha"     # the prior row's sha

def test_build_history_feed_none_when_no_pr_or_history():
    import tempfile
    from pathlib import Path as _Path
    rr = _load_script("run_review")
    with tempfile.TemporaryDirectory() as d:
        assert rr._build_history_feed(d, None, "sha") is None   # no PR -> no feed
        (_Path(d) / ".devloop").mkdir()
        assert rr._build_history_feed(d, 7, "sha") is None      # no history file -> None

def test_format_comment_shows_models():
    """review 级 model 身份打进 header（alias×次数、按 alias 排序去重），clean review 也打。"""
    rr = _load_script("run_review")
    head = rr._format_comment([], 0, "origin/main..HEAD", "abc1234567",
                              {"seed-2.1-turbo": 1, "deepseek-v4-pro": 2})
    assert "models: deepseek-v4-pro×2, seed-2.1-turbo×1" in head  # sorted by alias, with counts
    assert "clean" in head                                        # still the clean line
    assert "models:" not in rr._format_comment([], 0, "r", "s", {})  # no models -> no segment


def test_format_comment_shows_cost_and_tool():
    """引擎自报的 cost（整秒）与身份（`<engine> <version>`）打进 header；引擎没报（0 / 空）不打——
    ocr 不吐这两个字段时 header 自动退回旧形态。"""
    rr = _load_script("run_review")
    head = rr._format_comment([], 0, "origin/main..HEAD", "abc1234567",
                              {"seed-2.1-turbo": 1}, 200, "ccr v0.1.0")
    assert "cost: 200s" in head and "ccr v0.1.0" in head
    bare = rr._format_comment([], 0, "r", "s", {}, 0, "")
    assert "cost:" not in bare and "ccr" not in bare


def test_post_inline_findings():
    """findings 逐条发成 diff 锚点评论（换取 forge 原生 outdated 生命周期）：finding 自带粒度
    ——带行号的锚在末行，不带的（file-level finding）锚在文件上；连文件都没有的、forge 全拒的
    回落汇总；无 MR 原样回落。"""
    rr = _load_script("run_review")
    fake = _FakeForge([PullRequest(number=7, state="open")])
    pr = fake.get(7)
    comments = [
        {"path": "a.py", "start_line": 3, "end_line": 5, "alias": "m1", "content": "bug"},
        {"path": "b.py", "content": "file-level: 缺测试"},      # 无行号 → 本就是 file-level
        {"content": "no path at all"},                          # 无锚可锚 → 汇总
    ]
    n, fb = rr._post_inline(fake, pr, comments)
    assert n == 2 and [c.get("content") for c in fb] == ["no path at all"]
    assert fake.diff_posted[0][:3] == (7, "a.py", 5)            # line-level：锚在 end_line
    assert "m1" in fake.diff_posted[0][3] and "bug" in fake.diff_posted[0][3]
    assert fake.diff_posted[1][:3] == (7, "b.py", None)         # file-level：直接锚文件
    assert len(fake.diff_posted) == 2                           # 行锚成功 → 不会再补一条文件锚

    class Refusing(_FakeForge):
        def diff_comment(self, *a, **kw):
            raise ForgeError("no anchor")
    rf = Refusing([PullRequest(number=7, state="open")])
    n2, fb2 = rr._post_inline(rf, rf.get(7), comments)
    assert n2 == 0 and len(fb2) == 3                            # 全部回落，单条失败不致命
    assert rr._post_inline(None, None, comments) == (0, comments)   # 无 MR → 原样回落


def test_post_inline_degrades_line_to_file_anchor():
    """line-level finding 的行锚不上（context 行 finding：行不在当前 diff 里）→ 先退一级到
    文件锚,而不是直接掉进汇总。理由是可打标性:锚点评论才有 thread、能被回复 `ccr:label=`,
    汇总里只是一行文本。"""
    rr = _load_script("run_review")

    class LineRefusing(_FakeForge):
        """收文件锚、拒行锚——GitLab「行不在 diff」/ GitHub 422 的形状。"""
        def diff_comment(self, number, body, path, line=None):
            if line is not None:
                raise ForgeError("line not in diff")
            super().diff_comment(number, body, path, None)

    f = LineRefusing([PullRequest(number=7, state="open")])
    comments = [{"path": "a.py", "start_line": 3, "end_line": 5,
                 "content": "bug", "fingerprint": "fp1"}]
    n, fb = rr._post_inline(f, f.get(7), comments)
    assert (n, fb) == (1, [])                          # 没掉进汇总
    assert f.diff_posted[0][:3] == (7, "a.py", None)   # 退到文件级锚点
    assert "ccr:fp=fp1" in f.diff_posted[0][3]         # 指纹仍在 → 仍 join 得回 finding


def test_review_feedback_joins_fp_to_label():
    """review_feedback 把 finding comment（`ccr:fp=`）和它线程里的 `ccr:label=` 回复 join
    起来——join 键在 body 里、锚在 forge 上，不依赖任何本地状态。汇总 note 里也有 ccr:fp
    （每条回落 finding 一个），但它没锚点、没 thread，回不进去也就标不了，必须排除。"""
    from domain.forge import Comment
    from domain.review_feedback import Finding, findings, pending

    cs = [
        # 汇总 note：无 thread，body 里有多个 ccr:fp → 不是 published finding
        Comment(id="1", thread_id="", body="2 finding(s)\n- `a.py` `ccr:fp=aaa`\n- `b.py` `ccr:fp=bbb`"),
        Comment(id="20", thread_id="20", path="a.py", line=5, body="漏判空 <sub>ccr:fp=fp1</sub>"),
        Comment(id="21", thread_id="20", reply_to="20", body="ccr:label=wrong — 该路径走不到 #textbook"),
        Comment(id="30", thread_id="30", path="b.py", body="缺测试 <sub>ccr:fp=fp2</sub>"),   # file-level
    ]
    fs = findings(cs)
    assert [(f.fp, f.label) for f in fs] == [("fp1", "wrong"), ("fp2", "")]   # 汇总 note 未入列
    assert [f.fp for f in pending(cs)] == ["fp2"]
    assert fs[1].comment.path == "b.py" and fs[1].comment.line is None

    # 词表外的 verdict 视为未标——打错字必须显示成还没标,不能污染 ground truth
    typo = [Comment(id="40", thread_id="40", body="x <sub>ccr:fp=fp3</sub>"),
            Comment(id="41", thread_id="40", reply_to="40", body="ccr:label=importnat — oops")]
    assert [f.fp for f in pending(typo)] == ["fp3"]

    # pending_key 认 fp 集合、不认条数：标掉一条又新来一条 → 数没变但活变了 → key 必须变
    from domain.review_feedback import pending_key
    def _fs(*fps): return [Finding(fp=f, comment=Comment(id=f, thread_id=f)) for f in fps]
    assert pending_key(_fs("a", "b")) == pending_key(_fs("b", "a"))   # 顺序无关（两个面交织）
    assert pending_key(_fs("a", "b")) != pending_key(_fs("a", "c"))   # 同为 2 条,活不同
    # fingerprint 是稳定问题身份；同一问题在新 review 轮次重新发布时 comment id 会变，必须重开 nudge
    assert pending_key(_fs("a")) != pending_key(
        [Finding(fp="a", comment=Comment(id="new-round", thread_id="new-round"))])
    assert pending_key([]) == ""

    # 带 ccr:label 但不是回复（thread 根自己提了一嘴）→ 不算 verdict
    selfref = [Comment(id="50", thread_id="50", body="讲讲 ccr:label=wrong 的用法 <sub>ccr:fp=fp4</sub>")]
    assert [f.fp for f in pending(selfref)] == ["fp4"]


def test_format_comment_counts_inline():
    """汇总评论只列回落的 findings；总数 = 回落 + 锚上的，并标注锚上条数。回落项没有行号
    （file-level finding）时只渲染 path，不拼空的 `:0-0`。"""
    rr = _load_script("run_review")
    body = rr._format_comment([{"path": "b.py", "content": "left"}], 0, "r", "s", {}, 0, "",
                              inline_posted=2)
    assert "**3 finding(s)**" in body and "2 条已锚到 diff" in body and "b.py" in body
    assert "b.py:" not in body                  # 无行号 → 不拼 `:0-0`
    assert "clean" in rr._format_comment([], 0, "r", "s", {}, 0, "", inline_posted=0)


if __name__ == "__main__":
    run_main(globals())
