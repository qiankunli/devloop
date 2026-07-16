"""Python 生态（uv 基线）：venv 归属 + editable 回指是它特有的泄漏面。

为什么 ready 校验先看 `.venv` 存在：uv 是自锚定的——`uv run` 向上找 pyproject.toml、
用项目目录自己的 `.venv`，`VIRTUAL_ENV` 指向别处时忽略并告警；devloop 自己 sync 过的环境
再用 manifest+lockfile 指纹校验一致性。真正致命的是 `.venv` **整个缺失**时 shell 里残留的
主 checkout venv 接管执行：editable install（uv sync 默认）在 site-packages 里硬编码
**创建时的源码绝对路径**，于是 import 到的是另一 checkout 的代码——测试全绿但测错了树。
也因此 Python 没有 Node 那种 "lockfile 一致就 symlink" 的 fast path：venv 的脚本
shebang / editable 路径都绑死创建位置，共享 = 必然回指。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .base import Ecosystem


class PythonEcosystem(Ecosystem):
    name = "python"
    # setup.py 是 legacy 项目边界；requirements.txt 刻意不进（依赖清单≠项目边界，
    # 常见形态是仓根放一份给容器构建、真项目在 server/——认它就把仓根误判成 component）。
    manifests = ("pyproject.toml", "setup.py")

    def matches_language(self, path):
        # requirements.txt 仍是**语言**线索（这是什么语言 ≠ 这是不是项目）。
        return super().matches_language(path) or (Path(path) / "requirements.txt").exists()

    @staticmethod
    def is_uv_managed(path: str | Path) -> bool:
        """uv 管理的判据 = pyproject + uv.lock **同目录**。guard（pip_install）与 prepare
        共用这一个谓词——判断散两份，守卫就会和真实路径打架。"""
        p = Path(path)
        return (p / "pyproject.toml").exists() and (p / "uv.lock").exists()

    def prepare_command(self, path):
        # 只给 uv 仓 prepare：legacy pip 仓没有 frozen 语义可用，猜一条 install 命令
        # 反而把"环境错误"变成"devloop 搞坏了我的环境"。绿地基线是 uv，不为 legacy 修路。
        return ["uv", "sync", "--frozen"] if self.is_uv_managed(path) else None

    def env_problem(self, path):
        if not self.is_uv_managed(path):
            return None
        venv = Path(path) / ".venv"
        if not venv.is_dir():
            return (".venv missing — a stale VIRTUAL_ENV would silently run another "
                    "checkout's venv (editable install points at its source tree)")
        marker = venv / ".devloop-envhash"
        if not marker.exists():
            return None                      # 用户自装、无指纹：fail-open，不逼重装
        try:
            prepared = marker.read_text(encoding="utf-8").strip()
        except OSError as e:
            return f"cannot read devloop environment fingerprint: {e}"
        if prepared != _env_hash(path):
            return "pyproject.toml or uv.lock changed since devloop last synced (stale .venv)"
        return None

    def mark_prepared(self, path):
        venv = Path(path) / ".venv"
        if self.is_uv_managed(path) and venv.is_dir():
            (venv / ".devloop-envhash").write_text(_env_hash(path), encoding="utf-8")


def _env_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    for f in (Path(path) / "pyproject.toml", Path(path) / "uv.lock"):
        h.update(f.name.encode())
        h.update(b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()
