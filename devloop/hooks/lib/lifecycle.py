"""devops 生命周期 hook facade —— pre_commit / post_commit / pre_mr / post_mr。

**为什么存在**：lint / test / code-review /（将来）e2e·eval·perf verdict 形状相同——
都是「在某个 git 生命周期相位触发的一个验证 / 动作」。今天它们各自 ad-hoc 接线
（lint 的 gate 是一条 PreToolUse 规则、/lint 是手动命令…）。CC 原生事件只到工具层
（`PreToolUse(Bash)` 命令字符串），**git 生命周期这个 altitude 没有原生事件**，故这是
一个正当的「缺失缝」——与 `lib/notify`（推端口）、`lib/forge`（评审平台 facade）同性质，
不是重造原生事件。

**模型**（详见 `docs/lifecycle-hooks.md`）：

- **hook 只有一种，都是阻塞的**。`dispatch` 并发起一个相位上的全部 hook、join 等全部返回，
  再聚合。「并发」让 `lint ‖ test` 同跑，墙钟 = 最慢的那个。
- **「非阻塞」不是 hook 的属性**，而是某个 hook 体只做「发信号」这件快事：它返回一个
  `relay`（`BackgroundSpec`），把异步下游交给**唯一能造 wake 的 agent/harness**。
  `dispatch` 自身永远同步——它**不能**起一个「跑完唤醒 session」的后台任务（subprocess
  起的子进程 harness 不跟踪），所以只**收集** relay、交还调用方去起。
- **veto 能力与同步性同源**：inline 干活的 hook 返回 `ok=False` 可挡（gate，如 lint 失败）；
  只发信号的 hook 恒 `ok=True` 且带 `relay`（信号发成功就是过，真活还没跑、无可 veto）。

`dispatch` 只做机制（并发 join + 聚合）；「这个相位挂哪些 hook」是 `config.lifecycle()`
的数据（opt-in，默认全空 = 每个相位 no-op、零行为变化）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import import_module
from typing import Callable

PHASES = ("pre_commit", "post_commit", "pre_mr", "post_mr")

# 内置 hook 注册表：name → "module:function"。**惰性**解析（在 dispatch 时才 import），
# 因为 handler 模块（lib.checks）反过来要 `from lib.lifecycle import HookResult`——模块级
# 直接引用会成 import 环。MR2 引入 code-review 时在此加一行 "review": "lib.checks:review"。
_BUILTIN: dict[str, str] = {
    "lint": "lib.checks:lint",
    "test": "lib.checks:test",
}


@dataclass(frozen=True)
class BackgroundSpec:
    """signal hook 的「下游」：一条要由 agent/harness 起到后台、跑完唤醒 session 的命令。

    `dispatch` 自己**不能**起它——subprocess 派生的子进程 harness 不跟踪、跑完不会
    re-invoke 会话。故 signal hook 只把这条 spec 作为 `HookResult.relay` 返回，由调用方
    （smart_git_ops → PLAN 的 `ARMED:` 行 → agent 用 run_in_background 起）落地。
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
        """需调用方起到后台的 signal 下游（MR1 无 signal hook，恒空；MR2 起 review）。"""
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
      宁可挡不可漏）。signal hook 要保证「永不挡」，须自己 catch 内部异常、恒返回
      `ok=True`——这是 handler 的契约，不是 dispatcher 的事。
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
        except Exception as e:  # gate fail-closed —— 把关出错按未通过处理
            return HookResult(name=name, ok=False, summary=f"{name} errored: {e}")

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(names)))) as ex:
        results = list(ex.map(_run, names))   # ex.map 保序，结果与 names 对齐
    return DispatchResult(phase=phase, results=results)
