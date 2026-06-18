"""PolicyContext —— 只读门面，包住 devloop 的 `.devloop/` 状态总线 + repo 事实。

Rule 只依赖这个对象（不各自 import repo_layout/gate/session/config）→ 纯函数、可测：
测试里塞一个带相同属性的假对象即可。对应架构图里的紫色总线——Rule 读它，绝不写它。

当前只暴露代码侧规则要用的少数事实（git_root / repo_code_dir / language / arch）；
命令侧/编辑侧 guard 迁入时再按需补 branch / owner / validation 等 accessor。
"""
from __future__ import annotations

import functools

from lib import config, repo_layout


class PolicyContext:
    def __init__(self, cwd: str, anchor_path: str = ""):
        # anchor_path：被编辑文件的路径（Edit/Write）。聚合工作区里 cwd 常停在 workspace 根，
        # 编辑却落在子项目内——必须从文件路径解析 repo，而非 cwd（同 edit-family 既有约定）。
        self._cwd = cwd or ""
        self._anchor = anchor_path or cwd or ""

    @functools.cached_property
    def git_root(self) -> str | None:
        return repo_layout.find_git_root(self._anchor) if self._anchor else None

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
