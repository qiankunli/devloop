#!/usr/bin/env python3
"""code-review hook 的后台执行体：跑 `ocr` 审整条 MR 的全量改动，结果写 `.devloop/review.json`
并作为一条评论**发到该分支的 MR 上**——让 review 历史挂在 MR 上、可跟踪对比。

由 lifecycle 的 `review` signal hook（挂 `post_mr`）经 detach 起（见 docs/code-review.md）。审
`origin/<target>..HEAD`（整条分支 vs target）；查到分支的开放 MR 就发评论，没有就只落 review.json。

advisory：从不挡 commit。ocr 没装 / LLM 没配好 → 写 `status=skipped` 退出 0，不报错。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))

from lib import cli, git_state  # noqa: E402
from lib.context import base, record_active_repo  # noqa: E402
from lib.forge import ForgeError, forge_for_repo, pr_label  # noqa: E402

_MAX_COMMENT_FINDINGS = 30   # 评论里最多列几条，避免超长 MR 评论


def _head_sha(repo: str) -> str:
    r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _branch(repo: str) -> str:
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _write(repo: str, **fields) -> None:
    base.save_segment(repo, "review", fields)


def _format_comment(comments: list, failed: int, range_label: str, sha: str) -> str:
    """把 ocr 结果格式化成一条 MR 评论(markdown)。run_review 自主发,无 agent 参与,故在此成文;
    优先级分级是 agent 在会话里做的事,这条历史评论只如实列出 ocr 的原始 findings。"""
    head = f"🤖 **devloop code-review** · `{range_label}` · `{sha[:9]}`"
    if not comments and not failed:
        return f"{head}\n\n✅ 无 findings(clean)。"
    bits = []
    if comments:
        bits.append(f"**{len(comments)} finding(s)**")
    if failed:
        bits.append(f"⚠️ {failed} 个文件未能 review(LLM 超时 / token 超限等)")
    lines = [head, "", " · ".join(bits), ""]
    for c in comments[:_MAX_COMMENT_FINDINGS]:
        loc = c.get("path", "?")
        s, e = c.get("start_line", 0), c.get("end_line", 0)
        if s or e:
            loc += f":{s}-{e}"
        body = (c.get("content") or "").strip().replace("\n", " ")
        lines.append(f"- `{loc}` — {body[:300]}" if body else f"- `{loc}`")   # 空 content 不留悬空破折号
    if len(comments) > _MAX_COMMENT_FINDINGS:
        lines.append(f"- … 另有 {len(comments) - _MAX_COMMENT_FINDINGS} 条,见 `.devloop/review.json`")
    return "\n".join(lines)


def _post_to_mr(repo: str, branch: str, body: str) -> str:
    """把评论发到 `branch` 对应的开放 MR;返回一句状态(供 stdout / review.json)。"""
    if not branch:
        return "branch unresolved — MR comment skipped"   # 空分支别去 prs_for_branch("")
    forge = forge_for_repo(repo)
    if forge is None:
        return "no forge/token — MR comment skipped"
    try:
        pr = next((p for p in forge.prs_for_branch(branch) if p.is_open), None)
        if pr is None:
            return f"no open MR for {branch} — comment skipped"
        forge.comment(pr.number, body)
        return f"posted to {pr_label(forge.provider, pr.number)}"
    except ForgeError as e:
        return f"MR comment failed (non-fatal): {e}"


def main(argv: list[str]) -> int:
    ap = cli.ArgParser(prog="run_review.py", description="ocr review the MR diff → .devloop/review.json + MR comment")
    cli.add_repo_arg(ap)
    ap.add_argument("--background", "-b", default=None, help="optional business/requirement context for ocr")
    ns = ap.parse_args(argv)
    resolved, _ = cli.resolve_repo_or_exit(ns, "run_review")
    repo = resolved.git_root
    record_active_repo(repo)
    sha = _head_sha(repo)
    _write(repo, status="running", reviewed_sha=sha, comments=[], count=0, message="review in progress", generated_at=base.now())

    def skip(msg: str) -> int:
        _write(repo, status="skipped", reviewed_sha=sha, comments=[], count=0, failed=0, message=msg, generated_at=base.now())
        print(f"run_review: {msg} — skipped")
        return 0

    if not shutil.which("ocr"):
        return skip("ocr CLI not installed (npm i -g @alibaba-group/open-code-review)")
    if subprocess.run(["ocr", "llm", "test"], cwd=repo, capture_output=True).returncode != 0:
        return skip("ocr LLM not configured (ocr config set llm.* or OCR_LLM_*)")

    target = git_state.get_default_target(repo)
    range_label = f"origin/{target}..HEAD"
    cmd = ["ocr", "review", "--from", f"origin/{target}", "--to", "HEAD", "--format", "json", "--repo", repo]
    if ns.background:
        cmd += ["--background", ns.background]

    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        _write(repo, status="error", reviewed_sha=sha, comments=[], count=0, failed=0,
               message=(r.stderr or r.stdout or "ocr produced no JSON")[-2000:], generated_at=base.now())
        print(f"run_review: ocr output not parseable (rc={r.returncode}) — see .devloop/review.json")
        return 0

    comments = out.get("comments") or []
    warnings = out.get("warnings") or []
    failed = sum(1 for w in warnings if isinstance(w, dict) and w.get("type") == "subtask_error")
    posted = _post_to_mr(repo, _branch(repo), _format_comment(comments, failed, range_label, sha))
    _write(repo, status=out.get("status", "success"), reviewed_sha=sha, comments=comments,
           count=len(comments), failed=failed, warnings=warnings, message=out.get("message", ""),
           reviewed_range=range_label, mr_comment=posted, generated_at=base.now())
    print(f"run_review: {len(comments)} comment(s), {failed} file(s) failed on {range_label}"
          + (f" · {posted}" if posted else "") + " → .devloop/review.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
