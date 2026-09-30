# Liquidity Book scope: protocol expansion, model certification, cost calibration (R021-P15, WHI-1560)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` ([`contract.md`](contract.md)); this memo changes no shared schema, vocabulary, example, check or identity row |
| Publication key | `R021-P15`, WHI-1560, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository base | `origin/dev` `1ce50763b84b7daf4eec844665848b5b1c27fb6a` (B `81559ab` + WHI-1547/1549/1557/1551 research). `pools/`, `routing/`, `snapshot/` unchanged since B |
| Outcome | **`narrow_go`** for scope A only: a future, separately named, exact-quote identity `uni_sor_lb` (§5), outside the 0.2.1 roster. Scope B1 (fixed-fee CPMM/CFMM model transfer to LB) **`no_go`**. Scope B2 (an LB-native certified relaxation) **`blocked`**. Scope C (LB economic cost calibration) **`blocked`** |
| Records | [`fixtures/lb-scope.json`](fixtures/lb-scope.json): counterexamples K1–K5, fork facts, L1 random-suite parameters, the `uni_sor_lb` spec records, the tuning census |
| Executable check | `uv run pytest tests/routing/test_lb_scope_contract.py -q` |
| Downstream | WHI-1562 (LB disposition in the roster report); amendment text in §11. No new issue is created by this memo |

This memo implements no solver, adds no LB capability to any registered or 0.2.1 identity and
changes no pool source. It separates three concerns the historical handoff mixed together:

- **A. Protocol expansion.** Can exact LB quotes be added to an SOR-derived selection without
  touching `uni_sor_port`'s parity boundary? Yes, as a new identity, because SOR's
  combination is pool-disjoint. That needs exact quotes only, and no curve property.
- **B. Model certification.** Does any LB model support a *bound* or a *continuous* method
  (the CPMM tangent of WHI-1551, the CFMM dual of WHI-1557)? The fixed-fee transfer is refuted
  (B1). An LB-native relaxation has a proof sketch and checks, but no admitted consumer or
  contract ceiling (B2).
- **C. Economic cost calibration.** Can LB plans be ranked on estimated net output? Only for
  the cost-model v1 cohorts that are already supported. Split LB shapes are low-confidence.

Evidence classes stay apart (R021-C/1 §1):

- *Fork evidence* is what the deployed LBPair emitted on a Mantle fork (committed WHI-1433
  fixtures).
- The *hand model* re-derives the fee and bin rules from the pinned Solidity in the test. It
  shares no code with `pools/liquidity_book.py`.
- *Simulator replays* use the admitted `pools.liquidity_book` and the unchanged evaluator.
- The *executable specification* is `lb_sor_select`. It is never registered or timed.
- The *tuning census* is enumeration and per-pool checks on `bundle_tuning` only. There is no
  solve, no timing and no report-split read.

No number here is a measured solve, a benchmark result or a gain.

## 1. Sources

| Source | Pin | Used for |
| --- | --- | --- |
| `pools/liquidity_book.py`, `snapshot/models.py` (`LiquidityBookPoolState`, `LB*FeeParameters`) | at base, read-only | the admitted LB v2.2 transition (WHI-1433/1434) |
| `lfj-gg/joe-v2` v2.2.0 `1297c3822f0605e643155c35948959c0a0d05e17` (MIT): `src/LBPair.sol` (sha256 `a805b243…f345`), `src/libraries/PairParameterHelper.sol` (`9b54c786…5e1d`), `FeeHelper.sol` (`795d3568…4d4f`), `BinHelper.sol` (`a2dfb744…5ecd`), `Constants.sol` (`ac5a710d…f3ba`) | fetched from `raw.githubusercontent.com` at the commit, 2026-09-30; not committed | fee, reference and bin rules for the hand model; static-parameter validity (`_setStaticFeeParameters` L962–996, `setStaticFeeParameters` L319–343); swap loop (`swap` L493–579) |
| [L] LFJ fee documentation `developers.lfj.gg/concepts/fees` | fetched 2026-09-30, HTML sha256 `a683e878…e3477` | agrees with the pinned code (per-bin variable fee, volatility reference window). The pinned code is authoritative; the page is not a source of numbers |
| Fork evidence `tests/fixtures/liquidity_book/*.jsonl.gz` | at base (WHI-1433, block 101057678) | 127 executed swaps with per-bin `Swap` events (volatility accumulator, fees, amounts) |
| `tests/fixtures/moe_lb/bundle` (`b62b04aa…0e4e`), `tests/fixtures/routing/mantle_mixed` (`e03e3c9b…8cff`) | at base | LB-only admission bundle; real mixed V3/LB/Classic states |
| `routing/algorithms/uni_sor_port.py`, [`uni-sor-port-contract.md`](../uni-sor-port-contract.md) | at base, read-only | the pinned SOR routing core (A-1…A-7, D-1…D-4, B-* behaviors) |
| [`cost-model.md`](../cost-model.md), `config/costs/mantle-101082044-cost-v1.json` | at base | LB cost cohorts and applicability |
| Corpus `bundle_tuning` `ee7afa7e…279b` (primary clone's gitignored `data/corpus/mantle-5src-101082044/`) | 96 cases, 143 pools (45 LB) | the §6 census only |
| External report [`sources/research-report.md`](sources/research-report.md) (sha256 `0588e83a…ba1d9`) | archived | its §N2 note ("LB 动态费用…不是直接套用 CPMM 切线的理由"), §4 row "LB 覆盖和搜索质量分拆", §5.1 item 6 and D1. External claims only; its `verify_research.py`/`results.json` are absent and were not run |
| [`handoff`](../../handoff/algorithm-exploration-handoff.md) §6.6, [`sources.md`](sources.md) D1, [`v1-acceptance.md`](../v1-acceptance.md) §5.3 | at base | the +31.31 bps p95 coverage gap, used as motivation only |

## 2. The admitted LB transition (what "exact" means)

The migrated `LBPair.swap` (`pools/liquidity_book.py`, fork-verified by WHI-1433/1434) does
the following for one exact-input swap at the frozen `block_timestamp`:

1. **`updateReferences(t)`**, once, before any bin. With `dt = t − timeOfLastUpdate`:
   - if `dt ≥ filterPeriod`, `idReference ← activeId` and `volRef ← volAcc·reductionFactor/10⁴`
     (when `dt < decayPeriod`) or `0` (when `dt ≥ decayPeriod`);
   - otherwise both are kept.
   The time is always stamped.
2. **Per non-empty bin** `id` along the direction (X→Y walks down, Y→X walks up):
   - `volAcc ← min(volRef + |id − idReference|·10⁴, maxVolAcc)`;
   - `fee(id) = baseFactor·binStep·10¹⁰ + ⌈(volAcc·binStep)²·variableFeeControl/100⌉`
     (1e18 = 100 %);
   - at price `p(id) = (1 + binStep/10⁴)^(id−2²³)` (128.128, `pow`), the bin is either drained
     (input `⌈out·p⌉` plus the fee on top, rounded up) or partially filled (fee included,
     rounded up; output rounded down).
3. **After the swap**:
   - `activeId` and `volAcc` persist in the new state;
   - `volRef` and `idReference` persist as set in step 1;
   - the protocol share leaves the credited input without changing the trader's output.

Consequences used below:

- Within one plan every swap runs at the same `t`. The first swap on a pool may reset the
  references. A later swap on the same pool has `dt = 0`: it keeps them when
  `filterPeriod > 0` and resets them again when `filterPeriod = 0`.
- The fee of a bin is a function of `(id, volRef, idReference)`. It is not a constant of the
  pool.
- On the plan-token DAG a pool is used in one direction only: two directions would be a token
  cycle, which `routing/evaluator.py` rejects.

## 3. The three scopes are separate

| Scope | Question | Needs | Outcome |
| --- | --- | --- | --- |
| **A. Protocol expansion (exact LB quote entries)** | add LB pools to an SOR-derived candidate set, quote table and selection | exact quotes on the snapshot state, physical-pool disjointness, complete-plan replay; **no** curve assumption | `narrow_go` — future identity `uni_sor_lb` (§5); not in 0.2.1 |
| **B1. Fixed-fee model transfer** | apply WHI-1551's CPMM tangent / WHI-1557's CPMM market oracle (or any fixed-fee concave/convex CFMM premise) to LB | a fixed fee, a state-only trading function, concavity | `no_go` — counterexamples K1, K3, K4, K5 and fork facts (§4) |
| **B2. LB-native certified relaxation** | a rational piecewise-linear upper function per LB swap, usable for certified bounds | lemma L1 (§4.6), concave envelopes, complete bin range, a consumer and a contract ceiling | `blocked` (§7) |
| **C. Economic cost calibration** | net-output ranking of LB plans | cost cohorts for LB shapes | `blocked` beyond cost-model v1's supported cohorts (§8) |

The scopes stay independent:

- A GO for A grants nothing under B or C.
- Exact quoting is not a model proof.
- The ability to route LB exactly (heuristics already do) is not a certificate.
- A gross-output coverage gain is not a net-output claim.

## 4. Mathematical assumptions: evidence and counterexamples

All outputs below are raw integer units. Every counterexample uses the admitted simulator's
transition and the independent hand model. The two agree to the unit on every quoted value.
Their static parameters pass the pinned `setStaticFeeParameters` acceptance (filter ≤ decay ≤
4095, reduction ≤ 10⁴, share ≤ 2500, maxVolAcc ≤ 2²⁰−1, total fee at maxVolAcc ≤ 10 %,
not all zero). "Protocol-valid" means the pinned contract accepts the parameters. It does not
mean they were observed in the corpus.

### 4.1 Fork facts (deployed contract, no Python)

| Fact | Value |
| --- | --- |
| executed swaps with per-bin events | 127 |
| swaps whose bins carry more than one volatility-accumulator level (fee not fixed within one swap) | 48 |
| swaps whose accumulator **falls** from bin to bin (after an opposite swap in the same second) | 7 |
| consecutive drained-bin pairs whose realized rate `out/(in + protocol fee)` **rises** | 0 of 3,399 |
| `real_wmnt_usdt_15` `repeat_x_to_y`: first-bin accumulator of step 0 / step 1 (same input twice, one second) | 0 / 160,000 (= step 0's last bin) |

The hand schedule, meaning the references and the per-bin accumulator from the pinned rules,
reproduces the deployed contract's accumulator on all 2,470 bins of every first swap from a
pristine fork state.

### 4.2 K1 — an exact LB quote is not a CPMM

The pool has one bin at price 1, X reserve 10¹⁸ and a fixed fee of 5·10¹³ (0.005 %). The exact
output is the linear `a − ⌈a·f/10¹⁸⌉`: 99,995,000,000,000,000 for `a = 10¹⁷`. The fixed-fee
CPMM formula on the same reserves (10¹⁸, 10¹⁸) and fee gives 90,904,958,658,902,995. The LB
curve is piecewise linear with a kink at each bin edge. It is not the CPMM hyperbola for any
reserve choice, so a CPMM derivative or closed-form oracle is not an LB model.

### 4.3 K3 — reachable nonconcavity breaks every local tangent bound

The state is ordinary and reachable. The static parameters are protocol-valid (binStep 1,
baseFactor 5000, variableFeeControl 10⁷, maxVolAcc 10⁵, filter 30, decay 600). The book holds
10¹⁸ Y in each of the bins 2²³ … 2²³+10, with the active bin at 2²³+10. A prior X→Y swap of
10.6·10¹⁸ in the same second walks the price down 10 bins. The admitted transition then leaves
`activeId = 2²³`, `idReference = 2²³+10`, `volRef = 0`, `volAcc = 10⁵` and time `T`, which is
the hand prediction.

A Y→X swap now moves back toward `idReference`:

- the accumulator falls from 10⁵ at bin 2²³ to 9·10⁴ at bin 2²³+1;
- the fee falls from 1.05·10¹⁵ to 8.6·10¹⁴;
- the 1.9 bp fee drop exceeds the 1 bp price step;
- so the marginal rate **rises** across the bin edge.

At the points z = 10¹⁷, m = 4·10¹⁷ and u = 1.6·10¹⁸ the outputs are 99,895,000,000,000,000,
399,580,000,000,000,000 and 1,598,409,845,525,776,571. The secant from m to u exceeds the
secant from z to m. The in-bin tangent at z, `q(z) + (1 − f₀)(u − z) = 1,598,320,000,000,000,000`,
lies **8.98·10¹³ below** `q(u)`, which is far beyond rounding. The exact output is not
concave, and the hypograph of the swap is not convex. So:

- a tangent/secant upper bound in the style of WHI-1551's Rule T is invalid on LB without a
  concavity proof;
- the CFMM routing premise of a convex trading set, and with it a dual certificate, fails for
  this state.

### 4.4 K5 — the quote depends on the frozen timestamp

The same bins and fee tuple quoted at `T + 700` (`dt ≥ decayPeriod`) reset `idReference` to
the active bin. The fees rise across bins and the secants fall (concave). The outputs are
99,995,000,000,000,000, 399,980,000,000,000,000 and 1,599,810,255,451,734,923, and all three
differ from K3. An LB model is therefore a function of `(bins, fee state, block_timestamp)`,
not of reserves. Any certificate domain would have to bind all three.

### 4.5 K4 — path dependence (sequential versus merged)

The book has two bins at price 1 and 1.0001 with 10¹⁸ X each and a fresh fee state.

- **`filterPeriod = 0`** (protocol-valid, since filter ≤ decay). Swapping `a = 1.5·10¹⁸` and
  then `b = 3·10¹⁷` in the same second yields 1,799,824,268,073,217,677. One swap of `a + b`
  yields 1,799,822,018,298,195,180. Splitting is **2.25·10¹²** better, because the second swap
  resets `idReference` and decays `volRef`. The pool has no fixed trading function of its
  reserves, "one merged step per pool" is not always the best execution, and a static
  per-pool curve cannot represent multi-swap plans.
- **`filterPeriod = 30`**. The two differ by 1 unit (rounding), because the second swap keeps
  the schedule. The real fork pairs (filter 10–30) were sampled at two split sizes per
  direction. In every sample sequential is never above merged, and the gap is at most what 3
  input units buy, plus 2. It is above zero in some samples, so the two are distinct plans.

### 4.6 What does hold

**Lemma L1 (rational relaxation, one swap).** Fix a state and a direction. Let `f_j` and
`p_j` be the protocol's integer fee and 128.128 price of the j-th non-empty bin the swap
visits: the fee is computed with the references after step 1 of §2, so it is the same function
the integer swap uses. Define `R(x)` as the piecewise-linear function with slope
`s_j = (1 − f_j/10¹⁸)·p_j` (X→Y) or `(1 − f_j/10¹⁸)/p_j` (Y→X). Bin j is available for the
drain cost `c_j = out_j / s_j` in exact rationals. Then every successful integer quote
satisfies `q(x) ≤ R(x)`.

*Proof sketch.*

1. Integer drain costs dominate the rational ones: `⌈out·p⌉ + ⌈⌈out·p⌉·f/(1−f)⌉ ≥ out·p/(1−f)`.
2. The integer path therefore reaches every bin with no more remaining input than the rational
   path.
3. In the bin where the integer swap stops, `⌊(y − ⌈y·f⌉)·p⌋ ≤ y·s` (or it is capped at
   `out_j`). `R` is nondecreasing.
4. Protocol share, hooks (admitted no-ops) and revert branches either leave the trader's
   output unchanged or produce no output.

*Checks.*

- A seeded random suite of 300 protocol-valid synthetic states, with all filter/decay
  branches and `idReference` on either side. It has 600 directions, 12 of them with a
  nonconcave `R`. All 3,183 successful quotes satisfy `q ≤ R`.
- K3's points.
- Five grid points per direction on every fork pair.
- 1,303 seeded quotes on the tuning split (§6), with zero violations.

The gap is rounding only. With `k` visited bins and `s_max` the largest slope, the observed
`R − q − 1` never exceeds `(2k + 1)·s_max`: the largest ratio is 0.69 on the random suite and
0.551 on the tuning split. This is an observation, asserted as a check, and is not part of the
lemma.

L1 needs **no** concavity: it holds on K3.

**Concavity domain `D_conc`.** `R` is concave exactly when its slopes do not increase. This is
decidable per (state, direction) with exact rationals over the collected bins, because it
uses the protocol's own integer prices. Two sufficient reasons:

- The first swap resets the references (`dt ≥ filterPeriod`). The fees are then nondecreasing
  away from the active bin, and the prices move monotonically.
- A static screen: the largest adjacent fee drop, `fee(maxVolAcc) − fee(maxVolAcc − 10⁴)`, is
  at most `(1 − f_max)·binStep/10⁴`. This screen is a heuristic under price rounding; the
  exact slope check stays authoritative.

On `bundle_tuning`:

- all 33 liquid pool-directions (17 of 45 LB pools have bins) reset at their first swap and
  have concave `R` (§6);
- all 11 distinct static parameter sets pass the static screen;
- in the fork evidence, no realized rate rises.

This is a snapshot- and parameter-specific fact, not a protocol guarantee (K3).

### 4.7 Assumption register

| Assumption | Status | Evidence |
| --- | --- | --- |
| LB fee is fixed per pool | **counterexample** | fork 48/127 swaps, K3, K5 |
| LB output is a CPMM-like curve | **counterexample** | K1 |
| LB exact output is concave in input | **counterexample** in general; **supported** on `D_conc` (exact per-state check) | K3; §4.6, §6 census |
| a local tangent/secant bounds LB output | **counterexample** | K3 (8.98·10¹³ violation) |
| LB pool = fixed trading function of reserves (CFMM) | **counterexample** | K4 (filter 0), K5 |
| sequential swaps = merged swap on one LB pool | **counterexample** for filter 0; **supported up to rounding** for filter > 0 | K4; §4.5 fork pairs |
| `q ≤ R` (L1) | **supported** (proof sketch + checks); proof review pending | §4.6 |
| concave envelope of `R` bounds a direct split of LB pools | **unknown** (follows from L1 by standard LP relaxation; not reviewed or implemented) | §7 |
| an LB market oracle for a CFMM dual | **unknown** | §7 |
| a quote table entry on the snapshot state equals the replayed leg in an SOR combination | **supported** (pool-disjointness) | §5.3, spec tests |
| cost model covers LB split plans | **counterexample / unsupported** | §8 |

## 5. Scope A: the proposed future identity `uni_sor_lb` (`narrow_go`)

### 5.1 Identity and status in this release

`uni_sor_lb` is an independently named, experimental, `custom`-group variant: the unmodified
`uni_sor_port` routing core applied to the V2/V3 cohort plus the admitted Merchant Moe LB
pools.

- It is **not** one of R021-C/1's five identities. Adding it needs **R021-C/2** (new identity
  row, capability and `protocols_ceiling` including `liquidity_book`), plus a separate
  owner-approved implementation issue in a later release. This memo creates neither.
- `uni_sor_port`, `uni_sor_fast`, the L08 recipes, `uni_sor_cycle_safe` and their parity
  goldens are untouched. LB never enters their cohorts (D-4 stays).
- It is based on `uni_sor_port`, not on `uni_sor_cycle_safe`, so that a matched-cohort
  difference is attributable to the one change. It inherits SOR's token-cycle
  `invalid_plan` combinations and reports them. A cycle-safe LB variant would be a further
  identity after both results exist.

### 5.2 Algorithm (normative; `lb_sor_select` in the test is the executable form)

| ID | Rule |
| --- | --- |
| A-LB1 candidates | V3 and V2 lists exactly as `uni_sor_port.prepare` (A-1, ascending `pool_id`). A third list `LB` = every bundle pool that is a `LiquidityBookPoolState` of an admitted source (`moe_lb_v2_2`), in ascending `pool_id`, **including empty books** (SOR has no TVL filter; A-1 cohort fidelity) |
| A-LB2 route blocks | upstream's `V3`, `V2`, `MIXED` blocks exactly as `compute_family_routes`, then `LB` = `compute_all_routes` over the LB list, then `MIXED_LB` = `compute_all_routes` over `V3 ++ V2 ++ LB`, keeping DFS-order routes with ≥ 1 LB and ≥ 1 non-LB pool. The five blocks partition the union route set (tested) |
| A-LB3 quote list | upstream's `build_route_quotes` for the three upstream blocks, then LB and MIXED_LB entries appended in (route, percent ascending) order, list indices continuing |
| A-LB4 null rules | A-2 unchanged: zero input, failed hop, partial fill (LB is all-or-nothing, so it never occurs), zero intermediate output, pure-V2 zero output. `insufficient_output_amount` (LB dust) is a failed hop |
| A-LB5 selection | `get_best_swap_route` unchanged (B-S1…B-S11, B-F1/B-F2), A-3 zero gas, `forceCrossProtocol`/`forceMixedRoutes` false |
| A-LB6 recovery | D-1 integer fill, `split_path_plan`, `report_candidate`, in-solve `evaluate` through the metered cache (D-3). The status comes from the evaluator |

Physical-pool conflict is B-S9 on `pool_id`. Each LB pair address is one physical pool, and two
bin steps of one token pair are two pools, both usable (tested: `0xf6c9…` and `0x3657…` in
one plan).

### 5.3 Why exact quoting suffices here, and only here

SOR combines pool-disjoint simple routes. Every selected pool is therefore swapped exactly
once, from its snapshot state, at the tabled amount. So the table entry is the replayed leg,
whatever the LB fee state, curvature or timestamp. Any residual difference comes from the D-1
residual on the last route alone. The check `cached quote == evaluated gross` whenever the
residual is 0 passes on every `mantle_mixed` case.

This argument does not extend to shared-pool plans, merged-step models or bounds (§4). Those
remain B.

### 5.4 Domain, statuses, ties, stops, failures, work

- **Domain record** (`r021.domain/1`): `protocols` = `constant_product`, `concentrated`,
  `liquidity_book`; `universe` = full-source bundle and candidate set (cohort ∪ LB);
  `pool_order` = the three ascending lists; `hops` = `search.max_hops`; `splits` =
  `{search.max_splits, governs: allocation}`; `amount_grid` = the SOR-family record that
  WHI-1555 publishes for `uni_sor_cycle_safe` (SOR percent grid, D-1 remainder); `token_reuse`
  `simple_path`; `pool_reuse` `disjoint`; `dag_admission` `plan_token_dag`; `zero_output_leg`
  `infeasible`; `full_fill` `v1_full_fill`.
- **Bound**: `bound_kind` is always `unknown`; there is no certificate.
- **Objectives**: as `uni_sor_port`. Selection is by raw quote (A-3); the final score comes
  from `ObjectiveContext.score`.
- **Statuses**:
  - `ok`;
  - `invalid_plan` from the evaluator (for example a token cycle), counted;
  - `no_route` only after the complete table has no selection;
  - `incomplete_snapshot` when no selection exists and an entry needed uncollected bins;
  - `timeout` when `max_candidates` is below the enumerated routes of all five blocks, or
    `max_quotes` would cut the table or the replay. A partial table is never SOR's input.
  - `unsupported` only when the all-admitted DFS has routes and the extended cohort has none.
    That case is unreachable while every admitted source is in the cohort, and is kept for a
    future unadmitted protocol.
- **Ties**: exactly SOR's stable sort by quote descending, with ties in list order. V2/V3
  entries therefore win exact ties against LB entries, and within the LB blocks DFS order over
  ascending `pool_id` decides.
- **Determinism**: no randomness, no seed use.
- **Work units**: `quotes_executed`, `quotes_memoized`, and `max_candidates_unit`
  `enumerated_routes_threshold`. The per-block `routes_enumerated`, `quote_entries(_null)` and
  `d1_residual` go in `search_stats` as in `uni_sor_port`. No new R021 unit is needed.
- **One ledger**: table quotes and the in-solve replay share the worker meter and the wall
  budget, with no retry.

### 5.5 Options and preset

`algorithm_options.uni_sor_lb` has **no keys**. The validator refuses any key, and every
reserved shared key stays authoritative. Preset v1 is `{}` (sha256-pinned when implemented).

- The bounded comparison uses the profile's shared `search.*` and budget values. The primary
  comparison is `search.max_hops` 2, as `daily_gross.yaml` declares. 3 hops is a separately
  labeled stress profile, because the route table grows 2.86× (§6).
- The comparison must declare `max_candidates` explicitly and report `timeout` honestly; no
  cap is invented here.

### 5.6 Coverage comparisons (future pre-registration)

| Name | Class | Bundles | Claim allowed |
| --- | --- | --- | --- |
| `lb_reduction_parity` | `same_domain` | `sor_cohort_*` (no LB pools) | **defect gate**: plans identical to `uni_sor_port` (tested on `mantle_mixed` with LB removed); any difference is a bug, not a result |
| `lb_expanded_protocol` | `expanded_protocol` | `bundle_*` full source | coverage/capability: unconditional status table (`unsupported` → `ok` moves), paired gross on common successes labeled `⚑ coverage`; never a search-quality claim |
| side-by-side with `path_split`, `incremental_graph`, `metis_inspired` | `incomparable_domain` unless domain hashes are equal | `bundle_*` | unranked side by side |

The historical `path_split` versus `uni_sor_port` coverage gap (p95 +31.31 bps, mean
+1,598.63 bps, `v1-acceptance.md` §5.3, `sources.md` D1) compares two strategies with
different protocol coverage. It is **motivation only**: it is not the attainable gain of
`uni_sor_lb`, nor an upper bound on it. On `bundle_tuning` there are **zero** LB-only cases at
2 and 3 hops (§6). Any tuning-split difference would come from extra LB candidates, not from
status coverage. Report-split exposure is `previously_exposed`.

### 5.7 Tests the implementation must carry

- Reduction parity: with no LB pool, the route blocks, quote table, selection, plan and gross
  equal `uni_sor_port`'s; the SOR golden parity tests stay unchanged and green.
- Block partition (the five blocks equal the union DFS, with no duplicates).
- Cached equals evaluated when the residual is 0, and D-3 delta reporting otherwise.
- Pool-disjoint selections; two LB bin steps of one pair both usable.
- Mixed LB/CL/Classic routes on `mantle_mixed`.
- `unsupported` for `uni_sor_port` becomes `ok` on `moe_lb/bundle` LB-only cases:
  - `weth_usdt_exhaust` fills by splitting, which `direct` cannot;
  - `usdc_usdt_dust` stays `no_route`.
- `incomplete_snapshot` on a truncated `bin_range`.
- A D-1 residual that exhausts an LB book yields `invalid_plan`, never a claimed success.
- Token-cycle combinations yield `invalid_plan` (counted).
- `max_candidates`/`max_quotes` truncation yields `timeout`.
- Option refusal, preset pin, `run` and `quote --details` on a fixture profile, and literal
  replay.
- LB leg replays use the fork-verified simulator (existing `tests/pools/test_liquidity_book.py`,
  `tests/snapshot/test_moe_lb.py`).

## 6. Tuning-split census (bounded, `bundle_tuning` only)

`PYTHONPATH=. uv run python tests/routing/test_lb_scope_contract.py probe <bundle_tuning> probe.json`
does enumeration and per-pool checks only. There are no quotes or solves.

| Item | 2 hops | 3 hops |
| --- | ---: | ---: |
| routes over the V2/V3 cohort (98 pools) | 9,816 | 181,398 |
| routes with every LB pool (45) | 19,986 | 519,624 |
| routes with LB pools that have bins (17) | 13,374 | 286,296 |
| LB-only cases (cohort none, LB some) | 0 of 96 | 0 of 96 |

LB pool-directions with bins: 33. At block 101082044:

- every one resets its references at its first swap and has a concave relaxation `R`;
- all 11 distinct (bin step, static parameter) sets pass the static screen;
- 1,303 seeded quotes show no L1 violation, with the largest rounding-gap ratio 0.551.

Reading: the LB expansion roughly doubles the 2-hop quote table, or triples it at 3 hops, with
empty-book pools included per A-LB1. On the tuning split it changes candidates, not status
coverage. No gain is inferred.

## 7. Scope B: model certification

**B1 `no_go`.** WHI-1551's exact-rational tangent (Rule T) and WHI-1557's closed-form CPMM
market oracle must not be applied to LB pools, and neither may any bound that assumes a fixed
fee, a state-only trading function or concavity:

- K1: the curve is not a CPMM;
- K3: tangent violation of 8.98·10¹³;
- K4: path dependence;
- K5: timestamp dependence;
- fork: 48 swaps with varying fees.

`direct_split_certified` and `cfmm_dual` keep LB outside their R021-C/1 ceilings. Their LB cases
stay `unsupported`.

**B2 `blocked`.** A sound LB-native route exists on paper. Take the L1 relaxation `R_i` of each
pool. For a direct split, the concave upper envelope of each `R_i`, merged by slope (an exact
rational LP on piecewise-linear functions), upper-bounds every allocation of any grid over
those pools. On `D_conc` the envelope is `R_i` itself.

It is blocked on missing inputs, not on a known refutation:

1. an independent review of the L1 proof against every revert and width branch;
2. an R021-C/2 ceiling widening for a named consumer (for example a separately identified
   `direct_split_certified` LB domain), because R021-C/1 forbids widening in place;
3. complete bins: a bound over a truncated `bin_range` is `unknown`, never extrapolated;
4. for a CFMM dual, an LB market oracle and a convexity treatment of non-`D_conc` states. This
   does not exist.

Until then every LB bound is `unknown` and never `certified` or `estimate`.

## 8. Scope C: economic cost calibration

Cost model v1 (`cost-model.md` §3–§4) supports only these LB shapes:

- `c0.l1.p0.chain` (one LB pool: 260 train / 98 holdout);
- `c1.l1.p0.chain` (CL→LB or LB→CL: 40 / 22).

Everything else is `low_confidence`, with no net score:

- two LB pools (11/4);
- every `parallel` split shape (≤ 6 training transactions);
- more LB bins than the training range.

Shared-pool plans are `unsupported`. SOR selection is gas-blind (A-3). So for `uni_sor_lb`:

- selection stays raw-quote;
- `gross_only` is the primary objective;
- under `empirical_cost` a split LB plan is unranked on net output, while its gross is kept.

LB-specific calibration needs new transaction samples. It is **blocked** as out of scope: it
is not a WHI-1560 or 0.2.1 deliverable, and needs its own issue with Dune scope.

## 9. The five 0.2.1 identities gain nothing from this memo

| Identity | LB after this memo | Why |
| --- | --- | --- |
| `metis_history` | routes LB by exact quotes only, as `metis_inspired` does; no LB dominance certificate beyond WHI-1549's own rules | ceiling unchanged; B1/B2 |
| `direct_split_certified` | LB `unsupported` | ceiling `constant_product` only; B1 `no_go`, B2 `blocked` |
| `incremental_graph_repair` | routes LB by exact quotes only, as `incremental_graph` does; `bound_kind` `unknown` | ceiling unchanged |
| `uni_sor_cycle_safe` | no LB (V2/V3 parity boundary) | ceiling V2/V3; LB would be a different identity (`uni_sor_lb` is not it) |
| `cfmm_dual` | LB never a market (WHI-1557 G-L5) | B1 `no_go` |

The test asserts that the contract ceilings are unchanged, that `uni_sor_port`'s capability is
`(V2, V3)`, that LB is excluded by `sor_protocol_of`, and that `uni_sor_lb` is registered
nowhere.

## 10. Outcome

**`narrow_go`**, scoped to A.

- The implementable contract for a future `uni_sor_lb` is §5: exact LB quote entries in a new
  SOR-derived identity, with reduction parity to `uni_sor_port`, pool-disjoint exact replay,
  `gross_only` primary, no options and `bound_kind: unknown`. It requires R021-C/2 and a new
  owner-approved issue, and is not part of 0.2.1.
- B1 is `no_go`. B2 is `blocked` on the four inputs of §7. C is `blocked` (§8).
- No global optimality, no attainable-gain claim and no speedup are asserted.

## 11. Amendment text for WHI-1562 (for the parent to apply)

> **LB disposition (WHI-1560, `docs/references/research-021/lb-scope.md`).**
> - Report the research outcome per scope: A `narrow_go` for a *future* identity `uni_sor_lb`
>   outside R021-C/1 (not implemented, not in the 14-strategy roster, not a
>   `not_implemented` row of the five); B1 `no_go`; B2 `blocked`; C `blocked`.
> - `direct_split_certified`, `cfmm_dual` and `uni_sor_cycle_safe` rows on LB-touching cases
>   stay visible as `unsupported` (protocol ceiling).
> - `metis_history` and `incremental_graph_repair` route LB only through exact quotes, and any
>   LB-involving bound is `unknown`.
> - Any comparison of an LB-capable strategy with an SOR-family strategy on a full-source
>   bundle is `expanded_protocol`, never same-domain search quality.
> - The historical p95 +31.31 bps coverage gap is motivation only, never an attainable gain or
>   upper bound.
> - Add one check: no 0.2.1 identity declares `liquidity_book` beyond R021-C/1's ceilings, and no
>   LB certificate is rendered as `certified`.

**Proposed future issue (not created; parent/owner decides).** Title: "[future] [Routing]
Implement `uni_sor_lb` exact-quote LB SOR variant (R021-C/2)". Scope: R021-C/2 identity row,
§5 algorithm, §5.5 options and preset, §5.6 comparisons, §5.7 tests. Dependencies: R021-C/2
publication; WHI-1555's `amount_grid` record for the SOR family.

**Shared-contract proposals (parent).**

1. `sources.md` §2 says "[L] belongs to WHI-1560". This memo fetched [L] (hash above) and found
   it agrees with the pinned code, and a one-line status update there is suggested.
2. `uni_sor_lb` needs R021-C/2. For 0.2.1 no shared change is needed.

## 12. Limitations

- Fork facts are at block 101057678 on four real pairs plus controlled pairs. The census is one
  tuning split at block 101082044. Concavity on `D_conc` is snapshot-specific.
- L1 is a proof sketch with checks, not a reviewed proof. The random suite is synthetic
  (bins near 2²³, bin steps 1–100).
- The Solidity files were fetched, not rebuilt. Bytecode identity rests on WHI-1426/1433's
  evidence for the same tag.
- The spec is a test-only executable form. Its recorded outputs are regression records; the
  independent checks are reduction parity, pool-disjointness and cached = evaluated.
- No LB-expanded solve was run on any corpus split, no timing was taken, and nothing is claimed
  about quality on the report split.

## 13. Reproduction

```bash
uv run pytest tests/routing/test_lb_scope_contract.py -q
uv run pytest tests/pools/test_liquidity_book.py tests/routing/test_uni_sor_port.py tests/routing/test_uni_sor_parity.py tests/docs/test_research_021_contract.py -q
C=…/router-algorithms-optimizer/data/corpus/mantle-5src-101082044
PYTHONPATH=. uv run python tests/routing/test_lb_scope_contract.py probe "$C/bundle_tuning" probe.json
PYTHONPATH=. uv run python tests/routing/test_lb_scope_contract.py examples probe.json  # rewrites the fixture
```
