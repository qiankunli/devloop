#!/usr/bin/env python3
"""lifecycle 与 code policy：pre/post 阶段 dispatch、config 分层、命令规则引擎。

Standalone: `python3 devloop/tests/test_lifecycle.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _git, _hook_input, _load_hook, _load_script, run_main  # noqa: E402  (bootstrap first)


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

def test_command_guards_judge_the_parsed_run_dir():
    """命令侧 guard 必须按**解析后的 run_dir**（cd/-C 已算完）判，不能回头读 session 原始 cwd。

    这两条红过的样子：`cd cli && pip install x` / `cd cli && pytest` 里，parser 早把 run_dir
    算成 cli/ 了，guard 却拿 `ctx.cwd`（= 仓根，cd **之前**的位置）去问 code unit——于是不管
    cd 到哪都在问默认 unit：该拦的不拦（下面 R），不该拦的误拦（下面 U）。
    """
    from lib.context import RepoContext
    bash = _load_hook("pretool_policy_bash")

    def d(cmd, cwd):
        return bash.decide(_hook_input("Bash", {"cwd": cwd, "session_id": "",
                                                "tool_input": {"command": cmd}}))

    # R：仓根是**非** uv、无 make test；cli/ 是 uv 仓、有 make test → 该拦的必须拦
    R = "/tmp/dlut_rundir"; shutil.rmtree(R, ignore_errors=True); os.makedirs(f"{R}/cli")
    _git(R, "init", "-q")
    Path(f"{R}/Makefile").write_text("build:\n\ttrue\n")           # 根：无 test target
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    Path(f"{R}/cli/uv.lock").write_text("")
    Path(f"{R}/cli/Makefile").write_text("test:\n\techo hi\n")
    RepoContext.refresh_all(R)
    assert d("pip install requests", cwd=R) is None                # 根非 uv → 放行
    assert d("pytest", cwd=R) is None                              # 根无 make test → 放行
    assert d("cd cli && pip install requests", cwd=R), "cd cli 后是 uv 仓，pip install 必须拦"
    assert "cli" in (d("cd cli && pytest", cwd=R) or ""), "cd cli 后有 make test，裸 pytest 必须拦"
    # env 前缀仍放行：env 与 cd 现在出自同一次解析，别为了拿 run_dir 把 env 判定弄丢
    assert d("cd cli && PYTHONPATH=. pytest", cwd=R) is None
    # 子 shell 的 cd 不外泄（cd scope 是 parser 的既有语义，guard 白拿）
    assert d("(cd cli) && pytest", cwd=R) is None

    # U：反向——仓根是 uv 仓，tools/ 不是 → 不该拦的不许误拦
    U = "/tmp/dlut_rundir_rev"; shutil.rmtree(U, ignore_errors=True); os.makedirs(f"{U}/tools")
    _git(U, "init", "-q")
    Path(f"{U}/pyproject.toml").write_text("[project]\nname = 'root'\nversion = '0'\n")
    Path(f"{U}/uv.lock").write_text("")
    Path(f"{U}/tools/pyproject.toml").write_text("[project]\nname = 'tools'\nversion = '0'\n")
    RepoContext.refresh_all(U)
    assert d("pip install requests", cwd=U)                        # 根是 uv → 拦
    assert d("cd tools && pip install requests", cwd=U) is None, "tools 无 uv.lock，不该按仓根误拦"


def test_lifecycle_dispatch():
    """facade 机制：并发 join + 聚合。gate fail / 异常 fail-closed / 未知 hook 可见 /
    signal hook 不挡且其 relay 进 to_launch / 空配置 no-op / 未知相位抛。"""
    from lib import lifecycle as lc
    HR, BG = lc.HookResult, lc.BackgroundSpec

    seen: list = []
    reg = {
        "ok":   lambda repo, paths: HR("ok", ok=True, summary="fine"),
        "bad":  lambda repo, paths: HR("bad", ok=False, summary="boom"),
        "boom": lambda repo, paths: (_ for _ in ()).throw(RuntimeError("kaboom")),
        "sig":  lambda repo, paths: HR("sig", ok=True, relay=BG("sig", ["run", "x"])),
        "spy":  lambda repo, paths: (seen.append(paths), HR("spy", ok=True))[1],
    }

    # 相位范围下传到 handler：dispatch 不解释 paths，只如实转交（None 与 [] 是两种不同的语义，
    # 见 select_units——「不知道范围」vs「知道且为空」，dispatch 不得把它们抹平）
    lc.dispatch("post_commit", "/r", names=["spy"], registry=reg, paths=["cli/a.py"])
    lc.dispatch("post_commit", "/r", names=["spy"], registry=reg, paths=[])
    lc.dispatch("post_commit", "/r", names=["spy"], registry=reg)
    assert seen == [["cli/a.py"], [], None], seen

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
    reg["soft"] = lambda repo, paths: HR("soft", ok=False, advisory=True, summary="advisory boom")
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

def test_lint_passport_is_bound_to_content_not_to_edit_events():
    """lint 通行证绑**内容指纹**，不绑「有没有人报告过改动」。

    旧机制（`edits_since_lint`）由 PostToolUse 计数，而那个 hook 只认 Edit/Write/NotebookEdit：
    **Codex 用 apply_patch 改文件一次都不会计**（它的 matcher 甚至写了 apply_patch，handler 却
    第一行就 return，意图与实现早已漂开），Bash 里的 `sed -i` / 脚本更不会。于是计数器读出的 0
    是「没人报告」而不是「没改过」——一个只在部分 CLI、部分工具上生效的计数器守不住硬 gate。
    这条测试就模拟那类「hook 看不见的改动」：**不经任何 devloop hook**，直接写文件。
    """
    import json as _json

    from lib import repo_layout, repo_resolve
    from lib.context import RepoContext
    from lib.lifecycle import checks
    bash = _load_hook("pretool_policy_bash")

    R = str(Path("/tmp/dlut_passport").resolve())   # canonical：config 的 repos key 也用它
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/cli"); os.makedirs(f"{R}/.devloop")
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "feat/x")
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    Path(f"{R}/cli/Makefile").write_text("lint:\n\ttrue\n")
    Path(f"{R}/.devloop/config.json").write_text(
        _json.dumps({"lifecycle": {"repos": {R: {"pre_commit": ["lint"]}}}}))
    _git(R, "add", "-A"); _git(R, "commit", "-qm", "init")
    RepoContext.refresh_all(R)

    def commit_denied():
        return bash.decide(_hook_input("Bash", {"cwd": R, "session_id": "",
                                                "tool_input": {"command": "git commit -m x"}}))

    Path(f"{R}/cli/a.py").write_text("x = 1\n")
    assert commit_denied(), "lint 从未跑过 → 必须拦"
    assert checks.lint(R).ok
    assert commit_denied() is None, "lint 刚过、内容没动 → 必须放行"

    # 「hook 看不见的改动」：直接写盘，不经 Edit/Write 事件。旧的计数器在这里恒为 0 → 放行。
    Path(f"{R}/cli/a.py").write_text("x = 2\n")
    assert commit_denied(), "改过 tracked 文件却仍放行 —— 正是计数器机制的洞"

    # 未跟踪的新文件改内容：路径没变，diff 也看不出来，但 lint 会 lint 它 → 指纹必须变
    assert checks.lint(R).ok
    Path(f"{R}/cli/newfile.py").write_text("y = 1\n")     # 新增未跟踪
    assert commit_denied()
    assert checks.lint(R).ok
    Path(f"{R}/cli/newfile.py").write_text("y = 2\n")     # 未跟踪文件**改内容**，路径不变
    assert commit_denied(), "未跟踪文件的内容改动必须让通行证作废（diff 抓不到，hash bytes 才抓得到）"

    # 删除同样让通行证作废（tombstone）
    assert checks.lint(R).ok
    os.remove(f"{R}/cli/newfile.py")
    assert commit_denied()

    # 指纹算不出（None）→ 按未验证，fail-closed：宁可多拦一次，不可拿不准还放行
    unit = repo_layout.CodeUnit.at(f"{R}/cli", R)
    assert repo_resolve.unit_fingerprint("/tmp/definitely-not-a-repo-xyz", unit) is None


def test_dispatch_reaches_the_real_lint_handler():
    """dispatch 必须能真的调到**内置** lint/test handler，而不只是调到测试用的假 registry。

    这条红过、且被 code-review 抓到：dispatch 曾位置传 `handler(repo, paths)`，而 lint/test 的
    `paths` 在 `*,` 之后是 keyword-only → TypeError → 被 gate 的 fail-closed 收敛成 ok=False
    → **每一次 gcampr 都被静默挡掉**（不是崩，是「lint 没过」）。而全部 dispatch 测试都用假
    registry（`lambda repo, paths:` 位置可接），真 handler 一次没被 dispatch 调到，所以全绿——
    这条测试补的就是那个洞：假替身接得住的调用约定，真身未必接得住。
    """
    import json as _json

    from lib import lifecycle as lc
    from lib.context import RepoContext

    # macOS 的 /tmp 是指向 /private/tmp 的软链：config 的 repos key 用 canonical 路径，若拿
    # 未解析的 /tmp/... 去 dispatch 就匹配不上、names 落空 → results 为空 → proceed 空过为 True，
    # 这条测试会「绿着什么都没测」。故全程用 canonical 路径。
    R = str(Path("/tmp/dlut_dispatch_real").resolve())
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/cli"); os.makedirs(f"{R}/.devloop")
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    Path(f"{R}/cli/Makefile").write_text("lint:\n\ttrue\ntest:\n\ttrue\n")
    Path(f"{R}/.devloop/config.json").write_text(
        _json.dumps({"lifecycle": {"repos": {R: {"pre_commit": ["lint", "test"]}}}}))
    _git(R, "add", "-A"); _git(R, "commit", "-qm", "init")
    RepoContext.refresh_all(R)
    Path(f"{R}/cli/a.py").write_text("x = 1\n")

    res = lc.dispatch("pre_commit", R)          # 不给 registry → 走真实 _BUILTIN 解析
    # 先钉住「真的跑了」：config 没读到 → names 空 → results 空 → proceed 空过为 True，
    # 那样这条测试会绿着什么都没测（正是它要防的那类假绿）。
    assert {r.name for r in res.results} == {"lint", "test"}, res.results
    assert res.proceed, [f"{r.name}: {r.summary}" for r in res.results]
    assert all("errored" not in r.summary for r in res.results), \
        [r.summary for r in res.results]        # TypeError 会被 fail-closed 收敛成这个形状
    # 范围确实下传到了真 handler（而不是被它自己重算）
    assert all("changed files under: cli" in r.summary for r in res.results)


def test_phase_scope_survives_a_clean_tree():
    """commit 之后（工作树已干净）的相位必须仍按**本次改动**收范围，不得退化成跑全仓。

    这条红过的样子：post_commit / pre_mr 的 handler 手里只有 repo，去读工作树得到「什么都没改」
    → select_units 读成「不知道范围」→ repo-wide 枚举全部 unit → 一个你根本没碰、却有存量 lint
    错误的 unit 让 gate fail → commit 已落地，push 和 MR 全被拦。
    """
    from lib import repo_resolve
    from lib.lifecycle import checks
    sgo = _load_script("smart_git_ops")

    R = "/tmp/dlut_phase_scope"
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(f"{R}/cli"); os.makedirs(f"{R}/legacy")
    _git(R, "init", "-q", "-b", "main")
    _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    Path(f"{R}/cli/Makefile").write_text("lint:\n\ttrue\n")            # 你改的 unit：干净
    Path(f"{R}/cli/pyproject.toml").write_text("[project]\nname = 'cli'\nversion = '0'\n")
    Path(f"{R}/legacy/Makefile").write_text("lint:\n\tfalse\n")        # 没碰的 unit：存量坏 lint
    Path(f"{R}/legacy/pyproject.toml").write_text("[project]\nname = 'legacy'\nversion = '0'\n")
    _git(R, "add", "-A"); _git(R, "commit", "-qm", "init")
    _git(R, "checkout", "-q", "-b", "feat/x")
    Path(f"{R}/cli/a.py").write_text("x = 1\n")
    _git(R, "add", "-A"); _git(R, "commit", "-qm", "touch cli only")   # 工作树现在是干净的

    # 相位边界各自算出的范围：post_commit=刚落地那个 commit；pre_mr=整条分支 vs target
    assert repo_resolve.committed_paths(R) == ["cli/a.py"]
    assert repo_resolve.range_paths(R, "main") == ["cli/a.py"]
    assert sgo.phase_paths(R, "pre_commit", "main") is None            # pre_commit：工作树即答案
    assert sgo.phase_paths(R, "post_commit", "main") == ["cli/a.py"]

    # 有范围 → 只跑 cli，legacy 的存量坏 lint 拦不到你
    res = checks.lint(R, paths=["cli/a.py"])
    assert res.ok, f"没碰 legacy 却被它拦下：{res.summary}"
    assert "changed files under: cli" in res.summary and "legacy" not in res.summary

    # 对照：范围丢失（老行为）→ clean tree 回落 repo-wide → 被无关 unit 拦下
    degraded = checks.lint(R)
    assert not degraded.ok and "clean tree, all units" in degraded.summary

    # 「知道范围且为空」≠「不知道范围」：前者 0 个 unit 干净跳过，后者才全跑
    assert repo_resolve.select_units(R, paths=[]).units == ()
    assert len(repo_resolve.select_units(R).units) == 2

    # git 算不出范围 → **None（不知道）而非 []（知道且为空）**。gitcmd 是 failure-safe 的，
    # 两者原始输出都是空；读成 [] 就等于 origin/<target> 没 fetch 时静默跳过整个 lint gate。
    assert repo_resolve.range_paths(R, "origin/nope-not-fetched") is None
    assert repo_resolve.committed_paths(R, "deadbeef") is None
    assert sgo.phase_paths(R, "pre_mr", "nope-not-fetched") is None    # → handler 回落全跑，不放行


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
