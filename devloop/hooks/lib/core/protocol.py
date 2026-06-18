"""Rule 契约——可积累的判定单元。base class + 安全默认，具体规则在 `lib/rules/` 里 override。
房子风格对齐 loop-harness/middleware（base class 默认 no-op，子类覆写）。"""
from __future__ import annotations

from enum import Enum

from .domain import Finding, Target, TargetKind


class FailurePolicy(str, Enum):
    FAIL_OPEN = "fail_open"  # 规则抛异常 → 放行（devloop 铁律：守卫的 bug 绝不拦用户）
    FAIL_CLOSED = "fail_closed"


class Rule:
    """一条规则：声明关心哪类 target，给出 applies(场景门) 与 check(判定)。

    匹配 = `target_kind` + `applies()`（= skill+ 的 when(state) / k8s webhook 的 rule scope）。
    `needs_content=True` 的 FileChange 规则会触发引擎惰性解析 imports/decls。
    """

    name: str = "rule"
    target_kind: TargetKind = TargetKind.CHANGE
    needs_content: bool = False

    def applies(self, target: Target, ctx) -> bool:
        """场景门：默认对该 kind 的所有 target 生效。子类按 ctx/target 细化（如按 subcommand）。"""
        return True

    def check(self, target: Target, ctx) -> list[Finding]:
        """读 target + ctx，产出 Finding（空=无违规）。子类实现。"""
        return []

    def failure_policy(self) -> FailurePolicy:
        return FailurePolicy.FAIL_OPEN
