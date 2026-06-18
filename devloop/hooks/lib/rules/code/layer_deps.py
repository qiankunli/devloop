"""LayerDepsRule —— 层级依赖方向（lint-deps）：低层文件不得 import 更高层。

判定不看调用点、看 import：依赖方向的权威信号是 import 路径。
方向由 `arch.order`（前=上层）定；`arch.layers`（路径片段→层名）定文件属哪层。
默认 `arch.enabled=False`（opt-in per repo），避免装上就按猜的层映射误拦任何仓。
"""
from __future__ import annotations

from lib.core.domain import FileChange, Finding, Severity, TargetKind
from lib.core.protocol import Rule


class LayerDepsRule(Rule):
    name = "layer-deps"
    target_kind = TargetKind.FILE_CHANGE
    needs_content = True  # 要 imports → 触发引擎惰性解析

    def applies(self, target: FileChange, ctx) -> bool:
        arch = getattr(ctx, "arch", None) or {}
        return bool(arch.get("enabled")) and bool(arch.get("order"))

    def check(self, target: FileChange, ctx) -> list[Finding]:
        order = (ctx.arch or {}).get("order") or []
        rank = {name: i for i, name in enumerate(order)}  # 越小=越上层
        if target.layer not in rank:
            return []  # 文件不在任何已知层 → 不管
        out: list[Finding] = []
        for imp in target.imports or []:
            tl = imp.target_layer
            if tl in rank and tl != target.layer and rank[tl] < rank[target.layer]:
                out.append(
                    Finding(
                        rule=self.name,
                        severity=Severity.DENY,
                        message=(
                            f"❌ 层级违规：{target.layer} 层文件 {target.path} 不应依赖更高层 {tl}"
                            f"（import: {imp.raw}）。{target.layer} 只能依赖更低层；跨层编排上移到 {tl} 层。"
                        ),
                        locator=target.path,
                    )
                )
        return out
