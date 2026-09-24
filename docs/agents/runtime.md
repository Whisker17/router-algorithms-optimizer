# Agent runtime adapter

How this repo's skills reach a *second* agent — for review, escalation, or codebase
exploration — without assuming which runtime or vendor you are working in.

Skills in `.claude/skills/` name a **role**. This file maps roles to real commands.
That indirection is the whole point: model generations turn over every few months, and
the mapping must have exactly one edit point.

- **Mapping:** `config/agent-roles.conf`
- **Executable seam:** `scripts/agent-dispatch.sh`

## The invariant

> Review must be performed by a **different context** than the one that wrote the code,
> running a model **at least as capable** as the implementer, and **preferably from a
> different vendor**.

Everything below is machinery for holding that invariant in whatever runtime you happen
to be in. `model: "opus"` hardcoded in a skill is *one encoding* of the invariant, valid
only inside Claude Code — not the invariant itself.

## Roles

| Role | Purpose | Wants |
|------|---------|-------|
| `IMPLEMENTER` | The session reading this. Never dispatched — it *is* the caller. | — |
| `REVIEWER` | Fresh-context review of a diff (`/code-review`, both axes). | ≥ implementer strength; different vendor preferred |
| `ESCALATOR` | Resolves findings the review loop could not close (`/implement` round-3 escalation). | Strongest reasoner available. May equal `REVIEWER` |
| `EXPLORER` | Read-only codebase sweeps, parallelised (`/improve-codebase-architecture`, `/wayfinder` research, design-it-twice). | Cheap and fast; depth matters less |

## Two dispatch mechanisms

**1. Subprocess (canonical).** `scripts/agent-dispatch.sh <ROLE> <prompt-file>` shells out
to a non-interactive CLI. Works in every runtime, including ones with no sub-agent
primitive. It is the *definition* of what dispatching a role means: the prompt contract
and the output contract are identical everywhere, so a skill written against it behaves
the same under Claude Code, Codex, or a chat-only agent driving a terminal.

**2. Native sub-agent (optimization).** A runtime with its own fresh-context primitive may
use it instead — Claude Code's `Agent` tool with `model:` set to the role's `_MODEL` from
`config/agent-roles.conf`. Saves a process launch and keeps tool output in-band. Use it
when available; it is not a different design, just a faster path to the same contract.

Do **not** write a skill with a three-way branch on runtime. Write it against the role,
note that a native primitive may substitute, and let this file hold the details.

## Choosing a reviewer

Cross-vendor is the **preferred** configuration, not a fallback:

- Implementer Claude Sonnet → reviewer Claude Opus: stronger, but shares the
  implementer's training-induced blind spots.
- Implementer Codex or Grok → reviewer Claude Opus (or the reverse): independent failure
  modes. A blind spot in one is not systematically a blind spot in the other.

So a runtime with no strong sibling model is not disadvantaged — installing the `claude`
CLI purely as a review sidecar gives you the *better* arrangement. Whatever you pick,
`IMPLEMENTER_LABEL` in the conf should say who is implementing, so `--probe` can warn
when both sides of the review are the same vendor.

## Preflight

Before any skill relies on a dispatched role:

```bash
scripts/agent-dispatch.sh --probe            # all roles
scripts/agent-dispatch.sh --probe REVIEWER   # one role
```

Exit `0` = usable, `3` = unusable. The probe confirms the role is configured and its
binary resolves on `PATH`. It **cannot** confirm the flags are correct or that the CLI is
authenticated — verify a new `_CMD` once by hand (`--help`, then one real dispatch)
before trusting it.

## Degraded mode

**This is the load-bearing rule of this file.**

`/implement`'s authorization to self-squash-merge is *derived from* the review loop having
actually run (`docs/GIT_WORKFLOW.md` § Agent / automation constraints #4). So a
portability gap must never silently become an unreviewed merge.

If a required role probes unusable:

1. **Stop. Do not review your own work in the implementing context** and call the loop
   complete. Same-context self-review does not satisfy the invariant — it is the exact
   failure the two-context split exists to prevent.
2. Finish the implementation, open the PR, and leave the tracker at **`In Review`** for a
   human. Say plainly in the PR body which role was unavailable and that the review loop
   did not run.
3. Never record findings in `docs/DEFERRED_ISSUES.md` on the strength of a review that
   did not happen.

A missing `EXPLORER` is less severe — it degrades breadth, not the safety gate. Fall back
to searching inline and say the sweep was narrower than intended.

## When a skill wants parallel sub-agents

Several skills spawn agents in parallel — `/code-review` (2), design-it-twice (3+),
`/wayfinder` research (N). Parallelism there buys wall-clock time and, more importantly,
**context isolation**. Only the isolation is load-bearing.

If your runtime cannot run dispatches concurrently, run them **serially** — one
`agent-dispatch.sh` call per agent, each with its own prompt file. Isolation is preserved
because each subprocess starts clean. Report that you ran serially; do not silently drop
agents to compensate for the wall-clock cost, and in particular never collapse
`/code-review`'s two axes into one agent — the separation is what stops one axis from
masking the other.

## Per-runtime notes

| Runtime | Native sub-agent | Skill loading | Notes |
|---------|------------------|---------------|-------|
| Claude Code | `Agent` tool, `model:` override | `.claude/skills/` auto-loaded, `/name` | `EnterWorktree` defaults to `origin/<default-branch>` — see the base trap in `docs/GIT_WORKFLOW.md` |
| Codex | none equivalent | reads `AGENTS.md`; each skill ships `agents/openai.yaml` interface metadata | Use the subprocess path for all roles |
| Any other | assume none | may have no skill loader — see below | Use the subprocess path for all roles |

**Runtimes with no skill loader.** A skill is just a markdown file. If yours does not
auto-discover `.claude/skills/`, it can still be told the path: *"read
`.claude/skills/implement/SKILL.md` and follow it."* The `## Agent skills` section of
`AGENTS.md` lists the paths for exactly this reason.

## Changing models

When a generation turns over (`opus` → whatever supersedes it), edit
`config/agent-roles.conf` and nothing else. If you find yourself editing a model name
inside `.claude/skills/`, that skill has drifted back to hardcoding — fix the skill to
name a role instead.
