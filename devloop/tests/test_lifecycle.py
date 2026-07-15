#!/usr/bin/env python3
"""lifecycle 与 code policy：pre/post 阶段 dispatch、config 分层、命令规则引擎。

Standalone: `python3 devloop/tests/test_lifecycle.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _git, _hook_input, _load_hook, run_main  # noqa: E402  (bootstrap first)


def test_code_policy_engine():
    """变更策略引擎纵切：project(工具→Target) + codemodel(惰性解析改后全文) + LayerDepsRule(层级方向)。
    现有 10 个 guard 尚未迁入，这里只验代码侧 lint-deps 这条新规则端到端跑通。"""
    from lib import rules
    from lib.core import engine
    from lib.core.domain import Command, FileChange

    arch = {"enabled": True, "layers": {"/dao/": "dao", "/service/": "service"},
            "order": ["api", "service", "dao", "model"]}

    class Ctx:  # 假 PolicyContext：代码侧规则只用到 .arch
        def __init__(self, a=None):
            self.arch = arch if a is None else a

    def evl(inp, ctx=None):
        return engine.evaluate(engine.project(inp), ctx or Ctx(), rules.REGISTRY)

    # project：工具 → Target 类型 + mode；Bash 拆多条 Command
    w = engine.project(_hook_input("Write", {"tool_input": {"file_path": "/r/a.py", "content": ""}}))
    assert isinstance(w.targets[0], FileChange) and w.targets[0].mode == "write"
    e = engine.project(_hook_input("Edit", {"tool_input": {"file_path": "/r/a.py", "old_string": "a", "new_string": "b"}}))
    assert e.targets[0].mode == "edit"
    ap = engine.project(_hook_input("apply_patch", {"tool_input": {"patch": "*** Begin Patch\n*** Update File: /r/a.py\n@@\n-x\n+y\n*** End Patch\n"}}))
    assert isinstance(ap.targets[0], FileChange) and ap.targets[0].path == "/r/a.py" and ap.targets[0].mode == "edit"
    bash = engine.project(_hook_input("Bash", {"cwd": "/r", "tool_input": {"command": "cd x && go build ./..."}}))
    assert len(bash.targets) == 2 and all(isinstance(t, Command) for t in bash.targets)

    # Write content：dao import service → DENY；service import dao → allow
    dao = _hook_input("Write", {"tool_input": {"file_path": "/r/internal/dao/u.py", "content": "from app.service import x\n"}})
    assert evl(dao).action == "deny"
    svc = _hook_input("Write", {"tool_input": {"file_path": "/r/internal/service/u.py", "content": "from app.dao import x\n"}})
    assert evl(svc).action == "allow"

    # 非分层路径 → allow；arch 关 → allow；语法错 → allow（fail-open）
    assert evl(_hook_input("Write", {"tool_input": {"file_path": "/r/util/u.py", "content": "from app.service import x\n"}})).action == "allow"
    assert evl(dao, Ctx({"enabled": False})).action == "allow"
    assert evl(_hook_input("Write", {"tool_input": {"file_path": "/r/internal/dao/u.py", "content": "def (:\n"}})).action == "allow"

    # Edit "改后全文"：盘上无 import，edit 插入 import service → 命中（验证读盘+套用替换，而非只看片段）
    R = "/tmp/dlut_codepolicy"; shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/internal/dao", exist_ok=True)
    fp = f"{R}/internal/dao/u.py"; Path(fp).write_text("x = 1\n")
    ed = _hook_input("Edit", {"tool_input": {"file_path": fp, "old_string": "x = 1", "new_string": "from app.service import y\nx = 1"}})
    assert evl(ed).action == "deny"

def test_migrated_command_rules_parity():
    """平替校验：5 个原先无测试的命令侧规则经新引擎跑通，行为与原 guard 一致。"""
    import json as _json

    from lib.context import RepoContext, session as session_lock
    bash = _load_hook("pretool_policy_bash")

    def d(cmd, cwd="/tmp", sid=""):
        return bash.decide(_hook_input("Bash", {"cwd": cwd, "session_id": sid, "tool_input": {"command": cmd}}))

    # add_all：-A / . / --all 拦，显式 staging 放行
    assert d("git add -A") and d("git add .") and d("git add --all")
    assert d("git add foo.py") is None

    # checkout_owner：他人占有 checkout → 切分支拦；文件恢复 / owner 自己 → 放行
    R = "/tmp/dlut_co"; shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q")
    session_lock.acquire(R, "other", "br", pid=os.getpid())
    assert "worktree" in (d("git checkout main", cwd=R, sid="me") or "")
    assert d("git switch main", cwd=R, sid="me")
    assert d("git checkout -- f", cwd=R, sid="me") is None      # 文件恢复(非切分支)
    assert d("git checkout main", cwd=R, sid="other") is None   # owner 自己

    # pip_install：uv-managed 仓拦 pip install，放行 -e . 与非 uv 仓
    P = "/tmp/dlut_pip"; shutil.rmtree(P, ignore_errors=True); os.makedirs(P)
    _git(P, "init", "-q"); Path(f"{P}/pyproject.toml").write_text("[project]\nname = 'x'\n")
    Path(f"{P}/uv.lock").write_text(""); RepoContext.refresh_all(P)
    assert d("pip install requests", cwd=P)
    assert d("pip install -e .", cwd=P) is None
    assert d("pip install requests", cwd="/tmp") is None        # 非 uv 仓

    # pytest_naked：有 make test 时裸 pytest 拦，env 前缀 / make test 放行
    T = "/tmp/dlut_pt"; shutil.rmtree(T, ignore_errors=True); os.makedirs(T)
    _git(T, "init", "-q"); Path(f"{T}/Makefile").write_text("test:\n\techo hi\n"); RepoContext.refresh_all(T)
    assert d("pytest", cwd=T)
    assert d("PYTHONPATH=. pytest", cwd=T) is None              # env 前缀
    assert d("make test", cwd=T) is None

    # precommit_gate：lint 在 lifecycle.pre_commit + 有 lint target + lint 从未跑 → 拦 commit。
    G = "/tmp/dlut_pcg"; shutil.rmtree(G, ignore_errors=True); os.makedirs(f"{G}/.devloop")
    _git(G, "init", "-q"); _git(G, "config", "user.email", "t@t.t"); _git(G, "config", "user.name", "t")
    _git(G, "checkout", "-q", "-b", "feat/x")
    gabs = str(Path(G).resolve())
    Path(f"{G}/.devloop/config.json").write_text(
        _json.dumps({"lifecycle": {"repos": {gabs: {"pre_commit": ["lint"]}}}}))
    RepoContext.refresh_all(G)
    # 无 lint target → 放行：dispatch 的 lint 只会干净跳过、盖不出戳，硬要戳=锁死裸 commit
    assert d("git commit -m x", cwd=G) is None
    Path(f"{G}/Makefile").write_text("lint:\n\ttrue\n")
    assert "Refusing `git commit`" in (d("git commit -m x", cwd=G) or "")
    # lint 不在 pre_commit → 不拦（opt-in 默认放行）
    Path(f"{G}/.devloop/config.json").write_text(_json.dumps({"lifecycle": {"repos": {gabs: {"pre_commit": ["test"]}}}}))
    RepoContext.refresh_all(G)
    assert d("git commit -m x", cwd=G) is None

def test_lifecycle_dispatch():
    """facade 机制：并发 join + 聚合。gate fail / 异常 fail-closed / 未知 hook 可见 /
    signal hook 不挡且其 relay 进 to_launch / 空配置 no-op / 未知相位抛。"""
    from lib import lifecycle as lc
    HR, BG = lc.HookResult, lc.BackgroundSpec

    reg = {
        "ok":   lambda repo: HR("ok", ok=True, summary="fine"),
        "bad":  lambda repo: HR("bad", ok=False, summary="boom"),
        "boom": lambda repo: (_ for _ in ()).throw(RuntimeError("kaboom")),
        "sig":  lambda repo: HR("sig", ok=True, relay=BG("sig", ["run", "x"])),
    }

    # 全过 → proceed；ex.map 保序
    r = lc.dispatch("pre_commit", "/r", names=["ok", "sig"], registry=reg)
    assert r.proceed and [x.name for x in r.results] == ["ok", "sig"]
    # signal hook 不挡，其 relay 进 to_launch
    assert [b.name for b in r.to_launch] == ["sig"]
    # inline gate 失败 → 不放行
    r = lc.dispatch("pre_commit", "/r", names=["ok", "bad"], registry=reg)
    assert not r.proceed and [x.name for x in r.failures] == ["bad"]
    # handler 抛异常 → fail-closed
    r = lc.dispatch("pre_commit", "/r", names=["boom"], registry=reg)
    assert not r.proceed and "kaboom" in r.results[0].summary
    # 未知 hook 名 → 可见的 ok=False，不静默吞
    r = lc.dispatch("pre_commit", "/r", names=["nope"], registry=reg)
    assert not r.proceed and "unknown" in r.results[0].summary
    # 软提示（advisory）失败 → 通报但不阻断（proceed），进 advisory_failures 而非 blocking_failures
    reg["soft"] = lambda repo: HR("soft", ok=False, advisory=True, summary="advisory boom")
    r = lc.dispatch("pre_commit", "/r", names=["ok", "soft"], registry=reg)
    assert r.proceed
    assert [x.name for x in r.advisory_failures] == ["soft"] and r.blocking_failures == []
    # 硬拦截 + 软提示混合：硬的仍挡，软的只进 advisory
    r = lc.dispatch("pre_commit", "/r", names=["bad", "soft"], registry=reg)
    assert not r.proceed and [x.name for x in r.blocking_failures] == ["bad"]
    assert [x.name for x in r.advisory_failures] == ["soft"]
    # 空配置 → no-op、proceed
    assert lc.dispatch("pre_commit", "/r", names=[]).proceed
    # 未知相位 → 抛
    try:
        lc.dispatch("nope", "/r", names=["ok"], registry=reg)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # 内置注册表解析得到可调用 handler
    assert callable(lc.resolve_handler("lint")) and lc.resolve_handler("nope") is None

def test_code_unit_test_target_detect_matches_execute():
    """CodeUnit.test_target 探测即执行：只有 `test-ci` 的仓要跑 `make test-ci`，不能判成「有
    测试」却硬跑不存在的 `make test`（旧 `has_target(suffix=True)` bug）。无 test 目标 → None。"""
    from lib.repo_layout import CodeUnit
    D = "/tmp/dlut_testtarget"
    shutil.rmtree(D, ignore_errors=True)
    os.makedirs(f"{D}/ci"); os.makedirs(f"{D}/plain"); os.makedirs(f"{D}/none"); os.makedirs(f"{D}/go")
    Path(f"{D}/ci/Makefile").write_text("test-ci:\n\techo ok\n")
    Path(f"{D}/plain/Makefile").write_text("test:\n\techo ok\ntest-local:\n\techo ok\n")
    Path(f"{D}/none/Makefile").write_text("build:\n\techo ok\n")
    Path(f"{D}/go/go.mod").write_text("module x\n")
    assert CodeUnit.at(f"{D}/ci", D).test_target() == "test-ci"   # 判据==执行目标，不再错跑 make test
    assert CodeUnit.at(f"{D}/plain", D).test_target() == "test"   # canonical `test` 优先
    assert CodeUnit.at(f"{D}/none", D).test_target() is None      # 无 test 目标 → 跳过
    assert CodeUnit.at(f"{D}/ci", D).test_command() == ("make", "test-ci")
    assert CodeUnit.at(f"{D}/go", D).test_command() == ("go", "test", "./...")
    # 身份在出生点算清，消费方直接读 .id（不再各自拿 git_root 重推）
    assert CodeUnit.at(f"{D}/ci", D).id == "ci" and CodeUnit.at(D, D).id == "."

def test_lifecycle_checks_follow_changed_code_unit():
    """gcampr lifecycle 与 run-test 必须共用 WorkSet：只改 cli 时不得跑仓根 test。"""
    from lib.lifecycle import checks

    R = "/tmp/dlut_lifecycle_unit"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/cli")
    _git(R, "init", "-q")
    Path(f"{R}/Makefile").write_text("test:\n\tfalse\n")
    Path(f"{R}/cli/Makefile").write_text("test:\n\ttrue\n")
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    _git(R, "add", "-A")
    _git(R, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    Path(f"{R}/cli/change.py").write_text("x = 1\n")
    result = checks.test(R)
    assert result.ok and result.advisory
    assert "changed files under: cli" in result.summary
    assert "make test passed" in result.summary


def test_partial_unit_lint_failure_does_not_unlock_bare_commit():
    """一个 unit 过、另一个挂时，防绕过守卫必须**仍然拦**裸 `git commit`。

    这是 validation 按 unit 键的理由本身。repo 级单戳下这条必红：fan-out 里 cli 通过盖的那个
    戳是 repo 级的，`precommit_gate` 读到「已验、无待验编辑」就放行——于是 gate 挡住了 gcampr，
    却正好给它唯一要拦的东西（裸 commit）发了通行证。守卫和正常路径必须是同一份策略。
    """
    import json as _json

    from lib.context import RepoContext
    from lib.lifecycle import checks
    bash = _load_hook("pretool_policy_bash")

    R = "/tmp/dlut_partial_unit"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/cli"); os.makedirs(f"{R}/server"); os.makedirs(f"{R}/.devloop")
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/x")
    Path(f"{R}/cli/Makefile").write_text("lint:\n\ttrue\n")            # cli 过
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    Path(f"{R}/server/Makefile").write_text("lint:\n\tfalse\n")        # server 挂
    Path(f"{R}/server/pyproject.toml").write_text("[project]\nname = 'server'\nversion = '0'\n")
    Path(f"{R}/.devloop/config.json").write_text(
        _json.dumps({"lifecycle": {"repos": {str(Path(R).resolve()): {"pre_commit": ["lint"]}}}}))
    _git(R, "add", "-A"); _git(R, "commit", "-qm", "init")
    RepoContext.refresh_all(R)

    # 两个 unit 都有改动 → WorkSet 命中两个；lint fan-out 一过一挂 → 整体不放行
    Path(f"{R}/cli/a.py").write_text("x = 1\n")
    Path(f"{R}/server/b.py").write_text("y = 2\n")
    res = checks.lint(R)
    assert not res.ok, "server 的 lint 挂了，聚合结果必须 ok=False"

    # cli 已盖戳，但 server 没有——裸 commit 守卫要看的是「本轮 required units 是否都验过」
    v = RepoContext.load(R).validation
    assert v.unit("cli").last_lint_at and not v.unit("server").last_lint_at
    msg = bash.decide(_hook_input("Bash", {"cwd": R, "session_id": "",
                                           "tool_input": {"command": "git commit -m x"}})) or ""
    assert "Refusing `git commit`" in msg, "cli 的戳把守卫的锁打开了 —— 正是 repo 级单戳的 bug"
    assert "server" in msg and "cli" not in msg, f"应只点名未验的 server：{msg}"

def test_lifecycle_config_layering():
    """config.lifecycle()：default 全空（opt-in），repo 级 .devloop/config.json 覆盖该 repo 的相位。"""
    import json as _json
    from lib import config
    W = "/tmp/dlut_lcfg"; shutil.rmtree(W, ignore_errors=True); os.makedirs(f"{W}/cfg"); os.makedirs(f"{W}/repo/.devloop")
    old = os.environ.get("DEVLOOP_CONFIG_DIR")
    os.environ["DEVLOOP_CONFIG_DIR"] = f"{W}/cfg"   # 隔离全局，免被本机 ~/.devloop 干扰
    try:
        assert (config.lifecycle(f"{W}/repo").get("pre_commit") or []) == []   # 默认空
        # key 用 abspath（非 resolve）对齐 config.lifecycle 的查找：macOS /tmp 是 /private/tmp
        # 软链，resolve 会解开导致 key 不匹配。
        rabs = os.path.abspath(f"{W}/repo")
        Path(f"{W}/repo/.devloop/config.json").write_text(
            _json.dumps({"lifecycle": {"repos": {rabs: {"pre_commit": ["lint", "test"]}}}}))
        assert config.lifecycle(f"{W}/repo")["pre_commit"] == ["lint", "test"]
    finally:
        if old is None:
            os.environ.pop("DEVLOOP_CONFIG_DIR", None)
        else:
            os.environ["DEVLOOP_CONFIG_DIR"] = old

def test_lifecycle_review_signal_hook():
    """code-review 是 signal hook：恒不挡（proceed），返回一个指向 run_review.py 的后台 relay。"""
    from lib import lifecycle as lc
    r = lc.dispatch("post_mr", "/some/repo", names=["review"])   # 走真实 _BUILTIN 解析
    assert r.proceed                                               # signal hook 永不挡
    assert [s.name for s in r.to_launch] == ["review"]
    spec = r.to_launch[0]
    assert spec.argv[0] == "python3" and spec.argv[-2:] == ["--repo", "/some/repo"]
    assert spec.argv[1].endswith("run_review.py")


if __name__ == "__main__":
    run_main(globals())
