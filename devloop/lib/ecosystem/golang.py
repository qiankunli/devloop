"""Go 生态：列进来主要是为了说明它**为什么不需要 prepare**。"""
from __future__ import annotations

from .base import Ecosystem


class GoEcosystem(Ecosystem):
    """Go 对 worktree 环境问题天然免疫：module cache 全局共享（$GOMODCACHE），仓内没有
    依赖目录，`go build/test` 按 go.mod+go.sum 自行解析——不存在"环境没准备好"这回事，
    也就没有跨 checkout 泄漏面。prepare/env_problem 用基类的中性默认值。"""

    name = "go"
    manifests = ("go.mod",)

    def fallback_test_command(self, path):
        # 纯 Go module 无 Makefile 也能测——避免多 unit 仓已正确识别 Go unit 却被误跳过
        # （原 CodeUnit.test_command 的回落，迁到生态：这是 Go 的事实，不是 unit 模型的）。
        return ("go", "test", "./...")
