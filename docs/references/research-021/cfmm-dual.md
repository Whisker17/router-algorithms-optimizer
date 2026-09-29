# `cfmm_dual`: CFMM dual-routing port and integer plan recovery (WHI-1557)

| Item | Value |
| --- | --- |
| Contract | inside `R021-C/1` ([`contract.md`](contract.md)); this file publishes the §11 obligations of identity 5 |
| Publication key | `R021-P11`, WHI-1557, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Research outcome | **`narrow_go`** — gross-only, single-source exact input, estimate-only (no `certified` bound), market universe `simple_path_union`, CPMM stage first, V2/V3 CL stage gated, LB excluded |
| Implementation | WHI-1558 (CPMM stage, §11.1 gate), then WHI-1559 (CL stage, §11.2 gate) |
| Executable model | [`tests/routing/cfmm_contract_model.py`](../../../tests/routing/cfmm_contract_model.py) (validation-only, no optimizer) |
| Executable check | `uv run pytest tests/routing/test_cfmm_contract.py -q` |
| Author reference | `tools/upstream/cfmm/` (CFMMRouter.jl pin, offline, never timed), fixtures in `tests/fixtures/cfmm/` |

This document fixes the algorithm, its numerical and integer semantics, the R021 domain
row, option schema, bounded preset, stop/tie/failure rules and gates so that WHI-1558 and
then WHI-1559 can implement without inventing semantics. It implements no solver. The
six base identities, the optimized recipes, `metis_inspired` and every historical record
are untouched. No performance gain is promised; no runtime/SLA threshold exists.

## 1. Evidence classes (never merged)

| Class | What | Where |
| --- | --- | --- |
| Author paper | arXiv:2302.04938**v1** (the only version), equations cited as (n) below | §2 |
| Author code execution | pinned CFMMRouter.jl run OFFLINE on 13 oracle and 7 router cases | `tests/fixtures/cfmm/author_reference.json` |
| Python model reference | this repository's model + SciPy L-BFGS-B, ephemeral environment (**not** author execution) | `tests/fixtures/cfmm/model_reference.json` |
| Independent integer expectations | hand `getAmountOut` brute force, the real evaluator, hand-built continuous flows | `tests/routing/test_cfmm_contract.py` |
| Tuning probe | model + SciPy on the 96-case tuning split, CPMM markets only | artifacts, §10 |

The external report (`sources/research-report.md`) contributes no CFMM evidence beyond
citing [C1]/[C2]; nothing here relies on its absent verification bundle.

## 2. Pinned sources and notices

| Source | Pin | Verification |
| --- | --- | --- |
| Paper | Diamandis, Resnick, Chitra, Angeris, *An Efficient Algorithm for Optimal Routing Through Constant Function Market Makers*, arXiv:2302.04938v1 (9 Feb 2023; submission history lists only v1). HTML `https://arxiv.org/html/2302.04938v1` = unversioned HTML, sha256 `4e74be954c8a6f8107d46dbcc5fc4441966f49a810b38448ea5cf0d59ec7aee0`; PDF sha256 `8b26956fb768240791081189506448a70c646aa6bc23bdfa7d5fc700899a5ed0`; TeX e-print sha256 `074935444cf99772476ae1920816bafeb4928246eb54dddfd831c119077ae059` | fetched 2026-09-29; licence: arXiv non-exclusive distribution — cited, not redistributed |
| Author code | `bcc-research/CFMMRouter.jl` commit `5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267` (= `main` on 2026-09-29, package 0.3.1), tree `dbf991b18897abdcca08647dd753a955f7a477f6` | MIT, "Copyright (c) 2021 Guillermo Angeris, Theo Diamandis"; verbatim `tools/upstream/cfmm/LICENSE-CFMMRouter.jl.txt` (sha256 `99056ed3…d042e`) |
| Author optimizer | LBFGSB.jl 0.4.1 (MIT, Yupei Qi, tree `e2e6f53e…91b5`) wrapping `L_BFGS_B_jll` 3.0.1+0 (L-BFGS-B 3.0, BSD-3) | `tools/upstream/cfmm/Manifest.toml`, `LICENSE-LBFGSB.jl.txt` |
| Reference runtime | Julia 1.10.10 (official tarball sha256 `52d3f82c…18be`), `JULIA_NUM_THREADS=1` | `author_reference.json` `provenance` |
| Port optimizer (WHI-1558) | SciPy 1.18.1 `minimize(method="L-BFGS-B")` (C translation of the same L-BFGS-B 3.0), NumPy 2.5.3 | model reference `environment` |

Code read for the mapping: `src/router.jl`, `src/cfmms.jl`, `src/objectives.jl`,
`src/utils.jl`, `test/*.jl`, `examples/Univ3.jl`, LBFGSB.jl `src/wrapper.jl`. The README
was read only to record what *not* to use (it rounds net flows with `round.(Int, …)`).

## 3. The author algorithm (paper) and what the code actually does

**Problem (1):** maximize U(Ψ) s.t. Ψ = Σᵢ Aᵢ Δᵢ, Δᵢ ∈ Tᵢ (closed convex, 0 ∈ Tᵢ).
CFMM acceptance (2): φ(R − γΔ₋ − Δ₊) ≥ φ(R). **Relaxation** with prices ν: maximize
U(Ψ) − νᵀΨ + Σᵢ (Aᵢᵀν)ᵀΔᵢ, separable into Ū(ν) = sup_Ψ (U(Ψ) − νᵀΨ) and the per-market
optimal-arbitrage problems (5) arbᵢ(Aᵢᵀν) = sup_{Δᵢ∈Tᵢ} (Aᵢᵀν)ᵀΔᵢ. **Dual (7)–(8):**
minimize g(ν) = Ū(ν) + Σᵢ arbᵢ(Aᵢᵀν); **gradient (9):** ∇g(ν) = −Ψ*(ν) + Σᵢ AᵢΔᵢ*(ν)
— the coupling residual. Solved with L-BFGS-B (§2.3). **Swap markets** (§3): forward
function f₁, scalar arbitrage (10) max −ν₁δ + ν₂f₁(δ), δ ≥ 0, no-trade band
f₁′(0) ≤ ν₁/ν₂ ≤ 1/f₂′(0). **Bounded liquidity / aggregate CFMMs** (§3, §4.1): Uniswap v3
= disjoint bounded-product intervals φ = √((R₁+α)(R₂+β)) (4); only the active interval
needs work. **App. A** CPMM closed form: δ* = max{(R₁/γ)((γ ν₂R₂/(ν₁R₁))^{1/2} − 1), 0}.

Audit of the pinned code (each item is checked by the offline tests or visible in the
fixtures):

| Point | Actual behaviour at `5932e42` | Consequence for the port |
| --- | --- | --- |
| Return value | `route!` returns `nothing` (the value of the final threaded `find_arb!`); LBFGSB.jl returns `(f, x)` and drops the L-BFGS-B task string, so **no convergence status, iteration or evaluation count** is observable | our port classifies termination itself (§5.4) |
| Defaults | `m=5` (allocates 17), `factr=1e1` (ftol ≈ 2.2e-15), `pgtol=1e-5` on the **raw-unit** gradient, `maxfun=maxiter=15000`; `nbd=2` with an `Inf` upper bound | raw-unit tolerances are meaningless across token decimals |
| Start / bounds | ν₀ = 1/n (projected into the box); `Swap` = `BasketLiquidation`: ν_out ≥ 1 + √eps, every other ν ≥ √eps | a lower bound on ν means the relaxed primal allows **free disposal** (surplus) of every non-output token |
| Threads | `find_arb!(r, v)` uses `Threads.@threads` over markets (results independent of order) | the port is sequential; the reference ran with one thread |
| CPMM oracle | `prod_arb_δ/λ` = App. A with η = 1; fee on the input (`γΔ` enters the invariant) | identical to the migrated `getAmountOut` without the final floor (§4.2) |
| UniV3 oracle | `lower_ticks` are the interval **upper** prices (descending); interval `i` spans `[lower_ticks[i+1], lower_ticks[i]]`, the last one `[0, lower_ticks[end]]`, no liquidity above `lower_ticks[1]`; `liquidity` holds L²; fee applied once to the aggregate input. The docstring's "k+1st price is Inf" contradicts the code | we follow the code; the mapping is §8.3 |
| Observed | with defaults: `r-tiny` returns ν with net input −22.5 for a 2-unit order (infeasible) and dual value 917 vs a continuous optimum ≈ 198.4; the raw-unit Moe triangle returns a WMNT net flow of −2.0e11 (borrowed); `maxiter=1` returns silently; the L-BFGS-B Fortran prints `ascent direction in projection` to stdout | optimizer completion is not evidence of feasibility; normalization is required |

## 4. Our problem (single-source exact input, gross)

### 4.1 Objective, units and markets

A case (`token_in` s, `token_out` t, raw integer A > 0) under `gross_only` only. Units are
raw integer token units; prices ν are "raw t units per raw unit" with **ν_t ≡ 1**
(g is homogeneous of degree one, and Ū(ν) = A·ν_s for ν_t ≥ 1, so the author's free
ν_t ≥ 1 is attained at 1). For the `Swap` utility the dual is

  g(ν) = A·ν_s + Σ_{p∈M} arb_p(ν),  ∂g/∂ν_j = A·[j = s] + Σ_p (received_pj − tendered_pj).

**Market universe M (`simple_path_union`).** Admitted pools of the stage's protocols
(CPMM: `pools.constant_product` admission — source-free or a known source with its fee,
both reserves > 0; CL: `pools.concentrated` source admission incl. the LM-hook rule) that
lie on at least one simple s→t path of at most `search.max_hops` admitted pools
(`routing.search.enumerate_paths`), in bundle insertion order. Tokens are the market
tokens; variables are all of them except t. Liquidity Book pools never enter M.

### 4.2 Continuous market model with the actual fee

- **CPMM** (`fee_bps`, γ = (10000 − fee_bps)/10000): f(δ) = γδR_out/(R_in + γδ) — the
  migrated `getAmountOut` numerator/denominator without the final floor (exact output =
  ⌊f(δ)⌋ for every admitted source, `moe_classic_v1` `fee_bps` 30). Oracle: trade a→b iff
  r² = γν_bR_b/(ν_aR_a) > 1; then δ = R_a(r−1)/γ, λ = R_b(1 − 1/r); at most one direction.
- **CL** (`fee` pips, γ = 1 − fee/10⁶): the continuous aggregate of §8.

### 4.3 Why the continuous value is not an integer bound here

Every plan of the declared domain (§9.1: one merged step per market, integer amounts,
plan-token DAG, full fill) has integer outputs ⌊f_p(x_p)⌋ ≤ f_p(x_p), so its net flows are
feasible for the relaxation and **weak duality** gives gross ≤ g(ν) for *every* ν > 0 in
exact arithmetic (a sequential second use of a fee-bearing CPMM yields less than the
merged swap, so `shared_sequential` plans are dominated too). The inclusion argument is
therefore available, but g is evaluated in float64 (the CPMM oracle rounds a square root
and cancels R(r−1)), no outward-safe evaluation is implemented or demonstrated, and the
relaxation also admits cycles and free disposal. **Hence `bound_kind` is `estimate` or
`unknown`, never `certified`** (§7).

## 5. Numerical method (WHI-1558 implements exactly this)

### 5.1 Scale σ (deterministic)

σ_t = 1; grow a maximum-depth spanning tree from t over M (Prim): repeatedly take the
market with the largest priced-side depth valued in t units, σ_k·depth_k (CPMM depth =
reserve of k; CL depth = virtual reserve L/√P for token0, L·√P for token1), ties by
admitted market order; set σ_j = σ_k × fee-free spot value of j in k. (The first-found BFS
alternative was measured on the tuning split and rejected: a 1-unit-reserve pool set the
scale of several tokens 10⁸× too high.)

### 5.2 Variables and optimizer function

x_j = log(ν_j/σ_j) with the box |x_j| ≤ `log_price_bound`, x₀ = 0 (spot prices; the
restricted re-solve warm-starts from the previous ν). The optimizer minimizes

  Φ(x) = log g(ν(x)),  ∂Φ/∂x_j = ν_j·(∂g/∂ν_j)/g,

i.e. the net-flow imbalance of token j **valued at the point's own prices, as a fraction
of the dual value**: unit-free and invariant to decimals and price scale. g ≥ A·ν_s > 0
and log/exp are monotone, so Φ's stationary points are exactly the interior minimizers of
the convex g (no spurious stationary points; convexity in x is not claimed). Summation is
`math.fsum` in admitted market order.

### 5.3 Optimizer and budgets

`scipy.optimize.minimize(Φ, x₀, jac=True, method="L-BFGS-B", bounds=box,
options={maxcor: lbfgs_memory, ftol, gtol: pgtol, maxiter: max_iterations, maxfun:
max_function_evaluations})` — an established implementation of the same algorithm the
paper and author use; no hand-written optimizer. One evaluation = one Φ, one gradient and
|M| oracle calls. The initial solve and each restricted re-solve have their own
`max_iterations`/`max_function_evaluations` caps; at most one re-solve runs (§6.3), so the
numeric work per solve is bounded by 2 × (`max_function_evaluations` + 1) × |M| oracle
calls. The oracle makes no exact quote and does not touch the quote meter (numeric units,
R021 §5.3). Single thread: the oracle loop is sequential Python; the optimizer is called
in the worker process, and BLAS is used only on vectors of length ≤ |tokens| and
2·`lbfgs_memory` square matrices (provenance must record the BLAS vendor).

### 5.4 Stop rules and termination (never inferred from optimizer success)

After L-BFGS-B returns, evaluate once more at its x (counted) and compute the projected
residual r = maxⱼ |P_box(x − ∇Φ) − x|ⱼ.

| Condition | `termination` |
| --- | --- |
| r ≤ `residual_tolerance` (whatever SciPy's `status`/`message`) | `converged` |
| r > tolerance and SciPy stopped on `maxiter`/`maxfun` (status 1) | `iteration_cap` |
| r > tolerance otherwise (relative-reduction stop, `ABNORMAL` line search, …) | `not_converged` |
| a non-finite Φ or gradient (exception inside the objective) | recovery code `numeric_failure` |

`converged` means only that the continuous KKT residual is small. Recovery runs in all
three finite cases; the termination is reported, and only `converged` may carry an
estimate (§7). Non-smoothness: arb_p is C¹ but piecewise (zero inside the fee band,
kinks at CL interval boundaries/empty ranges), and dust orders against deep pools meet
the float noise floor; both show up as `not_converged`, never as silent success.

## 6. Integer plan recovery `cfmm_share_projection/1`

Input: the final oracle trades (x_p tendered, y_p received, one direction per market) of
the initial solve, its ν, the case. Output: one complete `RoutePlan` or a failure code.

1. **Support.** Keep markets with x_p > 0 and x_p ≥ `min_split_share` × (total continuous
   outflow of x_p's input token).
2. **Relevance.** Keep only markets on a directed s→t path of the support; never an
   edge out of t. Dead ends, disconnected components and surplus-only flows drop out.
3. **Cycles.** While the token digraph has a cycle (found by DFS in sorted token order,
   adjacency in admitted order): remove the cycle market with the smallest continuous
   input value ν_in·x_p (ties: the later admitted market); count `admission_checks` and
   `combinations_rejected_cycle`. If any market was removed and `cycle_resolve`: **re-solve**
   §5 on the remaining markets with their directions fixed (one-directional oracles: a
   market trades only in its kept direction; any flow on a DAG is acyclic), warm-started,
   and take support + relevance from that solution. Without `cycle_resolve`, only step 2
   is re-applied. A re-solve that yields no trades is `resolve_failed`.
4. **Share projection (exact, integer).** Visit tokens in topological order (Kahn; ties by
   first use = smallest admitted index of an incident support market, then token id). A
   token's exact inflow I (A at s; otherwise the sum of exact outputs of its incoming legs)
   is split over its out-markets in admitted order: every leg but the last gets
   ⌊I·x_p/Σx⌋ (shares as exact `Fraction`s of the float x values), the last gets the exact
   remainder; a zero leg is not emitted. Each emitted leg is quoted once on the pool's
   original state through `pools.quote.quote_exact_in` (via the per-solve `QuoteCache`).
5. **Prune and retry.** The first failing leg — quote status `insufficient_output_amount`,
   `insufficient_liquidity`, `incomplete_snapshot`, `reverted`, `unsupported`, or an `ok`
   quote with `amount_out == 0` (`zero_output`; CL pools return this for fee-only dust) —
   is removed, relevance is re-applied, and step 4 restarts (a new `recovery_attempt`). The
   next quote would exceed the remaining quote budget → `quote_budget`. No token left out
   of s → `support_exhausted`; `max_recovery_attempts` reached → `attempts_exhausted`;
   an empty support after steps 1–3 → `empty_support`.
6. **Plan and replay.** The integer flows go through `incremental_graph.merged_plan`
   (one merged step per market, explicit amounts, the last out-leg per token takes
   `ALL_REMAINING` of every fund holding a balance) and are replayed by
   `routing.evaluator.evaluate` with the same cache (memoized: `internal_evaluations` += 1,
   no new quote). Evaluated gross must equal the accounted gross, else it is an
   `algorithm_error` (a bug, not a recovery outcome).

**Invariants of every returned plan** (tested on all fixtures): evaluator `ok`; the whole
request consumed; no residual fund (no intermediate surplus, no dust donation); one step
per market; no token cycle; integer amounts or `ALL_REMAINING` only; every executed leg
has positive input and output; no external or borrowed fund. The continuous solution is
never trusted for money: only exact quotes decide amounts.

**Fallback.** On a recovery failure (`empty_support`, `support_exhausted`,
`attempts_exhausted`, `resolve_failed`, `numeric_failure`) and `fallback: single_path`, run
the best exact single path over the same markets (`enumerate_paths` hop-major DFS, first
best kept on ties, `Budget.max_candidates` caps the paths evaluated, remaining quote
budget only). It is charged to the same ledger, reported as `fallback = {used: true,
source: "single_path", reason: <code>}` and `termination = recovery_failed`; it is inside
the declared domain. `fallback: none` returns `model_error` with the code.

### 6.1 Status mapping (R021 §10 vocabulary)

| Situation | Status | Termination |
| --- | --- | --- |
| objective ≠ `gross_only` | `unsupported` (scope `objective`) | `unsupported_scope` |
| M empty but a ≤ `max_hops` simple path exists through non-stage pools | `unsupported` (scope `protocol_ceiling`) | `unsupported_scope` |
| M empty and no ≤ `max_hops` path at all | `no_route` (complete structural search) | — |
| recovery produced a plan | `ok` | solve termination (§5.4) |
| recovery failed, fallback plan found | `ok` (fallback visible) | `recovery_failed` |
| recovery failed, fallback found none | fallback status (`no_route` after its complete search, `incomplete_snapshot`, `timeout`) | `recovery_failed` |
| recovery failed, `fallback: none` | `model_error` | `recovery_failed` |
| `quote_budget` (cooperative) with no valid plan | `timeout` | `quote_budget` |
| runner hard wall/quote limit | runner `timeout`, certificate `unavailable (hard_timeout)` | — |

Every complete plan is reported with `SolveContext.report_candidate` after its replay.

## 7. Estimates, certificates and what would be needed for `certified`

A solve emits `r021.certificate/1` with `lower_raw` = evaluated score, `upper_raw` =
`gap_raw` = null, `optimality_proven` = false, and

- `bound_kind: estimate` with `estimate = {value: g(ν̂) of the **initial full-network**
  solve, residual: r, tolerance: residual_tolerance}` (decimal strings of the floats) iff
  its termination is `converged` and no fallback was used;
- `bound_kind: unknown` (estimate null) otherwise.

The restricted re-solve value is never the estimate (it covers a sub-domain). The estimate
may exceed any plan by arbitrage-loop profit (r-cycle: 627.01 vs plan 511) or integer
granularity. `certified` would additionally require (a) an outward-rounded evaluation of
g at a rational ν (e.g. exact rational CPMM oracle with a floored integer square root),
(b) the §4.3 inclusion proof restated for the exact domain and every admitted source, and
(c) tests; none is in WHI-1558/1559 scope.

## 8. CL extension (WHI-1559): continuous V3 aggregate and exact-replay boundary

### 8.1 Known range per pool and direction

From a `ConcentratedPoolState` (P = (sqrtPriceX96/2⁹⁶)² = token1 per token0, spacing s,
collected words [w_lo, w_hi]), initialized ticks are the set bits of the collected
bitmap words. Going **down** (token0 in): starting at the current price with the active
L, cross initialized ticks t ≤ `tick` in descending order (L −= liquidity_net), up to the
first of: an initialized tick without `TickInfo` (`missing_tick_data`, cannot be crossed),
the collected bottom w_lo·256·s (`collected_range`; the next step would read word
w_lo − 1), MIN_TICK. Going **up** (token1 in): ticks `tick` < t (L += liquidity_net) up to a
missing tick, (w_hi·256+255)·s, or MAX_TICK. If the current word itself is outside the
range the direction is empty. Segments with L = 0 are **empty ranges**: crossing them
costs no input and yields no output, as in the exact swap loop. Multiple positions give
multiple segments; nothing beyond the known range is ever extrapolated.

### 8.2 Oracle

Sell token0 while γ·P > ν₀/ν₁, down to √P* = √(ν₀/(γν₁)): net input Σ L(1/√P_b − 1/√P_a),
output Σ L(√P_a − √P_b) over the traversed parts, gross input = net/γ. Sell token1 while
P < γν₀/ν₁, up to √P* = √(γν₀/ν₁): net Σ L(√P_b − √P_a), output Σ L(1/√P_a − 1/√P_b).
Both stop at the known boundary (bounded liquidity: the continuous solution may then
leave input unspent, which the exact replay turns into `insufficient_liquidity` or
`incomplete_snapshot` → prune).

### 8.3 Author mapping and evidence

UniV3(current_price = P, lower_ticks = boundary prices from the top of the known range
down to its bottom, liquidity = L² per interval plus a final 0 for the author's implicit
`[0, P_bottom]`, γ). On the synthetic two-position fixture (with an empty range and a
missing-tick variant) and a real Uniswap v3 USDT/WMNT state, our oracle equals the author
`find_arb!` within 5e-14 relative on all 13 probes (no-trade band, near, across the
empty range, drain, both directions).

### 8.4 Admitted CL sources

`uniswap_v3`, `agni_v3`, `fusionx_v3`: the continuous model is identical (the protocol
fee only splits the fee between LPs and protocol, the Agni/FusionX LM hook is an amount-
free no-op), but each source keeps its admission record and **exact** semantics
(protocol-fee encoding, hook calls) in `pools.concentrated`, which alone executes plans.
Checked per source: exact output ≤ continuous and within 1e-9 relative inside the known
range; beyond it the exact swap is `incomplete_snapshot` and the model returns none.
CL mathematical interval efficiency never replaces the exact per-step rounding.

### 8.5 LB

Liquidity Book pools are excluded from every stage (`admitted` is false); LB-only
cases are `unsupported (protocol_ceiling)`. No LB fee or bin assumption exists here.

## 9. The R021-C/1 row of `cfmm_dual`

### 9.1 Domain record (`r021.domain/1`)

`universe.pools` = M; `protocols` = stage protocols; `pool_order` = M in bundle order;
`hops = {max: search.max_hops, param: "search.max_hops"}` — it bounds the market universe
(every market lies on a ≤ max_hops simple path), exactly as for `incremental_graph`, the
merged DAG may contain longer composite paths; `splits = {max: null, param: null,
governs: "none"}`; `amount_grid = {kind: "recovered_continuous", recovery:
"cfmm_share_projection/1", min_split_share: <decimal>, remainder:
"last_leg_all_remaining"}`; `zero_output_leg: infeasible`; `token_reuse: simple_path`;
`pool_reuse: shared_merged`; `dag_admission: plan_token_dag`; `full_fill: v1_full_fill`.

### 9.2 §3.3 row

| Hop bound | Split bound | `Budget.max_candidates` unit | Separate caps |
| --- | --- | --- | --- |
| `search.max_hops` (market universe) | none: `search.max_splits`, `search.percent_step` unused (`governs: none`); at most one merged step per market | `fallback_paths_evaluated` (only the single-path fallback consumes it; the dual and recovery never do) | `max_iterations`, `max_function_evaluations` (per solve), `max_recovery_attempts`; one re-solve |

Factory: `search_params = ("max_hops",)`, capabilities multi-hop, split, shared pools.

### 9.3 Options (`algorithm_options.cfmm_dual`) and preset `cfmm_dual/1`

| Key | Type | Range | Preset | Role |
| --- | --- | --- | --- | --- |
| `market_protocols` | enum | `constant_product`; `constant_product+concentrated` (accepted only after WHI-1559) | `constant_product` | stage |
| `max_iterations` | int | 1–1000 | 200 | L-BFGS-B `maxiter` per solve |
| `max_function_evaluations` | int | 1–3000 | 600 | `maxfun` per solve |
| `lbfgs_memory` | int | 3–30 | 10 | `maxcor` |
| `pgtol` | float | 1e-14–1e-3 | 1e-9 | optimizer `gtol` on ∇Φ |
| `ftol` | float | 1e-16–1e-3 | 1e-15 | optimizer relative reduction (author uses factr 1e1 ≈ 2.2e-15) |
| `residual_tolerance` | float | 1e-12–1e-2 | 1e-5 | §5.4 acceptance (0.1 bp of the dual value) |
| `log_price_bound` | float | 1–200 | 50.0 | box on x |
| `min_split_share` | float | 0–0.1 | 1e-6 | §6 step 1 (part of the domain) |
| `max_recovery_attempts` | int | 1–64 | 8 | §6 step 5 |
| `cycle_resolve` | bool | — | true | §6 step 3 |
| `fallback` | enum | `none`, `single_path` | `single_path` | §6 fallback |

No key is reserved (R021 §9.1); booleans are refused for ints, non-finite floats and
out-of-range values are refused before any worker starts.

### 9.4 Work units, ties, determinism

Work (§5.2 units only): `market_oracle_calls`, `objective_evaluations`,
`gradient_evaluations` (each SciPy `nfev` + 1 final, per solve), `optimizer_iterations`
(`nit` summed), `recovery_attempts`, `admission_checks`, `combinations_rejected_cycle`,
`quotes_executed`, `quotes_memoized`, `exact_replay_quotes` (quotes of steps 4–6),
`internal_evaluations`, `paths_scored` (fallback only). Details that are not R021 units
(pruned legs by reason, support size, re-solve termination, SciPy messages, residuals)
go to `search_stats["cfmm"]`, outside the `r021` object.

Ties: admitted market order everywhere; cycle victims by (value, later market); topological
ties by first use; leg order by admitted order with the remainder last; fallback keeps the
first best path. No randomness, no seed. Deterministic given the same pinned SciPy/NumPy
wheels and platform; bitwise cross-platform identity of float paths is not claimed, so
downstream tests compare integer plans only where the fixture's recovery is insensitive,
and otherwise within the stated tolerances plus evaluator validity.

## 10. Tuning evidence and bounded comparison recipe

Probe (Python model + SciPy 1.18.1, CPMM markets, `max_hops` 3, `bundle_tuning`
`ee7afa7e…279b`, 96 cases, preset §9.3; tuning exposure only):

| Measure | Result |
| --- | --- |
| cases with a CPMM market universe | 96 / 96 (1–12 markets, 2–7 tokens) |
| termination | 88 `converged`, 8 `not_converged`, 0 `iteration_cap` |
| iterations / evaluations | max `nit` 113 (cap 200), max `nfev` 303 (cap 600), median `nit` 34; ≤ 2,376 oracle calls |
| recovery | 94 plans; 2 failures (`support_exhausted`, `empty_support`, both into a 14,410-unit output pool) → fallback; 7 cases broke an arbitrage cycle and re-solved; 14 pruned the same dust leg (a pool holding 1 raw unit of the output token), 13 of them recovered on the second attempt |
| recovered vs best exact single path over the same markets | 69 higher, 25 equal, 0 lower (median +6.1 bp, max +741 bp) |
| recovered / estimate (converged) | median 0.99999, min 0.974 |
| exact quotes per recovery | ≤ 12 |

Ablations (same split): `ftol` at SciPy's default converges 34/96; `max_iterations` 50
caps 10 cases, 25 caps 62; `lbfgs_memory` 5 → 87, 20 → 90 at 1e-5; `min_split_share` 0
beats 1e-6 on 3 cases, 1e-4 loses 6, 1e-3 loses 16, 1e-2 loses 30 (1e-6 kept as a
float-noise guard ≥ 10⁴× the observed noise floor). The preset is finite everywhere.

**Bounded comparison recipe (WHI-1562).** Preset `cfmm_dual/1` only; profile values of
`config/full_gross.yaml` (`max_hops` 3, budgets); objective `gross_only`. (1) cfmm_dual vs
`path_split` and `incremental_graph` on `sor_cohort_tuning` (exploration) then
`sor_cohort_report` (frozen), class **`incomparable_domain`** (neither feasible set
contains the other): side by side, per family, never ranked as a mechanism gain; (2)
`cfmm_cpmm_vs_cl` on the same cohorts, class `expanded_protocol`; (3) five-source
bundles: controls also use LB → `expanded_protocol`. Report all statuses, evaluated gross,
per-family bp differences, recovery failures/fallbacks, estimate coverage and
recovered/estimate, §5.2 work units, wall/CPU per L01. No report-split tuning.

## 11. Gates

### 11.1 CPMM GO gate (WHI-1558)

G-C1 oracle and dual equal the author fixtures (`cpmm_oracle`, router cases at the author
point) within 1e-12 / 1e-9 relative; G-C2 the port reproduces every `model_reference`
dual point (value 1e-9, residual, termination class) and its optimum matches the author
where the author converged (1e-6); G-C3 recovered plans equal the stored flows on the
fixtures and satisfy §6 invariants under a fresh evaluator replay; G-C4 every §6/§6.1
failure category and cap has a test (cycle with/without re-solve, surplus, disconnected,
tiny/dust, quote budget, attempts, empty support, numeric failure, resolve failure,
fallback, `model_error`); G-C5 certificates validate under R021 §4.4 and are never
`certified`; estimate only when converged; G-C6 work units §9.4 and the one-ledger rule
(no unmetered exact quote); G-C7 options validator + preset pinned through WHI-1548's
seam; G-C8 determinism (two solves, identical plan and counters); G-C9 tuning-split
smoke: no `invalid_plan`/`algorithm_error`, every non-`ok` categorized. A performance win
is not a gate.

### 11.2 CL GO gate (WHI-1559)

G-L1 the CL oracle equals the author `UniV3` fixtures; G-L2 known-range extraction per
§8.1 (collected words, missing tick data, MIN/MAX tick, empty and multiple ranges, both
directions); G-L3 exact ≤ continuous within tolerance inside the range, never
extrapolated outside; G-L4 per-source tests for `uniswap_v3`, `agni_v3` (LM hook),
`fusionx_v3`; G-L5 LB never a market, LB-only cases `unsupported`; G-L6 CL zero-output
legs pruned; G-L7 all CPMM gates on mixed networks, new preset version tuned on
`sor_cohort_tuning`.

## 12. Executable checks and regeneration

```bash
uv run pytest tests/routing/test_cfmm_contract.py -q          # offline, no Julia/SciPy
tools/upstream/cfmm/regen.sh                                  # author inputs + Julia reference + byte check
uv run --with scipy==1.18.1 --with numpy==2.5.3 python tools/upstream/cfmm/python_reference.py fixtures
uv run --with scipy==1.18.1 --with numpy==2.5.3 python tools/upstream/cfmm/python_reference.py tuning|sweep <bundle_tuning> <out>
```

## 13. Limitations

The estimate is float and uncertified; the recovery is a heuristic projection with no
integer optimality claim (r-triangle: the converged point recovers 137, a two-iteration
capped point 138, which is the brute-force integer optimum); `not_converged` remains for dust orders and saturated output
pools; the market universe can exclude a pool reachable only by a longer path; cross-
platform float identity is not claimed; the CL stage is specified and oracle-verified but
not tuned; only 14 CPMM pools exist in the frozen corpus.

<!-- cfmm-dual-contract -->
```json
{
  "contract": "R021-C/1",
  "identity": "cfmm_dual",
  "research_issue": "WHI-1557",
  "outcome": "narrow_go",
  "stages": {
    "cpmm": {"issue": "WHI-1558", "market_protocols": "constant_product", "gate": ["G-C1", "G-C2", "G-C3", "G-C4", "G-C5", "G-C6", "G-C7", "G-C8", "G-C9"]},
    "cl": {"issue": "WHI-1559", "market_protocols": "constant_product+concentrated", "gate": ["G-L1", "G-L2", "G-L3", "G-L4", "G-L5", "G-L6", "G-L7"]}
  },
  "objectives": ["gross_only"],
  "search_params": ["max_hops"],
  "market_universe": "simple_path_union",
  "recovery": "cfmm_share_projection/1",
  "row": {
    "hops": "search.max_hops (market universe)",
    "splits": "none",
    "max_candidates_unit": "fallback_paths_evaluated",
    "separate_caps": ["max_iterations", "max_function_evaluations", "max_recovery_attempts"]
  },
  "options": {
    "market_protocols": {"type": "enum", "values": ["constant_product", "constant_product+concentrated"]},
    "max_iterations": {"type": "int", "min": 1, "max": 1000},
    "max_function_evaluations": {"type": "int", "min": 1, "max": 3000},
    "lbfgs_memory": {"type": "int", "min": 3, "max": 30},
    "pgtol": {"type": "float", "min": 1e-14, "max": 1e-3},
    "ftol": {"type": "float", "min": 1e-16, "max": 1e-3},
    "residual_tolerance": {"type": "float", "min": 1e-12, "max": 1e-2},
    "log_price_bound": {"type": "float", "min": 1.0, "max": 200.0},
    "min_split_share": {"type": "float", "min": 0.0, "max": 0.1},
    "max_recovery_attempts": {"type": "int", "min": 1, "max": 64},
    "cycle_resolve": {"type": "bool"},
    "fallback": {"type": "enum", "values": ["none", "single_path"]}
  },
  "preset": {
    "market_protocols": "constant_product",
    "max_iterations": 200,
    "max_function_evaluations": 600,
    "lbfgs_memory": 10,
    "pgtol": 1e-9,
    "ftol": 1e-15,
    "residual_tolerance": 1e-5,
    "log_price_bound": 50.0,
    "min_split_share": 1e-6,
    "max_recovery_attempts": 8,
    "cycle_resolve": true,
    "fallback": "single_path"
  },
  "preset_version": "cfmm_dual/1",
  "work_units": ["market_oracle_calls", "objective_evaluations", "gradient_evaluations", "optimizer_iterations", "recovery_attempts", "admission_checks", "combinations_rejected_cycle", "quotes_executed", "quotes_memoized", "exact_replay_quotes", "internal_evaluations", "paths_scored"],
  "terminations": ["converged", "not_converged", "iteration_cap", "recovery_failed", "quote_budget", "unsupported_scope"],
  "bound_kinds": ["estimate", "unknown"],
  "recovery_failures": ["empty_support", "support_exhausted", "attempts_exhausted", "resolve_failed", "numeric_failure", "quote_budget"],
  "prune_reasons": ["insufficient_output_amount", "insufficient_liquidity", "incomplete_snapshot", "reverted", "unsupported", "zero_output"],
  "example_domain": {
    "schema": "r021.domain/1",
    "universe": {"bundle": "fixture:cfmm-r-grid38", "cohort": "fixture", "pools": ["p1", "p2"]},
    "protocols": ["constant_product"],
    "pool_order": ["p1", "p2"],
    "hops": {"max": 3, "param": "search.max_hops"},
    "splits": {"max": null, "param": null, "governs": "none"},
    "amount_grid": {"kind": "recovered_continuous", "recovery": "cfmm_share_projection/1", "min_split_share": "0.000001", "remainder": "last_leg_all_remaining"},
    "zero_output_leg": "infeasible",
    "token_reuse": "simple_path",
    "pool_reuse": "shared_merged",
    "dag_admission": "plan_token_dag",
    "full_fill": "v1_full_fill"
  },
  "example_domain_hash": "c478636b14bad33fed2710b7db8cefac1df412ac26d58951dd7a15dd561ad71b",
  "example_certificate": {
    "schema": "r021.certificate/1",
    "candidate_domain_hash": "c478636b14bad33fed2710b7db8cefac1df412ac26d58951dd7a15dd561ad71b",
    "objective": "gross_only",
    "lower_raw": "59",
    "upper_raw": null,
    "gap_raw": null,
    "bound_kind": "estimate",
    "upper_source": null,
    "estimate": {"value": "59.3676093701934", "residual": "8.234801729400942E-12", "tolerance": "0.00001"},
    "optimality_proven": false,
    "termination": "converged"
  }
}
```
