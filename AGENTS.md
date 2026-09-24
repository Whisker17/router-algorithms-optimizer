# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, etc.) working in this
repository. `CLAUDE.md` is a symlink to this file — edit here only.

## What this is

An offline Python benchmark for Mantle swap-routing algorithms: freeze one real
liquidity snapshot, compare exact-input solvers with a common evaluator, and report
quality/latency/cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is a separate research challenge.

The full PRD — requirements, architecture, milestones, rejected alternatives, open
risks — lives in `docs/DESIGN.md`. Read it before making any design or architectural
decision; do not re-derive parameters or decisions that are already validated there.

## Status

<!-- Keep this section current: what has landed, what is architected-for but NOT
implemented yet. Update it the moment reality changes instead of leaving stale
placeholders. Agents must not assume a module exists until its issue lands. -->

The complete product specification, issue/release plans and supporting references landed
on `dev` through WHI-1469 / PR #2. Read `docs/DESIGN.md`, `docs/ISSUE_PLAN.md`,
`docs/RELEASE_PLAN.md` and `docs/references/0.1.0-execution-decisions.md`. The original
25-issue publication (WHI-1425–WHI-1449) and its six milestones are recorded in
`docs/references/linear-publication-audit.md` (a dated as-of snapshot); later
documentation handoffs are tracked separately by WHI-1469/1470.
Repository bootstrap is complete (`origin/dev` established and project binding configured
via WHI-1425).

The current execution batch is **0.1.0 only**: the five-source static benchmark and all
six mandatory algorithms, including the source-pinned, parity-tested Uniswap SOR port.
Use the default public Mantle RPC (preflight fixed-block state; no `latest`/mixed-block
fallback or silent private endpoint) and existing Enterprise Dune access with bounded,
cached queries. Runtime is measured, with **no total-runtime acceptance threshold**.
Suitable upstream code may be reused autonomously for private internal research while
retaining required notices and source pins; do not change repository visibility or
publish/relicense code incidentally. These decisions do not relax integer protocol
correctness, independent verification, source parity, coverage, reproducibility or the
review/merge rules below. Jupiter/Metis (0.2.0) remains future work.

Product implementation has **not** started: source, tests and tooling remain template
scaffolding; no pool adapters, collectors, algorithms or benchmark modules exist yet.
The first product issues are WHI-1426 (I01) and WHI-1427 (I02), using the committed spec
and the resolved bootstrap base.

## Build, test, run

<!-- Replace with the real commands once the stack is confirmed. Default Python/uv
stack: -->

```bash
uv sync                                  # install deps (creates .venv)
uv run pytest                            # unit tests
uv run pytest tests/test_smoke.py        # single test file
uv run ruff check .                      # lint
uv run mypy                              # type check
uv run python main.py                    # entrypoint
```

## Runtime configuration

Secrets live in `.env` at the repo root (`.env.example` is the checked-in
template), loaded at startup — a missing required var must fail fast with a clear
error. **Never commit `.env`.** Non-secret runtime parameters (thresholds, feature
flags, tunables) live in `config/` as validated, typed config — not hardcoded, not in
`.env`. See `config/README.md` for the convention.

## Architecture

Module layout is fixed by `docs/DESIGN.md` §4.2. Keep this section a short mirror of
that section — one bullet per top-level module, its single responsibility, and the
load-bearing interfaces other modules may depend on.

Planned layout from `docs/DESIGN.md` §4.2 — **not implemented yet**:

- **`snapshot/`** — verified deployments, frozen bundles/cases and provenance; exposes
  immutable snapshot and request data.
- **`pools/`** — exact protocol math and state transitions; exposes `quote_exact_in`.
- **`routing/`** — funding plans, evaluator and algorithm registry; depends on snapshot
  data and pool simulation.
- **`benchmark/`** — cost context, worker budgets and measurements; depends on the
  solver/evaluator interfaces.
- **`report/`** — offline CSV/HTML from versioned result records only.
- **`main.py`** — thin CLI; `config/` holds validated profiles, `tests/` holds behavioral
  evidence, and `tools/upstream/` is reserved for validation-only reference harnesses.

## Git workflow (mandatory)

**One issue = one git worktree off the resolved base = one PR into that base.**
Do **not** implement issues in the primary clone working tree.
**Never default to `dev`** — resolve the base from the table below; if the issue
does not fall into exactly one row, **stop and surface it**.

| Category | Recognised by | Worktree base | PR base |
| --- | --- | --- | --- |
| Hotfix | `hotfix` label | `origin/main` | `main` |
| Repo-wide governance | touches **only** the carve-out file list | `origin/dev` | `dev` |
| Version-scoped work | everything else | `origin/release/v{version}` | `release/v{version}` |

**Bootstrap:** before the first production tag exists, the version-scoped row
resolves to `dev` and no `release/v*` integration branch exists yet — a resolved
value from the table, not a default. See `docs/GIT_WORKFLOW.md`
§ Resolving the base branch.

**Carve-out** (governs every branch; pinning it to one release leaves hotfixes on
stale rules) — same list as `docs/GIT_WORKFLOW.md`:

- `AGENTS.md` (`CLAUDE.md` is a symlink — edit `AGENTS.md` only)
- `docs/GIT_WORKFLOW.md`
- `docs/agents/` (issue template, tracker binding, triage labels, runtime)
- `.githooks/`
- CI config (`.github/workflows/`)
- `.github/pull_request_template.md`
- `config/agent-roles.conf`
- `scripts/agent-dispatch.sh`
- `.claude/skills/`
- `README.md` (locally-customized skills inventory)
- `CHANGELOG.md` (template feedback loop)

A mixed PR (feature + carve-out) must be **split** — governance → `dev` and
fans out; feature → its release branch.

**Version for the third row — two signals, fail closed:**

- **Primary:** the issue-title prefix `[X.Y.Z]` (e.g. `[0.2.0] [Scheduler] …`).
  Tracker-independent, no API call.
- **Cross-check:** the tracker's release binding (`docs/agents/issue-tracker.md`
  § Release ↔ version binding). A tracker with no release entity drops the
  cross-check; the prefix then stands alone.

If they disagree, or the cross-check exists and either signal is missing,
**refuse to start**. Do not infer the version from a milestone, and do not fall
back to `dev`. If `origin/release/v{version}` does not exist, refuse — do not
create it as a side effect of picking up a ticket.

**Then, once the base is resolved:**

1. `git fetch` + create the worktree from the **resolved** base
   (`fix/whi-NNN-topic`, `feat/whi-NNN-topic`, or `chore/whi-NNN-topic`).
   Verify immediately — `git merge-base HEAD origin/<resolved-base>` must equal
   `git rev-parse origin/<resolved-base>` — whatever tooling created the worktree.
   *(Runtime aside: Claude Code's `EnterWorktree` defaults to `origin/main`, which
   is right for hotfix and wrong for everything else. The check is what settles
   it.)*
2. Implement only that issue; tracker state → **`In Progress`**.
3. `gh pr create --base <resolved-base>` (title/body include `WHI-NNN`
   **and the resolved base plus the signals it was derived from**); tracker →
   **`In Review`**. Any review finding you intentionally leave unfixed goes in
   `docs/DEFERRED_ISSUES.md` as part of this PR — see that file for the format.
4. Ordinary development PRs into `dev` or a live version-integration branch are
   **pre-authorized to self-squash-merge** after issue acceptance criteria, the required
   tests/lint/type checks, scope and semantic-conflict checks pass and GitHub reads
   MERGEABLE/CLEAN. **No fixed per-issue review rounds or escalation pass are required.**
   Focused review is optional when requested or justified by a concrete concern; reviewer
   unavailability alone does not block the ordinary development lane. Complete the
   mandatory **whole-release independent review** in `docs/GIT_WORKFLOW.md`
   § Release review gate before shipping a release.
   **Human gates remain:** configured high-risk paths (none in this offline benchmark),
   `release/*` → `main` promotions, and a finished version-integration `release/v*` → `dev`.
   After merging, run the post-merge cleanup below. **No waiver of these human gates is
   in force.** Any bounded waiver must follow `docs/GIT_WORKFLOW.md` § Waiving an exception
   and amend every rule site it overrides; a tracker label alone waives nothing.

### Two `release/` lifecycles

Same prefix, different job. `release/*` → `main` is a human gate for **both**.

- **Temporary cut** (`release/v0.1.4`): branched from `dev`, receives **no**
  feature work, PR → `main`, deleted after merge. A multi-commit hotfix *batch*
  may cut from `main` instead; a single `hotfix/*` PR still goes straight to
  `main`.
- **Long-lived version integration** (`release/v0.2.0`): branched from `dev`,
  **receives feature PRs**, lives for the whole version. When the version is
  done: merge it into `dev` (merge commit, **human gate**) so `dev` stays
  "validated and shippable", then promote `dev` → `main` via a fresh temporary
  cut. Do not PR the integration branch to `main` directly.

Cutting an integration branch is a **deliberate act**, never a side effect of
picking up a ticket.

### Post-merge cleanup (mandatory, in order)

Drive these from the **primary clone**. Never commit **feature work** to `dev` or
a `release/v*` integration branch directly. Three merges are documented
exceptions — not PR-gated, pushed with `ALLOW_DIRECT_PUSH=1`: fan-out into live
integration branches (step 5), the hotfix backmerge of `main` into `dev`, and the
first push of a freshly cut `release/v*`.

0. **If the PR is CONFLICTING** (the resolved base advanced since you branched):
   inside the feature worktree, `git merge origin/<resolved-base>`, resolve,
   rerun the affected tests, and `git push`.
   The PR must read **MERGEABLE / CLEAN** before you merge.
1. **Squash-merge + drop the remote branch:** `gh pr merge <N> --squash --delete-branch`.
2. **Remove the worktree:** `git worktree remove <worktree-path>` then
   `git worktree prune`.
3. **Delete the local branch:** `git branch -D fix/whi-NNN-topic`
   (this fails while the worktree still holds the branch — do step 2 first).
4. **Fast-forward the resolved base:** `git fetch origin --prune` then
   `git merge --ff-only origin/<resolved-base>` (must fast-forward — if it would
   create a merge commit, your local copy diverged: reset it to the remote rather
   than merging).
5. **Fan-out whenever `dev` advanced:** merge `dev` into every **live**
   `release/v*` integration branch in this same session (query in
   `docs/GIT_WORKFLOW.md` — never a hardcoded name list).
   A governance rule is only in force on branches that carry it.
   `main` is excluded. After a hotfix, this step is what puts the fix onto version
   branches — and it reads `origin/dev`, so the backmerge must be **pushed** first
   or the fan-out ships nothing.
6. **Tracker → `Done`.**

### Promotion lanes (`→ main`)

`main` **equals production** — always the last deployed tag. Never open a PR with `dev` as
head into `main` (the branch would be auto-deleted by `delete_branch_on_merge`). Two lanes
reach `main`, and picking the wrong one ships unreviewed work:

- **Release** — everything on `dev` is shippable. Cut a temporary `release/vX.Y.Z` from
  `dev`, PR → `main`. **Always a human gate.**
- **Hotfix** — production is broken *and* `dev` holds work that must not ship. Branch off
  `origin/main`, PR → `main`, then, from the primary clone and after a `git fetch`,
  **merge `main` back into `dev` and push it** (`ALLOW_DIRECT_PUSH=1`), then **fan out
  `dev` into every live version-integration branch** or the next version ships without
  the fix.

The decision rule: run `git log --oneline origin/main..origin/dev`. **If that list holds a
single commit you would not ship right now, you must use the hotfix lane.**

Merge strategy is per-lane: **squash** into `dev` and into a long-lived
`release/v*`, but **merge commit** into `main` and for a finished integration
branch merging back into `dev` —
squashing a release/hotfix disconnects the tag from `dev`'s history and silently breaks
`git log <tag>..origin/dev`. Bump the project version before tagging, **deploy from the
tag and never from a branch**, and keep the tracker Release ↔ git tag ↔ GitHub Release
triple in agreement (backfill the Release's `commitSha`).

Enable the local push guard once per clone **and per worktree**:
`git config core.hooksPath .githooks`.

Full rules: `docs/GIT_WORKFLOW.md`.

## Template feedback loop

This repo was bootstrapped from the shared project template
(`git@github.com:Whisker17/code-template.git`). When work here surfaces an improvement that belongs to the
**template layer** — a workflow rule that bit us, a skills configuration fix, a doc
convention worth standardizing — tell the user explicitly so they can port it back to
the template repo (and its `CHANGELOG.md`). Project-specific learnings stay here;
process-level learnings flow back.

## Agent runtime (any agent, any vendor)

This repo is runtime-neutral: Claude Code, Codex, or anything else. Nothing in the
workflow names a model. Instead, skills name a **role** — `REVIEWER`, `ESCALATOR`,
`EXPLORER` — mapped to real commands in `config/agent-roles.conf` and dispatched through
`scripts/agent-dispatch.sh`. Full contract: **`docs/agents/runtime.md`**.

Two rules matter more than the mechanism:

- **Release review happens in a different context than implementation**, with a model
  at least as capable (cross-vendor preferred). Prove the selected dispatch works before
  relying on it; a binary-only `--probe` is not an authentication check.
- **If a required release reviewer is unavailable, the release gate has not passed** —
  keep the release PR open and its tracking issue at `In Review`; do not mark the Linear
  Release as Released. Report the missing role. Ordinary issue development
  may continue after its validation gates pass. Never relabel implementing-context
  self-checks as independent review. Reviewer preflight is not a default per-issue gate.

When a model generation turns over, edit `config/agent-roles.conf` and nothing else.

## Agent skills

Skills live in `.claude/skills/<name>/SKILL.md`. Runtimes that auto-discover them expose
each as `/<name>`; **in a runtime with no skill loader, read the file directly** — a skill
is just markdown. The load-bearing ones:

| Skill | Path |
|-------|------|
| `/implement` | `.claude/skills/implement/SKILL.md` |
| `/code-review` | `.claude/skills/code-review/SKILL.md` |
| `/orchestrate` | `.claude/skills/orchestrate/SKILL.md` |
| `/ponytail` | `.claude/skills/ponytail/SKILL.md` |
| `/grill-me` → `/to-spec` → `/to-tickets` | `.claude/skills/{grill-me,to-spec,to-tickets}/SKILL.md` |
| `/tdd`, `/diagnosing-bugs`, `/handoff`, `/triage` | `.claude/skills/<name>/SKILL.md` |
| `/ask-matt` (which skill do I want?) | `.claude/skills/ask-matt/SKILL.md` |

### Issue tracker

Issues and PRDs live in **Linear** (project `Mantle Router Algorithm Optimizer`, team
`Whisker-Personal`). Access is a fallback ladder — MCP tools, else the GraphQL API with
`LINEAR_API_KEY` — and reaching the tracker is mandatory, not optional: workflow state
moves in lockstep with the PR. External PRs are not a triage surface. See
`docs/agents/issue-tracker.md`.

### Triage labels

Canonical role names (`needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`) used verbatim as Linear labels. See
`docs/agents/triage-labels.md`.

### Domain docs

This repo's spec of record is `docs/DESIGN.md` (PRD: requirements, architecture,
milestones, rejected alternatives, open risks) plus `docs/adr/` for narrower decisions
made after v1 ships. See `docs/agents/domain.md`.
