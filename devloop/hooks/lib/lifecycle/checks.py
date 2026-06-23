"""内置 inline-gate handler：`lint` / `test`。

这两个 hook 与 `/lint` `/test` 命令是同一段逻辑、同一处盖 `.devloop` validation 戳——命令侧
是 CLI 入口，gate 侧是 dispatch 调用，跑的是这里。stamp 在通过时盖，所以裸 `git commit` 的
守卫（`rules/command/precommit_gate`）查到的戳与 dispatch 跑出的结果一致。

handler 契约：`fn(repo) -> HookResult`。lint/test 是 inline gate——干实际活、失败返回
`ok=False` 可挡 commit。`capture=False`（命令侧）让 make 直接走父进程 stdout（实时）；
`capture=True`（dispatch 并发跑）收口输出、失败时把尾部塞进 summary，避免并发 lint‖test 的
输出交错刷屏。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from lib import repo_layout
from lib.context import RepoContext
from lib.lifecycle.base import HookResult

_TAIL_LINES = 40   # 失败时回带的输出尾行数（够定位、不淹没 PLAN）


def _code_dir(repo: str) -> str:
    """make/uv 的 workdir：优先 RepoContext 记录的 code_dir，否则探测。"""
    ctx = RepoContext.load(repo)
    return (ctx.repo.code_dir if ctx and ctx.repo.code_dir else None) or repo_layout.find_repo_code_dir(repo)


def has_target(code_dir: str, name: str, *, suffix: bool = False) -> bool:
    """Makefile 是否有名为 `name` 的 target。suffix=True 时 `name-ci` / `name-local` 也算命中。"""
    mk = Path(code_dir) / "Makefile"
    if not mk.exists():
        return False
    pat = rf"^{re.escape(name)}(-\w+)?\s*:" if suffix else rf"^{re.escape(name)}\s*:"
    try:
        return bool(re.search(pat, mk.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


def pick_lint_target(code_dir: str) -> str | None:
    """优先 CI lint 入口：`lint-ci` 通常先 `uv sync` 钉版工具链，跑 plain `lint` 用本地新版
    formatter 会本地过、CI 挂。有 lint-ci 就用它，与 CI 对齐。"""
    for t in ("lint-ci", "lint"):
        if has_target(code_dir, t):
            return t
    return None


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


def lint(repo: str, *, capture: bool = True) -> HookResult:
    """`make fix` 后跑 lint target；通过则盖 lint 戳。只有 `make fix` 能改文件——此处从不手改代码。

    跑 lint 前清 `.mypy_cache`：热缓存对一棵冷跑会被标红的树报过绿，一个能放行坏 MR 的戳比慢
    一点更糟。无 lint target → 干净跳过（ok，无可验证）。
    """
    code_dir = _code_dir(repo)
    target = pick_lint_target(code_dir)
    if target is None:
        return HookResult("lint", ok=True, summary=f"no make lint/lint-ci target in {code_dir} — skipped")

    sink: list[str] = []
    if has_target(code_dir, "fix"):
        _make(code_dir, "fix", capture=capture, sink=sink)   # 可改文件；rc 忽略（fixer 非零正常）
    shutil.rmtree(Path(code_dir) / ".mypy_cache", ignore_errors=True)
    rc = _make(code_dir, target, capture=capture, sink=sink)
    if rc == 0:
        ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
        ctx.mark_lint_passed()
        return HookResult("lint", ok=True, summary=f"make {target} passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("lint", ok=False, summary=f"make {target} failed (only `make fix` may edit files){detail}")


def test(repo: str, *, capture: bool = True, extra: list[str] | None = None) -> HookResult:
    """跑 `make test`（仓库 canonical 入口，设好 PYTHONPATH 等）；通过则盖 test 戳。
    无 test target → 干净跳过。"""
    code_dir = _code_dir(repo)
    if not has_target(code_dir, "test", suffix=True):
        return HookResult("test", ok=True, summary=f"no make test target in {code_dir} — skipped")

    extra = extra or []
    sink: list[str] = []
    header = f"--- make test (cwd={code_dir}) {' '.join(extra)} ---"
    if capture:
        sink.append(header)
        r = subprocess.run(["make", "test", *extra], cwd=code_dir, capture_output=True, text=True)
        sink += [r.stdout, r.stderr]
        rc = r.returncode
    else:
        print(header)
        rc = subprocess.run(["make", "test", *extra], cwd=code_dir).returncode
    if rc == 0:
        ctx = RepoContext.load(repo) or RepoContext.refresh_all(repo)
        ctx.mark_test_passed()
        return HookResult("test", ok=True, summary="make test passed — stamped")
    detail = f"\n{_tail(sink)}" if capture else ""
    return HookResult("test", ok=False, summary=f"make test failed{detail}")
