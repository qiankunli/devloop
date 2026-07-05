"""保护分支上拦 `git commit` / `git push`。

每条 commit/push 按它自己的目标 repo（`-C <dir>` 或 cwd）判定，故从 workspace 根对
`git -C subrepo commit` 也能命中。gate.evaluate 读 LIVE 分支（git rev-parse），不读缓存——
经未观测渠道切到保护分支也不会漏判。

push 有两类豁免（guard 保护的是"保护分支的远端历史"，这两类都动不到它）：
1. tag-only push（`push origin refs/tags/*` / `--tags`）——发版打 tag 是站在保护分支上的
   正当操作；bare name 要本地能证明"是 tag 且非分支"才算，证明不了 fail-closed。
2. 空仓首推——远端一个分支都没有，没有可保护的历史（新仓 init 后的第一次 push main）。
commit 无豁免：保护分支的本地提交依然只走 MR 流程。
"""
from __future__ import annotations

from pathlib import Path

from lib import gitcmd, repo_layout
from lib.context import gate
from lib.core.domain import Command, Finding, Severity, TargetKind
from lib.core.protocol import Rule

# push 里取独立 token 作值的选项——不跳过的话，值会被误当 remote/refspec（fail-closed
# 方向的误判：多判不豁免，不会少判）。`--opt=val` 粘连形态天然按单个 `-` token 跳过。
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}

# 会波及分支 ref 或做远端删除的形态，一律不参与豁免判定
_PUSH_UNSAFE_FLAGS = {"--all", "--branches", "--mirror", "--follow-tags", "--delete", "-d"}


class ProtectBranchRule(Rule):
    name = "protect-branch"
    target_kind = TargetKind.COMMAND

    def applies(self, target: Command, ctx) -> bool:
        return target.subcommand in ("commit", "push")

    def check(self, target: Command, ctx) -> list[Finding]:
        git_root = repo_layout.find_git_root(target.run_dir)
        if not git_root:
            return []
        gv = gate.evaluate(git_root)
        if not gv.protected():
            return []
        if target.subcommand == "push" and _push_exempt(target.args, git_root):
            return []
        where = f" in repo '{Path(git_root).name}'" if target.dash_c else ""
        return [
            Finding(
                rule=self.name,
                severity=Severity.DENY,
                message=(
                    f"⚠️  Refusing `git commit/push` on protected branch '{gv.branch or '?'}'{where}.\n"
                    f"Create a feature branch first: `git checkout -b <name> origin/{gv.target}` "
                    f"(or use /gcampr to do it properly)."
                ),
                locator=" ".join(target.argv),
            )
        ]


def _push_exempt(args: list[str], git_root: str) -> bool:
    """这条 push 是否属于豁免形态（tag-only push / 空仓首推）。判不出一律 False。"""
    parsed = _parse_push(args)
    if parsed is None:
        return False
    remote, refspecs, tags_flag = parsed
    if refspecs:
        if all(_is_tag_refspec(s, git_root) for s in refspecs):
            return True
    elif tags_flag:
        return True  # `git push --tags`（无 refspec）：只推 tag
    return _remote_has_no_branches(git_root, remote or "origin")


def _parse_push(args: list[str]) -> tuple[str | None, list[str], bool] | None:
    """push 的 args → (remote, refspecs, tags_flag)；命中 unsafe flag 返回 None。"""
    tags_flag = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PUSH_UNSAFE_FLAGS:
            return None
        if a in _PUSH_OPTS_WITH_VALUE:
            i += 2
            continue
        if a == "--tags":
            tags_flag = True
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    remote = positional[0] if positional else None
    return remote, positional[1:], tags_flag


def _is_tag_refspec(spec: str, git_root: str) -> bool:
    """refspec 是否只动 tag ref。显式 `refs/tags/` 前缀（src 或 dst 侧）直接判；
    bare name 用本地 ref 证明是 tag 且非分支（同名分支连 git 自己都会报歧义）；
    `:dst` 空 src 是远端删除，不豁免。"""
    spec = spec.lstrip("+")
    if ":" in spec:
        src, dst = spec.split(":", 1)
        return bool(src) and dst.startswith("refs/tags/")
    if spec.startswith("refs/tags/"):
        return True
    return (
        gitcmd.git(git_root, "show-ref", "--verify", "--quiet", f"refs/tags/{spec}").ok
        and not gitcmd.git(git_root, "show-ref", "--verify", "--quiet", f"refs/heads/{spec}").ok
    )


def _remote_has_no_branches(git_root: str, remote: str) -> bool:
    """空仓首推判定：远端零分支 → 没有可保护的历史。先看本地 remote-tracking refs
    （非空即远端已有分支，免网络调用——正常 deny 路径保持廉价）；本地为空时才
    ls-remote 权威确认，失败/超时 fail-closed。"""
    if gitcmd.git(git_root, "for-each-ref", "--count=1", f"refs/remotes/{remote}").out:
        return False
    r = gitcmd.git(git_root, "ls-remote", "--heads", remote, timeout=10)
    return r.ok and not r.out
