"""`RepoContext` — per-repo state, persisted as per-owner segment files under
`<git_root>/.devloop/` (`meta.json` / `branch.json` / `remote_branches.json` / `pr.json` /
`validation.json` / `injection.json`). `RepoContext` is the in-memory *view* that `load()`
assembles by merging them; each mutator writes back only its own segment.

Why one file per owner: the state has several independent writer-roles (the refresh, the MR
monitor, validation marks, injection marks) running in different processes. A single shared
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
from . import base
from .base import (
    REPO_STALE_SEC,
    SESSION_TTL_SEC,
    TURN_TTL_SEC,
    AgentsMd,
    Cadence,
    PullRequest,
    Reference,
    pr_label,
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
    target: str = "release"                                    # canonical trunk to MR into / fork from
    pr_number: int | None = None                               # joined from pr.json (monitor-owned)
    remotes_fetched_at: float | None = None                    # provenance of `remotes`

    @classmethod
    def from_local_dict(cls, d: dict | None) -> "BranchTopology":
        """Build the refresh-owned half (local + worktrees + target) from branch.json. remotes /
        remotes_fetched_at / pr_number are merged in by `load` from their own segments."""
        d = d or {}
        return cls(
            local=Branch.from_dict(d.get("local")),
            worktrees=[Branch.from_dict(w) for w in (d.get("worktrees") or [])],
            target=d.get("target", "release"),
        )

    def base_branch(self) -> str:
        """The trunk this branch is measured against — its recorded fork point, else the
        repo's canonical target."""
        return self.local.fork_from or self.target

    def remote_tip(self, name: str | None) -> Branch | None:
        return next((r for r in self.remotes if r.name == name), None)


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
    branch: BranchTopology = field(default_factory=BranchTopology)
    validation: Validation = field(default_factory=Validation)
    injection: Injection = field(default_factory=Injection)
    prs: list[PullRequest] = field(default_factory=list)   # monitor-owned recent-PR window
    provider: str = ""   # repo-level forge ("github"/"gitlab"); drives display vocabulary
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
        branch = BranchTopology.from_local_dict(base.load_segment(repo_dir, "branch") or {})
        # monitor-owned remote trunk tips (remote_branches.json) + their provenance stamp.
        rb = base.load_segment(repo_dir, "remote_branches") or {}
        branch.remotes = [Branch.from_dict(r) for r in (rb.get("remotes") or [])]
        branch.remotes_fetched_at = rb.get("fetched_at")
        # Join the monitor-owned pr_number only when it was computed for the CURRENT (cached)
        # branch — DISPLAY-grade; write-gates re-derive against LIVE branch (lib.context.gate).
        # pr.json is branch-keyed, so a branch switch self-invalidates the stale number.
        pr = base.load_segment(repo_dir, "pr") or {}
        branch.pr_number = pr.get("pr_number") if pr.get("branch") == branch.local.name else None
        ctx = cls(
            repo=RepoMeta.from_dict(meta.get("repo")),
            agents_md=AgentsMd.from_dict(meta.get("agents_md") or {}),
            branch=branch,
            validation=Validation.from_dict(base.load_segment(repo_dir, "validation") or {}),
            injection=Injection.from_dict(base.load_segment(repo_dir, "injection") or {}),
            prs=[PullRequest.from_dict(p) for p in (pr.get("prs") or []) if p.get("number") is not None],
            provider=pr.get("provider", ""),
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
        """Refresh-owned: the LOCAL half only (local + worktrees + target). `remotes` is the
        monitor's (remote_branches.json) and `pr_number` is pr.json's — never written here."""
        if not self._root():
            return
        base.save_segment(self._root(), "branch", {
            "local": asdict(self.branch.local),
            "worktrees": [asdict(w) for w in self.branch.worktrees],
            "target": self.branch.target,
        })

    def _save_pr(self) -> None:
        """Monitor's write surface (also used by gcampr via a one-shot poll). Branch-keyed
        so a later branch switch invalidates pr_number on read without anyone clearing it.
        `provider` is repo-level (the forge backing this repo) — stored once in the header,
        not on every PR."""
        if not self._root():
            return
        base.save_segment(self._root(), "pr", {
            "branch": self.branch.local.name,
            "provider": self.provider,
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

        Writes only the refresher-owned segments (meta + branch). validation / injection /
        pr / remote_branches live in their own files and are left untouched — their values
        are merged in (via prev) only to keep the *returned* object complete."""
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
        target = ctx.branch.target or git_state.get_default_target(ctx.repo.real_repo_dir)
        ctx.branch = _build_topology(ctx.repo.real_repo_dir, target, ctx.branch)
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
    mirror_stale = False
    if remote and remote.commit:
        local_mirror = git_state.rev_parse(repo_dir, f"origin/{base_name}")
        mirror_stale = bool(local_mirror) and local_mirror != remote.commit
    if mirror_stale:
        asof = f", ⚠ trunk moved since fetch {base.fmt_ts(b.remotes_fetched_at)} — fetch to recount"
    elif b.remotes_fetched_at:
        asof = f", as of {base.fmt_ts(b.remotes_fetched_at)}"
    else:
        asof = ""
    return {"base": base_name, "ahead": ahead, "behind": behind, "asof": asof}


def _format_turn(ctx: "RepoContext") -> str:
    lines = [f"[Current repo: {ctx.repo.code_dir} ({ctx.repo.language or '?'})]"]
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

    v = ctx.validation
    stale = f", {v.edits_since_lint} edits since" if v.edits_since_lint else ""
    lines.append(f"Validation: lint={base.fmt_ts(v.last_lint_at)}{stale}; test={base.fmt_ts(v.last_test_at)}")

    if ctx.prs:
        noun, sigil = vocab(ctx.provider)
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
