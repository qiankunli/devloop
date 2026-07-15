"""`RepoContext` — per-repo state, persisted as per-owner segment files under
`<git_root>/.devloop/` (`meta.json` / `branch.json` / `remote_branches.json` / `pr.json` /
`injection.json`, plus branch-domain `branches/<b>/{branch,lint,test,injection,review}.json`).
`RepoContext` is the in-memory *view* that `load()` assembles by merging them; each mutator
writes back only its own segment.

Why one file per owner: the state has several independent writer-roles (the refresh, the MR
monitor, **the lint handler**, **the test handler**, injection marks) running in different
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
  (`lib.context.gate`) instead of trusting this snapshot.
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

from .. import git_state, parsers, repo_layout
from ..forge import ForgeError, forge_for_repo
from . import base, store
from .base import (
    DEFAULT_BRANCH_TTL_SEC,
    LABEL_NUDGE_CAP,
    REPO_STALE_SEC,
    REVIEW_NUDGE_CAP,
    SESSION_TTL_SEC,
    TURN_TTL_SEC,
    AgentsMd,
    Cadence,
    MergeReadiness,
    Nudge,
    PullRequest,
    Reference,
    pr_label,
    vocab,
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
class UnitValidation:
    """**一个** code unit 的验证戳。

    `lint_fingerprint` 是 lint 通过那一刻、该 unit 待验证内容的指纹（`repo_resolve.unit_fingerprint`）
    ——通行证绑**内容**，不绑「有没有人报告过改动」。gate 拿当前指纹与它比：不等 = 内容变过 = 这张
    通行证已作废。这样谁改的、用什么工具改的都不重要（见 `unit_fingerprint` 的 why）。"""
    last_lint_at: float | None = None
    lint_fingerprint: str = ""
    last_test_at: float | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "UnitValidation":
        d = d or {}
        return cls(
            last_lint_at=d.get("last_lint_at"),
            lint_fingerprint=d.get("lint_fingerprint", "") or "",
            last_test_at=d.get("last_test_at"),
        )


@dataclass
class Validation:
    """验证戳，**按 code unit 键**（key = `CodeUnit.id`，仓相对路径 `.` / `server`）。

    key 的粒度必须与**执行**的粒度一致：lint/test 本就按 unit 跑（一个仓可有 `server/` + `cli/`
    两套工具链、各自的 Makefile），repo 级单戳表达不了「A 过 B 挂」——unit A 通过盖下的戳会让
    `precommit_gate` 读到「已验、无待验编辑」而放行整个仓，于是**一次 partial-fail 的 fan-out
    恰好把防绕过守卫的锁打开**（gate 挡住了 gcampr，却给裸 `git commit` 发了通行证）。
    按 unit 键之后这类偏差不可表达，而不是靠各消费方记得多问一句。

    旧格式（repo 级扁平 / 单个 validation.json）读进来是空 units——即「都没验过」，gate 要求重跑
    一次 lint。**刻意不写迁移**：`.devloop` 是 cache 不是事实源，退化方向是 fail-closed。

    **落盘按 check 拆两个段**（`branches/<b>/lint.json` + `test.json`），本类是 `load` 合并出的
    内存视图。三个维度各归各位：**branch** 是目录（域，切分支即自动隔离）、**check** 是文件
    （= writer-role）、**unit** 是文件内的 JSON key。

    为什么 check 必须拆到文件：`dispatch` 用线程池**并发**跑 lint 与 test，而 segment 的纪律是
    「single-writer whole-file overwrite」（见 `context/store`）。两个 writer 写同一个文件 =
    load-modify-write 互相覆盖，实测会丢戳——丢 lint 戳只是白跑一遍，丢 **test** 戳则是状态说
    「没测过」而其实测过，是记录失真。拆开之后各写各的文件，这一类**结构上不可能**（store 的原话），
    不需要锁、也不需要「写前重读合并」那种把不可能降级成窗口更窄的 race 的做法。

    为什么 unit 不拆成目录：unit **不是 writer**（同一个 check 内部 fan-out 是顺序的），拆了不解决
    这个 race；且 unit id 不是安全路径分量（根 unit 是 `.`、`eval/reviewbench` 带斜杠），当目录还得
    枚举目录才能读回全集。当 JSON key 三个问题都没有。
    """
    units: dict[str, UnitValidation] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, lint: dict | None, test: dict | None) -> "Validation":
        """两个 check 段 → 一个视图。段形状 `{unit_id: {...}}`（unit 是 key，不是目录）。"""
        out = cls()
        for uid, v in (lint or {}).items():
            if isinstance(v, dict):
                u = out.of(uid)
                u.last_lint_at = v.get("passed_at")
                u.lint_fingerprint = v.get("fingerprint", "") or ""
        for uid, v in (test or {}).items():
            if isinstance(v, dict):
                out.of(uid).last_test_at = v.get("passed_at")
        return out

    def lint_segment(self) -> dict:
        """lint 段的落盘形状——只含 lint 拥有的字段，绝不带 test 的（那是另一个 writer 的）。"""
        return {uid: {"passed_at": u.last_lint_at, "fingerprint": u.lint_fingerprint}
                for uid, u in self.units.items() if u.last_lint_at is not None}

    def test_segment(self) -> dict:
        return {uid: {"passed_at": u.last_test_at}
                for uid, u in self.units.items() if u.last_test_at is not None}

    def unit(self, uid: str) -> UnitValidation:
        """`uid` 的戳，只读；从未验过 → 全空默认值（无戳即未验，fail-closed）。"""
        return self.units.get(uid) or UnitValidation()

    def of(self, uid: str) -> UnitValidation:
        """`uid` 的戳，可写（缺则建）——mutator 专用。"""
        return self.units.setdefault(uid, UnitValidation())


def _review_status(rv: dict) -> str:
    """The review status as DISPLAYED, which is not always the one stored.

    `stale` is DERIVED, never written: a detached run_review that got killed (sleep / OOM /
    kill) never wrote a terminal status, so review.json sits at `running` forever — past
    REVIEW_STALE_SEC that reads as stale. "" for skipped / no review (no signal, pure noise).

    Shared by the renderer and `_review_key` on purpose: keying on the STORED status would
    make running and stale the same event, so a review told once while running could never
    report that it died — the exact case the staleness backstop exists for.
    """
    status = rv.get("status")
    if not status or status == "skipped":
        return ""
    if status == "running" and (base.now() - (rv.get("generated_at") or 0)) > base.REVIEW_STALE_SEC:
        return "stale"
    return status


def _review_key(rv: dict) -> str:
    """Identity of a review RESULT — what makes it a different thing worth telling again.

    Displayed status + the sha it ran against + its counts. A re-run over new code, a run
    finishing (running → success), or one dying (running → stale) is new; the same result
    re-read every turn is not.
    """
    st = _review_status(rv)
    return f"{st}:{rv.get('reviewed_sha') or ''}:{rv.get('count', 0)}:{rv.get('failed', 0)}" if st else ""


@dataclass
class Injection:
    turn: Cadence = field(default_factory=Cadence)
    session: Cadence = field(default_factory=Cadence)
    # Event/chore delivery ledgers, decaying independently of the turn cadence — Cadence
    # hashes the whole block, so an unrelated line moving would re-deliver forever. Also NOT
    # cleared by PostCompact, unlike the cadences: see clear_injection_dedup. base.Nudge.
    label_nudge: Nudge = field(default_factory=Nudge)
    review_nudge: Nudge = field(default_factory=Nudge)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Injection":
        d = d or {}
        return cls(
            turn=Cadence.from_dict(d.get("turn") or {}),
            session=Cadence.from_dict(d.get("session") or {}),
            label_nudge=Nudge.from_dict(d.get("label_nudge") or {}),
            review_nudge=Nudge.from_dict(d.get("review_nudge") or {}),
        )


@dataclass
class RepoContext:
    repo: RepoMeta = field(default_factory=RepoMeta)
    agents_md: AgentsMd = field(default_factory=AgentsMd)
    branch: BranchTopology = field(default_factory=BranchTopology)
    validation: Validation = field(default_factory=Validation)
    injection: Injection = field(default_factory=Injection)
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
        # DISPLAY-grade; write-gates re-derive against LIVE branch (lib.context.gate).
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
            injection=Injection.from_dict(
                store.load_segment(repo_dir, store.branch_segment(live, "injection")) or {}),
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

    def _save_injection(self) -> None:
        if self._root():
            store.save_segment(self._root(), self._branch_seg("injection"), asdict(self.injection))

    # ── refresh (re-derive from authoritative sources) ─────────────────────────
    @classmethod
    def refresh_all(cls, repo_dir: str | Path) -> "RepoContext":
        """Full rebuild (normal-impl boundary: SessionStart / enter / TTL).

        Writes only the refresher-owned segments (meta + branch). validation / injection /
        pr / remote_branches live in their own files and are left untouched — their values
        are merged in (via prev) only to keep the *returned* object complete."""
        repo_dir_in = str(Path(repo_dir))
        repo_dir_abs = str(Path(repo_dir).resolve())
        code_dir = repo_layout.find_repo_code_dir(repo_dir_abs)
        language = repo_layout.detect_language(code_dir)
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
            injection=prev.injection,
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
    def mark_lint_passed(self, uid: str, fingerprint: str) -> None:
        """`fingerprint` 必填、且必须是**刚验过的那份内容**的指纹（lint 跑完后现算，不是跑之前）——
        lint 的 `make fix` 会改文件，跑前算的指纹配不上跑后的树。空串 = 算不出，gate 会按未验证
        处理（fail-closed）。"""
        u = self.validation.of(uid)
        u.last_lint_at = base.now()
        u.lint_fingerprint = fingerprint
        self._save_lint()

    def mark_test_passed(self, uid: str) -> None:
        self.validation.of(uid).last_test_at = base.now()
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

    # ── PR derivation (DISPLAY-grade; write-gates use lib.context.gate) ─────────
    def current_pr(self) -> PullRequest | None:
        if self.branch.pr_number is None:
            return None
        return next((p for p in self.prs if p.number == self.branch.pr_number), None)

    def branch_pr_inactive(self) -> bool:
        """True if the current branch's PR/MR is merged/closed (derived, not stored).

        DISPLAY-grade — keyed off the *cached* branch name. The hard gates do NOT read this;
        they re-derive against the LIVE branch + HEAD via `lib.context.gate` (a cached branch
        name could be stale after an unobserved checkout). See docs/branch-state.md."""
        p = self.current_pr()
        return bool(p and p.inactive)

    def branch_pr_in_flight(self) -> bool:
        """True if the current branch's PR/MR is still open / awaiting human merge (derived).
        DISPLAY-grade (see `branch_pr_inactive`). Surfaced so the orchestrator notes that
        committing here continues an in-flight PR, and new work re-bases off origin/<target>."""
        p = self.current_pr()
        return bool(p and p.is_open)

    # ── injection: turn / session cadences ─────────────────────────────────────
    def turn_text(self) -> str:
        return _format_turn(self)

    def session_text(self) -> str:
        if not self.agents_md.references:
            return ""
        return _format_session(self)

    def emit_turn_if_changed(self) -> str:
        text = self.turn_text()
        return text if self.injection.turn.should_emit(text, now=base.now(), ttl=TURN_TTL_SEC) else ""

    def emit_session_if_changed(self) -> str:
        text = self.session_text()
        return text if self.injection.session.should_emit(text, now=base.now(), ttl=SESSION_TTL_SEC) else ""

    @property
    def label_nudge_due(self) -> bool:
        """Whether the待打标 nudge should speak this turn. ONE predicate, read by both
        `_format_turn` (to decide whether to say it) and `mark_turn_emitted` (to count that it
        was said) — the two are separate calls over the same unchanged ctx, so a single
        definition is what keeps them from drifting into counting asks that never went out."""
        return bool(self.label_pending) and self.injection.label_nudge.due(
            self.label_pending_key, cap=LABEL_NUDGE_CAP)

    def review_nudge_due(self, rv: dict) -> bool:
        """Whether this review RESULT still needs telling. Same emit/mark pairing as
        `label_nudge_due`; `rv` is the freshly-read review segment (the formatter reads it
        live because an external process writes it, so it can't come off `self`)."""
        return bool(_review_key(rv)) and self.injection.review_nudge.due(
            _review_key(rv), cap=REVIEW_NUDGE_CAP)

    def mark_turn_emitted(self, text: str) -> None:
        self.injection.turn.mark(text, now=base.now())
        # Only reached when `text` actually went out (see userprompt_inject), so these count
        # deliveries the agent really saw — not ones the block-level dedup swallowed.
        if self.label_nudge_due:
            self.injection.label_nudge.bump(self.label_pending_key)
        rv = store.load_segment(self._root(), self._branch_seg("review")) or {}
        if self.review_nudge_due(rv):
            self.injection.review_nudge.bump(_review_key(rv))
        self._save_injection()

    def mark_session_emitted(self, text: str) -> None:
        self.injection.session.mark(text, now=base.now())
        self._save_injection()

    def reset_turn_injection(self) -> None:
        self.injection.turn.clear()
        self._save_injection()

    def reset_session_injection(self) -> None:
        self.injection.session.clear()
        self._save_injection()

    def clear_injection_dedup(self) -> None:
        """PostCompact: drop both cadences' stamps so STATE re-injects next turn.

        Deliberately does NOT touch the nudges. Compaction drops what was said, so state must
        be re-said — an agent acting on a branch/PR picture it no longer holds is the failure
        this prevents. Events and chores are the opposite: re-delivering a review result makes
        the agent re-triage findings it already handled, and re-asking for verdicts the user
        declined three times re-litigates a decision compaction didn't undo. Their ledgers
        record "this was delivered", which stays true across a compaction.
        """
        self.injection.turn.clear()
        self.injection.session.clear()
        self._save_injection()


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


def _branch_staleness(repo_dir: str, b: BranchTopology) -> dict:
    """ahead/behind of local vs its trunk baseline, plus a freshness qualifier.

    ahead/behind is computed against the LOCAL `origin/<base>` mirror (no network). The monitor's
    TRUE tip (`b.remotes`) is compared to that mirror to detect 'trunk moved since you last
    fetched' — the silent-staleness the count alone hides (see docs/branch-state.md §read-freshness)."""
    base_name = b.base_branch()
    ahead, behind = git_state.get_ahead_behind(repo_dir, base_name) or (0, 0)
    remote = b.remote_tip(base_name)
    if remote and remote.commit:
        # we have the server's TRUE tip for this baseline → a real freshness signal. Claim
        # "as of" ONLY here: if the baseline isn't among the monitor's tracked tips, saying
        # "as of <t>" would falsely imply this count reflects the latest remote.
        local_mirror = git_state.rev_parse(repo_dir, f"origin/{base_name}")
        if local_mirror and local_mirror != remote.commit:
            asof = f", ⚠ trunk moved since fetch {base.fmt_ts(b.remotes_fetched_at)} — fetch to recount"
        else:
            asof = f", as of {base.fmt_ts(b.remotes_fetched_at)}"
    else:
        asof = ""
    return {"base": base_name, "ahead": ahead, "behind": behind, "asof": asof}


_READINESS_BLURB = {
    MergeReadiness.CONFLICT: "merge conflict with target — rebase/merge & resolve",
    MergeReadiness.DISCUSSIONS_UNRESOLVED: "unresolved review discussions — address the comments",
    MergeReadiness.CI_BLOCKED: "CI not passing",
}


def _format_turn(ctx: "RepoContext") -> str:
    lines = [f"[Current repo: {_display_code_dir(ctx)} ({ctx.repo.language or '?'})]"]
    b = ctx.branch
    cur = b.local.name or "?"
    wt = " (worktree)" if git_state.is_linked_worktree(ctx.repo.real_repo_dir) else ""
    extras = []
    if b.local.is_protected():
        extras.append("PROTECTED")
    pr = ctx.current_pr()
    if pr and pr.inactive:
        extras.append(
            f"INACTIVE ({pr_label(ctx.provider, pr.number)} {pr.state}) — cut a new branch from latest origin/{b.target}"
        )
    elif pr and pr.is_open:
        # Soft hint, not a guard: an in-flight PR has one legitimate edit case (amending it for
        # review), so we surface the state and let the agent choose rather than hard-blocking.
        noun = vocab(ctx.provider)[0]
        extras.append(
            f"IN-FLIGHT ({pr_label(ctx.provider, pr.number)} open) — new work needs a fresh branch (gcampr --branch); "
            f"edit here only to amend this {noun}"
        )
    # Surface an actionable merge blocker on the current open MR (conflict / unresolved discussions
    # / CI). A pr.json hint as of the last poll — the nudge to act; the gate re-checks live at merge.
    if pr and pr.is_open and ctx.merge_readiness:
        try:
            rd = MergeReadiness(ctx.merge_readiness)
        except ValueError:
            rd = None
        if rd and rd.blocks_merge:
            extras.append(f"MERGE-BLOCKED: {_READINESS_BLURB[rd]}")
    extra_str = f" ⚠️ {'; '.join(extras)}" if extras else ""
    st = _branch_staleness(ctx.repo.real_repo_dir or ctx.repo.repo_dir, b)
    lines.append(
        f"Branch: {cur}{wt} (ahead {st['ahead']}, behind {st['behind']} vs {st['base']}{st['asof']}, target={b.target})"
        f"{extra_str}"
    )

    raw = git_state.get_workspace_status(ctx.repo.real_repo_dir or ctx.repo.repo_dir)
    if raw.get("dirty"):
        lines.append(f"Workspace: dirty: {raw.get('modified_count', 0)} modified, {raw.get('untracked_count', 0)} untracked")
    else:
        lines.append("Workspace: clean")

    # 按 unit 逐段渲染（key 排序：dict 序来自落盘顺序，会随哪个 unit 先盖戳而变；整块 hash 去重
    # 下那等于无意义重发）。没有任何 unit 验过 → 一句话，不摆两个 never。
    v = ctx.validation
    if not v.units:
        lines.append("Validation: never run")
    else:
        # 只报「什么时候验过」，**不报「验的还算不算数」**：后者要现算指纹（读改动文件的 bytes），
        # 而这里每轮都跑——按成本铁律（AGENTS.md〈成本原则〉）不值当。而且它本就不该由提示承担：
        # 陈旧与否由 gate 在 commit 时判（那里算一次指纹是合算的），gcam 路径更是直接把 lint 跑掉。
        # 旧的「N edits since」看着便宜，代价是它在 Codex 侧恒为 0——一个说谎的提示比没有更糟。
        segs = [f"{uid}: lint={base.fmt_ts(v.units[uid].last_lint_at)}; test={base.fmt_ts(v.units[uid].last_test_at)}"
                for uid in sorted(v.units)]
        lines.append("Validation: " + " | ".join(segs))

    # 后台 code-review 的结果回流（pull）：run_review（由 smart_git_ops detach 起）跑完写
    # review.json，这里在下一轮把它捎进上下文——advisory，只通报、不挟持。读 fresh（段由
    # 外部进程写，RepoContext 视图可能滞后）；skipped 不出（无信号价值、避免噪声）。
    # 这是 pull 路径（醒着才看见）；另有 push 路径 notify 端口的 review Source（lib/notify/sources/review.py），
    # 经 channel 或 waiter（scripts/notify.py）在 review 出终态时主动唤醒 idle 会话并带上 findings——pull 是兜底。
    _rv = store.load_segment(ctx.repo.repo_dir,
                            store.branch_segment(ctx.branch.local.name or None, "review")) or {}
    _sha = (_rv.get("reviewed_sha") or "")[:9]
    # Review 是**事件**,不是状态——「这次 review 出了什么」讲一遍就讲完了,它不描述「你现在
    # 在哪」。所以按 review 身份（status+sha+计数）报一次就闭嘴,而不是每轮跟着整块重发:
    # turn Cadence 按整块 hash 去重,随便哪行一动就整块重发,事件行会被反复重投,agent 于是
    # 反复 triage 同一批已经处理过的 findings。同理它不该被 PostCompact 复活（见 Nudge）。
    # 状态取 `_review_status`（与 key 同源）:stale 是推导出来的,不在段里。
    _rs = _review_status(_rv) if ctx.review_nudge_due(_rv) else ""
    if _rs == "stale":
        lines.append(f"Review: stale on {_sha} — 疑似中途中断（见 .devloop/review.json）；下次 gcampr/commit 会重跑")
    elif _rs == "running":
        lines.append(f"Review: running on {_sha} (.devloop/review.json)")
    elif _rs:   # success / completed_with_(warnings|errors) / error；skipped 已被 _review_status 滤掉
        _n, _failed = _rv.get("count", 0), _rv.get("failed", 0)
        # 诚实呈现：findings 与「N 文件 review 失败」分别报；都没有才是 clean。
        # 关键修正：completed_with_errors+0 评论曾被误报成 clean——失败要看得见。
        parts = []
        if _n:
            parts.append(f"{_n} finding(s)")
        if _failed:
            parts.append(f"{_failed} file(s) failed")
        if _rs == "error":
            parts.append("review errored")
        summary = ", ".join(parts) if parts else "clean (no findings)"
        lines.append(f"Review: {summary} on {_sha} — see .devloop/review.json")

    # 待打标 nudge（ground truth 双向积累）——让所有 session/agent（含 Codex）都收到，
    # 不依赖个体记忆。数来自 pr.json（forge 派生:有 ccr:fp 却没有 ccr:label 回复的 finding
    # comment），刻意不用 review.json 的 finding 数:那是「上次 review 出了几条」,不是「还剩
    # 几条没标」——标完了它照喊，review.json 被下轮覆盖 / 换机器 / worktree 删了它就没了，
    # 而 MR 上没标的 finding 还挂着。pending 锚在 forge 上，跟本地状态无关。
    # 独立于上面的 Review 行:review.json 没了不影响它，两者是不同的事实源。
    # `label_nudge_due` 而非 `label_pending`:这是**要人干活**的行,不是状态行——同一批 finding
    # 问满 LABEL_NUDGE_CAP 次就闭嘴（不理也是一种回答),来了新的才重新开口。
    if ctx.label_nudge_due:
        lines.append(f"Review findings: {ctx.label_pending} 条待打标 — 逐条求证后回复 "
                     f"`ccr:label=`（label-review skill）")

    if ctx.prs:
        noun, sigil = vocab(ctx.provider)
        parts = []
        for p in ctx.prs:
            star = "*" if p.number == b.pr_number else ""
            parts.append(f"{sigil}{p.number}{star} {p.state or '?'}({p.source_branch or '?'})")
        lines.append(f"Recent {noun}s: " + "  ".join(parts) + ("   (*=current branch)" if b.pr_number else ""))

    # Requirement segment — "where is my TASK" vs the repo lines' "where am I STANDING".
    # Cross-repo/cross-MR live view derived from the requirement spine + each repo's pr.json;
    # empty unless the current branch belongs to an in-flight requirement (zero-token default).
    from .loopstate import requirement as _requirement
    rl = _requirement.turn_line(ctx.repo.repo_dir, b.local.name or None)
    if rl:
        lines.append(rl)

    return " | ".join(lines)


def _display_code_dir(ctx: "RepoContext") -> str:
    """Human-facing repo identity for turn injection.

    `code_dir` intentionally follows the live checkout so lifecycle hooks run in the
    worktree being edited. The prompt header is different: it should name the repo,
    not the transient `.worktrees/...` checkout path. For worktrees, project the
    code-dir relative path back onto the main checkout; keep execution paths untouched.
    """
    code_dir = Path(ctx.repo.code_dir or ctx.repo.real_repo_dir or ctx.repo.repo_dir)
    checkout = Path(ctx.repo.real_repo_dir or ctx.repo.repo_dir or code_dir)
    main = store.state_dir(checkout).parent
    try:
        rel = code_dir.resolve().relative_to(checkout.resolve())
    except (OSError, ValueError):
        return str(code_dir)
    return str(main / rel)


def _format_session(ctx: "RepoContext") -> str:
    lines = ["Repo AGENTS.md references (Read with the Read tool when your task touches these topics):"]
    for r in ctx.agents_md.references:
        lines.append("  - " + _format_ref(r))
    return "\n".join(lines)


def _format_ref(r: Reference) -> str:
    title = r.title or "?"
    desc = (r.hook or "").strip()
    basename = Path(r.path).name if r.path else ""
    desc_is_path = desc and (desc == basename or desc == r.path
                             or (desc.endswith(".md") and Path(desc).name == basename))
    if desc and not desc_is_path:
        return f"{title} — {desc}  ← {basename}"
    return f"{title}  ← {basename}"
