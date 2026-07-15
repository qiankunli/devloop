"""内置 inline-gate handler：`lint` / `test`。

这两个 hook 与 fix-lint / run-test skill（run_fixlint.py / run_tests.py）是同一段逻辑、
同一处盖 `.devloop` validation 戳——skill 侧是 CLI 入口，gate 侧是 dispatch 调用，跑的是
这里。stamp 在通过时盖，所以裸 `git commit` 的守卫（`rules/command/precommit_gate`）查到
的戳与 dispatch 跑出的结果一致。

handler 契约：`fn(repo) -> HookResult`。lint/test 是 inline gate——干实际活、失败返回
`ok=False` 可挡 commit。`capture=False`（skill 侧）让 make 直接走父进程 stdout（实时）；
`capture=True`（dispatch 并发跑）收口输出、失败时把尾部塞进 summary，避免并发 lint‖test 的
输出交错刷屏。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lib import repo_resolve
from lib.context import RepoContext
from lib.repo_layout import CodeUnit
from lib.lifecycle.base import HookResult

_TAIL_LINES = 40   # 失败时回带的输出尾行数（够定位、不淹没 PLAN）


def _aggregate(name: str, reason: str, results: list[HookResult], *, advisory: bool = False) -> HookResult:
    """把 lifecycle 对多 code unit 的 fan-out 收回一个 hook 结果。

    dispatch 的契约是每个 hook 名返回一个 HookResult；unit 是该 hook 内部的执行范围，
    不应暴露成多个 lifecycle hook。
    """
    detail = "; ".join(r.summary for r in results)
    return HookResult(name, ok=all(r.ok for r in results), advisory=advisory,
                      summary=f"{reason}; {detail}")


def _make(code_dir: str, target: str, *, capture: bool, sink: list[str]) -> int:
    """跑 `make <target>`。capture=False → 走父进程 stdout（实时）；True → 收口进 sink。"""
    header = f"--- make {target} (cwd={code_dir}) ---"
    if capture:
        sink.append(header)
        r = subprocess.run(["make", target], cwd=code_dir, capture_output=True, text=True)
        sink.append(r.stdout)
        sink.append(r.stderr)
        return r.returncode
    print(header)
    return subprocess.run(["make", target], cwd=code_dir).returncode


def _tail(sink: list[str]) -> str:
    lines = "".join(sink).splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def lint(repo: str, *, capture: bool = True, unit: CodeUnit | None = None) -> HookResult:
    """`make fix` 后跑 lint target；通过则盖 lint 戳。只有 `make fix` 能改文件——此处从不手改代码。

    `unit` 给出即用它（CLI 已按操作目标选好）；否则是 lifecycle gate 入口，
    按本次改动选 WorkSet 并 fan-out，避免多 unit 仓静默回落 server / 仓根。
    跑 lint 前清 `.mypy_cache`：热缓存对一棵冷跑会被标红的树报过绿，一个能放行坏 MR 的戳比慢
    一点更糟。无 lint target → 干净跳过（ok，无可验证）。
    """
    if unit is None:
        ws = repo_resolve.select_units(repo)
        results = [lint(repo, capture=capture, unit=u) for u in ws.units]
        return _aggregate("lint", ws.reason, results)
    code_dir = unit.path
    target = unit.lint_target()
    if target is None:
        return HookResult("lint", ok=True, summary=f"no make lint/lint-ci target in {code_dir} — skipped")

    sink: list[str] = []
    if unit.has_target("fix"):
        _make(code_dir, "fix", capture=capture, sink=sink)   # 可改文件；rc 忽略（fixer 非零正常）
    shutil.rmtree(Path(code_dir) / ".mypy_cache", ignore_errors=True)
    rc = _make(code_dir, target, capture=capture, sink=sink)
    if rc == 0:
        ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
        ctx.mark_lint_passed()
        return HookResult("lint", ok=True, summary=f"make {target} passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("lint", ok=False, summary=f"make {target} failed (only `make fix` may edit files){detail}")


def test(repo: str, *, capture: bool = True, extra: list[str] | None = None,
         unit: CodeUnit | None = None) -> HookResult:
    """跑 unit 的 canonical test 命令（Make target 或 Go module 的 `go test ./...`）；
    通过则盖 test 戳。无 test 命令 → 干净跳过。`unit` 给出即用它；否则按本次改动
    选 WorkSet 并 fan-out，使 gcampr lifecycle 与 run-test skill 的选择逻辑一致。

    **advisory（软提示）**：失败只通报、不阻断 commit/MR。test 挂常因基线坏测 / 环境，与本次
    diff 未必有关；要不要拦该看「diff 是否与挂掉的测试相关」，那需 baseline-aware 分析（TODO），
    现阶段先不硬拦，把判断交给 CI / 人。lint 仍是硬拦截。"""
    if unit is None:
        ws = repo_resolve.select_units(repo)
        results = [test(repo, capture=capture, extra=extra, unit=u) for u in ws.units]
        return _aggregate("test", ws.reason, results, advisory=True)
    code_dir = unit.path
    command = unit.test_command()
    if command is None:
        return HookResult("test", ok=True, advisory=True, summary=f"no test command in {code_dir} — skipped")

    extra = extra or []
    argv = [*command, *extra]
    display = " ".join(argv)
    sink: list[str] = []
    header = f"--- {display} (cwd={code_dir}) ---"
    if capture:
        sink.append(header)
        r = subprocess.run(argv, cwd=code_dir, capture_output=True, text=True)
        sink += [r.stdout, r.stderr]
        rc = r.returncode
    else:
        print(header)
        rc = subprocess.run(argv, cwd=code_dir).returncode
    if rc == 0:
        ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
        ctx.mark_test_passed()
        return HookResult("test", ok=True, advisory=True, summary=f"{display} passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("test", ok=False, advisory=True, summary=f"{display} failed (advisory — not blocking){detail}")
