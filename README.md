[English](./README.md) | [简体中文](./README.zh-CN.md)

# devloop

**A guard-railed development loop for AI coding agents.** A cross-CLI plugin marketplace: `devloop` is the first (and flagship) plugin — a developer workflow for Claude Code and Codex, working with both **GitHub (PR)** and **GitLab (MR)** (picked per-repo from the origin remote); `example` is a placeholder showing the repo is built to host *multiple* plugins.

> Claude Code and Codex are supported. Design / architecture: [AGENTS.md](./AGENTS.md). Each plugin's own docs live in its directory.

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

## Where it's heading — from step-level to requirement-level

devloop makes the loop *run smoothly*, but its **granularity** is still step-level — a human nudges at every step. The north star is to lift that intervention from *step-level* to **requirement-level**: you state a requirement; the agent develops → verifies → reads the result → self-corrects in a closed loop; a human only **accepts** at the end.

That hinges on the **verify** link climbing from static checks (lint / test) to a real **verdict** the loop can converge against — and a verdict worth converging on isn't one pass/fail. Four parallel, *accumulable* judgment dimensions, each answering its own question:

| dimension | answers | how |
|-----------|---------|-----|
| **correctness** | does the contract / behavior hold | black-box, against a running system |
| **effectiveness** | is the agent's output actually good | black-box (incl. LLM-as-judge) |
| **capacity** | does it hold under load | black-box |
| **taste** | is it built the way you'd want — design / boundaries / naming | white-box, reads the diff, no deploy needed |

Two boundaries keep it honest (and line up with the *levels-of-autonomy*, *eval-driven* and *spec-driven* directions the field is converging on): **the human keeps merge** — release authority never moves into the loop — and **the agent changes code to meet the bar, never moves the bar itself** (specs and thresholds stay human-governed, the same side as merge). Think L4 "human as approver," not L5.

devloop is the **loop machine** — the state bus, the hard intercepts, and the run / verify / deploy beats; the verdict producers are a separate, pluggable concern. So the open surface is wide: more judgment dimensions and sensors, wiring a verdict back into the loop as feedback, the deploy beat that lets behavioral checks hit a real system, the white-box taste judge. **If this frontier interests you, open an issue or a discussion — ideas here are exactly the kind of contribution we're after.**

*Adjacent lines of work this draws on and sits among: **agentic coding** / **autonomous coding agents**, **self-correcting** & **verifier-driven** loops, **eval-driven development** (**LLM-as-judge**), **spec-driven development**, and the **levels of autonomy** framing for **human-in-the-loop** AI software engineering.*

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

Codex support is packaged through `.agents/plugins/marketplace.json` and `devloop/.codex-plugin/plugin.json`.

```
codex plugin marketplace add https://github.com/qiankunli/devloop.git
# then install devloop from /plugins, or use `codex plugin add devloop@devloop`
```

Codex does not expose every Claude event that devloop uses. The Codex manifest points at `devloop/hooks/hooks.codex.json`, which uses the supported subset (`PreToolUse` / `PostToolUse` / `SessionStart` / `UserPromptSubmit` / `PostCompact`) and refreshes cwd/state from `PostToolUse` as the fallback for Claude's `CwdChanged`. `FileChanged` and `SessionEnd` have no Codex equivalent yet, so AGENTS.md reparse and owner-lock release rely on the existing prompt/TTL fallback paths there.

opencode remains placeholder-only until its plugin/hook protocol is wired.

## Plugins

| Plugin | What it is | README |
|--------|-----------|--------|
| `devloop` | Developer workflow: git/PR (GitHub + GitLab) + cwd-aware state + lifecycle hooks (lint/test/code-review per phase) + live state injection + execution-level hard intercepts (Claude + Codex) | [devloop/README.md](./devloop/README.md) |
| `example` | Placeholder demonstrating the multi-plugin marketplace structure | [example/README.md](./example/README.md) |

## Adding a plugin

See [CONTRIBUTING.md](./CONTRIBUTING.md).
