#!/usr/bin/env python3
"""guard 命令解析（cmdtree/cmdparse）：git 调用识别、-C/cd 归因、粘连操作符、子 shell 作用域。

Standalone: `python3 devloop/tests/test_cmdparse.py`（也 pytest-collectable）；共享设施见 _testkit.py。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from _testkit import _git, _load_hook, run_main  # noqa: E402  (bootstrap first)
from hooks.cmdtree import cmdparse  # noqa: E402


def test_cmdparse_git_invocations():
    gi = cmdparse.git_invocations
    assert [i.subcommand for i in gi("git commit -m x")] == ["commit"]
    # false negative fixed: global options before the subcommand
    assert gi("git -C /repo commit")[0].subcommand == "commit"
    assert gi("git -c user.name=x push")[0].subcommand == "push"
    assert gi("GIT_DIR=.git git commit")[0].subcommand == "commit"
    assert gi("/usr/bin/git push")[0].subcommand == "push"
    # false positive fixed: pattern inside quoted text is NOT a git invocation
    assert gi('echo "git add -A"') == []
    assert gi("grep -r 'git commit' .") == []
    # operator inside quotes must not split the command
    assert [i.subcommand for i in gi('git commit -m "a && b"')] == ["commit"]
    # chained commands
    assert [i.subcommand for i in gi("cd r && git push")] == ["push"]
    # add -A detection (incl. -C form)
    assert gi("git add -A")[0].args == ["-A"]
    assert gi("git -C r add -A")[0].subcommand == "add"
    # -C target captured so guards can judge the right repo
    assert gi("git -C /repo commit")[0].dash_c == "/repo"
    assert gi("git commit")[0].dash_c is None

def test_protect_branch_checks_dash_c_target():
    """Codex #4: protect guard must judge the `-C` target repo, not the caller's cwd."""
    pb = _load_hook("pretool_policy_bash")
    from hooks import hook_io
    from lib.context import RepoContext
    R = "/tmp/dlut_prot"
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    _git(R, "init", "-q"); _git(R, "config", "user.email", "t@t.t"); _git(R, "config", "user.name", "t")
    _git(R, "checkout", "-q", "-b", "master")
    Path(f"{R}/f").write_text("x"); _git(R, "add", "f"); _git(R, "commit", "-qm", "i")
    RepoContext.refresh_all(R)

    def hi(cmd, cwd):
        return hook_io.HookInput(event="PreToolUse", tool_name="Bash",
                                 tool_input={"command": cmd}, cwd=cwd, raw={})

    # the hole: `git -C <master repo> commit` from a NON-repo cwd (e.g. workspace root)
    assert pb.decide(hi(f"git -C {R} commit -m x", "/tmp"))
    assert pb.decide(hi("git commit -m x", R))                      # plain, on master
    _git(R, "checkout", "-q", "-b", "feat/x"); RepoContext.refresh_all(R)
    assert pb.decide(hi("git commit -m x", R)) is None              # feature branch → allow
    assert pb.decide(hi(f"git -C {R} commit -m x", "/tmp")) is None  # -C feature repo → allow

def test_cmdparse_commands():
    assert cmdparse.commands("PYTHONPATH=. pytest x")[0][0] == "pytest"   # env stripped
    assert cmdparse.first_token_is("make test", "make") is True
    assert cmdparse.first_token_is('echo "make test"', "make") is False

def test_cmdparse_docstring_api_list_is_real():
    """模块 docstring 自称的 Public API 必须真的存在——删了函数忘删文档，读者会照着调一个
    不存在的名字。这类漂移是机械可查的，别留给下一次 code-review（`segments` 被删后就在那份
    清单里滞留了一版，正是 review 抓到的）。"""
    import re as _re
    doc = cmdparse.__doc__ or ""
    api = doc.split("Public API", 1)[1] if "Public API" in doc else ""
    assert api, "模块 docstring 丢了 Public API 段"
    # 反引号里首字母小写的标识符 = 函数名（大写的是 Invocation/GitInvocation 这些类型，
    # `.env` 这类带点的是属性，都不在此列）
    for name in _re.findall(r"`([a-z_][a-z0-9_]*)`", api):
        assert hasattr(cmdparse, name), f"docstring 里的 Public API `{name}` 不存在"

def test_git_invocation_cd_prefix():
    """git_invocations 按位置跟踪 cd 前缀(取代 last_cd_target)——聚合工作区里 session
    cwd 停在 workspace 根,inp.cwd 不是命令真正触达的仓库;相对 cd 链按 shell 语义组合
    (`cd a && cd b` → a/b,旧 last-cd-wins 会错算成 b)。"""
    def cds(cmd):
        return [inv.cd for inv in cmdparse.git_invocations(cmd)]
    assert cds("cd /a/b && git commit -m 'x'") == ["/a/b"]
    assert cds("cd a && make && cd b && git push") == [os.path.join("a", "b")]
    assert cds("git commit -m 'cd /tmp'") == [None]    # 引号内不算
    assert cds("echo cd /x; git fetch") == [None]      # cd 不是命令词
    assert cds("git fetch && cd /x && git push") == [None, "/x"]  # 位置感知

def test_affected_roots_parsed_not_regex():
    """PostToolUse 刷新改 parsed 判定:`git -C repo commit` / `cd repo && git push`
    都解析到正确的 effective repo;引号内文本与非状态子命令不触发。"""
    pgr = _load_hook("posttool_git_refresh")
    W = "/tmp/dlut_ar"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/repo")
    _git(f"{W}/repo", "init", "-q")
    expected = {str(Path(f"{W}/repo").resolve())}
    def roots(cmd, cwd=W):
        return {str(Path(r).resolve()) for r in pgr.affected_roots(cmd, cwd)}
    assert roots(f"git -C {W}/repo commit -m x") == expected          # -C,cwd 非仓库
    assert roots(f"cd {W}/repo && git push") == expected              # cd 前缀
    assert roots("git -C repo fetch", cwd=W) == expected              # -C 相对路径
    assert roots('echo "git commit"') == set()                        # 引号内不算
    assert roots(f"cd {W}/repo && git status") == set()               # 非状态子命令
    assert roots("git commit -m x") == set()                          # cwd 不是仓库

def test_cmdparse_contract_table():
    """guard 协议层契约表:cmdparse 是全部硬拦截的共同地基,把真实踩过的 shell 形态
    固化成表——语义回退会让 guard 集体误判(误拦 kubectl+uv)或漏判(cd 前缀绕过)。"""
    # (command, 期望的段头序列)
    HEADS = [
        ("git push && cd other", ["git", "cd"]),                                  # 后置 cd 独立成段
        ("cd a && git commit -m x && cd b", ["cd", "git", "cd"]),                 # cd 夹击
        ("kubectl -o jsonpath='{range .items[*]}{\"\\n\"}{end}'; git status", ["kubectl", "git"]),  # 引号紧贴 ;
        ('echo "git add -A"', ["echo"]),                                          # 引号内不是调用
        ("FOO=1 BAR=2 git -C /tmp/r fetch", ["git"]),                             # env 前缀剥离
        ("make&&go test", ["make", "go"]),                                        # 胶连运算符
    ]
    for cmd, heads in HEADS:
        got = [os.path.basename(s[0]) for s in cmdparse.commands(cmd)]
        assert got == heads, f"{cmd!r}: {got} != {heads}"
    # git 调用归属:-C 绝对优先 / -C 相对叠在 cd 前缀上 / 后置 cd 不偷归属
    inv = cmdparse.git_invocations("FOO=1 git -C /tmp/r fetch")[0]
    assert inv.subcommand == "fetch" and inv.run_dir("/base") == Path("/tmp/r")
    inv = cmdparse.git_invocations("cd sub && git -C nested commit -m x")[0]
    assert inv.run_dir("/base") == Path("/base/sub/nested")
    inv = cmdparse.git_invocations("git push && cd /elsewhere")[0]
    assert inv.run_dir("/base") == Path("/base")
    # run_dir 规范化 `..`(否则 find_git_root 从带 .. 的路径起步会偏)
    inv = cmdparse.git_invocations("cd a && cd .. && git status")[0]
    assert inv.run_dir("/base") == Path("/base")

def test_cd_position_aware_attribution():
    """cd 前缀按位置生效,不是 last-cd-wins:`git checkout x && cd <非仓库>` 曾把
    checkout 归到 cd 目标,branch.json 不刷新、注入滞留已删分支;
    `cd subrepo && git commit` 的前缀语义保持不变(guards 也经 run_dir 受益)。"""
    pgr = _load_hook("posttool_git_refresh")
    W = "/tmp/dlut_cdpos"
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(f"{W}/repo"); os.makedirs(f"{W}/notrepo")
    _git(f"{W}/repo", "init", "-q")
    expected = {str(Path(f"{W}/repo").resolve())}
    def roots(cmd, cwd):
        return {str(Path(r).resolve()) for r in pgr.affected_roots(cmd, cwd)}
    # cd 在 git 之后:归属仍是发起时的 cwd(修复点)
    assert roots(f"git checkout -q master && cd {W}/notrepo && python3 x.py", cwd=f"{W}/repo") == expected
    # cd 前缀在前:照旧解析到目标仓库
    assert roots(f"cd {W}/repo && git push && cd {W}/notrepo", cwd=W) == expected
    # 相对 cd 链组合
    assert roots(f"cd {W} && cd repo && git fetch", cwd="/") == expected

def test_cmdparse_glued_operators():
    """运算符紧贴词尾时也要断句:shlex.split 会把 `jsonpath='...';` 的 `;` 吞进
    token,于是断不开句——cd 落到段中而非段头,workspace guard 的 cd 豁免
    失效,kubectl+cd+uv 串被误拦。punctuation_chars 化后修复。"""
    from hooks.cmdtree import cmdparse
    cmd = ("kubectl -o jsonpath='{range .items[*]}{\"\\n\"}{end}'; "
           "cd /tmp/sub && uv run x.py")
    assert [s[0] for s in cmdparse.commands(cmd)] == ["kubectl", "cd", "uv"]
    assert [s[0] for s in cmdparse.commands("make&&go test")] == ["make", "go"]
    # 引号内的运算符不断句(既有语义不回退)
    assert [s[0] for s in cmdparse.commands('echo "a; b" && make x')] == ["echo", "make"]

def test_cmdparse_subshell_scope():
    """AST 解析(Parable)拿到扁平模型拿不到的结构:子 shell 的 `(` 不再掩盖命令词,
    子 shell 的 cd 不外泄,命令替换里的 git 也被看见。"""
    from hooks.cmdtree import cmdparse
    # `(` 不再掩码命令词:workspace guard 能同时看到 cd 与 uv(原误拦的 case)
    assert [s[0] for s in cmdparse.commands("(cd repo && uv run pytest)")] == ["cd", "uv"]
    # cd 在子 shell 内对同 shell 的命令生效……
    assert [i.cd for i in cmdparse.git_invocations("(cd x && git push)")] == ["x"]
    # ……但不外泄给子 shell 之后的兄弟命令(扁平模型做不到的 soundness)
    assert [i.cd for i in cmdparse.git_invocations("(cd x); git push")] == [None]
    # brace group 的 cd 留在本 shell → 会外泄(与子 shell 相反)
    assert [i.cd for i in cmdparse.git_invocations("{ cd y; git status; }")] == ["y"]
    # 命令替换 `$(…)` 里的 git 也要被看见(否则 protect 守卫漏判),且其 cd 隔离
    assert [i.subcommand for i in cmdparse.git_invocations("echo $(git push)")] == ["push"]
    assert [i.cd for i in cmdparse.git_invocations("echo $(cd z && git push)")] == ["z"]
    assert cmdparse.git_invocations('echo "git push"') == []   # 引号内仍不算

def test_cmdtree_parser_protocol():
    """解析后端符合 cmdtree.base.Parser 接口(具名 Protocol)——这正是"可随时替换"的契约:
    换 parser 只要再写一个暴露 `parser`(带 `parse(str)->Node`)的后端模块。"""
    from hooks.cmdtree import base
    from hooks.cmdtree import parable as parable_backend
    assert isinstance(parable_backend.parser, base.Parser)        # runtime_checkable
    assert isinstance(parable_backend.parser.parse("git push"), base.Seq)

def test_cmdparse_command_invocations():
    """每个命令是一个 Invocation(argv + 作用域感知 cd),run_dir(base) 算出有效目录——守卫据此
    判某命令实际在哪执行,而非只看"有没有 cd token"。"""
    Inv = cmdparse.Invocation
    ci = cmdparse.command_invocations
    assert ci("cd x && uv run pytest") == [
        Inv(argv=["cd", "x"], cd=None),
        Inv(argv=["uv", "run", "pytest"], cd="x"),
    ]
    # 子 shell 的 cd 不归属其后的兄弟命令
    uv = [v for v in ci("(cd sub); uv run pytest") if v.argv[0] == "uv"][0]
    assert uv.cd is None
    assert ci("PYTHONPATH=. pytest x")[0].argv[0] == "pytest"   # env 同 commands() 剥离
    # env 被剥掉但**不丢**：「带没带 env 前缀」是调用自身的事实,有规则据此判定
    # (naked-pytest: `PYTHONPATH=. pytest` 不算裸)。它与 cd 出自同一次解析,规则才可能同时要到
    # env 和 run_dir——env 曾只能靠另一个**不追踪 cd** 的 walker 拿原始 token,那条路上二者只能选一个。
    assert ci("PYTHONPATH=. pytest x")[0].env == ["PYTHONPATH=."]
    assert ci("pytest x")[0].env == []
    assert ci("A=1 B=2 pytest")[0].env == ["A=1", "B=2"]
    inv = ci("cd cli && PYTHONPATH=. pytest")[-1]
    assert inv.env == ["PYTHONPATH=."] and inv.cd == "cli" and inv.run_dir("/r") == Path("/r/cli")
    assert ci("git -C sub push")[0].env == []                   # GitInvocation 也带 env 字段
    # run_dir 把 cd 叠在 base 上
    assert Inv(argv=["uv"], cd="sub").run_dir("/ws") == Path("/ws/sub")
    assert Inv(argv=["uv"], cd=None).run_dir("/ws") == Path("/ws")
    # go/make 自带的 `-C <dir>` 同 git -C：chdir 后再跑,run_dir 要据此落到真目录(否则在根上误拦)
    assert ci("go -C /repo build ./...")[0].dash_c == "/repo"
    assert ci("go -C /repo build ./...")[0].run_dir("/ws") == Path("/repo")
    assert ci("make -C sub test")[0].run_dir("/ws") == Path("/ws/sub")
    assert ci("make -Csub test")[0].run_dir("/ws") == Path("/ws/sub")   # make 粘连写法
    assert ci("go build ./...")[0].dash_c is None


if __name__ == "__main__":
    run_main(globals())
