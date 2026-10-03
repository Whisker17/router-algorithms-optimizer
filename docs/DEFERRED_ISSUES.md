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

- **`uni_sor_port` can select a route set whose union is an economic token cycle**
  (Medium, WHI-1447). `routing/algorithms/uni_sor_port.py` (B-S* combination, contract
  §3.3) — upstream excludes route combinations only by shared pool id, so at
  `max_hops >= 3` it can combine e.g. USDC→mETH→WETH→USDT with USDC→WETH→mETH→USDT over
  different pools. The v1 evaluator rejects any plan whose token graph has a cycle
  (DESIGN §2.5), so such cases are recorded as `invalid_plan` (never dropped); the port
  keeps upstream's selection for parity. Fix: either a documented adapter deviation that
  skips cycle-forming combinations (with its own golden/parity note), or a spec change that
  admits independent-route plans with opposite intermediate legs.
- **A second SIGTERM during interrupt finalization leaves a run `running`** (Low, WHI-1447).
  `benchmark/runner.py::run_experiment` (the `except BaseException` block) — the CLI turns
  SIGTERM into `KeyboardInterrupt`; if another SIGTERM arrives while the handler is killing
  the in-flight worker (`slot.kill()` → `join`), it escapes the handler, so the manifest is
  not finalized as `interrupted` and the unfinished cases get no `cancelled` records. The
  run stays explicitly incomplete (`state: running`, refused by `load_manifest`), so no
  result is misreported; seen when a `timeout` wrapper signalled `uv` and its child. Fix:
  ignore/mask SIGTERM for the duration of the finalization block.
- **The v1 acceptance wall times come from concurrent runs on a shared workstation**
  (Low, WHI-1447). `docs/references/v1-acceptance.md` §7 — DESIGN §2.10 asks for measured
  timing from sequential runs on an otherwise idle machine; the final runs executed 2–11 at
  a time on the owner's everyday 10-core workstation, so their latencies, wall times and
  memory-pass durations are inflated and noisy (deterministic results are unaffected).
  Runtime is not an acceptance threshold (owner decision), so no clean pass was made. Fix:
  replay the daily (and, if wanted, full) profile alone on a dedicated idle machine with
  the recorded `replay_command` and publish its `latency.csv` beside the v1 record.
- **`uni_sor_port` does not use the empirical cost in its own selection** (Medium, WHI-1445).
  `routing/algorithms/uni_sor_port.py` (contract A-3) — SOR's gas model is route-additive
  (`gasModel` per route); the empirical model prices a *complete plan shape*, and no parity
  contract maps one onto the other, so the port keeps zero gas scores and selects on raw
  quotes under `empirical_cost` too. Its returned plan is still net-evaluated and flagged.
  Fix: a documented SOR-gas-model adapter (per-route cohort cost) with its own parity note.
- **`direct_split` rechecks the objective only over its per-leg-count finalists**
  (Low, WHI-1445). `routing/algorithms/direct_split.py` step 4 — under `empirical_cost` a
  finalist that is unrankable (e.g. a single LB pool beyond the cohort bin range) is not
  replaced by the next-best rankable allocation of the same leg count, so 4/349 full-corpus
  plans fall back to a flagged `low_confidence` split. Fix: keep the best rankable
  allocation per leg count as an extra finalist.
- **Split/shared-graph and reverted-transaction costs are not calibrated**
  (Medium, WHI-1445). `benchmark/costs.py` — `parallel` cohorts have <= 6 training
  transactions in the 7-day window and failed swaps have no `dex.trades` rows, so split
  plans stay unranked on net. Fix: a longer or wider window (or trace-level evidence for
  split execution) in a new model version.

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

- **The latency harness's matched sentinel labels `cohort_of` users `full_source`**
  (Low, WHI-1606). `benchmark/latency.py::build_bundles` derives the `sor_compatible/sentinel`
  request bundle without `record_cohort`, so `metis_history`, `incremental_graph_repair`,
  `direct_split_certified` and `cfmm_dual` would record `universe.cohort = full_source` in
  their `r021.domain/1` if a future latency run included them (no recorded run does: stage L
  ran only base identities there). Deferred deliberately: recording the cohort changes that
  bundle's hash (`c967974a…` → `90f31051…` on the L01 parent), and `report.latency compare`
  refuses experiments whose derived bundles differ, so every recorded L01 experiment (the
  0.1.2 baseline `d5061563` and the 0.2.1 stage-L experiments) would stop being comparable.
  `main.py quote` records it (WHI-1606). Fix: pass `record_cohort=True` there under a new
  latency protocol version, whose derived-bundle hashes are recorded afresh.

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
