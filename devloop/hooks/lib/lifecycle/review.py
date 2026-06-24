"""code-review signal hook —— armed 一个后台 ocr review，结果落 `.devloop/review.json`。

这是 **signal hook**：不在 dispatch（subprocess）里跑 ocr——那会挡住 commit，且 subprocess
派生的子进程 harness 不跟踪、跑完不唤醒会话。它返回带 `relay` 的 HookResult；relay 描述的命令
由 agent 用 run_in_background 起（post-commit、审 HEAD），跑完 harness 唤醒会话读 review.json
分级汇报（见 docs/code-review.md）。

advisory：恒 `ok=True`、带 relay，从不挡 commit（信号发成功就是过，真活还没跑、无可 veto）。
"""
from __future__ import annotations

from lib import config
from lib.lifecycle.base import BackgroundSpec, HookResult


def review(repo: str) -> HookResult:
    """post_mr 用：detach 起 run_review——审整条 MR 的全量改动（origin/<target>..HEAD），
    结果写 review.json + 发评论到该分支的 MR 上做历史。"""
    script = str(config.plugin_root() / "scripts" / "run_review.py")
    spec = BackgroundSpec("review", ["python3", script, "--repo", repo],
                          note="ocr review origin/<target>..HEAD → .devloop/review.json + MR comment")
    return HookResult("review", ok=True, summary="armed background MR code-review", relay=spec)
