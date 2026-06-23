"""devops 生命周期 hook 子系统 —— pre_commit / post_commit / pre_mr / post_mr。

**为什么是一个独立子系统（包）。** lint / test / code-review /（将来）e2e·eval·perf
verdict 形状相同——都是「在某个 git 生命周期相位触发的一个验证 / 动作」。过去各自 ad-hoc
接线（lint 的 gate 是一条 PreToolUse 规则、/lint 是手动命令…）。CC 原生事件只到工具层
（`PreToolUse(Bash)` 命令字符串），**git 生命周期这个 altitude 没有原生事件**，故这是一个
正当的「缺失缝」——与 `lib/notify`（推端口）、`lib/forge`（评审平台 facade）同性质，不是
重造原生事件。

包内分工（与 notify/ forge/ context/ 同构）：
- `base.py`  —— facade 核心：`dispatch` + `HookResult` / `BackgroundSpec` / `DispatchResult`
  + 内置 hook 注册表。纯机制。
- `checks.py` —— 内置 inline-gate handler（`lint` / `test`），与 `/lint` `/test` 命令共用同
  一段逻辑、同处盖 `.devloop` validation 戳（单一事实源，不漂移）。
- （MR2）`review.py` —— code-review 的 signal handler，返回带 `relay` 的 HookResult。

**模型**（详见 `docs/lifecycle-hooks.md`）：

- **hook 只有一种，都是阻塞的**。`dispatch` 并发起一个相位上的全部 hook、join 等全部返回，
  再聚合（`lint ‖ test` 同跑，墙钟 = 最慢那个）。
- **「非阻塞」不是 hook 的属性**，而是某个 hook 体只做「发信号」这件快事：它返回一个
  `relay`（`BackgroundSpec`），把异步下游交给**唯一能造 wake 的 agent/harness**。`dispatch`
  自身永远同步——它**不能**起一个「跑完唤醒 session」的后台任务（subprocess 派生的子进程
  harness 不跟踪），所以只**收集** relay、交还调用方。
- **veto 能力与同步性同源**：inline 干活的 hook 返回 `ok=False` 可挡（gate）；只发信号的
  hook 恒 `ok=True` 且带 `relay`（信号发成功就是过，真活还没跑、无可 veto）。

「哪个相位挂哪些 hook」是 `config.lifecycle()` 的数据（opt-in，默认全空 = 每相位 no-op）。
"""
from __future__ import annotations

from .base import (
    PHASES,
    BackgroundSpec,
    DispatchResult,
    HookResult,
    dispatch,
    resolve_handler,
)
