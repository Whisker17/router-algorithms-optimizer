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
soon — anything touching high-risk paths defaults to at least High), **Medium**
(operational/perf, fix when convenient), **Low** (nit/consistency).

## Entry format

```markdown
- **<one-line description of the defect>** (<Severity>, <issue-id>).
  `<file>::<symbol>` — what's wrong, why it was deferred, and what the fix would be.
```

---

## Open

- **The LB collector walks a book one non-empty bin per RPC round trip** (Medium, WHI-1436).
  `snapshot/collectors/liquidity_book.py::LBCollector._walk` — `getNextNonEmptyBin` answers
  are sequential per side, so a wide book costs one request per bin even with Multicall3
  (WMNT/USDT bin step 15 alone took ~3,080 round trips; the whole LB corpus collection ~42
  min). Deferred: the corpus completed within its declared bound and its bundle is frozen.
  Fix options: read `getBin` over contiguous id ranges in one `aggregate3` (a bin is a tree
  member iff it is non-empty), or stop a side once the collected bins account for the
  pair's whole reserve of that side's token (bins above the active id hold only X, below
  only Y) and record the range as complete; either needs its own equivalence tests against
  a full walk.
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

- **The LB pool model does not see an `LBHooksRewarder`'s extra swap hook** (Medium,
  WHI-1433 → resolved in WHI-1434). `pools/liquidity_book.py::_swap_hook_calls` admitted a
  swap hook by the pair's hooks-clone implementation only, while the rewarder forwards
  `beforeSwap` to its own `_extraHooksParameters`. Fixed: the LB collector
  (`snapshot/collectors/liquidity_book.py::LBCollector._verify_hooks`) reads the pair's
  `getLBHooksParameters()` and the rewarder's `getExtraHooksParameters()`, verifies each
  clone's implementation against the catalog's approved `lb_collection.swap_hooks` /
  `extra_swap_hooks` (pinned code hashes, clone args and `getLBPair()` /
  `getParentRewarder()` bound to the pair and rewarder), records addresses and code hashes
  in provenance and refuses anything else (`unsupported_hook`); the state carries
  `extra_hooks_parameters` / `extra_swap_hook_implementation`, and the pool model returns
  `UNSUPPORTED` for an unadmitted swap-flagged extra hook
  (`LBSource.amount_neutral_extra_swap_hooks`).
