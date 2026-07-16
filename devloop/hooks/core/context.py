"""PolicyContext —— 只读门面，包住 devloop 的 `.devloop/` 状态总线 + repo 事实。

Rule 只依赖这个对象（不各自 import repo_layout/gate/session/config）→ 纯函数、可测：
测试里塞一个带相同属性的假对象即可。对应架构图里的紫色总线——Rule 读它，绝不写它。

当前只暴露代码侧规则要用的少数事实（git_root / repo_code_dir / language / arch）；
命令侧/编辑侧 guard 迁入时再按需补 branch / owner / validation 等 accessor。
"""
from __future__ import annotations

import functools
from pathlib import Path

from lib import config, repo_layout
from hooks.core.domain import FileChange, Target


class PolicyContext:
    def __init__(self, cwd: str, anchor_path: str = "", session_id: str = ""):
        # anchor_path：被编辑文件的路径（Edit/Write）。聚合工作区里 cwd 常停在 workspace 根，
        # 编辑却落在子项目内——必须从文件所在目录解析 repo，而非 cwd（同 edit-family 既有约定）。
        self._cwd = cwd or ""
        self.session_id = session_id or ""
        if anchor_path:  # edit 族：记文件绝对路径 + 它所在目录（git_root 从目录解析，`git -C <文件>` 会失败）
            f = anchor_path if Path(anchor_path).is_absolute() else str(Path(self._cwd) / anchor_path)
            self._anchor_file = f
            self._anchor_dir = str(Path(f).parent)
        else:  # 命令族：无文件，git_root 从 cwd 解析
            self._anchor_file = ""
            self._anchor_dir = self._cwd

    @property
    def cwd(self) -> str:
        return self._cwd

    def for_target(self, target: Target) -> PolicyContext:
        """Return the policy view anchored to the target being evaluated.

        A single ``apply_patch`` can contain files below an aggregate workspace or even span
        repositories. Repo-scoped rules must therefore resolve ownership from each file target,
        not from the session cwd or one call-level anchor.
        """
        if isinstance(target, FileChange):
            return PolicyContext(self._cwd, anchor_path=target.path, session_id=self.session_id)
        return self

    @property
    def anchor_abspath(self) -> str:
        """被编辑文件的绝对路径（edit 族规则做 gitignore / 路径判断用）。"""
        return self._anchor_file

    @functools.cached_property
    def git_root(self) -> str | None:
        return repo_layout.find_git_root(self._anchor_dir) if self._anchor_dir else None

    @functools.cached_property
    def repo_code_dir(self) -> str | None:
        return repo_layout.find_repo_code_dir(self.git_root) if self.git_root else None

    @functools.cached_property
    def language(self) -> str | None:
        return repo_layout.detect_language(self.repo_code_dir) if self.repo_code_dir else None

    @functools.cached_property
    def arch(self) -> dict:
        """层级/架构规则配置（layer 映射 + 方向序 + 开关），按 repo 解析、分层覆盖。"""
        return config.arch(self.git_root)
