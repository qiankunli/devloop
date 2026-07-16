"""内置 inline-gate handler：`lint` / `test`。

这两个 hook 与 fix-lint / run-test skill（run_fixlint.py / run_tests.py）是同一段逻辑、
同一处盖 `.devloop` validation 戳——skill 侧是 CLI 入口，gate 侧是 dispatch 调用，跑的是
这里。stamp 在通过时盖，所以裸 `git commit` 的守卫（`rules/command/precommit_gate`）查到
的戳与 dispatch 跑出的结果一致。

handler 契约：`fn(repo, paths) -> HookResult`（`paths` = 相位边界冻结的本次改动范围，见
`lifecycle.base.dispatch`）。lint/test 是 inline gate——干实际活、失败返回
`ok=False` 可挡 commit。`capture=False`（skill 侧）让 make 直接走父进程 stdout（实时）；
`capture=True`（dispatch 并发跑）收口输出、失败时把尾部塞进 summary，避免并发 lint‖test 的
输出交错刷屏。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lib import ecosystem
from domain import repo as repo_model
from domain.context import RepoContext
from domain.repo_layout import Component
from domain.lifecycle.base import HookResult

_TAIL_LINES = 40   # 失败时回带的输出尾行数（够定位、不淹没 PLAN）


def _aggregate(name: str, reason: str, results: list[HookResult], *, advisory: bool = False) -> HookResult:
    """把 lifecycle 对多 component 的 fan-out 收回一个 hook 结果。

    dispatch 的契约是每个 hook 名返回一个 HookResult；component 是该 hook 内部的执行范围，
    不应暴露成多个 lifecycle hook。

    0 个 component（本相位无改动）是合法结果、算过——`all([])` 恒 True，这正是「知道范围且为空 →
    干净跳过」该有的样子。此时只报 reason，不缀空 detail。
    """
    detail = "; ".join(r.summary for r in results)
    return HookResult(name, ok=all(r.ok for r in results), advisory=advisory,
                      summary=f"{reason}; {detail}" if detail else reason)


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


def _environment_failure(name: str, component: Component, *, advisory: bool = False) -> HookResult | None:
    """验证命令的环境前置条件：缺依赖先按生态 frozen 恢复，失败单列为环境错误。

    lint/test 会在 lifecycle 里并发进入；`ecosystem.ensure_ready` 自带 per-component single-flight，
    所以同一份 node_modules/.venv 只会有一个 writer。
    """
    problem = ecosystem.ensure_ready(component.path)
    if problem is None:
        return None
    return HookResult(name, ok=False, advisory=advisory,
                      summary=f"environment setup failed in {component.path}: {problem}")


def lint(repo: str, *, capture: bool = True, component: Component | None = None,
         paths: list[str] | None = None) -> HookResult:
    """`make fix` 后跑 lint target；通过则盖 lint 戳。只有 `make fix` 能改文件——此处从不手改代码。

    `component` 给出即用它（CLI 已按操作目标选好）；否则是 lifecycle gate 入口，按本次改动选 WorkSet
    并 fan-out，避免多 component 仓静默回落 server / 仓根。`paths`（相位边界冻结的改动范围）给出即用它，
    不再自己读工作树——commit 后工作树已干净，读出来会是「无改动」→ 退化成跑全仓。
    跑 lint 前清 `.mypy_cache`：热缓存对一棵冷跑会被标红的树报过绿，一个能放行坏 MR 的戳比慢
    一点更糟。无 lint target → 干净跳过（ok，无可验证）。
    """
    if component is None:
        ws = repo_model.select_components(repo, paths=paths)
        results = [lint(repo, capture=capture, component=u) for u in ws.components]
        return _aggregate("lint", ws.reason, results)
    code_dir = component.path
    target = component.lint_target()
    if target is None:
        return HookResult("lint", ok=True, summary=f"no make lint/lint-ci target in {code_dir} — skipped")
    env_failure = _environment_failure("lint", component)
    if env_failure is not None:
        return env_failure

    sink: list[str] = []
    if component.has_target("fix"):
        _make(code_dir, "fix", capture=capture, sink=sink)   # 可改文件；rc 忽略（fixer 非零正常）
    shutil.rmtree(Path(code_dir) / ".mypy_cache", ignore_errors=True)
    rc = _make(code_dir, target, capture=capture, sink=sink)
    if rc == 0:
        ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
        # 指纹在**此刻**算：`make fix` 刚改过文件，跑之前算的指纹配不上刚被验过的这棵树——
        # 盖上去就等于给一份没验过的内容发通行证。
        ctx.mark_lint_passed(component.id, repo_model.component_fingerprint(repo, component) or "")
        return HookResult("lint", ok=True, summary=f"make {target} passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("lint", ok=False, summary=f"make {target} failed (only `make fix` may edit files){detail}")


def test(repo: str, *, capture: bool = True, extra: list[str] | None = None,
         component: Component | None = None, paths: list[str] | None = None) -> HookResult:
    """跑 component 的 canonical test 命令（Make target 或 Go module 的 `go test ./...`）；
    通过则盖 test 戳。无 test 命令 → 干净跳过。`component` 给出即用它；否则按本次改动
    选 WorkSet 并 fan-out，使 gcampr lifecycle 与 run-test skill 的选择逻辑一致。
    `paths` 同 `lint`：相位边界冻结的改动范围，给出即用它，不自己读工作树。

    **advisory（软提示）**：失败只通报、不阻断 commit/MR。test 挂常因基线坏测 / 环境，与本次
    diff 未必有关；要不要拦该看「diff 是否与挂掉的测试相关」，那需 baseline-aware 分析（TODO），
    现阶段先不硬拦，把判断交给 CI / 人。lint 仍是硬拦截。"""
    if component is None:
        ws = repo_model.select_components(repo, paths=paths)
        results = [test(repo, capture=capture, extra=extra, component=u) for u in ws.components]
        return _aggregate("test", ws.reason, results, advisory=True)
    code_dir = component.path
    command = component.test_command()
    if command is None:
        return HookResult("test", ok=True, advisory=True, summary=f"no test command in {code_dir} — skipped")
    env_failure = _environment_failure("test", component, advisory=True)
    if env_failure is not None:
        return env_failure

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
        ctx.mark_test_passed(component.id)
        return HookResult("test", ok=True, advisory=True, summary=f"{display} passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("test", ok=False, advisory=True, summary=f"{display} failed (advisory — not blocking){detail}")
