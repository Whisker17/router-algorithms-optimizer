# AGENTS.md

The canonical instruction entry for coding agents in this repository. Project facts and
load-bearing rules are here; read linked details only when the task needs them.
`CLAUDE.md` remains a compatibility symlink to this file for the installed older client;
edit this file only. See `docs/agents/template-sync.md`.

## What this is

An offline Python benchmark for Mantle swap-routing algorithms: freeze one real liquidity
snapshot, compare exact-input solvers with a common evaluator, and report quality,
latency and execution-cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is a separate future research challenge.

The product spec is `docs/DESIGN.md`. Read it before design or architectural changes;
do not re-derive validated decisions. Template updates must not replace this business spec.

## Status

The 0.1.0 business modules and six-algorithm acceptance report landed through WHI-1447 /
PR #27. `docs/references/v1-acceptance.md` records the results and their limitations;
`docs/DEFERRED_ISSUES.md` records known debt. These artifacts are not a claim that every
release-review/promotion gate has passed. Linear is authoritative for current issue and
Release state. This template-sync task does not implement the planned 0.1.1 quote feature
(WHI-1498), start Jupiter/Metis, change a Release status, or publish a release.

The business contract stays intact: five-source frozen state, exact integer protocol
math, independent plan evaluation, source-pinned Uni SOR parity and honest comparison
coverage. Use the public Mantle RPC with fixed-block preflight (no `latest`/mixed-block
fallback) and bounded, cached Enterprise Dune queries. Runtime is measured without a
total-runtime acceptance ceiling. Suitable private internal source reuse is autonomous
while required notices/source pins remain; no incidental public distribution or relicensing.
Details: `docs/references/0.1.0-execution-decisions.md`.

Process tooling follows code-template v0.2; project adaptations and the source pin are in
`docs/agents/template-sync.md`. Completed legacy tickets remain historical evidence; new
or rescheduled work needs the new `## Execution` metadata before dispatch.

## Build, test, run

```bash
uv sync
uv run pytest                                      # complete offline test suite
uv run pytest tests/test_synthetic_run.py            # focused CLI/replay check
uv run ruff check .
uv run mypy
uv run python main.py --help
```

Required on every merge: Ruff, mypy and the tests relevant to the changed behavior.
Run the complete suite for governance, standalone implementation, hotfixes and release
candidates, per the workflow's three verification tiers. Full benchmark experiments are
release/acceptance work, not a reason to regenerate frozen evidence during tooling changes.

## Runtime configuration

Secrets use `.env` / the runtime environment; **never commit `.env`**. Validate required
credentials only for commands that need them. Offline replay/report needs no credentials.
Non-secret parameters and the default public RPC URL live in validated typed `config/`.
Preserve the existing profiles, calibrated costs and hashes; see `config/README.md`.

## Architecture

The existing modules follow `docs/DESIGN.md` §4.2:

- **`snapshot/`** — verified deployments, fixed-block collection, immutable bundles/cases and provenance.
- **`pools/`** — exact protocol math and state transitions; common `quote_exact_in` behavior.
- **`routing/`** — funding plans, independent evaluation, registry and the six algorithms.
- **`benchmark/`** — objective/cost context, worker budgets, measurements and result records.
- **`report/`** — offline CSV/HTML from versioned results, without live chain access.
- **`main.py`** — CLI; `config/` has typed profiles, `tests/` has behavioral evidence,
  and `tools/upstream/` contains validation-only reference harnesses.

## Git workflow (mandatory)

Full rules: **`docs/GIT_WORKFLOW.md`**, authoritative if this summary disagrees.
**One issue = one worktree off the resolved base = one PR into that base.** Never
implement in the primary clone. Never default to `dev` or create an integration branch
as a side effect of choosing a ticket.

| Category | Recognised by | Worktree base | PR base |
| --- | --- | --- | --- |
| Hotfix | `hotfix` label | `origin/main` | `main` |
| Governance | only the documented carve-out paths | `origin/dev` | `dev` |
| Version-scoped | everything else | `origin/release/v{version}` | `release/v{version}` |

- Version = `[X.Y.Z]` title prefix cross-checked against the actual tracker Release,
  never a milestone. Missing/disagreeing signals refuse. Before the first production tag,
  the version row resolves to `dev` by the bootstrap clause, not by a fallback.
- Governance has no version/Release. Preserve the recorded historical WHI-1425 metadata
  exception; do not use it for new issues. Mixed governance/version changes are split.
- Assert `git merge-base HEAD origin/<base>` equals `git rev-parse origin/<base>` after
  worktree creation. Enable `git config core.hooksPath .githooks` per worktree.
- Required project checks always run, relevant checks run per issue, full checks run for
  release candidates and lanes not covered by release acceptance.
- PR evidence records base/signals, commit SHA, commands/results/artifacts and actual
  role/model/effort. Tracker: `In Progress` → `In Review` (PR open) → `Done` (merged/cleaned).

### Merge authorization

| Work | Before merge | Agent may merge? |
| --- | --- | --- |
| Ordinary version issue under `/orchestrate` | Acceptance, required/relevant checks on final HEAD; orchestrator verifies evidence | Yes; review at release level |
| Bootstrap version issue → `dev` under `/orchestrate` | Same | Yes; first release still gets complete review |
| Governance → `dev`; standalone `/implement` | Required/relevant/full checks and one independent PR review on final commit | Yes |
| Configured high-risk path | Its lane's checks and documented verification | No — human; none configured in this offline project |
| `hotfix/*` → `main` | Required/relevant/full checks, independent review | No — human |
| Finished `release/v*` → `dev` | Full acceptance and release review on current SHA | No — human |
| Temporary `release/*` → `main` | Release flow | No — human |

No human-gate waiver is in force. A waiver must follow the complete documented procedure;
a tracker label alone changes nothing. The template's bounded review loop is **release
level**, never three mandatory reviews per issue.

### After a merge

From the primary clone: remove worktree/local branch, fast-forward the resolved base,
and whenever `dev` advances fan out into every live version-integration branch in the
same session (query them; do not assume names). Squash issue PRs into `dev`/integration;
use merge commits for production promotion and finished integration → `dev`.
`main` is production; deploy only from a tag. Promote with a temporary release cut,
never a direct `dev` → `main` PR. Preserve hotfix backmerge and all push guards.

## Agent runtime

Skills name **ORCHESTRATOR**, **IMPLEMENTER**, **REVIEWER** and an effort (`medium`/`high`).
`config/agent-roles.conf` is the only dispatch model/effort mapping;
`scripts/agent-dispatch.sh` implements it. Contract: `docs/agents/runtime.md`.

- Independent review runs in a different context, preferably a different vendor.
- `--probe` is static configuration evidence; a real bounded call proves dispatch/auth.
- Unsupported roles/efforts or failed authentication never cause silent model substitution
  or reduced effort. Configuration does not change the current session's model.
- Preflight once per release/configuration change; do not repeat it for each issue.
- The current role configuration is project-specific, not the template's blank defaults.

## Agent skills

Read `.claude/skills/<name>/SKILL.md` directly if the runtime has no skill loader.
The eight project-local core skills are:

| Skill | Use |
| --- | --- |
| `/grill-me` | Clarify goal, constraints and acceptance without reopening settled facts |
| `/to-spec` | Fill the numbered product spec in place; publish only on instruction |
| `/to-tickets` | Canonical issue template, complexity/scope and native dependencies |
| `/implement` | One issue: worktree, minimal implementation, relevant checks, PR/handoff |
| `/orchestrate` | Release scheduling/integration, fixed baseline and bounded review/fixes |
| `/code-review` | One independent reviewer covering the fixed PR or complete release |
| `/handoff` | Short evidence-linked handoff; durable state stays in GitHub/Linear |
| `/ponytail` | Minimum necessary implementation without dropping correctness or verification |

Other globally installed skills are unaffected. Project traps are on-demand references;
do not inject the complete historical registry into every worker prompt.

### Issue tracker and domain docs

Linear project **Mantle Router Algorithm Optimizer**, team **Whisker-Personal / WHI**.
Use MCP first, then the documented API fallback; unavailable tracker means a blocked
operation, never a shadow tracker. See `docs/agents/issue-tracker.md`,
`docs/agents/issue-template.md` (English), and `docs/agents/triage-labels.md`.
The product spec is `docs/DESIGN.md`, with later decisions in `docs/adr/` and accepted
debt in `docs/DEFERRED_ISSUES.md`. Do not substitute a template design for project facts.

## Template feedback loop

Source: `git@github.com:Whisker17/code-template.git`. Record reusable process improvements
in `CHANGELOG.md` and tell the owner so they can port them upstream. Project-specific
behavior and benchmark findings stay here.
