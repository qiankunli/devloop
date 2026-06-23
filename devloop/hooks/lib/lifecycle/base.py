"""生命周期 hook facade 核心：dispatch + 结果类型 + 内置注册表。纯机制。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import import_module
from typing import Callable

PHASES = ("pre_commit", "post_commit", "pre_mr", "post_mr")

# 内置 hook 注册表：name → "module:function"。**惰性**解析（dispatch 时才 import）：handler
# 模块（lib.lifecycle.checks）要 `from lib.lifecycle.base import HookResult`，模块级直接引用会
# 成 import 环。
_BUILTIN: dict[str, str] = {
    "lint": "lib.lifecycle.checks:lint",
    "test": "lib.lifecycle.checks:test",
}


@dataclass(frozen=True)
class BackgroundSpec:
    """signal hook 的下游：一条由 agent/harness 起到后台、跑完唤醒 session 的命令。

    `dispatch` 自己**不能**起它——subprocess 派生的子进程 harness 不跟踪、跑完不会 re-invoke
    会话。故 signal hook 把这条 spec 作为 `HookResult.relay` 返回，由调用方（smart_git_ops →
    PLAN 的 `ARMED:` 行 → agent 用 run_in_background 起）落地。
    """
    name: str
    argv: list[str]
    note: str = ""


@dataclass(frozen=True)
class HookResult:
    name: str
    ok: bool                              # gate：inline hook 的通过与否；signal hook 恒 True
    summary: str = ""
    relay: BackgroundSpec | None = None   # 非 None = signal hook，需 agent 起其后台下游


@dataclass(frozen=True)
class DispatchResult:
    phase: str
    results: list[HookResult]

    @property
    def proceed(self) -> bool:
        """是否放行下一步（commit / mr）。任一 inline gate fail 即 False；signal hook 恒不挡。"""
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> list[HookResult]:
        return [r for r in self.results if not r.ok]

    @property
    def to_launch(self) -> list[BackgroundSpec]:
        """需调用方起到后台的 signal 下游。"""
        return [r.relay for r in self.results if r.relay is not None]


def resolve_handler(name: str) -> Callable[[str], HookResult] | None:
    """name → handler 可调用（`handler(repo) -> HookResult`）；未注册返回 None。"""
    spec = _BUILTIN.get(name)
    if not spec:
        return None
    mod, fn = spec.split(":", 1)
    return getattr(import_module(mod), fn)


def dispatch(
    phase: str,
    repo: str,
    *,
    names: list[str] | None = None,
    registry: dict[str, Callable[[str], HookResult]] | None = None,
    max_workers: int = 4,
) -> DispatchResult:
    """跑 `phase` 上配置的全部 hook（并发 join），聚合成 DispatchResult。

    `names` 省略时从 `config.lifecycle(repo)[phase]` 读（opt-in，默认空 → no-op）。
    `registry` 仅测试用：注入 name→handler，绕过内置惰性解析。

    两条 fail 语义：
    - **未知 hook 名** → 一条 `ok=False` 的 HookResult（配置写错要看得见，不静默吞）。
    - **handler 抛异常** → 收敛成 `ok=False`（**gate fail-closed**：把关出错按未通过处理，
      宁可挡不可漏）。signal hook 要保证「永不挡」须自己 catch 内部异常、恒返回 `ok=True`——
      这是 handler 的契约，不是 dispatcher 的事。
    """
    if phase not in PHASES:
        raise ValueError(f"unknown lifecycle phase: {phase!r} (one of {PHASES})")
    if names is None:
        from lib import config
        names = list(config.lifecycle(repo).get(phase) or [])
    if not names:
        return DispatchResult(phase=phase, results=[])

    def _run(name: str) -> HookResult:
        handler = (registry or {}).get(name) or resolve_handler(name)
        if handler is None:
            return HookResult(name=name, ok=False, summary=f"unknown lifecycle hook {name!r}")
        try:
            return handler(repo)
        except Exception as e:  # gate fail-closed
            return HookResult(name=name, ok=False, summary=f"{name} errored: {e}")

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(names)))) as ex:
        results = list(ex.map(_run, names))   # ex.map 保序，结果与 names 对齐
    return DispatchResult(phase=phase, results=results)
