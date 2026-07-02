"""devloop 测试共享设施（非测试文件）：hermetic bootstrap + 公共 helper。

import 本模块的副作用即完成两件 bootstrap（必须发生在任何 `lib.*` import 之前，
所以每个测试文件的第一条 import 都应是 _testkit）：
1. 把 hooks/ 与 scripts/ 加进 sys.path；
2. 把 DEVLOOP_CONFIG_DIR 指向空临时目录——测试绝不读开发机真实 ~/.devloop/config.json
   （否则一个全局 lifecycle.pre_commit 会让 precommit-gate 在每个测试 repo 上生效、拦住 commit）。
   需要 config 的测试各自写自己的。

各测试文件独立可跑（`python3 devloop/tests/test_xxx.py`，也 pytest-collectable）；
全量入口是 `run_all.py`。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HOOKS))

_GCFG = "/tmp/dlut_global_cfg"
shutil.rmtree(_GCFG, ignore_errors=True)
os.makedirs(_GCFG, exist_ok=True)
os.environ["DEVLOOP_CONFIG_DIR"] = _GCFG

from lib.context import PullRequest  # noqa: E402
from lib.forge.base import Comment, Forge, ForgeNotFound  # noqa: E402


class _FakeForge(Forge):
    """In-memory Forge for testing the domain composition (build_window) + orchestration
    (reuse_or_create_pr) without HTTP — the port is small enough that this is trivial."""
    provider = "github"

    def __init__(self, prs, bodies=None):
        self._prs = {p.number: p for p in prs}
        self._bodies = dict(bodies or {})   # number → description text
        self.created = None

    def create(self, *, source_branch, target_branch, title, body=""):
        n = max(self._prs, default=0) + 1
        pr = PullRequest(number=n, state="open", source_branch=source_branch,
                         target_branch=target_branch, title=title, web_url=f"u/{n}")
        self._prs[n] = pr
        self._bodies[n] = body
        self.created = pr
        return pr

    def get(self, number):
        if number not in self._prs:
            raise ForgeNotFound(str(number))
        return self._prs[number]

    def description(self, number):
        return self._bodies.get(number, "")

    def update(self, number, **fields):
        if "body" in fields:
            self._bodies[number] = fields["body"]
        return self._prs[number]

    def close(self, number):
        pr = self._prs[number]
        self._prs[number] = replace(pr, state="closed")
        return self._prs[number]

    def prs_for_branch(self, branch):
        return sorted((p for p in self._prs.values() if p.source_branch == branch),
                      key=lambda p: p.number, reverse=True)

    def recent(self, limit):
        return sorted(self._prs.values(), key=lambda p: p.number, reverse=True)[:limit]

    def comments(self, number):
        return [Comment(author="x", body="y")]

    def comment(self, number, body):
        self.posted = getattr(self, "posted", [])
        self.posted.append((number, body))

    def diff_comment(self, number, body, path, line):
        self.diff_posted = getattr(self, "diff_posted", [])
        self.diff_posted.append((number, path, line, body))

    def default_branch(self):
        return "main"

def _load_from(base, name):
    spec = importlib.util.spec_from_file_location(name, str(base / f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules 再 exec:dataclass(及其它按 __module__ 反查注解的机制)
    # 需要 sys.modules[m.__name__] 存在,否则被测模块里定义 @dataclass 直接炸
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

def _load_script(name):
    return _load_from(SCRIPTS, name)

def _load_hook(name):
    return _load_from(HOOKS, name)

def _git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

def _git_out(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

def _hook_input(tool: str, raw: dict):
    from lib import hook_io
    return hook_io.HookInput(event="PreToolUse", tool_name=tool,
                             tool_input=raw.get("tool_input") or {},
                             cwd=raw.get("cwd", "/"), raw=raw)


def run_all(g: dict, label: str = "") -> tuple[int, list]:
    """跑 `g` 里所有 test_* 函数，返回 (总数, 失败名单)。各测试文件的 __main__ 和
    run_all.py 都走这里，保证输出与判定一致。"""
    tests = [v for k, v in sorted(g.items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception as e:
            print(f"  ✗ FAIL {t.__name__}: {e}")
            failed.append(t.__name__)
    tag = f" [{label}]" if label else ""
    print(f"RESULT{tag}:", "FAIL" if failed else f"ALL PASS ({len(tests)} tests)")
    return len(tests), failed


def run_main(g: dict) -> None:
    """单测试文件的 standalone 入口。"""
    _, failed = run_all(g)
    sys.exit(1 if failed else 0)
