#!/usr/bin/env python3
"""code-review 链路：引擎解析、run_review 成文/回喂 history、注入行、后台启动。

Standalone: `python3 devloop/tests/test_review.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _git, _load_script, run_main  # noqa: E402  (bootstrap first)


def test_run_review_skips_without_engine():
    """引擎没装（默认 ccr）→ run_review 写 status=skipped、退出 0（advisory，从不报错/挡事）。"""
    from lib.context import base
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
    seg = base.load_segment(G, "review")
    assert seg and seg["status"] == "skipped" and "not installed" in seg["message"] and seg["count"] == 0

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
    from lib.context import RepoContext, base
    G = "/tmp/dlut_revinj"; shutil.rmtree(G, ignore_errors=True); os.makedirs(G)
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    Path(f"{G}/a.txt").write_text("x"); _git(G, "add", "-A"); _git(G, "commit", "-q", "-m", "init")
    ctx = RepoContext.refresh_all(G)
    R = ctx.repo.repo_dir   # save 用与注入侧 load 相同的路径，避开 /tmp→/private/tmp 软链不一致

    def seg(**kw): base.save_segment(R, "review", {"reviewed_sha": "abcdef1234567", "comments": [], "generated_at": 1.0, **kw})
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

def test_launch_background_relays():
    """detach 起后台 relay：不抛、写 PLAN 行；空列表 no-op。"""
    sgo = _load_script("smart_git_ops")
    from lib.lifecycle import BackgroundSpec
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


if __name__ == "__main__":
    run_main(globals())
