# Cycle-safe SOR selection for `uni_sor_cycle_safe` (R021-P09, WHI-1555)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` ([`contract.md`](contract.md)). This memo fills the `uni_sor_cycle_safe` row that §11 delegated to it. It changes no shared schema, vocabulary or work unit |
| Publication key | `R021-P09`, WHI-1555, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository base | written on `origin/dev` `1ce50763b84b7daf4eec844665848b5b1c27fb6a`, then merged with `origin/dev` `6284a9d4c8c1c77d18f2fd9a8cfe836f410c2c3f` (WHI-1560 LB research, write-disjoint). `routing/`, `pools/`, `benchmark/`, `snapshot/` and `tests/fixtures/uni_sor/` are byte-identical to B `81559ab` at both bases |
| Outcome | **`go`** (§12) |
| Records | [`fixtures/cycle-safe-sor.json`](fixtures/cycle-safe-sor.json): hand-traced core fixtures K1–K6; adapter fixtures A1, A3, A4, A6, A7; the phase-semantics rows A5; the option schema and preset; the bounded corpus diagnostic, as a historical view (`corpus_probe`) and an enriched view (`corpus_probe_enriched`) |
| Executable check | `uv run pytest tests/routing/test_cycle_safe_sor_contract.py -q` |
| Downstream | WHI-1556 (`uni_sor_cycle_safe` implementation). The amendment text is in §13 |

This memo defines a separately named adaptation of the pinned Uniswap SOR combination
step. It adds one rule: a combination of routes is admitted only if the union of their
token edges is acyclic, which is the evaluator's plan-token-DAG rule. The rule is checked
at the single point where SOR combines routes. Everything else is the pinned reference,
unchanged: candidates, grid, quote table, V2/V3 coverage, ordering, ties, seeds, queue,
pruning, split cap, final order, D-1 remainder fill and D-3 replay.

No strategy is implemented here. The normative selector is an executable specification in
`tests/routing/test_cycle_safe_sor_contract.py` (`select_cycle_safe`, `spec_solve`). It runs
inside the actual `uni_sor_port` adapter, and only the selector is swapped. `uni_sor_port`,
its contract, its 35 upstream goldens, `uni_sor_fast` and the two optimized recipes are
unchanged. Their historical records are unchanged too.

**What is claimed (only this).**

- **S (safety).** The variant never returns a plan whose token graph has a cycle.
- **P (reference trajectory).** This is a statement about *completed* selector runs on the
  identical complete quote table. When the adapted selector ran to its own termination and
  rejected nothing, its entire search and selection are identical to `uni_sor_port`'s.
  If, in addition, the replay was not interrupted, the plan, status and score are identical
  too. Admission rejects something exactly when the reference formed a cyclic combination
  at some point. A solve whose selector never ran (an enumeration status, the
  `max_candidates` threshold, a quote cut while building the table, a hard kill) is
  **not** evidence of identity. Neither is a replay interrupted by a quote cut, as far as the
  end-to-end result goes (§4.7).
- **L4.** With `search.max_hops ≤ 2`, admission can never reject anything.
- **Budget and coverage.** The domain is the same (candidates, grid, V2/V3 coverage, quote
  table), the budget meaning is the same, and admission costs no quotes. It does cost CPU.

**What is not claimed.**

- That the variant is better than, or even no worse than, the reference on any case. K5
  shows it can lose to a *valid* reference selection, because admission changes the B-S5
  pruning rule.
- That the variant finds the best admissible selection. K1: the variant gets 126 and the
  oracle gets 131.
- Upstream parity for the variant. The goldens stay authoritative for `uni_sor_port` only.
- Any speedup or timing effect.
- End-to-end parity with `uni_sor_port` under wall-clock cuts. Admission costs CPU, so a
  hard wall can end the two solves at different points. CS-2 publishes later, so a cut
  during the replay leaves a different `last_valid_candidate` (A4, §4.7).

The evidence classes of R021-C/1 §1 stay separate:

- *Repository evidence*: the actual port and evaluator, the frozen WHI-1447 records.
- *The external proposal*: the archived report §3 row "独立 cycle-safe SOR 变体".
- *New reconstructions*: this issue's fixtures and oracle.
- *A bounded diagnostic pass* (§9.4). It is a separate in-process pass, never a measured
  solve, and it makes no timing claim.

## 1. Sources

| Source | Pin | Used for |
| --- | --- | --- |
| `routing/algorithms/uni_sor_port.py` | at base; sha256 `04ee4d08…745e` | `compute_all_routes` (B-R1…B-R8), `build_route_quotes` (B-Q*), `find_first_route_not_using_used_pools` (B-S9, l. 501), `get_best_swap_route_by` (B-S2…B-S12, B-F1, l. 538, chooser at l. 593), `get_best_swap_route` (B-S1, B-F2, l. 670), `integer_fill` (D-1), `solve` (A-1/A-2, statuses, budgets, `report_candidate` at l. 1024 **before** the replay at l. 1026). Read only |
| [`uni-sor-port-contract.md`](../uni-sor-port-contract.md) | at base | behaviour ids B-*, adaptations A-1…A-8, deviations D-1…D-4, golden categories G-1…G-14 |
| `tests/fixtures/uni_sor/` (35 goldens, `MANIFEST.json` sha256 `8a0370c4…efde`) | at base | produced by the actual pinned upstream functions (WHI-1443). Authoritative for `uni_sor_port` only |
| `routing/evaluator.py` | at base; sha256 `75d8a1f7…1440` | `_check_plan` static check: one edge `token_in → token_out` per step, `_token_cycle` (l. 240, 334) → `invalid_plan` "economic token cycle" |
| `routing/algorithms/path_split.py::split_path_plan` | at base | the plan shape: one chain per route, explicit first amount, `ALL_REMAINING` on the last route and on later hops |
| [`v1-acceptance.md`](../v1-acceptance.md) §3.1, §5, §9 and [`DEFERRED_ISSUES.md`](../../DEFERRED_ISSUES.md) | at base | the known defect: 12 report-split and 2 tuning-split `uni_sor_port` `invalid_plan` cases (token cycle), kept for parity |
| External report | [`sources/research-report.md`](sources/research-report.md), SHA-256 `0588e83a…ba1d9` | §3 table row: check the plan token DAG at combination time, keep `uni_sor_port` parity, test cycle/noncycle/tie first, use the old report failures only as regressions. This is an external proposal. Its absent `verify_research.py`/`results.json` were not run |

## 2. The defect and the three rules that must not be conflated

At `search.max_hops ≥ 3`, upstream combines routes only by physical pool identity (B-S9).
Two routes can each be a simple path and share no pool, and still have a union that
contains a token cycle. The evaluator rejects such a plan (DESIGN §2.5: no economic token
cycle). The real record, from `v1-acceptance.md` §3.1: USDC→mETH→WETH→USDT combined with
USDC→WETH→mETH→USDT over different pools, which gives `mETH → WETH → mETH`.

Three rules are involved. They are different, and the fixtures keep them apart
(`test_a1_overlap_and_path_local_revisit_are_different_rules`):

| Rule | Owner | Example in A1 | Consequence |
| --- | --- | --- | --- |
| **Physical pool overlap** | B-S9 (upstream) | s–a–x–b–y–c–t with s–a–x–f–t share pool `a`; their token union is **acyclic** | excluded before any admission check (the check counter stays 0) |
| **Path-local revisit** | B-R4 (upstream DFS `tokensVisited`) | the walk s–x–y–x–t (`a, b, e, f`) | never enumerated; as a one-route plan it is an evaluator token cycle |
| **Plan union cycle** | evaluator `_check_plan`; SOR has no rule | s–x–y–t (`a, b, c`) with s–y–x–t (`d, e, f`): both simple, pool-disjoint, union `x → y → x` | the reference selects it → `invalid_plan`. The variant rejects it at admission |

**A1 reproduction** (hand `getAmountOut`, fee 30 bps; input 2,000,000 s→t; `max_hops 3`,
`max_splits 2`, `percent_step 50`):

- Pool `b` gives about 2 y per x and pool `e` about 2 x per y, so x→y→x is a
  cyclic-arbitrage loop.
- The reference's selection is (d,e,f)@50 = 1,418,480 plus (a,b,c)@50 = 1,427,742, in the
  B-F1 order. The actual `uni_sor_port.solve` returns `invalid_plan`
  `economic token cycle: x -> y -> x`.
- The variant makes 2 admission checks and rejects both. It returns the baseline
  (a,b,c)@100. The evaluator replays that as `ok` with gross 2,231,431, which equals the
  hand value.
- The exhaustive oracle on the same table gives a best admissible value of 2,231,431 and
  a best value of 2,846,222 if the cycle rule is ignored.

A 3-route version of the defect also appears on the real corpus. From `v1-acceptance.md`
§5: USDT→USDC→WETH→mETH, USDT→WMNT→USDC→mETH and USDT→WETH→WMNT→mETH together give
USDC→WETH→WMNT→USDC. Any pair of those routes can be acyclic. K1 reproduces this shape.
Admission therefore tests the **whole union**; a pairwise check is not enough
(`test_k1_pairwise_admission_is_not_enough`).

## 3. Admission-point inventory (every place a combination can form)

| Stage (behaviour id) | Combination formed? | Cycle-safe rule |
| --- | --- | --- |
| Enumeration B-R1…B-R8, family lists A-1 | no (single routes; B-R4 keeps each route simple) | unchanged |
| Grid B-A1, quote table B-Q1…B-Q5, null rules A-2 | no | unchanged; the same entries in the same order |
| B-S2 per-percent stable sort | no | unchanged; the tie order is B-Q1 order |
| B-S3 100 % baseline | one route | unchanged, no check. A simple path's graph is acyclic (L1) |
| B-S4 seeds (best, then second best per percent) | one route | unchanged, no check (L1) |
| **B-S7/B-S9 chooser** in node expansion | **yes: the only point** | **admission (§4.2)** |
| B-S8 completion (strict `>`) and enqueue | uses the admitted `routes_new` | unchanged. The union is already acyclic (L2 makes a recheck redundant) |
| B-S5 layer, pruning rule, split cap | no | unchanged code. `best_swap` is now the best *admissible* selection, so pruning can differ (K5) |
| B-S10 no selection | — | `no_route` (§4.5) |
| B-F1 V8 final order, B-F2 rational remainder | reorders and re-amounts routes | unchanged. The edge set is unchanged (L3) |
| D-1 integer fill (`split_path_plan`) | reorders and re-amounts routes | unchanged. The edge set is unchanged (L3) |
| D-3 in-solve replay | — | unchanged. The evaluator stays the authority (§4.4) |

## 4. The algorithm (normative)

### 4.1 Admission test

- `edges(route)` = the consecutive pairs of the route's SOR `token_path`. These are the
  lowercase token addresses: the same identity B-R4 uses, and the `token_in → token_out`
  of each emitted plan step.
- A set of routes is **admissible** iff the directed graph ∪ `edges(route)` has no directed
  cycle. A self-loop counts as a cycle.
- The test is deterministic. The executable form is an iterative three-colour DFS over
  sorted nodes. Any correct acyclicity test is equivalent.

### 4.2 The chooser (replaces B-S7/B-S9 inside the variant only)

For node `N` (routes `N.cur`) and percent `p`, scan `sorted_groups[p]` in B-S2 order:

1. If the entry shares a pool identifier with any route of `N.cur`, skip it (B-S9, first).
   This skip is not counted as an admission check.
2. Otherwise `admission_checks += 1`. If `N.cur ∪ {entry}` is admissible, choose this entry
   and stop the scan.
3. Otherwise `combinations_rejected_cycle += 1` and **continue the scan** with the next
   entry.
4. If no entry is chosen, this percent contributes no child for `N`. The port behaves the
   same way when B-S9 finds nothing.

The continue-scan policy follows from treating a token cycle as one more conflict, exactly
like a pool overlap. The alternative "skip the percent at the first cyclic entry" loses
admissible partners. K2 is a mutation test for this: the skip policy returns r1@100 = 100,
while the contract returns r3@50 + r1@50 = 115. There is no option to switch between the
two policies. Every other line of `getBestSwapRouteBy` is the port's, including the
`special` flag, FIFO layers, the B-S5 order (pruning first, then the cap), the strict
comparison and the cached totals.

### 4.3 Adapter pipeline

`prepare` is `uni_sor_port.prepare`. The candidates are the A-1 cohort (V3 and V2 lists in
ascending lowercase `pool_id`, mixed = V3 ++ V2). LB pools stay excluded (D-4), and
`coverage_mode` keeps its meaning. `solve` is `uni_sor_port.solve` with the §4.2 selector.
It keeps the same enumeration, `unsupported`/`no_route` from enumeration, the
`max_candidates` threshold, the complete A-2 quote table through the guarded
per-solve memo, B-F1, D-1 (`integer_fill`, `split_path_plan`) and the D-3 replay with the
guarded quote function. WHI-1556 must import the port's functions for all of these rather
than copy them, so that parity cannot drift. The only new code is `getBestSwapRouteBy`,
adapted by exactly the §4.2 chooser (every other line stays the port's), plus the
publication rule, the status text and the diagnostics.

### 4.4 Publication and replay

The plan is published through `SolveContext.report_candidate` **only after** the in-solve
`evaluate` returned `ok`. It is published once, and the published plan equals the returned
plan. This is a deliberate difference from `uni_sor_port`, which publishes before its
replay (§10, CS-2). As a result, a hard timeout or a quote cut during the replay leaves no
`last_valid_candidate` from this identity, where the reference would have one (A4). Every
solve records this in `cycle_safe.publication`. Whenever a plan was built but not published
(the replay was interrupted, or it did not return `ok`), `withheld_by_cs2` is true. Reports
must show that difference and attribute it to CS-2. They must never treat it as a search
difference.

The admission check never replaces the evaluator. The variant's replay and the runner's
independent evaluation still decide the status. If the replay returns `invalid_plan`,
the variant returns that status. Admission proves that this cannot be a token cycle (S), so
the cause is something else, for example a D-1 remainder that no longer fills. Nothing is
published and nothing is retried.

### 4.5 Statuses

| Condition | Status | Detail |
| --- | --- | --- |
| cohort DFS empty, full-universe DFS non-empty | `unsupported` | the port's (D-4) |
| both DFS empty | `no_route` | the port's |
| routes `> max_candidates` | `timeout`, `truncated_by: max_candidates` | the port's. No selector runs, so both counters are 0 and `reference_trajectory` is `unavailable` (§4.7) |
| the quote table would exceed `max_quotes` | `timeout`, `truncated_by: max_quotes` | the port's. No selector runs; `unavailable` |
| no selection and some entry is `incomplete_snapshot` | `incomplete_snapshot` | the port's precedence |
| no selection after rejections | `no_route` | error "no admissible complete selection over N valid quote entries: R combinations rejected by plan-token-DAG admission (B-S10 under admission)"; `cycle_safe.no_admissible_selection: true` |
| no selection without rejections | `no_route` | the port's B-S10 text |
| the replay needs one quote beyond `max_quotes` | `timeout` | the port's text; **nothing published**; `replay.phase: interrupted`, `withheld_by_cs2: true`, not comparable |
| replay not `ok` | `invalid_plan` | the evaluator's error; nothing published |
| replay `ok` | `ok` | published once |

`no_route` here means the pinned SOR search ended by its own rules, as for the reference's
B-S10. It is not a claim that the V2/V3 domain holds no valid plan (K5, K1). Reports tell
the two `no_route` cases apart by `no_admissible_selection`.

### 4.6 Fallback and stop rules

There is **no fallback**. There is no second search, no rerun of the reference, no
single-path fallback and no restart with other parameters. The diagnostics record this as
`fallback: {used: false}`.

The stops are the reference's: B-S5 pruning, the split cap, an empty queue, and the runner's
hard wall/quote limits. No cooperative cap is added. The admission work per solve is
bounded by the scans of the queued nodes. Because a node has a child only if the port
would also have one (the admission test is an extra filter), no node has more children
than the port's rule allows. There is no randomness, and the solve ignores `seed`.

### 4.7 Phase, availability and completion semantics

Missing work is never read as evidence. Every solve that returns a result writes the
following into `search_stats["cycle_safe"]`. All of these are derived from what actually
ran; none is a new shared vocabulary.

| Field | Values | Meaning |
| --- | --- | --- |
| `selector.phase` | `completed`, `not_started` | `completed` only when the adapted `getBestSwapRouteBy` ran to its own termination (B-S5 stop, split cap, empty queue) on the complete quote table. The selector has no cooperative stop, so it never ends part-way |
| `selector.not_started_reason` | `enumeration_status`, `max_candidates`, `max_quotes_table`, null | why no selector ran: `unsupported`/`no_route` from enumeration, the §3.3 threshold, or a quote cut while building the table |
| `selector.selection` | true, false, null | whether a completed selector returned a selection (null when it never ran) |
| `replay.phase` | `not_reached`, `not_needed`, `completed`, `interrupted` | no selector run; no selection; the in-solve evaluator returned; a quote cut inside the replay |
| `replay.interrupted_by`, `replay.status` | `max_quotes` / null; `ok`, `invalid_plan` / null | how the replay ended |
| `reference_trajectory` | `identical`, `diverged`, `unavailable` | `identical` means the selector completed with zero rejections (Theorem P). `diverged` means at least one rejection. `unavailable` means the selector never ran: the zero counters prove nothing then |
| `comparable_completed` | true, false | the selector completed and no replay was interrupted. This is the only cohort the identity gate applies to (§11) |
| `publication` | `{rule, published, reference_publishes_before_replay: true, withheld_by_cs2}` | the CS-2 difference, kept visible |

A **hard wall kill** returns no `SolveResult`, so no phase is written. The runner shows
`unavailable (hard_timeout)`. Such a solve is never part of the identity cohort, in either
arm. Because admission costs CPU and wall timing is nondeterministic, a hard wall can end
a zero-rejection solve of the variant where the reference finished, or the other way round.
Theorem P says nothing about that.

Fixture `A5_phase_semantics` pins these fields on ten solves; §6 has the table:

- the early cuts and the enumeration status are `unavailable`;
- the replay cut has an `identical` selector but is not comparable, and CS-2 withholds its
  candidate;
- four completed cases are comparable: ok, replay `invalid_plan`, no selection, and
  diverged.

## 5. Proofs

**Setting.** The routes come from B-R*. Each route is a sequence of pools with a token
path in which no token repeats (B-R4 `tokensVisited`), starting at `token_in` and ending at
the first pool that involves `token_out` (B-R3).

- **L1 (single routes).** The graph of one route is a directed simple path, so it is
  acyclic. The B-S3 baseline and every B-S4 seed are therefore admissible, and checking
  them would be redundant.
- **L2 (monotonicity).** If `G(R)` has a cycle and `R ⊆ R'`, then `G(R')` has the same
  cycle. Two consequences follow:
  - If a node is admissible, it is enough to test the one new route against the node's
    union (§4.2 step 2), and completion (B-S8) needs no recheck.
  - A cyclic node in the reference has only cyclic descendants. Rejecting it removes no
    admissible completion that descends from it.
- **L3 (plan construction preserves the edge set).** B-F1 reorders the routes. B-F2 and
  D-1 change only amounts. `split_path_plan` emits exactly one step per route hop, with
  `token_in → token_out` along the route's token path. The plan's token graph is therefore
  exactly the union of the selected routes' edges.

  The evaluator compares exact bundle token strings. Lowercasing merges nodes, so any
  directed cycle over exact strings maps to a closed walk, and hence a cycle, over
  lowercase addresses. The lowercase admission test is therefore at least as strict as the
  evaluator's static check. The real cohorts (`sor_cohort_tuning`/`_report`: 98 pools,
  8 tokens) spell each address one way only.
- **Theorem S (safety).** Every selection the variant returns is admissible (§4.2 and L2).
  By L3, its plan passes the evaluator's token-cycle check. The variant can therefore never
  return `invalid_plan` "economic token cycle". This is checked on every fixture and on 60
  seeded random 3-hop bundles.
- **Theorem P (reference trajectory, completed selector runs only).** Run the port's
  selector and the variant's selector on the identical complete quote table (identical
  candidates, grid, entries and order), and let both run to their own termination. The two
  executions differ only in the chooser. Neither selector has a cooperative stop, so each
  run either completes or does not start.

  *If.* Suppose the variant's completed run has `combinations_rejected_cycle = 0`. Then at
  every chooser call, the first pool-disjoint entry was admitted, and that is exactly the
  port's choice. By induction over the loop, the queue contents, `best_quote`/`best_swap`
  and the pruning decisions are identical, and so is the selection. B-F1 and D-1 are
  deterministic functions of the selection. The plan and the in-solve replay are therefore
  identical as long as the replay runs to completion under the same quote budget. The replay
  consumes the same metered quotes in both arms, because admission costs none.

  *Only if.* Let the first rejection happen at step `k`. Up to `k` the runs are identical.
  At `k` the port chose the rejected entry, which is a cyclic combination.

  Hence: **zero rejections ⇔ the port never formed a cyclic combination**. In particular, a
  cyclic reference result implies at least one rejection. The converse does not hold: K4
  has a rejection and an acyclic reference result, and the variant's result differs.

  "Unaffected noncycle cases" are therefore defined exactly as **completed zero-rejection
  cases** (`reference_trajectory: identical`). End-to-end identity (status, plan, score)
  additionally needs `comparable_completed`.

  *Scope.* P says nothing about a solve whose selector never ran. A `max_quotes` or
  `max_candidates` cut before the table completes, and an enumeration status, all leave
  zero counters without any selector work. That is `unavailable`, and the parent's repro
  `Budget(max_quotes=0)` is now pinned as such
  (`test_the_reported_vacuous_repro_is_now_unavailable`).

  After a replay quote cut, the selector trajectory is still `identical`, but the result is
  not comparable end-to-end: the reference has already published its plan and the variant
  has not (CS-2, A4). P is not a wall-clock statement either (§4.7).

  WHI-1556 derives `reference_trajectory` from `selector.phase` and the rejection counter.
  It must never derive it from the counter alone.
- **L4 (hops ≤ 2).** With `max_hops ≤ 2`, every edge is `token_in → m`, `m → token_out` or
  `token_in → token_out`. `token_in` has no incoming edge (B-R4 never revisits it) and
  `token_out` has no outgoing edge (B-R3 stops there). No cycle can exist, so admission
  never rejects. By P, every completed variant run then equals the reference. The 2-hop daily profiles are
  an example: `v1-acceptance.md` §5 records 0 SOR `invalid_plan` there.
  With `max_hops = 3`, a route has at most one intermediate→intermediate edge, so a cycle
  among intermediates of length L needs at least L routes (K1: L = 3).
- **Not a dominance result.**
  - **K5.** The reference's cyclic two-route best (119) keeps B-S5 open, and it reaches the
    valid p@50+u@25+v@25 = 131. The variant's best stays the one-route baseline (100), and
    the rule "stop when `splits ≥ 3` and the best has fewer than `splits − 1` routes" ends
    its search at 100.
  - **K1.** The variant gets 126, while the best admissible value on the grid is 131
    (the greedy first entry per percent and the seeds limit it, as they do in the reference).

## 6. Fixtures (independent expectations)

The core fixtures are hand-built quote tables (V2 routes in B-Q1 order). They run through
the actual port core and through the specification. Each expected selection, value and
counter below was traced by hand through B-S3…B-S8 and B-F1. The exhaustive oracle
(`oracle`, which uses its own Kahn cycle test) supplies the best admissible value and the
best value ignoring the cycle rule. Selections are written in B-F1 order.

| Fixture | Grid / splits | Reference (`uni_sor_port`) | Variant | checks / rejected | Oracle admissible / any |
| --- | --- | --- | --- | --- | --- |
| `K1_three_route_cycle` | 25 / 3 | r1@50, r3@25, r2@25 = 140 (cyclic a→b→c→a; every pair acyclic) | r1@50, z@25, r2@25 = 126 | 16 / 4 | 131 / 140 |
| `K2_tie_continue_scan` | 50 / 2 | r2@50, r1@50 = 115 (cyclic) | r3@50, r1@50 = 115 (r3 ties r2 and follows it in B-Q1 order; equal amounts reverse under B-F1) | 4 / 2 | 115 / 115 |
| `K4_acyclic_reference_but_different_result` | 25 / 3 | h1@50, r1@50 = 113 (acyclic) | r1@50, u@25, r3@25 = 117 | 7 / 1 | 117 / 117 |
| `K5_pruning_regression` | 25 / 3 | p@50, v@25, u@25 = 131 (acyclic) | z@100 = 100 | 6 / 2 | 131 / 131 |
| `K6_no_admissible_selection` | 50 / 2 | q@50, p@50 = 119 (cyclic) | none | 2 / 2 | none / 119 |

Mutations that must fail:

- **Pairwise admission** on K1 returns the cyclic triple, 140.
- **The skip-percent policy** on K2 returns r1@100 = 100.

The adapter fixtures are synthetic CPMM bundles, run through the actual adapter with the
worker's quote meter and the unchanged evaluator:

| Fixture | Purpose | Reference | Variant |
| --- | --- | --- | --- |
| `A1_union_cycle` | union cycle vs overlap vs revisit; same table and ledger at 50/2 and 5/4 | `invalid_plan` (x→y→x) | `ok`, (a,b,c)@100, gross 2,231,431 = hand; published once after the replay; metered quotes = reference's = `quotes_executed` |
| `A3_no_admissible_overflow` | no admissible selection (Moe Classic `uint112` reverts null every 100 % entry and every 50 % entry except the cyclic pair; counted as `reverted`) | `invalid_plan` (x→y→x) | `no_route`, `no_admissible_selection`, 2 / 2, nothing published |
| `A4_replay_budget` | the budget ledger across the replay | `max_quotes 4`: the table fits; the D-1 leg 50,001 needs a 5th quote → `timeout`; **1 plan published before the replay** | `timeout`, **0 published**; with `max_quotes 5`: `ok`, 5 metered, gross = out(50,000) + out(50,001) |

Budgets (`test_budgets_are_the_references_and_admission_never_resets_them`) on A1:

- `max_candidates = routes − 1` makes both solvers return `timeout`, with
  `candidates_truncated = 6`.
- `max_quotes = table − 1` makes both return `timeout` while building the table, with
  exactly `table − 1` quotes metered.
- The variant publishes nothing in either case, and its certificate is unavailable
  (`not_produced`).

Two more adapter fixtures:

- `A6_all_entries_null`: one Moe pool 10 below `uint112`. Every entry reverts, so a
  completed selector returns no selection.
- `A7_replay_invalid`: 101 split 50/50 over a shallow pool and a Moe pool 50 below
  `uint112`. By the hand values, 49 + 33 beats 50. B-F1 puts the Moe pool last, so D-1
  gives it 51, which reverts during the replay. This is a completed zero-rejection selector
  whose replay completes as `invalid_plan`, the same status as the reference's.

**Phase semantics** (`A5_phase_semantics`, `test_phase_availability_and_completion_semantics`,
§4.7). The reference and the variant run under the same budget. "pub" is published plans,
variant / reference.

| Row | Fixture, budget | Status var / ref | Selector | Replay | Trajectory | Comparable | pub | withheld (CS-2) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `quote_cut_zero` | A1, `max_quotes 0` (the parent's repro) | timeout / timeout | not_started (`max_quotes_table`) | not_reached | unavailable | no | 0 / 0 | no |
| `quote_cut_table_minus_one` | A1, table − 1 | timeout / timeout | not_started (`max_quotes_table`) | not_reached | unavailable | no | 0 / 0 | no |
| `candidate_cut` | A1, `max_candidates 5` | timeout / timeout | not_started (`max_candidates`) | not_reached | unavailable | no | 0 / 0 | no |
| `enumeration_no_route` | A1, token_out `w` | no_route / no_route | not_started (`enumeration_status`) | not_reached | unavailable | no | 0 / 0 | no |
| `replay_cut` | A4, `max_quotes 4` | timeout / timeout | completed, 2 checks / 0 rejected | interrupted (`max_quotes`) | identical | **no** | **0 / 1** | **yes** |
| `completed_zero_rejection_ok` | A4, `max_quotes 5` | ok / ok | completed | completed ok | identical | yes | 1 / 1 | no |
| `completed_zero_rejection_replay_invalid` | A7 | invalid_plan / invalid_plan | completed | completed invalid_plan | identical | yes | 0 / 1 | yes |
| `completed_zero_rejection_no_selection` | A6 | no_route / no_route | completed, no selection | not_needed | identical | yes | 0 / 0 | no |
| `completed_rejections_ok` | A1 | ok / invalid_plan | completed, 2 / 2 | completed ok | diverged | yes | 1 / 1 | no |
| `completed_rejections_no_selection` | A3 | no_route / invalid_plan | completed, 2 / 2 | not_needed | diverged | yes | 0 / 1 | no |

On every comparable identical row, the variant equals the reference in status, selection,
allocation, plan, error and score.

## 7. Domain, §3.3 row, options and preset

**Domain (`r021.domain/1`).** The record is identical for `uni_sor_port` and
`uni_sor_cycle_safe`, so the comparison class is `same_domain`
(`test_domain_is_shared_with_the_reference_so_the_comparison_is_same_domain`). The shared
validator accepts it.

```json
{"schema": "r021.domain/1",
 "universe": {"bundle": "<bundle_hash>", "cohort": "sor_compatible", "pools": ["<A-1 order: V3 list ++ V2 list>"]},
 "protocols": ["constant_product", "concentrated"],
 "pool_order": ["<same A-1 order>"],
 "hops": {"max": "<search.max_hops>", "param": "search.max_hops"},
 "splits": {"max": "<search.max_splits>", "param": "search.max_splits", "governs": "allocation"},
 "amount_grid": {"kind": "repository_grid", "percent_step": "<search.percent_step>",
                 "remainder": "sor_d1_last_b_f1_route_all_remaining", "route_order": "sor_a1_b_r1_b_q1"},
 "zero_output_leg": "infeasible", "token_reuse": "simple_path", "pool_reuse": "disjoint",
 "dag_admission": "plan_token_dag", "full_fill": "v1_full_fill"}
```

Using the kind label `repository_grid` does not make this feasible set equal to
`direct_split`'s (R021-C/1 §3.2). The legs here are routes, the remainder goes to the last
route in B-F1 order, and the extra grid parameters make the hash distinct.

**§3.3 row, filled.** The hop bound is `search.max_hops` (route length). The split bound is
`search.max_splits` (routes per plan, `governs: allocation`, grid `search.percent_step`).
The `Budget.max_candidates` unit is `enumerated_routes_threshold`: fewer declared than
enumerated gives `timeout`. There are **no separate caps**. This is the WHI-1547 row
unchanged.

**Options (`algorithm_options.uni_sor_cycle_safe`, WHI-1548 seam).** No option exists.

- The validator accepts exactly the empty mapping and returns `{}`. Every key is refused,
  whether it is a reserved shared key (`max_hops`, `shortlist`, …) or not (`admission`,
  `policy`, …).
- Admission cannot be switched off, and the scan policy cannot be chosen. Both are
  identity-defining, not tuning knobs.
- The factory's `prepare` calls the validator (`validated_options`).
- **Preset `uni_sor_cycle_safe` v1**: `{key: uni_sor_cycle_safe, version: 1, algorithm:
  uni_sor_cycle_safe, options: {}}`. The proposed path is
  `config/strategy_options/uni_sor_cycle_safe.yaml`, or WHI-1548's registered location;
  WHI-1556 pins the file's sha256.
- `settings_sha256` = `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`
  (the canonical hash of `{}`).
- The preset is finite by construction because it has no caps. The finite bounds are the
  profile's shared `search.*` values and budget.

**Stress profiles.** None are needed. Larger `max_hops`/`max_splits` are profile values,
not identity options. They are labeled like any profile change.

## 8. Ties, work units, diagnostics, bound kind

- **Ties.**
  - Within a percent: the B-S2 stable order (B-Q1). Admission is a filter over that order
    and never re-ranks (K2).
  - Among completed selections: strict `>`, so the first one found wins and the baseline
    wins ties.
  - In the final order: V8 B-F1 (K2: equal amounts reverse).
  - All three are the port's rules.
- **Work units** (R021-C/1 §5.2, all already registered). They are listed side by side and
  never divided by each other.

  | Unit | Meaning |
  | --- | --- |
  | `quotes_executed` | the port's `cache.misses`, equal to the worker meter (test) |
  | `quotes_memoized` | per-solve memo hits (not metered) |
  | `internal_evaluations` | 1 when the replay started, else 0 |
  | `admission_checks` | pool-disjoint entries tested |
  | `combinations_rejected_cycle` | entries refused by admission; always ≤ `admission_checks` |

  Admission is CPU-only: it costs no quote, and the table and replay quotes are the
  reference's (A1). No other unit applies. There is no numeric work, so there is no
  `exact_replay_quotes`.
- **Diagnostics** (`search_stats["r021"]`, `r021.diagnostics/1`). The record holds:
  - the domain above and its hash;
  - `max_candidates_unit: enumerated_routes_threshold`;
  - `work`;
  - `fallback: {used: false, source: null, reason: null}`;
  - `scope: {supported: true}`.

  An additional `search_stats["cycle_safe"]` block holds the two counters,
  `no_admissible_selection` and `fallback`. It also holds the §4.7 phase view: `selector`,
  `replay`, `reference_trajectory` (`identical`/`diverged`/`unavailable`),
  `comparable_completed` and `publication` (CS-2, `withheld_by_cs2`).
  These are identity extras; no shared schema changes. All the port's existing `search_stats` keys are kept,
  including the upstream-shaped `selection`, `allocation`, `d1_residual`, `cached_quote`
  and `requote_delta`.
- **Bound kind.** The only kind is `unknown`. On `ok` the record carries a certificate with
  `lower_raw` = the evaluated score, `upper_raw`/`gap_raw` null, `optimality_proven` false
  and `termination: complete`. Here `complete` means only that the SOR search ended by its
  own rules; it does not mean the domain was resolved, and no bound is claimed. This follows
  the shared `P-HEUR-UNKNOWN` example.

  On any other status the certificate is null with reason `not_produced`. A hard kill is
  the runner's `hard_timeout`. The shared validator accepts the `ok`, `no_route` and
  `timeout` records, and it catches a wrong settings hash (`C_IDENTITY`).

## 9. Evidence

### 9.1 Original goldens (authoritative for `uni_sor_port` only)

All 35 goldens were run through the specification on their frozen inputs, including the
gas scores and `min_splits` variants. The reference core still reproduces every golden
selection. The selector completes on every golden, admission rejects nothing, and every
golden selection is acyclic. By Theorem P, the variant's selection therefore equals each
golden result exactly. These are selector-level runs on complete frozen tables, so no
budget cut applies.

The goldens are not an expectation of the variant. They are a zero-rejection check. A future
golden that exercises admission would fail this check loudly
(`test_original_goldens_stay_the_reference_and_the_variant_follows_theorem_p`).

### 9.2 Property checks with the real evaluator

These run on seeded random CPMM bundles: 5 tokens, 6–11 pools, grid 20, 3 splits.

- **3 hops, 60 bundles.**
  - The variant never produces a token cycle (Theorem S).
  - Every comparable identical case (the selector completed, zero rejections, the replay
    not interrupted) equals the reference in status, selection, allocation, plan, error and
    score (Theorem P).
  - Solves with no route are `unavailable`, with no selector work. They are counted apart
    and are never taken as identity evidence.
  - Every reference token-cycle case has at least one rejection.
  - The table sizes match, and `quotes_executed` equals the worker meter.
  - The run is required to contain identical, diverged, unavailable and reference-cycle
    cases.
- **2 hops, 40 bundles.** Zero rejections (L4). At least 20 of the bundles are comparable
  completed identities, and those are identical end to end.
- **`tests/fixtures/corpus/bundle`** (a 19-pool real-state excerpt with 96 cases, run at
  3/4/5). All 96 cases have a completed selector, a completed `ok` replay and zero
  rejections. All 96 are therefore comparable identities, and each equals the reference
  end to end.

### 9.3 What the fixtures show

The union cycle (A1, K1, K2, K6) is distinct from pool overlap and from revisits (A1). A
valid alternative wins after a rejection (A1, K2, K4). There is a no-valid case (A3, K6).
Ties are covered (K2), and budgets are covered (A1, A4). Early cuts are `unavailable`,
and the replay cut is identical at selector level but not comparable, with CS-2 withholding
its candidate (A5). Admission can change the result
even when the reference result is acyclic (K4), and it can lose to the reference (K5).

### 9.4 Bounded corpus diagnostic (separate pass, not a measured solve)

`PYTHONPATH=. uv run python tests/routing/test_cycle_safe_sor_contract.py probe <bundle>
<out.json> [case ids…]`. It uses `max_hops 3`, `max_splits 4`, `percent_step 5`,
`max_quotes 300000` (the full profile's search values and quote cap). Each case is one
solve. Inside it, the reference selector runs on the **identical** quote table, and the
reference plan is then replayed by the evaluator. It runs in a process pool (6 workers) on a
shared host, so no timing is recorded or claimed.

| Part | Bundle | Cases | Reference (`uni_sor_port`) | Variant | Rejections |
| --- | --- | ---: | --- | --- | --- |
| tuning (`matched_sor_cycle_safe` exploration) | `sor_cohort_tuning` `b900b866…7ade` | 96 | `ok` 94, `invalid_plan` 2 (token cycle: `emp-09bc4e-201eba-low-2`, `-medium-2`) | `ok` 96; no token cycle | 11 cases (12–4,476 each) |
| known report defects (**regression only**) | `sor_cohort_report` `8213b7b0…e640` | 12 of 302 | `invalid_plan` token cycle on all 12 (the WHI-1447 list, reproduced) | `ok` 12; no token cycle | all 12 (446–12,879 each) |

On the tuning split:

- **85 cases have zero rejections.** All of them equal the reference in selection and
  status (Theorem P observed on real V2/V3 state).
- **11 cases have rejections.**
  - 2 are the known defects, which become `ok`. There is no reference value to compare
    against.
  - 8 still end in the reference's selection: the rejected branches never won.
  - 1 differs, `emp-201eba-cda86a-low-2`. The variant evaluates to 229,268,011,368,923
    against the reference's 229,258,259,341,007 (+0.43 bps). This is a real K4 instance.
  - 0 cases are lower. That is an observation on 96 cases, not a guarantee (K5).

The work figures:

- `admission_checks` is at most 5,868 per case on tuning and 14,271 on the defect cases.
- `quotes_executed` is at most 84,344. That is the reference's table plus the replay.
  Admission adds no quote.

The report-split cases were run only to confirm that the defect is repaired. No quality
figure from them is reported or used.

**Historical view and enriched view.** Two records are kept, and neither overwrites the
other.

- **`corpus_probe` (historical).** The original pass, produced at `c9c6ca4`, is kept
  byte-for-byte in [`fixtures/cycle-safe-sor.json`](fixtures/cycle-safe-sor.json), and its
  raw outputs are pinned by sha256. That view predates §4.7: its "zero rejections ⇒
  identical" claims rest on `variant_status: ok`. In this adapter, `ok` is emitted only
  after a completed selector and a completed `ok` replay, so those claims hold for
  completed trajectories only. The test checks this (every historical case is `ok`).
- **`corpus_probe_enriched` (current view).** The same cases were re-run with the current
  code, adding the §4.7 phase fields. The re-run used the same settings and the phase-aware `spec_solve`; the selector, table,
candidate and order behaviour are unchanged. On all 108 cases, every historical numeric and
selection field is identical, and the test checks this per case via
`historical_fields_sha256`. The added fields show:

- the selector completed on every case, and so did the reference selector on the same
  table;
- every replay completed, so every case is `comparable_completed`;
- `reference_trajectory` is `identical` on 85 tuning cases and `diverged` on 23 (11 tuning
  cases and all 12 report defects).

The historical zero-rejection identities are therefore verified identities over completed
trajectories. None of them is a no-data case. Raw outputs: `enriched-tuning.json` sha256
`7f70dfe8…a7af` and `enriched-report-defects.json` sha256 `bd159c73…da8a`. Both are stored
beside the historical raw files in the artifacts directory of this issue's final SHA.

## 10. Source-deviation record

`uni_sor_cycle_safe` is **not** upstream parity. It inherits every `uni_sor_port`
adaptation and deviation unchanged (A-1…A-8, D-1…D-4) and adds:

| ID | Deviation (relative to `uni_sor_port` / upstream) | Where | Evidence |
| --- | --- | --- | --- |
| CS-1 | The B-S7/B-S9 chooser also requires the plan token union to be acyclic, and it continues the scan past a rejected entry (§4.2). This changes upstream's combination behaviour whenever a rejection occurs | `getBestSwapRouteBy` (`best-swap-route.ts:174-881`; port l. 593) | K1–K6, A1, A3; Theorems S/P |
| CS-2 | The candidate is published only after the in-solve replay is `ok`; the reference publishes before the replay | adapter (port l. 1024–1026) | A4 |
| CS-3 | `no_route` after rejections carries its own error text and `no_admissible_selection` | adapter status | A3, K6 |

Upstream goldens remain the expected behaviour of `uni_sor_port` alone. The variant's
expected behaviour is this memo's fixtures together with Theorems S and P. Its provenance
must list the upstream pin, `uni-sor-port-contract.md`, the inherited A-/D- ids and CS-1…CS-3.
It must also carry the GPL-3.0-only notice of the translated core it reuses, citing
`uni_sor_port.py` and the upstream file:lines, and state that the file was modified.

## 11. Bounded comparison recipe (WHI-1562, registered exploration `matched_sor_cycle_safe`)

- **Arms.** `uni_sor_port` against `uni_sor_cycle_safe` (preset v1). They share the same
  profile, candidates (the matched `sor_cohort_*` V2/V3 bundles), grid and search values
  (`max_hops 3`, `max_splits 4`, `percent_step 5`), budget, objective and deterministic
  order. The comparison class is `same_domain`.
- **No knobs are compared.** There is none: no shortlist, no adaptive percentages, no LB
  expansion, no `max_splits` scan inside this recipe.
- **Tuning split** (`sor_cohort_tuning`, 96 cases): exploration and reporting of the §9.4
  kind. There is nothing to tune. The preset is fixed at v1 by construction.
- **Report split** (`sor_cohort_report`, 302 cases, `holdout_exposure: previously_exposed`):
  one frozen comparison. The 12 known `invalid_plan` cases are **defect regressions**:
  - the reference stays `invalid_plan` (token cycle);
  - the variant must never be a token cycle;
  - these cases are listed apart from the quality comparison and are never tuning data.
- **What to report.**
  - Unconditional statuses per arm.
  - The **comparable identical cohort**: the variant's selector completed with zero
    rejections, no replay was interrupted, and the reference arm also completed without a
    budget or hard-wall cut. Identity with the reference is a gate there (Theorem P), not a
    result.
  - Everything else, reported apart and never as identity evidence:
    - `unavailable` solves (an enumeration status, a candidate or quote cut before the
      table completes), given by status per arm;
    - replay-interrupted solves;
    - hard-wall kills in either arm.
  - CS-2 publication differences (`withheld_by_cs2`, `last_valid_candidate` present for the
    reference but absent for the variant), attributed to CS-2.
  - Timing differences on zero-rejection solves (admission CPU) as timing, never as search
    quality.
  - The rejection cohort, with paired quality over common `ok` cases and counts of higher /
    equal / lower. Losses are expected to be possible (K5).
  - `admission_checks` and `combinations_rejected_cycle` distributions next to
    `quotes_executed`.
  - Timing per R021-C/1 §5.1 only, with host-load rule.
- **Gates for `keep_experimental`.** Theorem S holds on every case (no token cycle). There
  is exact identity on every comparable identical case (defined above). No `unavailable`
  or interrupted solve is ever counted as passing or failing this gate. The ledger equals the
  worker meter. The publication rule holds, and `withheld_by_cs2` is reported. There is no loss tolerance and no required win (R021-C/1 §8).

## 12. Outcome: `go`

The contract is implementable over the identity's whole declared domain: constant-product
and concentrated pools (V2/V3), the objectives of `uni_sor_port`, and the same candidates,
grid and budget. Nothing is missing:

- the algorithm, domain, row, option schema, preset, ties, stops, failures, statuses, work
  units and diagnostics are fixed (§3–§8);
- safety and exact reference-trajectory identity are proven and executable (§5, §9);
- the real defect is repaired on every known case without touching `uni_sor_port` (§9.4).

The outcome is `go` for an experimental comparator only. It carries no quality claim: K5
shows the variant can lose to a valid reference result, and K1 shows it is not optimal.
It is not `narrow_go`, because no part of the declared domain had to be excluded.

## 13. Amendment text for WHI-1556 (for the parent to apply)

> **Contract of record:** `docs/references/research-021/cycle-safe-sor.md` (R021-P09,
> outcome `go`). Executable specification and checks:
> `tests/routing/test_cycle_safe_sor_contract.py`. WHI-1556 must port or re-run the fixture
> checks against its module: K1, K2, K4, K5, K6, A1, A3, A4, A6, A7, the ten
> `A5_phase_semantics` rows, the 35-golden zero-rejection sweep, and the seeded 3-hop
> Theorem P/S and 2-hop L4 property checks.
>
> **Algorithm.** `routing/algorithms/uni_sor_cycle_safe.py`, registered as
> `uni_sor_cycle_safe` (group `custom`, R021-C/1 order 4, appended after `metis_inspired`
> and the earlier-order new identities under `--strategies all`).
>
> - `prepare` = `uni_sor_port.prepare` plus `validated_options`.
> - `solve` = `uni_sor_port`'s pipeline (imported functions) with one change: the
>   B-S7/B-S9 chooser scans the B-S2-sorted percent group. A pool conflict is skipped
>   first and uncounted. Each pool-disjoint entry costs one `admission_checks` and is chosen
>   iff the union of the node's route token edges (lowercase SOR token paths) with the
>   entry's is acyclic. Otherwise it costs one `combinations_rejected_cycle` and the scan
>   continues.
> - Baseline, seeds, layers, pruning, split cap, strict improvement, V8 final order, D-1
>   and D-3 are unchanged. There is no fallback and no retry.
> - The plan is published once, only after the in-solve replay is `ok`.
> - Statuses are memo §4.5: `no_route` with `no_admissible_selection: true` after
>   rejections.
> - **Phase semantics** are memo §4.7. `reference_trajectory` is `identical` only for a
>   completed selector with zero rejections, `diverged` after any rejection, and
>   `unavailable` when no selector ran (an enumeration status, a `max_candidates` cut, or a
>   quote cut before the table completes). It must never be derived from the rejection
>   counter alone. `comparable_completed` additionally excludes interrupted replays.
>   Selector, table, candidate and order behaviour stay exactly as specified above; the
>   phase view is reporting only.
>
> **Domain / row / options.** The `r021.domain/1` record is memo §7, identical to
> `uni_sor_port`'s (`same_domain`).
>
> - The §3.3 row is unchanged: `search.max_hops` / `search.max_splits` (allocation) /
>   `search.percent_step`, with unit `enumerated_routes_threshold` and no separate caps.
> - `options_validator` accepts only `{}`. The preset `uni_sor_cycle_safe` v1 has
>   `options: {}`, and its settings hash is `44136fa3…af8a`.
>
> **Diagnostics.** `search_stats["r021"]` holds work `quotes_executed`, `quotes_memoized`,
> `internal_evaluations`, `admission_checks` and `combinations_rejected_cycle`. The
> certificate is `unknown` on `ok` and null (`not_produced`) otherwise.
> `search_stats["cycle_safe"]` holds both counters, `no_admissible_selection`,
> `fallback.used = false` and the §4.7 phase view: `selector {phase, not_started_reason,
> selection}`, `replay {phase, interrupted_by, status}`, `reference_trajectory`,
> `comparable_completed` and `publication {rule, published,
> reference_publishes_before_replay, withheld_by_cs2}`. Keep every `uni_sor_port`
> `search_stats` key.
>
> **Provenance and notices.** Keep the GPL-3.0-only header naming the pin and
> `uni_sor_port.py`, and state that the file was modified. Record A-1…A-8, D-1…D-4 and
> CS-1…CS-3.
>
> **Acceptance additions.**
>
> - Forbidden union-cycle fixtures (A1, K1, K2, K6) never produce a cyclic plan. A1
>   returns `ok` at 2,231,431. A3 and K6 give `no_route` with `no_admissible_selection`.
> - All 35 original goldens pass unchanged in `test_uni_sor_parity.py`, and each golden
>   has zero rejections in the variant, with a selection equal to the golden.
> - On every **comparable completed** zero-rejection case (both arms completed, no replay
>   interrupted, no hard-wall cut) the variant equals `uni_sor_port`'s status, selection,
>   allocation, plan, error and score.
> - Early quote cuts, candidate cuts and enumeration statuses report
>   `reference_trajectory: unavailable` and are never counted as identity evidence.
> - The replay-budget case publishes nothing and reports `withheld_by_cs2: true`. The
>   reference's pre-replay `last_valid_candidate` difference is shown and attributed to
>   CS-2.
> - The work ledger equals the worker meter.
> - Both CLI modes (`main.py run`, `main.py quote --details`) run the identity once per
>   solve.
>
> **Comparison.** Memo §11 (`matched_sor_cycle_safe`). The 12 report-split and 2
> tuning-split known `invalid_plan` cases are defect regressions only.
>
> **Not claimed.** No dominance over `uni_sor_port` (K5), no optimality (K1), no timing
> effect, no end-to-end parity under wall-clock cuts (admission costs CPU; CS-2 publishes
> later), no upstream parity for the variant.

## 14. Limitations

- The fixtures are synthetic V2 (CPMM) tables and bundles. Admission does not depend on the
  pool protocol, but the concentrated-pool path is exercised only through the real-corpus
  diagnostic (§9.4) and the unchanged port.
- The diagnostic is one in-process pass, on the tuning split and on the known
  report-split defect cases only. It makes no quality or timing claim about the report
  split. WHI-1562 owns the frozen comparison.
- The executable specification swaps the port's selector with `unittest.mock.patch`
  inside a test module. WHI-1556 must implement a real module, reusing the port's functions
  by import. Research added no seam to runtime code.
- `termination: complete` on an `unknown` certificate follows the shared `P-HEUR-UNKNOWN`
  example. It means the search ended by its own rules, not that the domain was resolved.
- Theorem P and the identity gate cover completed, comparable selector runs only. Timeouts,
  hard-wall kills and replay cuts are reported apart, and their end-to-end results can
  legitimately differ from the reference's (admission CPU, CS-2).
- The corpus diagnostic has two views: the historical view kept verbatim, and an enriched
  re-run. Neither is a measured solve.
- No shared file changed. `DEFERRED_ISSUES.md` keeps the `uni_sor_port` entry; closing or
  rewording it after WHI-1556 lands is the parent's decision.

## 15. Reproduction

```bash
uv run pytest tests/routing/test_cycle_safe_sor_contract.py -q
uv run pytest tests/routing/test_uni_sor_parity.py tests/routing/test_uni_sor_port.py tests/docs/test_research_021_contract.py -q
PYTHONPATH=. uv run python tests/routing/test_cycle_safe_sor_contract.py probe \
  <primary clone>/data/corpus/mantle-5src-101082044/sor_cohort_tuning enriched-tuning.json
PYTHONPATH=. uv run python tests/routing/test_cycle_safe_sor_contract.py probe \
  <primary clone>/data/corpus/mantle-5src-101082044/sor_cohort_report enriched-report-defects.json \
  emp-09bc4e-201eba-low-1 emp-09bc4e-201eba-low-3 emp-09bc4e-201eba-medium-1 \
  emp-09bc4e-201eba-medium-3 emp-201eba-cda86a-low-1 emp-201eba-cda86a-low-3 \
  emp-78c1b0-09bc4e-low-3 emp-78c1b0-deadde-low-1 emp-78c1b0-deadde-low-3 \
  emp-78c1b0-deadde-low-4 bnd-09bc4e-201eba-liq_at bnd-09bc4e-201eba-liq_above
```
