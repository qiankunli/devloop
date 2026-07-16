"""repo_dir / repo_code_dir / language / AGENTS.md location helpers. Pure stdlib.

Routes the git call through `gitcmd`
(the single git runner) instead of an inline subprocess.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import ecosystem, gitcmd


@dataclass(frozen=True)
class CodeUnit:
    """仓库内一个自带工具链、可独立 build/lint/test 的目录（make/uv 的 workdir）。

    一个 git 仓可含多个 unit（`server/` + `cli/`、`packages/*`、`cmd/*`）；「操作落在哪个
    unit」由**操作目标路径**决定，不由 repo 的单值属性决定——这正是单值 `code_dir` 在多代码
    目录仓上选错目录的根因。path+id+language 在解析边界一次算清、绑成一个值对象随解析结果下传，
    消费方不再各自重推（与 `Repo` 同动机，低一层）。

    unit 有**两个身份，别混**：
    - `path`：绝对路径 = 执行事实「这次在哪跑 make」。随 checkout 变（worktree 里是
      `<repo>/.worktrees/foo/server`）。
    - `id`：仓相对路径（`.` / `server` / `cli`）= **持久化身份**，跨 checkout 稳定。落
      `.devloop` 的一切 key 用它（见 `context/repo.Validation`）。用 `path` 当 key 会让同一个
      unit 在 worktree 与主 checkout 下拿到不同 key——而 validation 段统一落**主仓**
      `branches/<b>/`，于是 worktree 里 lint 过的戳回主 checkout 就查不到（白跑一遍），且 key
      随 worktree 增删无限累积。

    `id` **必填、且只由 `at()` 推**：给它默认值的话，生产路径漏传就是个静默的空 key（多个 unit
    撞进同一个戳），比不设身份更糟；而让消费方自己算 id 就要各自再传一次 git_root，root 传错
    （worktree 里传主仓根）算出的 key 是错的——绑在出生点上，这两类错都不可表达。

    unit 也拥有**自己的工具链动作**（`has_target` / `lint_target` / `test_target`）：一个
    unit「能不能 / 该怎么 lint/test」是它自己的事实，收在这里，消费方（checks / gate rules）
    直接问 unit，而不是各自拿 `str` 路径去重解析 Makefile（避免 str 路径当隐式协议、同一判断
    出现多份实现）。"""
    path: str
    id: str
    language: str | None = None

    @classmethod
    def at(cls, path: str | Path, git_root: str | Path) -> "CodeUnit":
        """在 `git_root` 这个 checkout 里、位于 `path` 的 unit——**唯一**的构造入口（生产侧）。
        身份与语言在这里一次算清，之后谁都不用再碰 git_root。"""
        p = Path(path).resolve()
        root = Path(git_root).resolve()
        try:
            uid = p.relative_to(root).as_posix()
        except ValueError:
            # unit 落在仓外（不该发生）：退回绝对路径而不是抛——身份算不准最多让戳对不上
            # （fail-closed，多跑一次 lint），把关路径上崩掉才是真事故。
            uid = p.as_posix()
        return cls(str(path), uid, ecosystem.detect_language(path))

    def has_target(self, name: str, *, suffix: bool = False) -> bool:
        """本 unit 的 Makefile 是否有名为 `name` 的 target。suffix=True 时 `name-ci` /
        `name-local` 也算命中（用于「有没有这类目标」的宽判）。"""
        mk = Path(self.path) / "Makefile"
        if not mk.exists():
            return False
        pat = rf"^{re.escape(name)}(-\w+)?\s*:" if suffix else rf"^{re.escape(name)}\s*:"
        try:
            return bool(re.search(pat, mk.read_text(encoding="utf-8"), re.MULTILINE))
        except OSError:
            return False

    def lint_target(self) -> str | None:
        """要跑的 lint target：`lint-ci` > `lint`。`lint-ci` 通常先 `uv sync` 钉版工具链，
        跑 plain `lint` 用本地新版 formatter 会本地过、CI 挂；有 lint-ci 就用它与 CI 对齐。
        无 → None（无 lint 目标，干净跳过）。"""
        for t in ("lint-ci", "lint"):
            if self.has_target(t):
                return t
        return None

    def test_target(self) -> str | None:
        """要跑的 test target：`test` > `test-ci` > `test-local`。**探测即执行**——返回真正
        存在的目标名，判据与执行对齐（旧代码判「有测试」用宽判、却硬跑 `make test`，只有
        `test-ci` 的仓会误判成有测试再报错）。无 → None（干净跳过）。"""
        for t in ("test", "test-ci", "test-local"):
            if self.has_target(t):
                return t
        return None

    def test_command(self) -> tuple[str, ...] | None:
        """本 unit 的 canonical test 命令。Makefile target 优先；无 Makefile 时回落所属
        生态的 canonical 命令（如 Go 的 `go test ./...`），避免多 unit 仓已正确识别的
        unit 因没有 Makefile 被误跳过。"""
        target = self.test_target()
        if target is not None:
            return ("make", target)
        eco = ecosystem.detect(self.path)
        return eco.fallback_test_command(self.path) if eco else None


def find_git_root(cwd: str | Path) -> str | None:
    """Find git root by walking up. Returns absolute path or None if not in a git repo."""
    r = gitcmd.git(cwd, "rev-parse", "--show-toplevel", timeout=3)
    return r.out if r.ok and r.out else None


def is_git_repo(path: str | Path) -> bool:
    return find_git_root(path) is not None


def find_repo_code_dir(repo_dir: str | Path) -> str:
    """Find the actual code directory inside a repo.

    Python projects often use server/ or backend/ as repo_code_dir.
    Go and TS projects usually use repo_dir itself.
    """
    repo_dir = Path(repo_dir)
    for sub in ("server", "backend"):
        candidate = repo_dir / sub
        if candidate.is_dir() and _is_code_unit(candidate):
            return str(candidate)
    return str(repo_dir)


def default_code_unit(git_root: str | Path) -> CodeUnit:
    """repo 级默认 unit：没有更具体的操作目标路径时用（如按名字 /enter 一个仓、cwd 就是仓根）。
    探测规则同 `find_repo_code_dir`（`server/` > `backend/` > repo 根）。"""
    return CodeUnit.at(find_repo_code_dir(git_root), git_root)


def owning_code_unit(target: str | Path, git_root: str | Path) -> CodeUnit | None:
    """`target`（文件或目录）**属于**哪个 code unit——从它向上找最近的项目清单目录
    （`_is_code_unit`），边界止于 `git_root`。**没有任何 unit 拥有它就是 `None`**。

    `None` 是个**答案**，不是失败：仓根的 `README.md` / `docs/` / `.github/` 在「根不是 unit」
    的仓（`server/` + `cli/`，doctor 就是）里，确实不落在任何 unit 的项目边界内。硬给它派一个
    unit 就是编造归属——而唯一能派的 `default_code_unit` 是**选择**启发式（`server/` > `backend/`
    > 根），它答的是「没有具体目标时该选谁」，不是「这个文件属于谁」。让它来答归属，得到的是
    「改 README → 跑 server 的 lint」这种没有任何理由的结论（为什么是 server 不是 cli？）。

    走到仓根时**根自己是 unit 就是它**（判据同子目录、同 `discover_code_units`）：catalog 说根
    是 unit，归属就不能把根的文件判给别人，否则同一个文件走 clean-tree 全量和走改动投影会得到
    两个不同的 unit。
    """
    root = Path(git_root).resolve()
    p = Path(target).resolve()
    cur = p if p.is_dir() else p.parent
    # 只在 git_root 严格子目录里向上走；命中项目清单即为该 unit。
    while cur != root and root in cur.parents:
        if _is_code_unit(cur):
            return CodeUnit.at(cur, git_root)
        cur = cur.parent
    return CodeUnit.at(root, git_root) if _is_code_unit(root) else None


def enclosing_code_unit(target: str | Path, git_root: str | Path) -> CodeUnit:
    """站在 `target` 时**操作落在**哪个 code unit——必给一个答案：有 owner 就是它，没有则回落
    `default_code_unit` 的选择启发式（`server/` > `backend/` > 根）。

    与 `owning_code_unit` 的差别是**问的问题不同**，别混：

    - **归属**（`owning_code_unit`，「这个文件属于谁」）：README 不属于任何 unit，这是事实，
      答 `None` 才对。改动投影 / 指纹用它——凭空派个 unit 会让纯文档改动去跑那个 unit 的 lint。
    - **站位**（本函数，「我在这儿干活，算哪个 unit」）：站在仓根跑 `pip install`，总得有个 unit
      来判它是不是 uv 仓；`run_tests.py doctor` 走默认 `server/` 也是这一支。这时「没有具体目标
      该选谁」**正是**要问的问题，default 的启发式是对的问题的对的答案。

    命令侧 guard（`pip_install` / `pytest_naked`）与 `select_units(explicit=…)` 用本函数。"""
    return owning_code_unit(target, git_root) or default_code_unit(git_root)


# repo-wide 验证（clean tree 从仓根发起、无具体改动可依据）枚举全部 unit 时跳过的目录：
# VCS / 依赖 / 虚拟环境 / 构建产物 / 各类缓存——不是本仓的项目，进去只会拖慢扫描。
# node_modules / vendor 尤其要跳：里面每个包都带 package.json / go.mod，不跳会扫出成百上千个「unit」。
_DISCOVER_SKIP = {
    ".git", "node_modules", ".venv", "venv", "env", ".tox",
    "dist", "build", "target", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".idea", ".vscode", "vendor",
}


def discover_code_units(git_root: str | Path, *, max_depth: int = 4) -> list[CodeUnit]:
    """枚举 repo 内**全部**独立 code unit（带语言项目清单的目录，见 `_is_code_unit`），供
    repo-wide 验证用。

    遇到一个 unit 就收下、**不再下钻它内部**（unit 内的嵌套清单不算独立 unit）；跳过 VCS /
    依赖 / 构建产物目录，并限制递归深度。多代码目录仓（`server/` + `cli/`）因此返回全部 unit。

    **仓根不是特例**：判据与子目录同一条，所以「根是 Go module + `server/` 是 Python unit」的仓
    会同时收到两者。这里刻意**不问 `default_code_unit`**——那是「没有更具体
    目标时该选谁」的**选择**启发式（`server/` > `backend/` > 根），拿它回答「根有没有工具链」这个
    **目录事实**问题，答案会被 `server/` 的存在改写：`server/` 在时它返回 `server/`，而 `server/`
    早被 walk 收过了，于是「补根」永远补不进，根的 go.mod 就从 catalog 里消失。

    存在的意义是「绝不静默选 server」：clean tree 从仓根跑验证时要全跑（或让用户显式选），
    绝不退回单值默认 unit——所以这里返回「全部」而非「某一个」。"""
    root = Path(git_root).resolve()
    units: list[CodeUnit] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for child in children:
            # code unit 只属于当前 repo：不跟随仓内软链（可能自指/指向仓外），
            # 也不进嵌套 repo/worktree（.git 可为目录或 worktree 的文件）。
            if (child.name in _DISCOVER_SKIP or child.name.startswith(".") or
                    child.is_symlink() or not child.is_dir() or (child / ".git").exists()):
                continue
            if _is_code_unit(child):
                units.append(CodeUnit.at(child, root))
                # 命中即止：不下钻，unit 内部的子 marker（如 packages 内嵌）不当独立 unit
            else:
                walk(child, depth + 1)

    # 根自身是 unit 就收下，与子目录同一判据。walk 只看子目录、从不收根，故无需去重。
    if _is_code_unit(root):
        units.append(CodeUnit.at(root, root))
    walk(root, 1)
    if not units:
        # 全仓一份项目清单都没有：仍给出唯一 unit（探测出的 server/backend/根），绝不返回空——
        # 空会被消费方读成「没什么要验的」而静默跳过。这里才轮到选择启发式登场。
        units.append(default_code_unit(root))
    return units


def _is_code_unit(path: Path) -> bool:
    """`path` 自身是不是一个 code unit —— 它带没带某个语言的**项目清单**。

    **刻意不看 `Makefile`**：Makefile 是 unit 的**动作入口**（怎么 lint/test，见 `lint_target`），
    不是它的**身份**。两者正交——doctor 的 `server/`(pyproject) 和 `cli/`(package.json) 都有
    Makefile，但让它们成为 unit 的是前者。反过来，`docs/` 里放个 sphinx 的 Makefile 不该让 docs
    变成 code unit，仓根的编排 Makefile（`make dev` 转发到子目录）也不该让仓根变成 code unit。

    **也不看 `requirements.txt`**：那是依赖清单不是项目边界（常见形态恰恰是仓根放一份给容器构建、
    真项目在 `server/`——认它就等于把仓根误判成 unit，正是这里要消灭的那类误判）。它仍是
    `ecosystem.detect_language` 的语言线索：「这是什么语言」和「这是不是一个项目」是两个问题。

    判据数据归各生态自己（`lib/ecosystem/`——语言差异的唯一入口），这里只问"有没有生态认领"。
    """
    return ecosystem.detect(path) is not None


def find_agents_md(repo_dir: str | Path, repo_code_dir: str | Path | None = None) -> str | None:
    """Locate AGENTS.md. Prefer repo_code_dir, fallback to repo_dir."""
    candidates = []
    if repo_code_dir:
        candidates.append(Path(repo_code_dir) / "AGENTS.md")
    candidates.append(Path(repo_dir) / "AGENTS.md")
    for c in candidates:
        if c.exists():
            return str(c.resolve())
    return None


def expand_user_path(path: str) -> str:
    """Expand ~ and env vars in a config path."""
    return os.path.expanduser(os.path.expandvars(path))
