#!/usr/bin/env python3
"""code-review hook 的后台执行体：跑 `ocr review HEAD`，结果写 `.devloop/review.json`。

由 lifecycle 的 review signal hook 经 PLAN 的 `ARMED:` 行交给 agent 用 run_in_background 起
（post-commit、审 HEAD），跑完 harness 唤醒会话、agent 读 review.json 分级汇报（见
docs/code-review.md）。

advisory：从不挡 commit。ocr 没装 / LLM 没配好 → 写 `status=skipped` 退出 0，不报错。
stdout 打一行紧凑摘要——后台任务退出时 harness 把它带回会话（wake 那一轮先看到它）。

Usage: run_review.py [--repo R | R] [--background CTX]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import cli  # noqa: E402
from lib.context import base, record_active_repo  # noqa: E402


def _head_sha(repo: str) -> str:
    r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _write(repo: str, **fields) -> None:
    base.save_segment(repo, "review", fields)


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="run_review.py", description="ocr review HEAD → .devloop/review.json")
    cli.add_repo_arg(ap)
    ap.add_argument("--background", "-b", default=None, help="optional business/requirement context for ocr")
    ns = ap.parse_args(argv)
    resolved, _ = cli.resolve_repo_or_exit(ns, "run_review")
    repo = resolved.git_root
    record_active_repo(repo)
    sha = _head_sha(repo)
    # 先落一个 running 态：注入侧据此在下一轮显示「Review: running on <sha>」，跑完再覆盖。
    _write(repo, status="running", reviewed_sha=sha, comments=[], count=0, message="review in progress", generated_at=base.now())

    if not shutil.which("ocr"):
        _write(repo, status="skipped", reviewed_sha=sha, comments=[], count=0,
               message="ocr CLI not installed (npm i -g @alibaba-group/open-code-review)", generated_at=base.now())
        print("run_review: ocr not installed — skipped")
        return 0
    if subprocess.run(["ocr", "llm", "test"], cwd=repo, capture_output=True).returncode != 0:
        _write(repo, status="skipped", reviewed_sha=sha, comments=[], count=0,
               message="ocr LLM not configured (ocr config set llm.* or OCR_LLM_*)", generated_at=base.now())
        print("run_review: ocr LLM not configured — skipped")
        return 0

    cmd = ["ocr", "review", "--commit", "HEAD", "--format", "json", "--repo", repo]
    if ns.background:
        cmd += ["--background", ns.background]
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        _write(repo, status="error", reviewed_sha=sha, comments=[], count=0,
               message=(r.stderr or r.stdout or "ocr produced no JSON")[-2000:], generated_at=base.now())
        print(f"run_review: ocr output not parseable (rc={r.returncode}) — see .devloop/review.json")
        return 0

    comments = out.get("comments") or []
    _write(repo, status=out.get("status", "success"), reviewed_sha=sha, comments=comments,
           count=len(comments), message=out.get("message", ""), generated_at=base.now())
    print(f"run_review: {len(comments)} comment(s) on {sha[:9]} → .devloop/review.json "
          f"(status={out.get('status', 'success')}). Read it and report by priority.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
