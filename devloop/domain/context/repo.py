"""`RepoContext` — per-repo state, persisted as per-owner segment files under
`<git_root>/.devloop/` (`meta.json` / `branch.json` / `remote_branches.json` / `pr.json` /
plus branch-domain `branches/<b>/{branch,lint,test,review}.json`).
`RepoContext` is the in-memory *view* that `load()` assembles by merging them; each mutator
writes back only its own segment.

Why one file per owner: the state has several independent writer-roles (the refresh, the MR
monitor, **the lint handler**, and **the test handler**) running in different
processes — and lint/test additionally run CONCURRENTLY inside one `lifecycle.dispatch`.
That is why they own separate segments: "validation marks" was one entry in this list while
being two concurrent writers on one file, which is exactly the lost update this layout exists
to prevent (see `Validation`). A single shared
file would force a read-modify-write that can lose a concurrent writer's update; one file per
owner makes every write touch a disjoint file, so that whole class is structurally impossible
— no lock, atomic per-file writes (see base.py `_write_atomic`).

Branch model — three freshness tiers (see docs/branch-state.md):
- **identity** (`branch.local`: name + HEAD sha): cheap, volatile, owned by the *refresh*
  (local git events). This is the DISPLAY copy; write-gates re-derive identity LIVE
  (`domain.context.gate`) instead of trusting this snapshot.
- **read-freshness** (`branch.remotes`: the server's trunk tips + `remotes_fetched_at`): trunk
  moves under you when a colleague pushes — an unobservable channel — so it is owned by the
  *monitor* (`remote_branches.json`), never written by a refresh/script. ahead/behind is a
  *relationship*, derived on read against these tips, never stored.
- **write-gate** (`branch.pr_number` joined from `pr.json`): the current branch's PR/MR, also
  monitor-owned. "inactive"/"in-flight" are *derived* by joining number → prs, never stored.

`branch.local.fork_from` is the one fact git does not durably record: known exactly only when
devloop cut the branch (gcampr writes it), so the refresh PRESERVES it across rebuilds (like
pr_number) rather than recomputing it from git.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from lib import ecosystem, git_state, parsers
from domain.forge import ForgeError
from lib.forge import forge_for_repo

from .. import repo_layout
from . import base, store
from .base import (
    DEFAULT_BRANCH_TTL_SEC,
    REPO_STALE_SEC,
    AgentsMd,
    PullRequest,
    Reference,
)


def _resolve_default_branch(repo_dir: str, prev_branch: str, prev_at: float) -> tuple[str, float]:
    """The repo's default branch + when it was fetched. TTL-gated: a fresh cache is returned
    as-is (zero network); only when stale do we hit the forge (the authoritative source,
    fresher than the local origin/HEAD cache that `git fetch` never updates).

    Fail-open: a forge error / missing token falls back to refreshing the local origin/HEAD
    (git `set-head --auto`) then reading it; if even that yields nothing, the previous cached
    value is kept (and `at` left stale so the next refresh retries). The timestamp only
    advances on a successful authoritative fetch.
    """
    if prev_branch and not base.is_stale(prev_at, DEFAULT_BRANCH_TTL_SEC):
        return prev_branch, prev_at                          # fresh cache → no network
    forge = forge_for_repo(repo_dir)
    if forge is not None:
        try:
            db = (forge.default_branch() or "").strip()
            if db:
                git_state.set_local_default_head(repo_dir, db)  # sync git cache so local_default_target agrees
                return db, base.now()
        except ForgeError:
            pass
    refreshed = git_state.refresh_remote_head(repo_dir)      # network set-head (syncs origin/HEAD); True if it succeeded
    db = git_state.local_default_target(repo_dir)
    if refreshed and db:
        return db, base.now()                                # remote HEAD synced → authoritative → stamp (TTL now applies)
    return (db or prev_branch or "main"), prev_at            # offline/forge-down → best available, leave at stale to retry


# ── segment dataclasses ──────────────────────────────────────────────────────
@dataclass
class RepoMeta:
    repo_dir: str = ""        # git root as the caller referenced it (symlink preserved)
    real_repo_dir: str = ""   # Path(repo_dir).resolve() — for git IO
    code_dir: str = ""        # where make/uv run (repo root or server/ backend/)
    language: str | None = None
    default_branch: str = ""        # repo characteristic: the forge's default branch (canonical trunk).
    default_branch_at: float = 0.0  # when it was last fetched from the forge (TTL freshness gate)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RepoMeta":
        d = d or {}
        return cls(
            repo_dir=d.get("repo_dir", ""),
            real_repo_dir=d.get("real_repo_dir", ""),
            code_dir=d.get("code_dir", ""),
            language=d.get("language"),
            default_branch=d.get("default_branch", ""),
            default_branch_at=d.get("default_branch_at", 0.0),
        )


@dataclass
class Branch:
    """One branch's facts — its name, its tip commit, where it forked from, and (for a
    worktree entry) its path. A pure value object: "how fresh is this view" is a property of
    whoever holds it (the segment's `fetched_at`), not of the branch itself."""
    name: str | None = None
    commit: str = ""               # tip sha
    fork_from: str | None = None   # branch it forked from; recorded by gcampr at cut, else inferred/None
    path: str = ""                 # worktree path (empty for the non-worktree local / remote views)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Branch":
        d = d or {}
        return cls(
            name=d.get("name"),
            commit=d.get("commit", ""),
            fork_from=d.get("fork_from"),
            path=d.get("path", ""),
        )

    def is_protected(self) -> bool:
        return git_state.is_protected_branch(self.name)


@dataclass
class BranchTopology:
    """The repo's branch topology across vantages — assembled by `RepoContext.load()` from
    owner-disjoint segments (see module docstring). `local` is the working checkout
    (refresh-owned, the DISPLAY copy); `remotes` are the server trunk tips (monitor-owned);
    `worktrees` are the local linked worktrees (refresh-owned). `pr_number` / `remotes_fetched_at`
    carry from their owning segments at load time."""
    local: Branch = field(default_factory=Branch)
    remotes: list[Branch] = field(default_factory=list)        # monitor-owned (remote_branches.json)
    worktrees: list[Branch] = field(default_factory=list)
    target: str = ""                                           # this checkout's trunk; derived from RepoMeta.default_branch at refresh
    pr_number: int | None = None                               # joined from pr.json (monitor-owned)
    remotes_fetched_at: float | None = None                    # provenance of `remotes`

    @classmethod
    def from_local_dict(cls, d: dict | None) -> "BranchTopology":
        """Build the refresh-owned half (local + worktrees) from branch.json. `target` is derived
        from RepoMeta.default_branch by `load` (single source — not stored here); remotes /
        remotes_fetched_at / pr_number are merged in from their own segments."""
        d = d or {}
        return cls(
            local=Branch.from_dict(d.get("local")),
            worktrees=[Branch.from_dict(w) for w in (d.get("worktrees") or [])],
        )

    def base_branch(self) -> str:
        """The trunk this branch is measured against — its recorded fork point, else the
        repo's canonical target."""
        return self.local.fork_from or self.target

    def remote_tip(self, name: str | None) -> Branch | None:
        return next((r for r in self.remotes if r.name == name), None)


@dataclass
class ComponentValidation:
    """**一个** component 的验证戳。

    `lint_fingerprint` 是 lint 通过那一刻、该 component 待验证内容的指纹（`repo_model.component_fingerprint`）
    ——通行证绑**内容**，不绑「有没有人报告过改动」。gate 拿当前指纹与它比：不等 = 内容变过 = 这张
    通行证已作废。这样谁改的、用什么工具改的都不重要（见 `component_fingerprint` 的 why）。"""
    last_lint_at: float | None = None
    lint_fingerprint: str = ""
    last_test_at: float | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "ComponentValidation":
        d = d or {}
        return cls(
            last_lint_at=d.get("last_lint_at"),
            lint_fingerprint=d.get("lint_fingerprint", "") or "",
            last_test_at=d.get("last_test_at"),
        )


@dataclass
class Validation:
    """验证戳，**按 component 键**（key = `Component.id`，仓相对路径 `.` / `server`）。

    key 的粒度必须与**执行**的粒度一致：lint/test 本就按 component 跑（一个仓可有 `server/` + `cli/`
    两套工具链、各自的 Makefile），repo 级单戳表达不了「A 过 B 挂」——component A 通过盖下的戳会让
    `precommit_gate` 读到「已验、无待验编辑」而放行整个仓，于是**一次 partial-fail 的 fan-out
    恰好把防绕过守卫的锁打开**（gate 挡住了 gcampr，却给裸 `git commit` 发了通行证）。
    按 component 键之后这类偏差不可表达，而不是靠各消费方记得多问一句。

    旧格式（repo 级扁平 / 单个 validation.json）读进来是空 components——即「都没验过」，gate 要求重跑
    一次 lint。**刻意不写迁移**：`.devloop` 是 cache 不是事实源，退化方向是 fail-closed。

    **落盘按 check 拆两个段**（`branches/<b>/lint.json` + `test.json`），本类是 `load` 合并出的
    内存视图。三个维度各归各位：**branch** 是目录（域，切分支即自动隔离）、**check** 是文件
    （= writer-role）、**component** 是文件内的 JSON key。

    为什么 check 必须拆到文件：`dispatch` 用线程池**并发**跑 lint 与 test，而 segment 的纪律是
    「single-writer whole-file overwrite」（见 `context/store`）。两个 writer 写同一个文件 =
    load-modify-write 互相覆盖，实测会丢戳——丢 lint 戳只是白跑一遍，丢 **test** 戳则是状态说
    「没测过」而其实测过，是记录失真。拆开之后各写各的文件，这一类**结构上不可能**（store 的原话），
    不需要锁、也不需要「写前重读合并」那种把不可能降级成窗口更窄的 race 的做法。

    为什么 component 不拆成目录：component **不是 writer**（同一个 check 内部 fan-out 是顺序的），拆了不解决
    这个 race；且 component id 不是安全路径分量（根 component 是 `.`、`eval/reviewbench` 带斜杠），当目录还得
    枚举目录才能读回全集。当 JSON key 三个问题都没有。
    """
    components: dict[str, ComponentValidation] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, lint: dict | None, test: dict | None) -> "Validation":
        """两个 check 段 → 一个视图。段形状 `{unit_id: {...}}`（component 是 key，不是目录）。"""
        out = cls()
        for cid, v in (lint or {}).items():
            if isinstance(v, dict):
                u = out.of(cid)
                u.last_lint_at = v.get("passed_at")
                u.lint_fingerprint = v.get("fingerprint", "") or ""
        for cid, v in (test or {}).items():
            if isinstance(v, dict):
                out.of(cid).last_test_at = v.get("passed_at")
        return out

    def lint_segment(self) -> dict:
        """lint 段的落盘形状——只含 lint 拥有的字段，绝不带 test 的（那是另一个 writer 的）。"""
        return {cid: {"passed_at": u.last_lint_at, "fingerprint": u.lint_fingerprint}
                for cid, u in self.components.items() if u.last_lint_at is not None}

    def test_segment(self) -> dict:
        return {cid: {"passed_at": u.last_test_at}
                for cid, u in self.components.items() if u.last_test_at is not None}

    def component(self, cid: str) -> ComponentValidation:
        """`cid` 的戳，只读；从未验过 → 全空默认值（无戳即未验，fail-closed）。"""
        return self.components.get(cid) or ComponentValidation()

    def of(self, cid: str) -> ComponentValidation:
        """`cid` 的戳，可写（缺则建）——mutator 专用。"""
        return self.components.setdefault(cid, ComponentValidation())


@dataclass
class RepoContext:
    repo: RepoMeta = field(default_factory=RepoMeta)
    agents_md: AgentsMd = field(default_factory=AgentsMd)
    branch: BranchTopology = field(default_factory=BranchTopology)
    validation: Validation = field(default_factory=Validation)
    prs: list[PullRequest] = field(default_factory=list)   # monitor-owned recent-PR window
    provider: str = ""   # repo-level forge ("github"/"gitlab"); drives display vocabulary
    merge_readiness: str | None = None   # current branch's open-MR readiness — a pr.json hint
                                         # (MergeReadiness value); re-checked live before a merge
    label_pending: int | None = None     # open MR's findings still awaiting a ccr:label verdict
                                         # — a pr.json hint; None = no open MR / poll failed
    label_pending_key: str = ""          # identity of that pending set — nudge decay key
    updated_at: float = 0.0

    # ── load (merge segments) ──────────────────────────────────────────────────
    @classmethod
    def load(cls, repo_dir: str | Path) -> "RepoContext | None":
        """Assemble the in-memory view by merging the per-owner segment files.

        `meta` is the existence marker: absent → not initialized → None (caller
        refresh_all's). Every other segment defaults independently, so one missing /
        corrupt file degrades to its default without losing the rest (fail-open)."""
        meta = store.load_segment(repo_dir, "meta")
        if meta is None:
            return None
        # Branch-domain segments live under branches/<branch>/ — keyed by the LIVE branch (one
        # rev-parse), not by whatever some cached file last observed. This kills the whole
        # "stale branch.json after an unobserved checkout fools the display" class structurally:
        # switching branches switches which segment directory is read.
        live = git_state.get_current_branch(repo_dir)
        branch = BranchTopology.from_local_dict(
            store.load_segment(repo_dir, store.branch_segment(live, "branch")) or {})
        if not branch.local.name:
            branch.local.name = live or ""   # fresh/missing segment: identity is still the live read
        branch.target = (meta.get("repo") or {}).get("default_branch", "")   # single source: RepoMeta.default_branch
        # monitor-owned remote trunk tips (remote_branches.json) + their provenance stamp.
        rb = store.load_segment(repo_dir, "remote_branches") or {}
        branch.remotes = [Branch.from_dict(r) for r in (rb.get("remotes") or [])]
        branch.remotes_fetched_at = rb.get("fetched_at")
        # Join the monitor-owned pr_number only when it was computed for the CURRENT branch —
        # DISPLAY-grade; write-gates re-derive against LIVE branch (domain.context.gate).
        # pr.json is branch-keyed, so a branch switch self-invalidates the stale number.
        pr = store.load_segment(repo_dir, "pr") or {}
        on_branch = pr.get("branch") == branch.local.name   # pr.json is branch-keyed; only join if current
        branch.pr_number = pr.get("pr_number") if on_branch else None
        ctx = cls(
            repo=RepoMeta.from_dict(meta.get("repo")),
            agents_md=AgentsMd.from_dict(meta.get("agents_md") or {}),
            branch=branch,
            validation=Validation.from_dict(
                store.load_segment(repo_dir, store.branch_segment(live, "lint")),
                store.load_segment(repo_dir, store.branch_segment(live, "test"))),
            prs=[PullRequest.from_dict(p) for p in (pr.get("prs") or []) if p.get("number") is not None],
            provider=pr.get("provider", ""),
            merge_readiness=(pr.get("merge_readiness") if on_branch else None),
            label_pending=(pr.get("label_pending") if on_branch else None),
            label_pending_key=((pr.get("label_pending_key") or "") if on_branch else ""),
            updated_at=float(meta.get("updated_at", 0) or 0),
        )
        if not ctx.repo.repo_dir:
            ctx.repo.repo_dir = str(Path(repo_dir))
        return ctx

    # ── per-owner segment writers ──────────────────────────────────────────────
    # Each writes exactly one file. A writer-role only ever calls its own saver, so
    # two concurrent writers (e.g. the monitor and a refresh) touch disjoint files —
    # the lost-update class is structurally impossible, no lock needed.
    def _root(self) -> str:
        return self.repo.repo_dir or self.repo.real_repo_dir

    def _save_meta(self) -> None:
        root = self._root()
        if not root:
            return
        self.updated_at = base.now()
        store.save_segment(root, "meta", {
            "repo": asdict(self.repo),
            "agents_md": asdict(self.agents_md),
            "updated_at": self.updated_at,
        })
        git_state.ensure_gitignore_excluded(root)   # keep /.devloop/ out of git

    def _branch_seg(self, name: str) -> str:
        """Branch-domain segment name for THIS context's branch (branches/<b>/<name>)."""
        return store.branch_segment(self.branch.local.name or None, name)

    def _save_branch(self) -> None:
        """Refresh-owned: the LOCAL half only (local + worktrees + target). `remotes` is the
        monitor's (remote_branches.json) and `pr_number` is pr.json's — never written here."""
        if not self._root():
            return
        store.save_segment(self._root(), self._branch_seg("branch"), {
            "local": asdict(self.branch.local),
            "worktrees": [asdict(w) for w in self.branch.worktrees],
        })   # target not persisted here — derived from meta.default_branch (single source)

    def _save_pr(self) -> None:
        """Monitor's write surface (also used by gcampr via a one-shot poll). Branch-keyed
        so a later branch switch invalidates pr_number on read without anyone clearing it.
        `provider` is repo-level (the forge backing this repo) — stored once in the header,
        not on every PR."""
        if not self._root():
            return
        store.save_segment(self._root(), "pr", {
            "branch": self.branch.local.name,
            "provider": self.provider,
            "pr_number": self.branch.pr_number,
            "merge_readiness": self.merge_readiness,
            "prs": [asdict(p) for p in self.prs],
        })

    def _save_lint(self) -> None:
        """只写 lint 段。**绝不**顺手写 test 段——那是另一个 writer 的文件，碰它就把
        「一段一 writer」的不变量破掉，lost update 立刻回来（见 `Validation`）。"""
        if self._root():
            store.save_segment(self._root(), self._branch_seg("lint"), self.validation.lint_segment())

    def _save_test(self) -> None:
        if self._root():
            store.save_segment(self._root(), self._branch_seg("test"), self.validation.test_segment())

    # ── refresh (re-derive from authoritative sources) ─────────────────────────
    @classmethod
    def refresh_all(cls, repo_dir: str | Path) -> "RepoContext":
        """Full rebuild (normal-impl boundary: SessionStart / enter / TTL).

        Writes only the refresher-owned segments (meta + branch). validation /
        pr / remote_branches live in their own files and are left untouched — their values
        are merged in (via prev) only to keep the *returned* object complete."""
        repo_dir_in = str(Path(repo_dir))
        repo_dir_abs = str(Path(repo_dir).resolve())
        code_dir = repo_layout.find_repo_code_dir(repo_dir_abs)
        language = ecosystem.detect_language(code_dir)
        agents_md_path = repo_layout.find_agents_md(repo_dir_abs, code_dir)

        prev = cls.load(repo_dir_abs) or cls()
        # refresh_all is the TTL boundary (docstring): resolve the repo's default branch here,
        # gated so the forge is hit at most once per DEFAULT_BRANCH_TTL_SEC despite frequent rebuilds.
        default_branch, default_branch_at = _resolve_default_branch(
            repo_dir_abs, prev.repo.default_branch, prev.repo.default_branch_at)
        target = default_branch
        items = parsers.parse_references_section(agents_md_path) if agents_md_path else []
        ctx = cls(
            repo=RepoMeta(repo_dir=repo_dir_in, real_repo_dir=repo_dir_abs,
                          code_dir=code_dir, language=language,
                          default_branch=default_branch, default_branch_at=default_branch_at),
            agents_md=AgentsMd(
                path=agents_md_path,
                references=[Reference(title=r.get("title", ""), path=r.get("path", ""),
                                      hook=r.get("description")) for r in items],
            ),
            branch=_build_topology(repo_dir_abs, target, prev.branch),
            validation=prev.validation,
            prs=prev.prs,
            provider=prev.provider,
        )
        ctx._save_meta()
        ctx._save_branch()
        return ctx

    @classmethod
    def refresh_branch(cls, repo_dir: str | Path) -> "RepoContext":
        """Incremental branch refresh (fast; after a local git state change). No AGENTS.md
        re-parse. Writes only branch.json. Not-yet-initialized → fall back to a full build."""
        ctx = cls.load(repo_dir)
        if ctx is None:
            return cls.refresh_all(repo_dir)
        # cached repo fact (zero network); local origin/HEAD only if cache empty
        target = ctx.repo.default_branch or git_state.local_default_target(ctx.repo.real_repo_dir)
        ctx.branch = _build_topology(ctx.repo.real_repo_dir, target, ctx.branch)
        ctx._save_branch()
        return ctx

    @classmethod
    def is_stale_at(cls, repo_dir: str | Path, ttl: float = REPO_STALE_SEC) -> bool:
        meta = store.load_segment(repo_dir, "meta")
        if meta is None:
            return True
        return base.is_stale(meta.get("updated_at"), ttl)

    # ── mutators (each touches exactly one segment) ─────────────────────────────
    def mark_lint_passed(self, cid: str, fingerprint: str) -> None:
        """`fingerprint` 必填、且必须是**刚验过的那份内容**的指纹（lint 跑完后现算，不是跑之前）——
        lint 的 `make fix` 会改文件，跑前算的指纹配不上跑后的树。空串 = 算不出，gate 会按未验证
        处理（fail-closed）。"""
        u = self.validation.of(cid)
        u.last_lint_at = base.now()
        u.lint_fingerprint = fingerprint
        self._save_lint()

    def mark_test_passed(self, cid: str) -> None:
        self.validation.of(cid).last_test_at = base.now()
        self._save_test()

    def set_branch_pr_number(self, number: int | None) -> None:
        """Write surface for the current branch's PR/MR number (monitor + create flow)."""
        self.branch.pr_number = number
        self._save_pr()

    def set_prs(self, prs: list[PullRequest]) -> None:
        """Monitor's sole write surface for the recent-PR window."""
        self.prs = list(prs)
        self._save_pr()

    def set_fork_from(self, fork_from: str | None) -> None:
        """gcampr's surface: record where the (just-cut) current branch forked from — the one
        branch fact git doesn't durably keep. Sticky: `_build_topology` preserves it across
        refreshes, so a later rebuild-from-git won't clobber it."""
        self.branch.local.fork_from = fork_from
        self._save_branch()

    # ── PR derivation (DISPLAY-grade; write-gates use domain.context.gate) ─────────
    def current_pr(self) -> PullRequest | None:
        if self.branch.pr_number is None:
            return None
        return next((p for p in self.prs if p.number == self.branch.pr_number), None)

    def branch_pr_inactive(self) -> bool:
        """True if the current branch's PR/MR is merged/closed (derived, not stored).

        DISPLAY-grade — keyed off the *cached* branch name. The hard gates do NOT read this;
        they re-derive against the LIVE branch + HEAD via `domain.context.gate` (a cached branch
        name could be stale after an unobserved checkout). See docs/branch-state.md."""
        p = self.current_pr()
        return bool(p and p.inactive)

    def branch_pr_in_flight(self) -> bool:
        """True if the current branch's PR/MR is still open / awaiting human merge (derived).
        DISPLAY-grade (see `branch_pr_inactive`). Surfaced so the orchestrator notes that
        committing here continues an in-flight PR, and new work re-bases off origin/<target>."""
        p = self.current_pr()
        return bool(p and p.is_open)

    # Compatibility previews for callers that need text without mutating Board receipts.
    def turn_text(self) -> str:
        from domain.board import (
            DeliveryChannel,
            DeliveryPolicy,
            PromptScope,
            project_view,
            render_prompt,
        )

        _, view = project_view(self.repo.repo_dir, repo=self)
        items = DeliveryPolicy.items_for(
            view,
            DeliveryChannel.PROMPT,
            frozenset({PromptScope.TURN}),
        )
        return render_prompt(items)

    def session_text(self) -> str:
        from domain.board import (
            DeliveryChannel,
            DeliveryPolicy,
            PromptScope,
            project_view,
            render_prompt,
        )

        _, view = project_view(self.repo.repo_dir, repo=self)
        items = DeliveryPolicy.items_for(
            view,
            DeliveryChannel.PROMPT,
            frozenset({PromptScope.SESSION}),
        )
        return render_prompt(items)

# ── private builders / renderers ──────────────────────────────────────────────
def _build_topology(repo_dir: str, target: str, prev: BranchTopology | None) -> BranchTopology:
    """Rebuild the LOCAL half (identity + worktrees) from live git, preserving the
    monitor-owned and git-unrecorded facts from `prev` so the returned object stays whole.

    `fork_from` is git-unrecorded → carried from `prev` ONLY when the branch name is unchanged
    (a switch drops the old branch's fork point; gcampr re-records on the next cut). remotes /
    remotes_fetched_at / pr_number are merged from prev (load re-reads them from disk anyway)."""
    name = git_state.get_current_branch(repo_dir)
    commit = git_state.get_head_sha(repo_dir)
    fork_from = prev.local.fork_from if (prev is not None and prev.local.name == name) else None
    worktrees = [Branch(name=b, commit=sha, path=p) for (p, sha, b) in git_state.list_worktrees(repo_dir)]
    topo = BranchTopology(
        local=Branch(name=name, commit=commit, fork_from=fork_from),
        worktrees=worktrees,
        target=target,
    )
    if prev is not None:
        topo.remotes = prev.remotes
        topo.remotes_fetched_at = prev.remotes_fetched_at
        topo.pr_number = prev.pr_number
    return topo
