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
    "review": "lib.lifecycle.review:review",   # signal hook：后台审全量改动、写 review.json + 有开放 MR 时发评论（挂哪相位由 config 决定）
}


@dataclass(frozen=True)
class BackgroundSpec:
    """signal hook 的下游：一条跑到后台的命令。

    `dispatch` 自己**不能**起它——subprocess 派生的子进程 harness 不跟踪、跑完不会 re-invoke
    会话。故 signal hook 把这条 spec 作为 `HookResult.relay` 返回，由调用方 `smart_git_ops`
    在所裹的 git 动作后用 `Popen(start_new_session=True)` detach 起（fire-and-forget，结果经
    状态总线下轮浮现，不靠 wake）。
    """
    name: str
    argv: list[str]
    note: str = ""


@dataclass(frozen=True)
class HookResult:
    """一次 hook 执行的结果。三种 hook 模式由两个字段编码（见 docs/lifecycle-hooks.md）：

    - **硬拦截**（gate，如 lint）：`relay=None, advisory=False`——`ok=False` 阻断 commit/MR。
    - **软提示**（advisory，如 test）：`relay=None, advisory=True`——同步跑、本轮可见，但 `ok=False`
      只**通报不阻断**（失败常因基线坏测 / 环境，与本次 diff 无关；真正的判断 CI / 人来做）。
    - **异步信号**（signal，如 review）：`relay=BackgroundSpec`——detach 起后台、永不阻断、下轮浮现。
    """
    name: str
    ok: bool                              # inline hook 的通过与否；signal hook 恒 True
    summary: str = ""
    relay: BackgroundSpec | None = None   # 非 None = signal hook，需 agent 起其后台下游
    advisory: bool = False                # True = 软提示：ok=False 时通报但不阻断（不进 proceed）


@dataclass(frozen=True)
class DispatchResult:
    phase: str
    results: list[HookResult]

    @property
    def proceed(self) -> bool:
        """是否放行下一步（commit / mr）。只有**硬拦截** gate 的 fail 才挡；advisory（软提示）与
        signal（异步）永不阻断。"""
        return all(r.ok for r in self.results if not r.advisory and r.relay is None)

    @property
    def failures(self) -> list[HookResult]:
        return [r for r in self.results if not r.ok]

    @property
    def blocking_failures(self) -> list[HookResult]:
        """真正阻断的失败（硬拦截 gate）——用于中止 + 报错详情。"""
        return [r for r in self.results if not r.ok and not r.advisory and r.relay is None]

    @property
    def advisory_failures(self) -> list[HookResult]:
        """软提示的失败——通报但不阻断。"""
        return [r for r in self.results if not r.ok and r.advisory]

    @property
    def to_launch(self) -> list[BackgroundSpec]:
        """需调用方起到后台的 signal 下游。"""
        return [r.relay for r in self.results if r.relay is not None]


def resolve_handler(name: str) -> Callable[..., HookResult] | None:
    """name → handler 可调用（`handler(repo, paths) -> HookResult`）；未注册返回 None。"""
    spec = _BUILTIN.get(name)
    if not spec:
        return None
    mod, fn = spec.split(":", 1)
    return getattr(import_module(mod), fn)


def dispatch(
    phase: str,
    repo: str,
    *,
    paths: list[str] | None = None,
    names: list[str] | None = None,
    registry: dict[str, Callable[..., HookResult]] | None = None,
    max_workers: int = 4,
) -> DispatchResult:
    """跑 `phase` 上配置的全部 hook（并发 join），聚合成 DispatchResult。

    `paths` = **本相位「本次改动」涉及的文件**（仓相对），由调用方在相位边界算好后**冻结**下传。
    这是 handler 拿不到、也猜不出来的那半上下文：每个相位的答案不同（pre_commit 是将提交的脏
    文件、post_commit 是刚落地那个 commit、pre_mr 是整条分支 vs target），而 handler 手里只有
    `repo`，只能去读工作树——commit 之后工作树是干净的，那个答案会被读成「不知道范围」进而退化
    成跑全仓。冻结还顺带让同相位的 lint 与 test 看到**同一个**范围：lint 的 `make fix` 会改工作
    树，各自现算就可能算出两个不同的集合。
    `None` = 调用方也不知道范围，由 handler 自行判定（保持 CLI 语境的老行为）。

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
            # `paths=` 按**关键字**传：handler 各自决定把它声明成位置参数（`review`）还是
            # keyword-only（`lint`/`test` 把它放在 `*,` 之后，与 capture/unit 同列）。位置传在
            # 后者上炸 TypeError，而 gate 的 fail-closed 会把异常收敛成 ok=False——那不是崩，
            # 是**静默挡掉每一次 commit**。契约规定的是参数名，不是它的位置。
            return handler(repo, paths=paths)
        except Exception as e:  # gate fail-closed
            return HookResult(name=name, ok=False, summary=f"{name} errored: {e}")

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(names)))) as ex:
        results = list(ex.map(_run, names))   # ex.map 保序，结果与 names 对齐
    return DispatchResult(phase=phase, results=results)
