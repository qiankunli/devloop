[English](./README.md) | [简体中文](./README.zh-CN.md)

# devloop

**A guard-railed development loop for AI coding agents.** A cross-CLI plugin marketplace: `devloop` is the first (and flagship) plugin — a developer workflow built on native Claude Code events, working with both **GitHub (PR)** and **GitLab (MR)** (picked per-repo from the origin remote); `example` is a placeholder showing the repo is built to host *multiple* plugins.

> Currently Claude Code only. Design / architecture: [AGENTS.md](./AGENTS.md). Each plugin's own docs live in its directory.

## The problem

When you code with an AI agent, the time sink usually isn't "is the code correct" — it's three **structural losses**:

1. **Information lag** — the agent doesn't know the real git / workspace state and guesses from chat history. Classic failure: it grinds away on a feature branch whose MR was *already merged* (and source branch deleted) on the server, never realizing until commit-time preflight stops it — forcing a re-branch + re-commit + re-MR every time.
2. **Soft conventions can't enforce** — "don't commit to master", "don't `git add -A`" are just prompts. When the agent decides not to follow them, you have **no execution-level interception**. Committing to a protected branch, staging stray sensitive files, editing on a stale branch — all happen for real.
3. **Concurrent sessions collide** — running several CLI sessions (or several agents) on one workspace is routine, but they share checkouts and state: a second session switches the branch under the first one's feet and scrambles its uncommitted work, or one session's no-arg command silently resolves to the repo *another* session just touched. Out of the box, nothing arbitrates who owns what.

## What devloop does — two levers

- **A state bus eliminates information lag.** The current subproject's branch / working tree / recent MRs / validation state is injected into *every* prompt, so the agent knows reality before it edits the first line.
- **Hard intercepts turn soft conventions into execution-level boundaries.** `PreToolUse` hooks return `deny`; the agent cannot route around them.

Both levers share one hub: a structured state bus under `.devloop/`. State written on `git commit` / `cd` / background polling is reused across N later prompt injections and M protected-branch checks at zero extra cost.

Loss 3 is answered by **session-grain state** riding the same two levers: an owner lock per checkout (guests' branch switches and edits are denied, routed to a worktree) plus a per-session repo binding (one session's fallback never resolves to another session's repo) — see *Aggregate-workspace & multi-session as first-class* below.

## Design ideas worth knowing

**What to hard-block vs. soft-hint** — the rule: *no legal edit case → hard-block; a legal exception exists → soft-hint.* Your current branch is always in one of four states:

| State | Meaning | Handling |
|-------|---------|----------|
| protected | main / master / release* | **hard-block** commit/push |
| healthy | normal feature branch, in progress | allow |
| in-flight | PR/MR opened, awaiting human merge | **soft-hint** (inject one `IN-FLIGHT` line) |
| inactive | PR/MR merged / closed | **hard-block** Edit/Write |

`protected` and `inactive` hard-block cleanly — editing there has no legitimate reason. `in-flight` only hints, because there's a legal exception (you might be amending your own PR/MR) the machine can't reliably tell from new work, so it feeds the fact to the agent and lets it choose.

**Structural guarantees, not just hints** — a new branch's base is decided by *intent, not by where HEAD happens to sit*: opening new work (`--branch`) always cuts from `origin/<target>`, and a freshly cut branch is asserted to carry only this run's commits before push/PR. So even if the agent ignores the `IN-FLIGHT` hint, forking off an in-flight branch can't smuggle its commits into the new PR.

**Aggregate-workspace & multi-session as first-class** — a workspace root holds many independent git subprojects (often symlinked). Scripts never trust shell `cwd` (they resolve the repo by explicit `--repo` → cwd's repo → *this session's* last-active repo; with no binding of its own a session is asked for an explicit `--repo` rather than guessing from another session's activity), and an *owner lock* keeps two concurrent sessions from mixing changes into one working tree. Plain single-repo mode is fully supported too — auto-detected, no manual switch.

**native-first** — every capability sits on the most native event primitive instead of a workaround:

| Capability | Workaround (old) | devloop (native) |
|------------|------------------|------------------|
| project-enter awareness | regex-parse `cd` | **`CwdChanged`** auto-enter |
| survive compaction | TTL safety-net (timed guess) | **`PostCompact`** → re-inject |
| `AGENTS.md` changes | mtime polling | **`FileChanged`** + `watchPaths` |
| PR/MR awareness / branch staleness | hook-heartbeat scheduler | **`monitors`** background poll |

All git goes through one `gitcmd` seam, all code-review hosting through one `lib/forge` facade (GitHub / GitLab as peer adapters, picked per-repo), all user config through one `lib/config` seam. Every guard is **fail-open** — a broken guardrail at worst fails to block; it never blocks your work.

## Where it's heading

devloop makes the loop *run smoothly*, but the loop's **granularity** is still step-level — a human nudges at every step. The next step is automating the **verify** link (lint → test → automated eval) until intervention can lift from *step-level* to *requirement-level*: the human states a requirement, the agent develops + verifies + self-corrects in a closed loop, the human accepts the result. lint / test / eval are that loop's sensors — the more automatic and trustworthy they are, the fewer steps a human must touch.

---

## Install (Claude Code)

```
/plugin marketplace add https://github.com/qiankunli/devloop.git
/plugin install devloop@devloop
```

Optionally run init once (hooks also auto-init on first `cd` into a repo):

```
# Mode A: aggregate workspace (one root holding many git subprojects)
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_workspace.py <your-aggregate-workspace>

# Mode B: a single git repo
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_repo.py
```

Forge features (PR/MR creation + state injection) need a token for your host (`GITHUB_TOKEN` / `GITLAB_TOKEN`, or the `forges` block) — see [devloop/README.md](./devloop/README.md) for the unified `~/.devloop/config.json`.

### Codex / opencode

The marketplace layout is CLI-agnostic: a new CLI just needs a `.<cli>-plugin/marketplace.json` at the repo root (`.agents/plugins/marketplace.json` for Codex and `.opencode/marketplace.json` already exist) plus a matching manifest per plugin. `devloop` itself is currently **Claude Code only** (its hard intercepts / state injection sit on Claude-native events); the Codex side currently ships only the `example` placeholder.

## Plugins

| Plugin | What it is | README |
|--------|-----------|--------|
| `devloop` | Developer workflow: git/PR (GitHub + GitLab) + cwd-aware enter + lint/test gates + live state injection + execution-level hard intercepts (Claude-only) | [devloop/README.md](./devloop/README.md) |
| `example` | Placeholder demonstrating the multi-plugin marketplace structure | [example/README.md](./example/README.md) |

## Adding a plugin

See [CONTRIBUTING.md](./CONTRIBUTING.md).
