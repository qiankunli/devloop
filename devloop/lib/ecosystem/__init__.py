"""工具链生态注册表：devloop 认识哪些生态，这个目录就是答案（加生态 = 加文件 + 注册一行）。

主流程对语言差异的**唯一入口**：身份判据（`detect`）、语言探测（`detect_language`）、
环境就绪（`ensure_ready`）都从这里走——别处不要再散写 manifest 文件名匹配 / 生态命令。
契约与边界（make-first、两不变量）见 `base.py`。
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from .base import Ecosystem
from .golang import GoEcosystem
from .node import NodeEcosystem
from .python_uv import PythonEcosystem

# 顺序 = detect / detect_language 的探测优先级（python → go → node）。
# 同目录多 manifest 的仓极少见，谁先命中谁答。
ECOSYSTEMS: tuple[Ecosystem, ...] = (PythonEcosystem(), GoEcosystem(), NodeEcosystem())

_PREPARE_TIMEOUT = 600   # 冷 cache 的首次 install 可达分钟级；到顶仍不结束按环境错误报
_LOCKS_GUARD = threading.Lock()
_READY_LOCKS: dict[str, threading.Lock] = {}


def _ready_lock(path: str | Path) -> threading.Lock:
    """同一进程内每个 component 一把 single-flight 锁。

    lifecycle 会并发跑 lint/test；两边同时发现冷环境时，不能并发写同一份 node_modules/.venv。
    锁内必须重查 ready：先拿锁的一方 prepare 完，后拿锁的一方应直接返回。
    """
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        return _READY_LOCKS.setdefault(key, threading.Lock())


def detect(path: str | Path) -> Ecosystem | None:
    """`path` 这个 component 属于哪个生态（按身份判据 manifest）。None = 不认识。"""
    p = Path(path)
    for eco in ECOSYSTEMS:
        if any((p / m).exists() for m in eco.manifests):
            return eco
    return None


def detect_language(path: str | Path) -> str | None:
    """`path` 的展示语言。比 `detect` 宽（允许 requirements.txt 这类语言线索）——
    「这是什么语言」和「这是不是一个项目」是两个问题。"""
    p = Path(path)
    for eco in ECOSYSTEMS:
        if eco.matches_language(p):
            return eco.language(p)
    return None


def ensure_ready(path: str | Path) -> str | None:
    """把 `path` 的依赖环境带到就绪态（自愈式）：就绪/无从判断 → None；不就绪 → 跑一次
    生态 prepare（frozen 语义），成功盖指纹返回 None，失败返回原因（**环境错误**——消费方
    要把它与代码检查失败区分呈现，否则 agent 会去改代码修一个环境问题）。

    worktree 创建（worktree）与 gate（lifecycle.checks）共用这一个入口——
    正常路径与守卫路径必须是同一份策略。"""
    eco = detect(path)
    if eco is None:
        return None
    with _ready_lock(path):
        problem = eco.env_problem(path)
        if problem is None:
            return None
        cmd = eco.prepare_command(path)
        if cmd is None:
            return problem   # 确定有病但没有 frozen 恢复路径（如 Node 仓没 lockfile）
        try:
            r = subprocess.run(cmd, cwd=str(path), capture_output=True, text=True,
                               timeout=_PREPARE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"{problem}; auto-prepare `{' '.join(cmd)}` did not run: {e}"
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).splitlines()[-15:])
            return f"{problem}; auto-prepare `{' '.join(cmd)}` failed (rc={r.returncode}):\n{tail}"
        try:
            eco.mark_prepared(path)
        except OSError as e:
            return f"auto-prepare `{' '.join(cmd)}` passed but its environment fingerprint could not be written: {e}"
        if remaining := eco.env_problem(path):
            return f"auto-prepare `{' '.join(cmd)}` passed but the environment is still not ready: {remaining}"
        return None
