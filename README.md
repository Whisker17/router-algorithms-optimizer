# code-template

A project template for process-standardized, agent-driven development. Every new
project starts from this skeleton and inherits the same workflow: **PRD-first specs,
Linear-tracked issues, worktree-per-issue git flow, and a vendored suite of engineering
skills**.

**Runtime-neutral by design.** Nothing in the workflow names a model. Skills name a
*role* (`REVIEWER`, `ESCALATOR`, `EXPLORER`); `config/agent-roles.conf` maps roles to
commands and `scripts/agent-dispatch.sh` dispatches them, so the same process runs under
Claude Code, Codex, or an agent that only has a terminal. See `docs/agents/runtime.md`.

Extracted and generalized from `pm-arbitrage-bot`, where this process was battle-tested.

## How to use

1. Create a new repo from this template (GitHub "Use this template", or clone + re-init).
2. Open it in any coding agent and say: **"Read SETUP.md and execute it."**
   The agent interviews you (project name, Linear project, high-risk paths, stack,
   Docker, agent roles), replaces every placeholder marker, wires up git/GitHub merge
   policy, verifies the skeleton, and deletes `SETUP.md`.
3. Produce the spec of record: `/grill-me <your idea>` → `/to-spec` fills
   `docs/DESIGN.md`.
4. Break it down: `/to-tickets` publishes blocked/blocking Linear issues.
5. Implement: `/implement` per issue — worktree off the **resolved base** (version-scoped
   → `release/v{version}`, repo-wide governance → `dev`, hotfix → `main`; never a
   defaulted `dev`), three-round review loop, self-squash-merge on green (except
   {{HIGH_RISK_PATHS}} and releases). A ready *set* of tickets can go to `/orchestrate`
   instead of driving each `/implement` by hand.
6. Ship: cut a temporary `release/vX.Y.Z` from `dev` → PR into `main` (merge commit), tag,
   deploy **from the tag**. Production broken while `dev` holds unshippable work? Take the
   hotfix lane instead — `docs/GIT_WORKFLOW.md`.

## What's inside

| Layer | Contents |
|-------|----------|
| **Agent guidance** | `AGENTS.md` (canonical; `CLAUDE.md` is a symlink) |
| **Runtime adapter** | `docs/agents/runtime.md` (role contract, degraded mode), `config/agent-roles.conf` (role → command), `scripts/agent-dispatch.sh` (dispatch + `--probe`) |
| **Skills** (22 vendored from `mattpocock/skills` + first-party `/orchestrate` + `/ponytail`) | implement, code-review, orchestrate, ponytail, handoff, tdd, diagnosing-bugs, prototype, wayfinder, grill-me, grill-with-docs, grilling, triage, improve-codebase-architecture, research, resolving-merge-conflicts, setup-matt-pocock-skills, to-spec, to-tickets, domain-modeling, codebase-design, teach, writing-great-skills, ask-matt + `skills-lock.json` |
| **Docs system** | `docs/DESIGN.md` (PRD skeleton, spec of record), `docs/GIT_WORKFLOW.md`, `docs/DEFERRED_ISSUES.md`, `docs/TRAPS.md` (orchestrate trap registry), `docs/adr/`, `docs/references/`, `docs/agents/` (domain / issue-tracker / triage-labels / issue-template) |
| **Git workflow** | main ≡ production + dev + `release/v*` version integration + worktree-per-issue; fail-closed base resolution (version-scoped / governance carve-out / hotfix — never a defaulted `dev`); per-lane merge strategy (squash → `dev` and → long-lived `release/v*`, merge commit → `main` and for a finished integration branch); fan-out of `dev` into every live version branch; release vs hotfix decision rule; version axis (tracker Release ↔ tag ↔ GitHub Release); mandatory post-merge cleanup; Linear state lockstep; agent self-merge with human-review exceptions; `.githooks/pre-push` guard |
| **Stack layer** (default: Python/uv, swappable) | `pyproject.toml` (uv + hatchling + ruff + mypy strict + pytest), `main.py`, `tests/`, `config/` convention, `.env.example`, optional `Dockerfile` + `docker-compose.yml` |

The **process layer** (docs, workflow, skills) is stack-agnostic; only the stack layer
changes when a project isn't Python.

## Template evolution

This template is expected to improve as projects hit process-level problems. Downstream
repos carry a "Template feedback loop" section in their `AGENTS.md`: when a project
discovers a template-layer improvement, port it back here and record it in
`CHANGELOG.md`. Old projects pick changes up manually (there is deliberately no
auto-sync).

Skills are pinned by `skills-lock.json`; upgrade them here deliberately, not per-project.

> ⚠️ **Locally customized skills** — `skills-lock.json` records the *upstream* hash, so it
> cannot detect these edits and **re-vendoring via `/setup-matt-pocock-skills` will
> silently overwrite them.** Diff before accepting any skill upgrade to:
>
> - `implement/SKILL.md` — three-round review loop + escalation pass; self-merge
>   authorization; `REVIEWER`/`ESCALATOR` role dispatch; ponytail generation
>   constraint + shrink pass before review
> - `code-review/SKILL.md` — `REVIEWER` role dispatch on both axes; Reinvented Wheel
>   smell on the Standards baseline
> - `improve-codebase-architecture/`, `codebase-design/DESIGN-IT-TWICE.md`,
>   `wayfinder/SKILL.md` — `EXPLORER` role dispatch with a documented serial fallback
> - `ask-matt/SKILL.md` — runtime-neutral compaction wording; implement drives
>   tdd + ponytail
> - `orchestrate/` — first-party, not vendored; do not add it to `skills-lock.json`.
>   Trap registry lives in `docs/TRAPS.md` (skill-local `traps.md` is a pointer).
>   Verify checks the implementer's rung report.
> - `ponytail/` — first-party, not vendored; do not add it to `skills-lock.json`.
>   Generation constraint driven by `/implement`, not a process skill.
