# Deferred issues registry

A living log of issues that were **surfaced during review but consciously not fixed**
in the PR that found them. This is not a bug tracker for open work — it is the record of
*known, accepted debt*: things we decided to defer, so a future change touching the same
area starts from knowledge instead of rediscovery.

## How to use this file

- **Add an entry** whenever a review turns up a real issue that a PR deliberately leaves
  unfixed (scope, risk, or priority). Record it here in the same PR that defers it.
- **Reference it** before working on the affected area — check whether the thing you are
  about to "discover" is already logged, and whether a listed fix is now in scope.
- **Close an entry** by moving it to *Resolved* (bottom) with the PR/commit that fixed
  it, rather than deleting — the history is useful.
- Keep entries short. Link the originating tracker issue / PR and the code symbol so the
  entry stays findable as the code moves.

Severity is the reviewer's judgement at defer time: **High** (correctness/safety, fix
soon — anything touching {{HIGH_RISK_PATHS}} defaults to at least High), **Medium**
(operational/perf, fix when convenient), **Low** (nit/consistency).

## Entry format

```markdown
- **<one-line description of the defect>** (<Severity>, <issue-id>).
  `<file>::<symbol>` — what's wrong, why it was deferred, and what the fix would be.
```

---

## Open

- **A temporary `release/v*` cut is only distinguishable from a live integration branch
  by its open PR into `main`** (Medium, version-routed-PRs port).
  `docs/GIT_WORKFLOW.md` § Fan-out (the live-branch query) — between pushing a fresh cut
  and opening its PR, the query classifies the cut as "live", so a concurrent fan-out
  merges unreleased `dev` into a branch queued for production. Deferred because the only
  real fix changes the branch-naming contract (a distinct prefix for temporary cuts, or
  pushing the branch only after the PR exists); the port mitigates it with prose only
  ("open the PR immediately after the push", § Releasing to `main`).
- **The port's design record is deliberately untracked** (Low, version-routed-PRs port).
  `docs/references/version-routed-prs-port-plan.md` — committing it breaks acceptance
  criteria 1, 6 and 7 of its own §5: those criteria are `grep -rn` sweeps over all of
  `docs/`, and the plan quotes the very literals they forbid (the hardcoded governance
  PR base, the retired milestone title prefix, and placeholder markers) as examples.
  Fix: scope those greps to exclude `docs/references/`, then commit the doc. Until then
  the reasoning behind the workflow lives only in commit messages.

---

## Resolved

_(none yet)_
