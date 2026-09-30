# Certified integer allocation for `direct_split_certified` (R021-P05, WHI-1551)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` ([`contract.md`](contract.md)); this memo narrows the `direct_split_certified` row (§11 obligations) and changes no shared schema, vocabulary, example or check |
| Publication key | `R021-P05`, WHI-1551, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository base | `origin/dev` `b9e7310ea73b2cfa4a3388b80644199e5188e1d9` (B `81559ab` + WHI-1547 contract + WHI-1549 memo; `routing/`, `pools/`, `snapshot/` unchanged since B) |
| Outcome | **`narrow_go`** (§10): implementable and proven on all-CPMM direct pool sets; **zero multi-pool cases in the frozen corpus** |
| Records | [`fixtures/integer-allocation.json`](fixtures/integer-allocation.json): preset, stress profile, 11 regenerated domain/certificate records, grid-vs-raw summary, pinned probe and sweep summaries |
| Executable check | `uv run pytest tests/routing/test_integer_allocation_contract.py -q` |
| Downstream | WHI-1552 (`direct_split_certified` implementation); amendment text in §11 |

This memo defines a gross-output branch and bound over **exactly** `direct_split`'s finite
grid, with an exact-rational upper bound that covers every unresolved allocation, and a
separately identified `raw_integer` stress domain. It implements no strategy. The normative
algorithm is written out here and exists as an executable specification, `certify`, in
`tests/routing/test_integer_allocation_contract.py`. It runs on the actual bundle,
`routing.search.QuoteCache`, `direct_split.allocation_plan` and the unchanged evaluator.
`direct_split`, its profiles, every other identity and all historical records are
unchanged.

**What is claimed, and only this.** Take one request whose admitted direct pools are all
constant product and whose objective is `gross_only`. A certificate with
`bound_kind: certified` then has `lower_raw` ≤ max over the declared domain ≤ `upper_raw`,
and `optimality_proven` means `lower_raw` is that maximum (Theorems T1 and T2, §3). This
holds after completion, after every cooperative stop and under any bound mutation that
stays an upper bound. **What is not claimed:** optimality outside the declared domain
(another pool order, grid, cardinality, hop count, protocol or objective), identical tie
choices with `direct_split`, any speedup or quote saving as a latency result, and any
coverage of CL, LB or net objectives.

Evidence classes (R021-C/1 §1) stay apart. *Repository evidence*: the actual
`direct_split`, CPMM math, evaluator and committed fixture bundles. *External proposal*: the
archived report's §2.3 and N2. *New reconstructions*: this issue's oracle, random suites,
mutation tests, published records and synthetic sweeps. *Bounded probe*: the tuning splits
only, as a scope classification plus preset solves. Nothing here is a measured solve or a
timing claim.

## 1. Sources

| Source | Pin | Used for |
| --- | --- | --- |
| `routing/algorithms/direct_split.py` | at base | grid rule, `leg_amounts`, `allocation_plan`, the exact DP (Proposition P1), statuses (read-only) |
| `pools/constant_product.py`, `pools/quote.py`, `pools/result.py` | at base | fee/floor formula, dust, zero reserve, source/fee refusal, `uint112` revert; the metered seam; `QuoteLimitExceeded` |
| `routing/evaluator.py`, `routing/search.py` (`QuoteCache`) | at base | whole-plan replay; per-solve memo whose misses are the worker's metered quotes |
| `routing/algorithms/base.py` | at base | `SolveStatus`, `Budget` (hard `max_quotes`/wall, cooperative `max_candidates`), `report_candidate` |
| R021-C/1 [`contract.md`](contract.md), [`contract-v1.json`](contract-v1.json), [`fixtures/examples.json`](fixtures/examples.json), `tests/docs/test_research_021_contract.py` | at base, **read-only** | domain/certificate schemas; the validator `check_diagnostics` is loaded by path and reused unchanged |
| External report | [`sources/research-report.md`](sources/research-report.md), SHA-256 `0588e83a…ba1d9` | §2.3 integer marginals and plateau; N2 (two-pool tangent proposal). External claims only. Its `verify_research.py`/`results.json` are absent and were not run; every number here is recomputed NEW |
| WHI-1547 reconstructions | [`fixtures/reconstructions.json`](fixtures/reconstructions.json) R2, R3, R6 | reserves and expected values; R6 pool-order counterexample |
| Committed bundles | `tests/fixtures/synthetic` (`76347ddb…cc4d`), `tests/fixtures/routing/cpmm_graph` (`ae01173f…7d49`), `routing/mantle_mixed` (`e03e3c9b…8cff`), `moe_classic/bundle` (`a81b63a9…8341`) | actual-plan replays, dead pool, dust, unsupported mixed pair, real Moe Classic states |
| Corpus tuning splits | `bundle_tuning` `ee7afa7e…279b`, `sor_cohort_tuning` `b900b866…7ade` (primary clone's gitignored `data/corpus/mantle-5src-101082044/`) | scope probe (§9.1). No report-split case was read |

## 2. Domain

### 2.1 Request, pool set and scope

The candidate pools of a request are **all** of `bundle.pools_for_pair(token_in, token_out)`
in bundle insertion (admitted) order: `p_0 … p_{n−1}`. There is no shortlist, ranking cut or
hidden subset. `universe.pools` lists them all. `pool_order` is that order, and the test
asserts it for every published record. Rules are applied in this order, before any quote:

1. objective ≠ `gross_only` → `unsupported`, scope reason `objective_not_gross_only`;
2. any direct pool not `ConstantProductPoolState` → `unsupported`,
   `non_constant_product_direct_pool`. There is **no CPMM-subset fallback**: dropping the
   CL/LB pools would certify a smaller, different domain beside a `direct_split` row that
   uses them (`N-DOMAIN-TRANSFER`), so the whole case is unsupported;
3. no direct pool → `no_route` (the empty domain is complete);
4. `raw_integer` with `amount_in > raw_max_amount_in` → `unsupported`,
   `raw_integer_amount_above_limit`.

An unsupported row has no plan, no quote, `certificate: null`,
`certificate_unavailable_reason: "not_produced"` and `scope: {supported: false, reason}`.

### 2.2 `repository_grid` (same-domain mode)

Let `A = amount_in`, `N = 100 / search.percent_step`, `K = search.max_splits` and
`M = min(K, n)`. The domain `G` is the set of leg vectors `λ = ((i_1,u_1),…,(i_m,u_m))` with:

- `1 ≤ m ≤ M`, and pools strictly increasing in admitted order, `i_1 < … < i_m`;
- units `u_k ≥ 1` with `Σ u_k = N`;
- every **non-final** leg nonzero after flooring: `⌊A·u_k/N⌋ ≥ 1` for `k < m`.

Amounts are `a_k = ⌊A·u_k/N⌋` for `k < m` and `a_m = A − Σ_{k<m} a_k` (`ALL_REMAINING`).
These are `direct_split.leg_amounts`. The plan is `direct_split.allocation_plan`: a
non-final leg draws its explicit amount from the request fund, the final leg draws
`ALL_REMAINING`, and each output is a separate target fund. The final amount is always at
least 1, since `a_m ≥ A(N−Σ_{k<m}u_k)/N > 0`. A leg vector with a non-final zero floor is
**not** a member. Its amounts equal those of the member with those units moved to the final
leg, so the set of amount vectors is unchanged (`test_zero_floor_nonfinal_legs_…`).
Different `λ` may give identical amounts when `A < N`, which is harmless because the value
depends only on the amounts.

**Feasibility and value.** `λ` is feasible iff every leg's quote is `ok` and consumes its
input. Direct legs use distinct physical pools on original state, so the evaluator's gross
is exactly `Σ f_{i_k}(a_k)`. Here `f(x) = ⌊k·x·R_out / (R_in·10⁴ + k·x)⌋` with
`k = 10⁴ − fee_bps` (`pools/constant_product.py`). A leg is infeasible when it hits any of
the following:
- output flooring to 0 (`insufficient_output_amount`, R2);
- a zero reserve (`insufficient_liquidity`, independent of the input);
- an unmigrated `source_key` or a fee that differs from the source's fixed fee
  (`unsupported`, independent of the input);
- for a sourced pool, `R_in + x ≥ 2¹¹²` (`reverted`, the `MoePair._update` `uint112`
  check).

Because of the last rule, feasibility is not monotone in the input. The oracle
(`oracle_quote`) re-implements these rules by hand. The test checks it against
`pools.quote.quote_exact_in` and then replays **every** oracle allocation of 25 random
instances through `allocation_plan` + `evaluate`: evaluator gross equals the oracle value,
and a plan is `invalid_plan` exactly when the oracle says infeasible
(`test_oracle_grid_is_direct_split_and_every_allocation_replays`).

**Proposition P1 (`direct_split` is exact on `G` for `gross_only` when untruncated).** Its
DP state is (legs used, units used, Σ(A·u mod N)). That residue sum fixes the floored
prefix `Σ⌊A·u/N⌋ = (A·used − residue)/N` exactly, and the future depends only on the state
and the pool index. Keeping the best gross per state is therefore exact, and the best
finalist over `m` is `max_G`. It is checked on the 25 instances (`solve().score == max`)
and on 60 sweep instances (`all_equal_direct_split`). Consequence: the
`same_grid_allocation` exploration (R021-C/1 §6.2) must show **equal values**. The
certified identity adds a proven bound, a bounded interruption with an honest gap, and a
different quote profile. It does not add a better same-domain value.

### 2.3 `raw_integer` (expanded domain, stress only)

`R` is `G` with `N := A`. Then `⌊A·u/A⌋ = u`, so leg amounts are any positive integers
summing to `A` over at most `M` pools, and the non-final floor condition is vacuous. Every
grid allocation's amount vector is a raw allocation with the same pools and cardinality, so
`G ⊆ R` and `max_G ≤ max_R` (asserted on every raw instance). Raw results are
`expanded_domain` comparisons, never a same-domain or speed claim. The feasible set does not
depend on pool order. The order is still recorded because it fixes which leg is
`ALL_REMAINING` and how ties fall. The domain is admitted only when `A ≤ raw_max_amount_in`
(§6), so it covers small inputs only.

### 2.4 Domain record (`r021.domain/1`)

```text
universe  {bundle: <bundle_hash>, cohort: <full_source|sor_compatible|fixture>, pools: sorted(all direct pool ids)}
protocols ["constant_product"]        pool_order  [admitted order]
hops      {max: 1, param: null}      splits      {max: search.max_splits, param: "search.max_splits", governs: "allocation"}
amount_grid  {kind: "repository_grid", percent_step: <step>, remainder: "last_leg_all_remaining"}
          |  {kind: "raw_integer", percent_step: null, remainder: "explicit_integer_legs"}
zero_output_leg "infeasible"  token_reuse "simple_path"  pool_reuse "disjoint"
dag_admission "plan_token_dag"  full_fill "v1_full_fill"
```

`candidate_domain_hash` follows the R021-C/1 §3.1 rule. The R6 records hash to exactly
WHI-1547's `grid38_p1p2` (`e1afda09…989e`), `grid38_p2p1` (`f98ddef6…d84c`) and `raw38`
(`5925efb2…65c7`), so no new domain vocabulary is needed
(`test_published_r6_domains_are_the_shared_contract_domains`).

### 2.5 Counterexamples reproduced (NEW)

- **R2, integer marginals.** On (1000, 1000), fee 30: `f(0..6) = 0, ✗, 1, 2, 3, 4, 5`.
  The first marginal rises from 0 to 1, and a 1-unit leg is infeasible in the repository
  (the report treats `q(1) = 0` as feasible). Raw input 3 over two such pools: the optimum is
  2 (all on one pool), because every split has a dust leg.
- **R3, plateau.** `G(0) = G(1) = G(2) = 70`, `G(3) = 72`, `G(15) = 76 = raw max`. A strict ±1
  search stops at x = 1. The certified search returns 76 with zero gap. The root bound
  already exceeds 70, so a plateau incumbent is never certified.
- **R6, pool order.** Same pools, `percent_step` 5, `max_splits` 2. Order (p1, p2) gives a
  grid optimum of 58 and order (p2, p1) gives 59, with different domain hashes. Raw gives 59.
  The (p1, p2) proof's upper bound 58 is below the raw optimum, so it does not transfer.

## 3. Proof

Notation: `g_i(x) = k_i·R_out,i·x / (R_in,i·10⁴ + k_i·x)` for a **live** pool (both reserves
> 0). This is the CPMM output without the final floor.

- **L1 (floor).** For a live pool and integer `x ≥ 1`, `f_i(x) = ⌊g_i(x)⌋ ≤ g_i(x)`.
  With `f_i(0) = 0 = g_i(0)` (no leg).
- **L2 (concavity).** Write `g(x) = R_out·x/(α+x)` with `α = 10⁴·R_in/k > 0`. Then
  `g'' = −2R_out·α/(α+x)³ < 0`, so for every `t ≥ 0` and `x ≥ 0`,
  `g(x) ≤ g(t) + g'(t)(x − t) = c(t) + s(t)·x` with `s = g'(t)` and
  `c = g(t) − t·g'(t) ≥ 0`.
- **L3 (vertex).** On `{x ≥ 0, Σ x_i = R, x_0 ∈ [lo, hi]}` with `hi < R`, a linear function
  `Σ s_i x_i` is at most `max_{x ∈ {lo, hi}} [s_0·x + (max_{i≥1} s_i)(R − x)]`. Without
  the box it is at most `(max_i s_i)·R`.
- **L4 (superset relaxation).** Every completion of a node assigns non-negative integers,
  summing to the node's remaining raw input `R`, to the node's candidate pools. Dropping
  integrality, the grid, the cardinality limit, pool order and leg feasibility enlarges the
  set. Dead pools can be omitted because any positive leg on them is infeasible. So by
  L1–L3, `Σ c_i + (vertex term)` bounds every feasible completion, for **any** tangent
  points. The objective is an integer, so its floor is still a bound.
- **L5 (one leg left).** If exactly one leg remains, the completions are "all of `R` on one
  pool `i`", bounded by `⌊max_i g_i(R)⌋`. This bound respects the cardinality limit. With a
  box pool `j` followed by one final leg, the pair bound `max_i ⌊T({j, i}, R, box)⌋` also
  respects it.
- **L6 (partition).** The children of a node (§5.1) partition its completion set. For a
  state node on pool `j`: `j` is the final leg, or `j` is a non-final leg with units in
  `[max(1, ⌈N/A⌉), N − used − 1]`, or `j` is unused. An interval node splits
  `[lo, mid] ∪ [mid+1, hi]`. A unit interval fixes the leg, whose infeasible quote closes
  every completion (the leg is in all of them).
- **T1 (coverage invariant).** At every point of the search, every feasible allocation
  falls into one of four cases: (a) its value is ≤ the incumbent `L` (evaluated, or a
  quoted leaf not better than `L`); (b) it is in a region pruned when its bound was ≤ `L`,
  and `L` only increases; (c) it is in a closed infeasible region; or (d) it is in an open
  node whose bound dominates it (L4, L5). A cooperative stop re-pushes the node being
  expanded (§5.5), so (d) still holds after any stop. Hence
  `max_domain ≤ U = max(L, max_{open} ub)`.
- **T2 (zero gap).** `L` is the evaluator score of a returned feasible plan, so `U = L`
  means `L = max_domain`. `complete` (empty frontier) implies `U = L`.
- **T3 (termination).** The tree is finite: at most `n` state levels, each with
  `⌈log₂ N⌉` (grid) or `⌈log₂ A⌉` (raw) interval levels, and each node is expanded once.
  The preset's node and open-node caps bound the work regardless.
- **Unknown.** A bound rule may return *unknown*, for example as a hook for a future
  unsupported region. An unknown node is never pruned and sorts first. If any open node is
  unknown, `U` is unknown: `bound_kind: unknown` with null upper and gap, never 0. A
  complete search that expanded every unknown node is exhaustive there and may certify
  (T1 still applies).

Soundness does not depend on how the tangent points are chosen (L4), on floating point
(none is used) or on the heuristics of §5.2.

## 4. Bounds (normative)

All arithmetic is Python `int` / `fractions.Fraction`. No float appears anywhere.

- `final(j, R) = ⌊g_j(R)⌋`.
- `state(pools ≥ j live, R, legs_left)`: `⌊max_i g_i(R)⌋` if `legs_left = 1`, else
  `⌊T(pools, R)⌋`.
- `interval(j; later live pools; R; a_lo = ⌊A·lo/N⌋, a_hi = ⌊A·hi/N⌋; legs_left_after)`:
  if one leg is left after `j`, `max_i ⌊T({j, i}, R, [a_lo, a_hi])⌋`; else
  `⌊T({j} ∪ later, R, [a_lo, a_hi])⌋`.
- A node's `ub` is its exact prefix output `gp` plus the rule's value.
- `T(P, R, box)` = `Σ_{i∈P} c_i(t_i) + ` the L3 vertex term with `s_i = g_i'(t_i)`. For
  `j`, `t_j` is clamped into the box.

**Rule T (tangent points; efficiency only).** This is the integer water-filling of the
relaxation:
1. Set `α_i = 10⁴R_in/k` and `β_i = isqrt(R_out·10⁴·R_in·2¹²⁸ // k)/2⁶⁴`.
2. Order pools by `α_i/β_i` ascending, ties by index. Add pool `i` while
   `μ = (R + Σα + α_i)/(Σβ + β_i)` still gives `μβ_i > α_i`; the first pool is always
   added.
3. Set `t_i = min(R, max(0, ⌊μβ_i − α_i⌋))` for added pools, else 0.

WHI-1552 reproduces Rule T exactly, so that published `upper_raw` values are
reproducible. Any other rule stays sound but gives different numbers and needs its own
records. With both pruning checks active, `upper_source` is `exact_rational`. It is
`exhaustive` only for a complete search in which no node was pruned by a bound.

## 5. The algorithm (normative; `certify` is the executable form)

### 5.1 Nodes

| Kind | Fixed | Region (completions) | Children |
| --- | --- | --- | --- |
| `state(j, used, fl, gp, legs)` | legs on pools < j | a final leg on some pool ≥ j, optional non-final legs between | `final(j)` if `p_j` live; `interval(j, lo, hi)` if `p_j` live, a live pool > j exists, `|legs| + 1 ≤ M − 1` and `lo = max(1, ⌈N/A⌉) ≤ hi = N − used − 1`; `state(j+1)` if a live pool > j exists |
| `interval(j, lo, hi)` | as the state, plus `p_j` non-final with units in `[lo, hi]` | as named | `lo < hi`: `[lo, mid]`, `[mid+1, hi]` with `mid = ⌊(lo+hi)/2⌋`; `lo = hi`: quote `p_j` at `⌊A·lo/N⌋`, infeasible → closed, else `state(j+1, used+lo, fl+a, gp+out)` |
| `final(j)` | `p_j` is the final leg with `N − used` units and `A − fl` input | one allocation | quote; if `gp + out > L`, evaluate it as a candidate |

Children are created in the table's order, and each is pruned at creation if `ub ≤ L`.

### 5.2 Initialization and incumbents

1. Compute the root `state(0, 0, 0, 0, ())` bound. This is the first bound evaluation, and
   it covers the whole domain.
2. Singles stage: quote every live pool at `A` in admitted order. The best valid one
   (earliest on ties) is the first incumbent candidate (DESIGN §2.6). Dead pools are never
   quoted.
3. Rule H (efficiency only): take the `M` pools with the largest positive Rule-T hint
   `t_i` (ties by index). In admitted order, give each `u_i = ⌊t_i·N/A⌋` and drop legs with
   `u = 0` or a zero non-final floor. The last kept pool takes the remaining units (≥ 1).
   If at least two legs remain, quote them and consider the result. It matters mostly for
   interrupted raw searches (§9.3).
4. A candidate becomes the incumbent only if it is a complete plan built by
   `direct_split.allocation_plan`, the evaluator replays it `ok` (through the same
   `QuoteCache`, which costs no new quotes) and its score is **strictly** greater. Every new
   incumbent is published with `report_candidate`. An evaluator failure or score mismatch is
   counted (`evaluation_mismatches`, which must be 0) and never becomes the incumbent.

### 5.3 Frontier order and ties

The frontier is a binary heap with key `(unknown first, −ub, creation sequence)`. This is
deterministic best-first search with FIFO order among equal bounds. The certificate
certifies the **value**. When several allocations tie, the returned one is the first found
in this order and may differ from `direct_split`'s tie choice. That difference is not a
defect.

### 5.4 Loop, pruning and stops

Repeat while the frontier is non-empty:
1. If the top node's `ub ≤ L`, prune the whole frontier (all remaining bounds are ≤ `L`,
   and unknown nodes would sort first) and finish `complete`.
2. If `bb_nodes_expanded = max_bound_nodes`, stop with `node_cap`.
3. Pop and expand the node, then push the children that survive pruning. If the frontier
   would then exceed `max_open_nodes`, re-push the popped node, discard its children and
   stop with `state_cap`.

The cooperative stops before a new quote (`Budget.max_quotes` reached: `quote_budget`) and
before a new evaluation (`Budget.max_candidates` reached: `candidate_cap`) **re-push the
node being expanded**. A leaf keeps its pre-quote bound, which dominates its value. These
are the only terminations this identity emits: `complete`, `node_cap`, `state_cap`,
`quote_budget`, `candidate_cap`. `wall_budget` is not used. A hard wall or hard quote kill
by the runner gives no `SolveResult`: `certificate_unavailable_reason: hard_timeout`,
with only the runner's `last_valid_candidate`. The test
`test_a_hard_killed_solve_leaves_only_the_last_reported_candidate` shows the meter's
`QuoteLimitExceeded` escaping with no certificate. A stop can still leave `U = L` when
the frontier is already dominated (record `P-IA-SYNTHETIC-QUOTES`). That is a valid proof,
labeled with the actual stop reason.

### 5.5 Status mapping

| Situation | Status | Certificate |
| --- | --- | --- |
| incumbent exists (complete or stopped) | `ok` (`search_stats.truncated_by` = stop or null) | `certified` if `U` known, else `unknown` |
| no incumbent, search complete | `no_route` (every allocation infeasible, or no live pool) | null, `not_produced` |
| no incumbent, stopped | `timeout` (never evidence of `no_route`) | null, `not_produced` |
| §2.1 scope rule | `unsupported` / `no_route` | null, `not_produced`, `scope` set |

Certificate fields: `lower_raw = L`; `upper_raw = U` (null if unknown); `gap_raw = U − L`;
`upper_source` (§4); `estimate: null`; `optimality_proven = (U = L)`; `termination`;
`source` holds `{git_revision, bundle_hash, algorithm: "direct_split_certified",
effective_settings_sha256 = SHA-256 of the canonical options JSON}`; `request` holds the
exact case. The runner checks all of these against its own identities (R021-C/1 §9.4).

## 6. Options, preset, stress profile and the §3.3 row

`algorithm_options.direct_split_certified` (WHI-1548 seam), validated by
`validate_options`:

| Key | Type | Range | Meaning |
| --- | --- | --- | --- |
| `domain` | string | `"repository_grid"` \| `"raw_integer"` | the declared feasible set (§2.2/§2.3) |
| `max_bound_nodes` | integer | 1 … 10,000,000 | cooperative cap on `bb_nodes_expanded` → `node_cap` |
| `max_open_nodes` | integer | 1 … 10,000,000 | memory cap on the open frontier → `state_cap` |
| `raw_max_amount_in` | integer | 1 … 1,000,000 | required with `raw_integer`, refused otherwise; larger inputs are `unsupported` |

All keys are required after preset resolution (the solver has no defaults). The validator
refuses the following: unknown keys; booleans or non-integers (including non-finite floats)
for integers; out-of-range values; `raw_max_amount_in` without `raw_integer`; and every
R021-C/1 §9.1 reserved key. `max_splits`, `percent_step`, `max_quotes`, `max_candidates`,
`shortlist`, … stay profile values, and `percent_step` is read only by `repository_grid`.
Both domains use `search.max_splits`.

- **Bounded comparison preset** (proposed path
  `config/strategy_options/direct_split_certified.yaml`, version 1, used by
  `--strategies all`): `{domain: repository_grid, max_bound_nodes: 100000,
  max_open_nodes: 100000}`, settings SHA-256 `03cfe301…8406`. Evidence is in §9.2.
- **Stress profile** (explicit `--strategies profile` only, never in `all`):
  `{domain: raw_integer, max_bound_nodes: 1000000, max_open_nodes: 250000,
  raw_max_amount_in: 100000}`; the hash is in the fixture.

**§3.3 row (narrowed, same vocabulary):** hop bound 1; `search.max_splits` (allocation),
plus the grid and pool order in `repository_grid`. `Budget.max_candidates` counts
`finalist_plans_evaluated`, meaning complete candidate plans evaluated inside solve (the
singles, Rule H and improving leaves). Separate caps: `max_bound_nodes` and
`max_open_nodes`.

## 7. Result schema and published records

`search_stats["r021"]` (`r021.diagnostics/1`): `schema`, `contract`, `algorithm`, `domain`,
`candidate_domain_hash`, `certificate` (`r021.certificate/1`) or null,
`certificate_unavailable_reason`, `max_candidates_unit: "finalist_plans_evaluated"`,
`work` and `scope`. The `work` units, all non-negative integers, are:
- `quotes_executed` (`QuoteCache` misses, which equal the worker meter; asserted);
- `quotes_memoized`;
- `internal_evaluations`;
- `bb_nodes_expanded`;
- `bound_evaluations` (one per node created; a bound never quotes);
- `peak_open_nodes`.

Bound work and quote calls are separate units and are never divided by each other. Plain
`search_stats` extras (not §5.2 units): `truncated_by`, `nodes_pruned_bound`, `grid_units`,
`direct_pools`, `live_pools`, `evaluation_mismatches`, the returned allocation.

[`fixtures/integer-allocation.json`](fixtures/integer-allocation.json) holds 11 records.
Each is regenerated byte-for-byte by `certify` and validated by the unchanged R021-C/1
`check_diagnostics` (identity and request included). Where the domain is supported, each is
checked against the oracle optimum:

| Record | Domain | Result |
| --- | --- | --- |
| `P-IA-R6-GRID-P1P2` / `-P2P1` | R6 grid, two orders | 58 = 58 / 59 = 59, `complete` |
| `P-IA-R6-RAW` | R6 raw (stress) | 59 = 59, `complete` |
| `P-IA-R6-NODE-CAP` | R6 grid, `max_bound_nodes` 1 | [58, 59], `node_cap` (incumbent already optimal, not proven) |
| `P-IA-SYNTHETIC` / `-QUOTES` | `tests/fixtures/synthetic`, profile grid; `max_quotes` 1 | 271,983 proven; `quote_budget` with zero gap |
| `P-IA-DRY-POOL`, `P-IA-DUST` | `cpmm_graph` `a_b_large` (dead `ab_dry`), `a_b_dust` (3 units) | proven; the dead pool is never quoted |
| `P-IA-NO-DIRECT` | `synthetic` `direct_no_route` | `no_route`, null |
| `P-IA-UNSUPPORTED-MIXED` | real `mantle_mixed` USDC/USDT (LB + Moe Classic) | `unsupported`, 0 quotes, null |
| `P-IA-REAL-MOE` | real Moe Classic state (`moe_classic_v1`, `uint112`) | proven single pool |

Changing the git revision or the request makes the shared validator report exactly
`C_IDENTITY` or `C_REQUEST` (`test_published_identities_are_bound_to_run_and_request`).

## 8. Independent checks (this issue; WHI-1552 must port or re-run them)

`tests/routing/test_integer_allocation_contract.py`. Its oracle has its own enumeration
(`itertools` compositions), amount rule, CPMM formula and failure pins. It shares no code
with `certify`, `direct_split` or `pools/`.

1. Oracle quote ≡ `quote_exact_in` (dust, dead, fee 0/100, sourced revert, fee mismatch,
   unknown source). The oracle grid is `direct_split`'s domain: every allocation replays
   with the evaluator, and `direct_split.solve` returns the oracle maximum (P1).
2. Complete certificates on 40 random grid and 25 random raw instances. The instances cover
   1–5 pools, `percent_step` 4–100, `max_splits` 1–4, dust, dead pools, sourced pools a few
   units below the `uint112` revert, random admitted order and infeasible domains. In each,
   `L = max` with zero gap, and **every node's bound is ≥ the exhaustive maximum of its
   region**.
3. Forced truncation (node caps 1–13, open caps 1–4, `max_quotes` 0–9, `max_candidates`
   0–2) on 30 instances. `L ≤ max ≤ U` always, and every feasible allocation above `L`
   lies in an open node whose bound covers it (frontier coverage). Budgets are respected,
   `timeout` appears only without an incumbent, and every stop kind plus a nonzero
   certified gap occurs.
4. Mutations must be caught on 57 multipool instances. Observed at this head:

   | Mutation | Instances caught | False zero-gap certificates |
   | --- | ---: | ---: |
   | bound − 1 | 40 | 4 |
   | top-two shortlist | 5 | 1 |
   | unknown read as 0 | 30 | 13 |
   | relaxation value at the hint point used as a bound | 3 (node-level) | — |

   The genuine bounds are caught on 0 instances. The unknown-bound hook never certifies a
   truncated search.
5. R2, R3, R6; a real Moe Classic smoke over all 62 cases of `moe_classic/bundle` (value =
   `direct_split`); unsupported CL/LB pairs and net objectives; the hard kill; the quote
   ledger equal to the worker meter; the option validator; the pinned preset, sweep and
   probe.

## 9. Evidence

### 9.1 Real corpus scope (tuning splits only)

| Split | Cases | Scope | Pool-pair structure |
| --- | ---: | --- | --- |
| `bundle_tuning` (full source) | 96 | 72 `unsupported_non_cpmm`, 24 `no_direct_pool`, **0 supported** | 143 pools, 21 pairs, 14 CPMM pools, **0 all-CPMM pairs**, ≤ 1 CPMM pool per pair |
| `sor_cohort_tuning` (V2/V3) | 96 | 66 unsupported, 24 no direct pool, **6 supported single-pool** (6/6 proven, 1 quote each) | 98 pools, 19 pairs, 1 all-CPMM pair (1 pool) |

The report splits' `pools.json` are byte-identical to their tuning twins, so the pair
structure holds there too, without reading any report case. **The frozen corpus has no
request with two or more direct pools that are all CPMM**: every Mantle CPMM pair also has
CL/LB pools. In the frozen campaign, `direct_split_certified` will therefore show
`unsupported` on every multi-pool case, and on the SOR cohort only single-pool proofs.

### 9.2 Preset evidence (synthetic, real magnitudes)

These are not corpus data. The `sweep` pass (seed 20261004, pinned and reproduced by the
test) builds 30 instances of 2–8 parallel Moe-Classic-sourced pools with reserves
10²⁰–10²⁵, ±2 % price drift and inputs from 0.01 % to 50 % of the base reserve, at
`max_splits` 4:

| `percent_step` | nodes p50 / max | peak open max | bound evaluations max | quotes p50 / max | `direct_split` quotes p50 / max | result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 5 (profiles) | 7 / 499 | 349 | 1,039 | 8 / 34 | 174 / 459 | all `complete`, zero gap, = `direct_split` |
| 1 | 14 / 10,563 | 6,190 | 21,865 | 8 / 135 | 961 / 2,478 | same |

The preset's 100,000 node and open-node caps give ≥ 200× headroom at the profile grid and
≥ 9× at `percent_step` 1 (asserted ≥ 5×). The quote counts are a work-unit fact. They are
**not** a latency claim: exact-rational bound evaluations are a separate and possibly
dominant cost, and no timing was measured. The tuning split's 6 supported cases each needed
1 quote and 0 nodes, so they are not informative for the caps.

### 9.3 Grid versus raw (expanded domain)

The pinned summary covers 60 random instances (2–4 pools, input 20–60, reserves comparable
to the input). Raw exceeds the grid in 6, is equal in 54, and exceeds it by at most 1 unit.
R6 gains +1 unit. The `raw-sweep` artifact pass uses the stress profile, inputs 100–100,000
and 2–4 pools. Every row completes with zero gap, raw beats the grid by up to 42 units at
100,000, and the largest row needed 51,555 nodes. In a separate unpinned exploration
without Rule H, a node-capped raw search once returned an incumbent **below** the same
pools' grid optimum (8,126 vs 8,791). That is sound (it was certified [8,126, 8,796]) but
poor, and it is why Rule H seeds raw and grid runs alike. Raw gains are domain gains,
never an implementation speedup.

## 10. Outcome: `narrow_go`

**Supported (GO scope).** Requests whose admitted direct pools are all constant product,
under `gross_only`. In `repository_grid` the domain is exactly `direct_split`'s (§2.2), with
exact-rational certified bounds, deterministic best-first search, bounded caps and
coverage-preserving interruption (T1–T3). The separately identified `raw_integer` is a
stress domain up to `raw_max_amount_in`. The algorithm, domain hash, numerical
representation (int/`Fraction`, no float) and termination/result schema are
implementation-ready. All values fit R021-C/1 as is.

**Narrowed, not supported, or blocked.**
- CL, LB and mixed direct pool sets are `unsupported` (no CPMM approximation, no subset
  certificate). A concave-bound proof for the migrated `pools/concentrated.py` /
  `pools/liquidity_book.py` is **blocked** pending its own proof. Net objectives are
  `unsupported`.
- **Coverage:** the frozen corpus has no multi-pool case in scope (§9.1). The identity's
  measurable contribution is fixture evidence, the SOR cohort's single-pool proofs and
  visible `unsupported` rows. By P1, a `same_grid_allocation` value difference would signal
  a defect, not a gain. The parent/owner should decide whether WHI-1552 is still wanted
  with this coverage. This memo does not change that decision or the contract ceiling.
- No latency, quote-saving adoption or tie-identity claim.

## 11. Amendment text for WHI-1552 (for the parent to apply)

> **Contract:** implement `direct_split_certified` exactly as
> `docs/references/research-021/integer-allocation.md` §2–§5. The normative form is
> `certify` in `tests/routing/test_integer_allocation_contract.py`. Put it in a new
> `routing/algorithms/direct_split_certified.py` that reuses `direct_split.leg_amounts`,
> `allocation_plan`, `prepare`'s `max_splits`/`percent_step` validation, `QuoteCache`, the
> in-solve `evaluate` and `report_candidate`, without changing `direct_split`.
> Specifically:
> - Candidate pools are all `pools_for_pair` pools in admitted order. The §2.1 scope rules
>   apply before any quote: non-CPMM pair or net objective → `unsupported` with a reason,
>   and no subset fallback.
> - Bounds are exactly §4 in `int`/`Fraction` with Rule T. Nodes, children, order, pruning
>   and stops are exactly §5, including the re-push-on-stop rule, the singles stage, Rule H
>   and the strict-improvement, evaluator-validated incumbent.
> - Certificate, statuses and terminations follow §5.5, with `wall_budget` unused. A
>   hard-killed solve has no certificate.
>
> **Options** (§6): `domain` {repository_grid, raw_integer}, `max_bound_nodes` 1…1e7,
> `max_open_nodes` 1…1e7, `raw_max_amount_in` 1…1e6 (raw only). All are required and
> reserved keys are refused. Preset v1 = `{domain: repository_grid, max_bound_nodes:
> 100000, max_open_nodes: 100000}` in `all`. The raw stress profile `{raw_integer, 1000000,
> 250000, 100000}` runs only through `--strategies profile`.
> **Diagnostics:** §7 units and extras. `quotes_executed` equals the worker meter, and
> `bound_evaluations`/`bb_nodes_expanded` are reported separately from quotes.
> **Checks:** port §8 onto the real factory. That means the oracle equivalence and
> all-allocation replay; complete random grid/raw suites with per-node bound dominance;
> forced truncation with frontier coverage; the four mutations caught and genuine never;
> R2/R3/R6; the published records regenerated exactly (same numbers with Rule T); the real
> Moe smoke; unsupported mixed pairs and net objectives; the hard kill; the ledger; the
> validator. Also add `run` and `quote --details` on a CPMM-only fixture profile and the
> `sor_cohort_tuning` single-pool cases.
> **Comparison:** `same_grid_allocation` against `direct_split` on one domain hash must show
> equal values (P1). Report quotes, nodes, bound evaluations and peak open nodes side by
> side. There is no speed claim without L01's host rule.
> **Claims:** value certification within the declared domain only; no coverage beyond
> all-CPMM direct pool sets, which the frozen corpus does not contain with ≥ 2 pools (§9.1).

## 12. Shared-contract impact

None required. Every record validates under the unchanged R021-C/1 validator, the R6
domains hash to WHI-1547's, and every unit, termination, bound kind and grid kind used is
already registered. Two notes for the parent, not edits:
1. A capped termination with a zero gap (`P-IA-SYNTHETIC-QUOTES`) is allowed by
   `C_TERMINATION` and is correct here. `contract.md` §4.3 says "may still carry a
   certified nonzero gap", which WHI-1552 should not read as "must".
2. WHI-1547's illustrative `P-DSC-GAP` (57, tangent 59) is not produced by this algorithm.
   Rule H and the singles stage give 58 on R6 at one node (`P-IA-R6-NODE-CAP`). Both are
   valid records of different procedures.

## 13. Reproduction

```bash
uv run pytest tests/routing/test_integer_allocation_contract.py -q
uv run pytest tests/routing/test_direct_split.py tests/docs/test_research_021_contract.py -q
(cd docs/references/research-021/sources && shasum -a 256 -c SHA256SUMS)
C=…/router-algorithms-optimizer/data/corpus/mantle-5src-101082044
PYTHONPATH=. uv run python tests/routing/test_integer_allocation_contract.py probe \
  "$C/bundle_tuning" full_source "$C/sor_cohort_tuning" sor_compatible probe.json
PYTHONPATH=. uv run python tests/routing/test_integer_allocation_contract.py sweep sweep.json
PYTHONPATH=. uv run python tests/routing/test_integer_allocation_contract.py raw-sweep raw-sweep.json
PYTHONPATH=. uv run python tests/routing/test_integer_allocation_contract.py examples  # rewrites the fixture
```
