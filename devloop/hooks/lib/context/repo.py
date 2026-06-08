"""`RepoContext` — per-repo state, persisted as per-owner segment files under
`<git_root>/.devloop/` (`meta.json` / `branch.json` / `pr.json` / `validation.json` /
`injection.json`). `RepoContext` is the in-memory *view* that `load()` assembles by
merging them; each mutator writes back only its own segment.

Why one file per owner: the state has several independent writer-roles (the refresh,
the MR monitor, validation marks, injection marks) running in different processes. A
single shared file would force a read-modify-write that can lose a concurrent writer's
update; one file per owner makes every write touch a disjoint file, so that whole class
is structurally impossible — no lock, atomic per-file writes (see base.py `_write_atomic`).

Schema + operations (load/refresh/mark/emit) built on `base.py` leaves
(`AgentsMd`/`PullRequest`/`Cadence`/...) and the `gitcmd`-routed `git_state`. Notable schema choices:

- **PR model**: `branch.pr_number` holds only the current branch's PR/MR *number*; the
  recent-PR window lives in `prs`. Both are monitor-owned and persist in `pr.json`, which is
  *branch-keyed* — on `load` the number is joined in only if it was computed for the current
  branch, so a branch switch self-invalidates it with no writer. "branch inactive" is
  *derived* (`branch_pr_inactive`) by joining pr_number → prs, never stored as a bool. The
  forge (GitHub/GitLab) is recorded per-PR so display picks the right vocabulary.
- **No scheduler segment** — the MR sweep is a native `monitor` that self-paces.
- All timestamps are float epoch (see base.py).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import git_state, parsers, repo_layout
from . import base
from .base import (
    REPO_STALE_SEC,
    SESSION_TTL_SEC,
    TURN_TTL_SEC,
    AgentsMd,
    Cadence,
    PullRequest,
    Reference,
    WorktreeInfo,
    vocab,
)


# ── segment dataclasses ──────────────────────────────────────────────────────
@dataclass
class RepoMeta:
    repo_dir: str = ""        # git root as the caller referenced it (symlink preserved)
    real_repo_dir: str = ""   # Path(repo_dir).resolve() — for git IO
    code_dir: str = ""        # where make/uv run (repo root or server/ backend/)
    language: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "RepoMeta":
        d = d or {}
        return cls(
            repo_dir=d.get("repo_dir", ""),
            real_repo_dir=d.get("real_repo_dir", ""),
            code_dir=d.get("code_dir", ""),
            language=d.get("language"),
        )


@dataclass
class BranchState:
    current: str | None = None
    protected: bool = False
    target: str = "release"
    ahead: int = 0
    behind: int = 0
    pr_number: int | None = None       # current branch's PR/MR number; join into prs for the rest
    worktree: WorktreeInfo = field(default_factory=WorktreeInfo)

    @classmethod
    def from_dict(cls, d: dict | None) -> "BranchState":
        d = d or {}
        return cls(
            current=d.get("current"),
            protected=bool(d.get("protected")),
            target=d.get("target", "release"),
            ahead=int(d.get("ahead", 0) or 0),
            behind=int(d.get("behind", 0) or 0),
            pr_number=d.get("pr_number"),
            worktree=WorktreeInfo.from_dict(d.get("worktree") or {}),
        )


@dataclass
class Validation:
    last_lint_at: float | None = None
    last_test_at: float | None = None
    edits_since_lint: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Validation":
        d = d or {}
        return cls(
            last_lint_at=d.get("last_lint_at"),
            last_test_at=d.get("last_test_at"),
            edits_since_lint=int(d.get("edits_since_lint", 0) or 0),
        )


@dataclass
class Injection:
    turn: Cadence = field(default_factory=Cadence)
    session: Cadence = field(default_factory=Cadence)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Injection":
        d = d or {}
        return cls(
            turn=Cadence.from_dict(d.get("turn") or {}),
            session=Cadence.from_dict(d.get("session") or {}),
        )


@dataclass
class RepoContext:
    repo: RepoMeta = field(default_factory=RepoMeta)
    agents_md: AgentsMd = field(default_factory=AgentsMd)
    branch: BranchState = field(default_factory=BranchState)
    validation: Validation = field(default_factory=Validation)
    injection: Injection = field(default_factory=Injection)
    prs: list[PullRequest] = field(default_factory=list)   # monitor-owned recent-PR window
    updated_at: float = 0.0

    # ── load (merge segments) ──────────────────────────────────────────────────
    @classmethod
    def load(cls, repo_dir: str | Path) -> "RepoContext | None":
        """Assemble the in-memory view by merging the per-owner segment files.

        `meta` is the existence marker: absent → not initialized → None (caller
        refresh_all's). Every other segment defaults independently, so one missing /
        corrupt file degrades to its default without losing the rest (fail-open)."""
        meta = base.load_segment(repo_dir, "meta")
        if meta is None:
            return None
        branch = BranchState.from_dict(base.load_segment(repo_dir, "branch") or {})
        pr = base.load_segment(repo_dir, "pr") or {}
        # Join the monitor-owned pr_number only when it was computed for the CURRENT branch.
        # pr.json is branch-keyed, so a branch switch self-invalidates the stale number with
        # nobody writing — the monitor re-establishes it on its next poll.
        branch.pr_number = pr.get("pr_number") if pr.get("branch") == branch.current else None
        ctx = cls(
            repo=RepoMeta.from_dict(meta.get("repo")),
            agents_md=AgentsMd.from_dict(meta.get("agents_md") or {}),
            branch=branch,
            validation=Validation.from_dict(base.load_segment(repo_dir, "validation") or {}),
            injection=Injection.from_dict(base.load_segment(repo_dir, "injection") or {}),
            prs=[PullRequest.from_dict(p) for p in (pr.get("prs") or []) if p.get("number") is not None],
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
        base.save_segment(root, "meta", {
            "repo": asdict(self.repo),
            "agents_md": asdict(self.agents_md),
            "updated_at": self.updated_at,
        })
        git_state.ensure_gitignore_excluded(root)   # keep /.devloop/ out of git

    def _save_branch(self) -> None:
        if not self._root():
            return
        d = asdict(self.branch)
        d.pop("pr_number", None)   # pr_number is monitor-owned → the `pr` segment, not here
        base.save_segment(self._root(), "branch", d)

    def _save_pr(self) -> None:
        """Monitor's write surface (also used by gcampr via a one-shot poll). Branch-keyed
        so a later branch switch invalidates pr_number on read without anyone clearing it.
        `provider` is derived from the window so display keeps the right vocabulary."""
        if not self._root():
            return
        base.save_segment(self._root(), "pr", {
            "branch": self.branch.current,
            "provider": self.prs[0].provider if self.prs else "",
            "pr_number": self.branch.pr_number,
            "prs": [asdict(p) for p in self.prs],
        })

    def _save_validation(self) -> None:
        if self._root():
            base.save_segment(self._root(), "validation", asdict(self.validation))

    def _save_injection(self) -> None:
        if self._root():
            base.save_segment(self._root(), "injection", asdict(self.injection))

    # ── refresh (re-derive from authoritative sources) ─────────────────────────
    @classmethod
    def refresh_all(cls, repo_dir: str | Path) -> "RepoContext":
        """Full rebuild (normal-impl boundary: SessionStart / enter / TTL).

        Writes only the refresher-owned segments (meta + branch). validation /
        injection / pr live in their own files and are left untouched — their values
        are merged in from disk only to keep the *returned* object complete."""
        repo_dir_in = str(Path(repo_dir))
        repo_dir_abs = str(Path(repo_dir).resolve())
        code_dir = repo_layout.find_repo_code_dir(repo_dir_abs)
        language = repo_layout.detect_language(code_dir)
        agents_md_path = repo_layout.find_agents_md(repo_dir_abs, code_dir)
        target = git_state.get_default_target(repo_dir_abs)

        prev = cls.load(repo_dir_abs) or cls()
        items = parsers.parse_references_section(agents_md_path) if agents_md_path else []
        ctx = cls(
            repo=RepoMeta(repo_dir=repo_dir_in, real_repo_dir=repo_dir_abs,
                          code_dir=code_dir, language=language),
            agents_md=AgentsMd(
                path=agents_md_path,
                references=[Reference(title=r.get("title", ""), path=r.get("path", ""),
                                      hook=r.get("description")) for r in items],
            ),
            branch=_build_branch(repo_dir_abs, target),
            validation=prev.validation,
            injection=prev.injection,
            prs=prev.prs,
        )
        ctx.branch.pr_number = prev.branch.pr_number   # keep the join value on the returned object
        ctx._save_meta()
        ctx._save_branch()
        return ctx

    @classmethod
    def refresh_branch(cls, repo_dir: str | Path) -> "RepoContext":
        """Incremental branch refresh (fast; after git state change). No AGENTS.md re-parse.
        Writes only branch.json. Not-yet-initialized → fall back to a full build."""
        ctx = cls.load(repo_dir)
        if ctx is None:
            return cls.refresh_all(repo_dir)
        prev_num = ctx.branch.pr_number
        target = ctx.branch.target or git_state.get_default_target(ctx.repo.real_repo_dir)
        ctx.branch = _build_branch(ctx.repo.real_repo_dir, target)
        ctx.branch.pr_number = prev_num   # in-memory only; pr.json (the source of truth) is untouched
        ctx._save_branch()
        return ctx

    @classmethod
    def is_stale_at(cls, repo_dir: str | Path, ttl: float = REPO_STALE_SEC) -> bool:
        meta = base.load_segment(repo_dir, "meta")
        if meta is None:
            return True
        return base.is_stale(meta.get("updated_at"), ttl)

    # ── mutators (each touches exactly one segment) ─────────────────────────────
    def increment_stale_edits(self, delta: int = 1) -> None:
        self.validation.edits_since_lint += delta
        self._save_validation()

    def mark_lint_passed(self) -> None:
        self.validation.last_lint_at = base.now()
        self.validation.edits_since_lint = 0
        self._save_validation()

    def mark_test_passed(self) -> None:
        self.validation.last_test_at = base.now()
        self._save_validation()

    def set_branch_pr_number(self, number: int | None) -> None:
        """Write surface for the current branch's PR/MR number (monitor + create flow)."""
        self.branch.pr_number = number
        self._save_pr()

    def set_prs(self, prs: list[PullRequest]) -> None:
        """Monitor's sole write surface for the recent-PR window."""
        self.prs = list(prs)
        self._save_pr()

    # ── PR derivation ─────────────────────────────────────────────────────────
    def current_pr(self) -> PullRequest | None:
        if self.branch.pr_number is None:
            return None
        return next((p for p in self.prs if p.number == self.branch.pr_number), None)

    def branch_pr_inactive(self) -> bool:
        """True if the current branch's PR/MR is merged/closed (derived, not stored).
        The branch-merged guard reads this."""
        p = self.current_pr()
        return bool(p and p.inactive)

    def branch_pr_in_flight(self) -> bool:
        """True if the current branch's PR/MR is still open / awaiting human merge (derived).

        The loop's between-rounds state. Surfaced so the orchestrator can note that committing
        here continues an in-flight PR — and, more importantly, so starting NEW work is never
        stacked onto an in-flight feature branch by default (it re-bases off origin/<target>)."""
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

    def mark_turn_emitted(self, text: str) -> None:
        self.injection.turn.mark(text, now=base.now())
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
        """PostCompact: drop both cadences' stamps so state re-injects next turn."""
        self.injection.turn.clear()
        self.injection.session.clear()
        self._save_injection()


# ── private builders / renderers ──────────────────────────────────────────────
def _build_branch(repo_dir: str, target: str) -> BranchState:
    current = git_state.get_current_branch(repo_dir)
    ahead, behind = 0, 0
    if git_state.target_exists(repo_dir, target):
        ab = git_state.get_ahead_behind(repo_dir, target)
        if ab:
            ahead, behind = ab
    is_linked, common_dir, main_branch = git_state.get_worktree_metadata(repo_dir)
    # pr_number is intentionally NOT set here — it's monitor-owned (the `pr` segment) and
    # joined in by branch at load time, so a branch switch drops it without any writer.
    return BranchState(
        current=current,
        protected=git_state.is_protected_branch(current),
        target=target,
        ahead=ahead,
        behind=behind,
        worktree=WorktreeInfo(is_linked=is_linked, common_dir=common_dir, main_branch=main_branch),
    )


def _format_turn(ctx: "RepoContext") -> str:
    lines = [f"[Current repo: {ctx.repo.code_dir} ({ctx.repo.language or '?'})]"]
    b = ctx.branch
    cur = b.current or "?"
    if b.worktree.is_linked:
        wt = f" (worktree, main={b.worktree.main_branch})" if b.worktree.main_branch else " (worktree)"
    else:
        wt = ""
    extras = []
    if b.protected:
        extras.append("PROTECTED")
    pr = ctx.current_pr()
    if pr and pr.inactive:
        extras.append(
            f"INACTIVE ({pr.label} {pr.state}) — cut a new branch from latest origin/{b.target}"
        )
    elif pr and pr.is_open:
        # Soft hint, not a guard: an in-flight PR has one legitimate edit case (amending it for
        # review), so we surface the state and let the agent choose rather than hard-blocking.
        noun = vocab(pr.provider)[0]
        extras.append(
            f"IN-FLIGHT ({pr.label} open) — new work needs a fresh branch (gcampr --branch); "
            f"edit here only to amend this {noun}"
        )
    extra_str = f" ⚠️ {'; '.join(extras)}" if extras else ""
    lines.append(f"Branch: {cur}{wt} (ahead {b.ahead}, behind {b.behind}, target={b.target}){extra_str}")

    raw = git_state.get_workspace_status(ctx.repo.real_repo_dir or ctx.repo.repo_dir)
    if raw.get("dirty"):
        lines.append(f"Workspace: dirty: {raw.get('modified_count', 0)} modified, {raw.get('untracked_count', 0)} untracked")
    else:
        lines.append("Workspace: clean")

    v = ctx.validation
    stale = f", {v.edits_since_lint} edits since" if v.edits_since_lint else ""
    lines.append(f"Validation: lint={base.fmt_ts(v.last_lint_at)}{stale}; test={base.fmt_ts(v.last_test_at)}")

    if ctx.prs:
        noun, sigil = vocab(ctx.prs[0].provider)
        parts = []
        for p in ctx.prs:
            star = "*" if p.number == b.pr_number else ""
            parts.append(f"{sigil}{p.number}{star} {p.state or '?'}({p.source_branch or '?'})")
        lines.append(f"Recent {noun}s: " + "  ".join(parts) + ("   (*=current branch)" if b.pr_number else ""))

    return " | ".join(lines)


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
