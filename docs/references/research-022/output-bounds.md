# Per-pool output upper bounds for admitted CPMM, CL and LB (WHI-1597)

| Item | Value |
| --- | --- |
| Contract | `R022-Q01/1` |
| Issue | WHI-1597, Release 0.2.2 (`fca63897-b103-46f4-a690-c297246ed1ff`) — entry point; consumed by WHI-1598 (pruning contracts), WHI-1599 and WHI-1600 |
| Baseline | `origin/dev` `d23f3ef5ed41b29560ae516bb4b96179d7438c25` |
| Executable contract | [`tests/pools/test_output_bounds_contract.py`](../../../tests/pools/test_output_bounds_contract.py) — the formulae of §§3–5 as test-local functions, checked against the exact quote seam |
| Fixtures | [`fixtures/hand_cases.json`](fixtures/hand_cases.json) (hand-derived rates, R2), [`fixtures/lb_price_scan.py`](fixtures/lb_price_scan.py) (exhaustive LB price scan) |
| Command | `ROUTER_CORPUS_BUNDLE=<primary clone>/data/corpus/mantle-5src-101082044/bundle uv run pytest tests/pools/test_output_bounds_contract.py -q` |

This document states, **for every admitted pool family and swap direction**, an exact rational
output rate `r̄` computed from frozen pool state alone, proves

```
quote_exact_in(state, token_in, x).amount_out  <=  floor(r̄ · x)        for every x with status OK
```

against the *migrated integer code* (`pools/constant_product.py`, `pools/concentrated.py`,
`pools/cl_math.py`, `pools/liquidity_book.py`), and proves the chunk-marginal bound
`q(x+m) − q(x) ≤ r̄·m + s`, its multi-hop propagation, and same-direction validity. It adds no
solver and no production module; the production helper is WHI-1599's, and §7 is written so it
can implement from here without further research. Nothing here is a performance claim.

## 1. Result

Every admitted family and direction gets a proved bound. "No bound" (`None`) is returned only
under the conditions of the last column, and is never replaced by `0` or a guessed rate.

| Family (admitted sources) | Direction | `r̄` (exact rational, §3–§4) | Reads from frozen state | No bound when | Chunk slack `s` (§5.3) |
| --- | --- | --- | --- | --- | --- |
| CPMM (`moe_classic_v1`, or `source_key=None` generic) | either | `(D−f)·R_out / (D·R_in)`, `D = 10⁴`, `f = fee_bps` | `reserve0/1`, `fee_bps`, `source_key` | token not in pool; a zero reserve; `fee_bps ∉ [0, D)`; `source_key` unknown or `fee_bps ≠` the source's fixed fee | `1` |
| CL (`uniswap_v3`, `agni_v3`, `fusionx_v3`) | token0→token1 | `(10⁶−fee)·ψ² / (10⁶·2¹⁹²)` | `sqrt_price_x96 = ψ`, `fee`, `tick`, `source_key`, `lm_pool` | token not in pool; unadmitted source; `lm_pool` set on a source without the hook; `fee ∉ [0, 10⁶)`; guard `G_CL` fails (§4.2) | `ψ²/2¹⁹² + 1 + L̂/2⁹⁶`, only if `ψ < 2¹²⁸`, else none |
| CL | token1→token0 | `(10⁶−fee)·2¹⁹² / (10⁶·ψ²)` | same | same | `ρ₀·(1 + L̂/2⁹⁶) + 1`, `ρ₀ = 2¹⁹²/ψ²` |
| LB (`moe_lb_v2_2`) | X→Y (`token0`→`token1`) | `(10¹⁸−β)·p / (10¹⁸·2¹²⁸)` | `static_fee.base_factor`, `bin_step`, `active_id`, `source_key` | token not in pool; unadmitted source; `static_fee` missing; `bin_step < 1`; `β > 10¹⁷` (the 10 % `MAX_FEE`); `get_price_from_id(active_id, bin_step)` reverts; guard `2³⁸ ≤ p ≤ 2²¹⁸` fails (§4.3) | `p/2¹²⁸ + 1` |
| LB | Y→X (`token1`→`token0`) | `(10¹⁸−β)·2¹²⁸ / (10¹⁸·p)` | same | same | `2¹²⁸/p + 1` |

with `β = base_factor · bin_step · 10¹⁰` (the pair's *base* fee, `get_base_fee`), `p = get_price_from_id(active_id,
bin_step)` (the exact 128.128 integer the swap uses), and `L̂` an upper bound on the active liquidity
the swap can see (§5.3: `min(2¹²⁸−1, liquidity + Σ liquidity_gross)`, or just `2¹²⁸−1`).

On the frozen corpus (`mantle-5src-101082044`, 143 pools) **all 286 pool directions have a bound**:
no real pool direction falls in a "no bound" cell (checked by
`test_corpus_frozen_state_satisfies_the_guards`).

The bound is the same for *every* amount: it is a function of `(state, direction)` only. It is the
CPMM tangent at zero (`g'(0)` of `routing/algorithms/direct_split_certified.py`'s `g`), the CL spot rate
net of the pool fee, and the LB active-bin rate net of the *base* fee.

## 2. Domain: what "every admitted x" is

`q(x)` is `quote_exact_in(state, token_in, x).amount_out`, used only where the status is `OK`. What the
evaluator admits (`routing/evaluator.py`, `_check_plan` and the replay in `evaluate`):

- A step's total input is a non-negative integer. **Zero input makes no pool call** (`zero_input`, output 0),
  so a bound is needed only for `x ≥ 1`. There is no upper cap besides the fund balance; the proofs hold for
  every positive integer `x` for which the quote is `OK` (the seam's own limits — `int256` for CL, `2¹²⁸` for
  LB, the `uint112` reserve for sourced CPMM — only remove quotes).
- A quote that is not `OK`, or that does not consume exactly the merged input, makes the plan `invalid_plan`
  (`result.status is not QuoteStatus.OK`, `amount_in_consumed != total_input`). This covers: **zero-output
  legs** (CPMM `insufficient_output_amount`, LB `LBPair__InsufficientAmountOut`; R2 in
  [research-021/sources.md](../research-021/sources.md) §3 row 3), **insufficient liquidity**, **CL partial fills**
  (the quote returns `INSUFFICIENT_LIQUIDITY`, never a short OK), **incomplete snapshots**, **reverts** and
  **unsupported** state. A CL swap whose output floors to 0 is `OK` with output 0 (`concentrated.quote_exact_in`
  docstring) and is bounded like any other.
- Funds are positive integers; merged inputs (`inputs` with several funds) are summed into one swap, so the
  quote sees the total.
- The plan's token graph must be acyclic (`_token_cycle`); a later use of the same pool sees the state left by
  the earlier one (`next_states.get(step.pool_id, bundle.pools[...])`). Used in §5.2.

**Extension `q̃`.** For chunk statements (§5.3) `q̃(0) = 0` and `q̃(x) = 0` for a dust input whose status is
`insufficient_output_amount`; `q̃ = q` where `OK`; `q̃` is undefined where the quote fails otherwise. Every claim
about `q̃` is made only where both operands are defined.

**What a bound means for pruning.** The bound is claimed for `OK` quotes only. Where the exact quote would fail, the
plan containing it is invalid and not a candidate; a bound returned for such a pool is harmless (it is an upper
bound of nothing). Where no bound is returned, the pruner must not prune on that pool.

## 3. Premises of the issue that were checked

1. **LB "lower-bound the fee by the base fee if the variable part can decrease" — holds, and decreasing is real.**
   `swap` runs `update_references(params, block_timestamp)` once, then `update_volatility_accumulator` at *every*
   non-empty bin before `get_amounts` computes `total_fee = base + variable` (`get_total_fee`). The variable fee is
   `⌈(volAcc·binStep)²·variableFeeControl/100⌉ ≥ 0`, and `volAcc = volRef + |activeId − idRef|·10⁴` is rebuilt from the
   *reference*, which `update_references` resets (`idRef := activeId`) and decays (`volRef := volAcc·reductionFactor/10⁴`,
   or 0 after `decayPeriod`) from the *stored* values. So the first bin's fee can be below the stored
   `volatility_accumulator`'s fee: this happens for **16 of the 45 corpus LB pairs** (e.g. `0xdc39d7a2…`: stored total fee
   2.4875 % but 0.8 % — its base fee — at the first bin). For an *upper* bound on output one needs a **lower** bound on
   the fee, and `0 ≤ variable` makes `β` (the base fee) that bound regardless of crossings or the frozen timestamp.
   `base_factor`, `bin_step` and every static parameter are never written by `swap` (it rewrites only `idRef`, `volRef`,
   `volAcc` and the time stamp), so `β` is constant across bins and across successive same-pool swaps.
   Test: `test_lb_variable_fee_can_fall_below_the_stored_value_so_only_the_base_fee_is_safe`; mutation check: replacing `β`
   by the stored-state total fee is caught by the randomized test.
2. **CL "protocol fees must be handled explicitly" — they cannot move the output, so nothing needs handling.** In
   `concentrated.swap` the protocol delta is taken from `fee_amount` *after* `amount_remaining` and `amount_calculated`
   were updated with `amount_in + fee_amount` and `amount_out`; it only changes `protocol_fee` and `fee_growth_global`, which
   never feed back into a later step. The Agni/FusionX `lmPool` calls are no-ops. The bound reads neither `fee_protocol` nor
   `lm_pool` (except the admission gate). Test: `test_cl_protocol_fee_and_lm_hook_do_not_move_the_output` quotes identical
   amounts under different `fee_protocol` encodings and with/without `lm_pool`.
3. **R2 ("marginal 1 for m=1 at rate 0.997") — reproduces, and it is a CPMM floor effect, not an LB/CL one.** Recomputed
   through `quote_exact_in` on `(1000, 1000)`, 30 bps: `q(1..6) = 0*, 1, 2, 3, 4, 5` (`*` = `insufficient_output_amount`, so
   `q̃(0..6) = 0, 0, 1, 2, 3, 4, 5` as R2 records). `q(2) − q(1) = 1 > r̄·1 = 0.997`: a chunk bound without slack is false.
   Test: `test_r2_counterexample_reproduced_through_the_exact_seam`.
4. **"A proved per-hop slack `s`" — it is not a constant for CL and LB.** For CPMM `s = 1` (an output floor). For LB and CL an
   *input-side* rounding (the fee `ceil`, the net-amount `floor`) is worth one *input* unit, i.e. up to the price in output
   units, and the CL sqrt-price rounding adds `L/2⁹⁶`; so `s` carries the rate (§5.3). The issue's phrase is kept
   (`s` is proved per hop) but the value differs per family.
5. **Evaluator rule.** There is no `evaluation/` package. The plan-token-DAG rule is `routing/evaluator.py::_check_plan` (the last
   lines, `edges.setdefault(step.token_in, set()).add(step.token_out)` then `_token_cycle(edges)` → `"economic token cycle: …"`),
   exposed to solvers as `dag_admission: plan_token_dag` (`routing/algorithms/registry.py`, DESIGN §2.5 "no economic token
   cycle"). WHI-1599 is titled "Add the `single_path_bounded` strategy" in the tracker; the orchestration brief describes it as the
   bound-helper specification. This document serves both (§7).
6. **"Multi-hop composition from monotonicity".** Composition needs only the per-hop bound and `r̄ ≥ 0` (§5.1); monotonicity of
   `q` is needed for the chunk propagation (§5.4).
7. **The external opposite-direction counterexample.** The owner's bundle is unavailable and the archived report only *assumes*
   reverse reserves consistent with forward ones; there is no counterexample to cite. §5.2 reproduces the phenomenon with
   exact quotes and shows the evaluator never executes it.

## 4. Per-family proofs

Common step. If the swap is `OK`, the amounts consumed by its steps add to `x` (`amount_in_consumed == x` is required of an `OK`
CPMM/CL/LB quote). Every proof bounds a step's output by `r × (its gross input)` and sums.

### 4.1 CPMM

`get_amount_out`: `q(x) = ⌊k·x·R_o / (D·R_i + k·x)⌋`, `k = D − f`. Since `k·x ≥ 0` the denominator is `≥ D·R_i`, so
`q(x) ≤ ⌊k·R_o·x / (D·R_i)⌋ = ⌊r̄x⌋` (floor is monotone). This is the tangent at 0 of the concave `g` of
`direct_split_certified.py`, which lies above `g` everywhere and above the floored quote.
Gates mirror `quote_exact_in`: the failed quotes (`UNSUPPORTED` unknown source or wrong fixed fee, `INSUFFICIENT_LIQUIDITY` zero
reserve, `REVERTED` on the post-swap `uint112` overflow) have no output; the bound is not claimed for them.
State transition: `new_state` has reserves `(R_i + x, R_o − q)` so `r̄' ≤ r̄` (§5.2).

### 4.2 CL (`concentrated.swap`, `cl_math.compute_swap_step_exact_in`)

Notation: `ψ` = start `sqrt_price_x96`, `φ = fee/10⁶`, `Q = 2⁹⁶`; the active liquidity of a step is `L`. For a step starting at
`√A`, the *price* is `P_A = √A²/Q²` (token1 per token0) for 0→1 and `ρ_A = Q²/√A²` for 1→0. `a = mul_div(R, 10⁶−fee, 10⁶) ≤ (1−φ)R` is
the remaining input net of fee.

**Guard `G_CL`.** 0→1: `getSqrtRatioAtTick(tick) ≤ ψ`. 1→0: `getSqrtRatioAtTick(tick) ≤ ψ ≤ getSqrtRatioAtTick(tick+1)` (out-of-range
tick ⇒ no bound). Real pools satisfy it (`slot0.tick` is `getTickAtSqrtRatio(ψ)`, or one below when `ψ` sits exactly on a tick boundary
after a downward cross); the corpus does. It is **necessary**: with a frozen `tick` above the price, the loop's first target lies on the
wrong side, `compute_swap_step_exact_in` infers the opposite direction from `cur ≥ target`, and the exact 0→1 quote *out-earns* the naive
rate (`test_cl_tick_consistency_guard_is_necessary`: output 997 498 vs naive bound 996 501 at 10⁶ in).
Note `MAX_SQRT_RATIO = getSqrtRatioAtTick(MAX_TICK)`, so a price at the top clamp is `SPL`-reverted (no output) and `tick = MAX_TICK` has no
bound.

**Lemma CL-0 (direction).** Under `G_CL` every iteration of the loop either goes in the swap's direction or is the zero step. 0→1:
`tick_next ≤ tick` (lte search; after the `MIN_TICK` clamp it is still `≤ tick`, as `tick ≥ MIN_TICK`) so the target `max(limit, ratio(tick_next)) ≤ ratio(tick) ≤ cur`. 1→0:
`tick_next ≥ tick+1` (`(⌊tick/s⌋+1)·s > tick`; the `MAX_TICK` clamp keeps it `≥ tick+1` because `tick+1 ≤ MAX_TICK`), so the target
`≥ ratio(tick+1) ≥ cur`. The invariant is preserved: a step that reaches its target leaves `tick = tick_next − 1` (0→1, price
`ratio(tick_next)`) or `tick = tick_next` (1→0); a partial step consumes the whole remainder and ends the loop. A step with
`cur == target` computes `amountIn = getAmountΔ(target, cur) = 0`, reaches the target, and returns `(in, out, fee) = (0, 0, 0)` — in either
reading of the direction flag. Hence the step prices are monotone: `√A ≤ ψ` (0→1), `√A ≥ ψ` (1→0) at every step.

**Lemma CL-1 (per-step bound).** For each step, `out ≤ (1−φ)·r_A·g` where `g` is the step's gross input (`amountIn + feeAmount`)
and `r_A = P_A` (0→1) or `ρ_A` (1→0).

*0→1, step reaches its target `T`:* `in` is `ν = L·Q(√A−√T)/(√A√T)` rounded up (`get_amount0_delta(..., True)`), `fee_amt = ⌈in·fee/(10⁶−fee)⌉`, so
`in ≤ (1−φ)g`; `out = ⌊L(√A−√T)/Q⌋ ≤ ν·√A√T/Q² ≤ in·P_A`.
*0→1, partial:* `g = R` (`feeAmount = R − amountIn`), and `√n = getNextSqrtPriceFromInput(√A, L, a) ≥ ideal = LQ√A/(LQ + a√A)` — both
branches of `get_next_sqrt_price_from_amount0_rounding_up_add` round the quotient *up* (the second divides by a floored denominator,
which is smaller). Hence `out = ⌊L(√A−√n)/Q⌋ ≤ L(√A−ideal)/Q = a·√A²/(Q·(Q + a√A/L)) ≤ a·P_A ≤ (1−φ)R·P_A`.
*1→0, target:* `in = ⌈L(√T−√A)/Q⌉ ≥ L(√T−√A)/Q`; `out = ⌊⌊LQ(√T−√A)/√T⌋/√A⌋ ≤ LQ(√T−√A)/(√A√T) ≤ in·Q²/(√A√T) ≤ in·ρ_A`.
*1→0, partial:* `√n = √A + ⌊aQ/L⌋ ≤ √A + aQ/L` (floor, for both `a ≤ uint160` and the `mul_div` branch), `out ≤ LQ(√n−√A)/(√A√n) ≤
aQ²/(√A√n) ≤ a·ρ_A`. A zero-liquidity step has `out = 0`.
∎

**Theorem CL.** `out_total = Σ out_j ≤ (1−φ)·r₀·Σ g_j = (1−φ)·r₀·x` with `r₀ = ψ²/2¹⁹²` (0→1) or `2¹⁹²/ψ²` (1→0), since `r_A ≤ r₀` by CL-0.
`out_total` is an integer, so `out_total ≤ ⌊r̄x⌋`. `L`, the ticks crossed, the bitmap range and `fee_protocol` do not enter (§3.2). ∎

### 4.3 LB (`liquidity_book.swap`, `get_amounts`)

Let `p_i = get_price_from_id(i, bin_step)` (Y per X, 128.128), `φ_i = total_fee_i/10¹⁸ ∈ [β/10¹⁸, 0.1]` (`β = get_base_fee`; §3.1).
The loop starts at `active_id` and moves to `findFirstRight` (largest member `< id`, swapForY) or `findFirstLeft` (smallest `> id`), so
visited ids are `≤ active_id` for X→Y and `≥ active_id` for Y→X.

**Lemma LB-1 (per-bin bound).** With `g` the bin's gross input (`amountsInWithFees`), `y` its output reserve, X→Y:
*partial* (`g < maxIn`): `fee = ⌈gφ⌉ ≥ gφ`, `net = g − fee ≤ g(1−φ)`, `out = min(⌊net·p/2¹²⁸⌋, y) ≤ g(1−φ)p/2¹²⁸`.
*Drained* (`g ≥ maxIn`, `g := maxIn + maxFee`): `maxIn = ⌈y·2¹²⁸/p⌉ ≥ y·2¹²⁸/p`, `maxFee = ⌈maxIn·φ/(1−φ)⌉` so `g ≥ maxIn/(1−φ)` and
`out = y ≤ maxIn·p/2¹²⁸ ≤ g(1−φ)p/2¹²⁸`.
Y→X: the same with `2¹²⁸/p` in place of `p/2¹²⁸` (`out = ⌊net·2¹²⁸/p⌋`; `maxIn = ⌈y·p/2¹²⁸⌉`). So `out ≤ g(1−β/10¹⁸)·(price of the bin)`. ∎

**Lemma M (price monotonicity of `pow128`).** Let `b = base/2¹²⁸ ≥ 1.0001` (`get_base`, `bin_step ≥ 1`; `x > 2¹²⁸−1` so `pow128` takes the
"invert" path), `T(id) = 2¹²⁸·b^{id−2²³}` the exact real price and `p(id)` the integer `pow128` result (revert for `|id−2²³| ≥ 2²⁰`). Then for ids `lo < hi`
with `p(lo)`, `p(hi)` defined and **one of them in the guard `[2³⁸, 2²¹⁸]`**: `p(lo) ≤ p(hi)`.
*Error model.* For `y = id − 2²³ < 0`, `pow128` multiplies truncated squares `v_{k+1} = ⌊v_k²/2¹²⁸⌋` of `v₀ = ⌊(2²⁵⁶−1)/x⌋ ≤ 2²⁵⁶/x`; every truncation
rounds down, so `p(id) ≤ T(id)`. With `ρ = 1/T(id)` (all intermediate ideals `≥ T(id)`), `v₀ ≥ u₀ − 2`, `e_{k+1} ≤ 2e_k + ρ`, hence `e_k ≤ 3·2ᵏρ`, and
over at most 20 products `p(id) ≥ T(id)(1 − 2²²/T(id))`. For `y > 0` the same is computed for `|y|` and inverted: `T(1−2⁻¹²⁷) ≤ p ≤ T(1 + 2²³T/2²⁵⁶)`; `y = 0`
gives `p = T = 2¹²⁸`. Using `1 − 1/b > 2⁻¹⁴`:
(A) `hi ≤ 2²³`: `p(lo) ≤ T(lo) ≤ T(hi)/b ≤ T(hi)(1−2²²/T(hi)) ≤ p(hi)` as soon as `T(hi) ≥ 2³⁶`, which the guard gives (`T ≥ p`).
(B) `lo ≥ 2²³`: `p(lo) ≤ T(lo)(1 + 2²³T(lo)/2²⁵⁶) ≤ T(lo)·b(1−2⁻¹²⁷) ≤ T(hi)(1−2⁻¹²⁷) ≤ p(hi)` as soon as `T(lo) ≤ 2²¹⁹`, which the guard gives.
(C) `lo < 2²³ < hi`: `p(lo) ≤ T(lo) < 2¹²⁸ < p(hi)`. ∎
The guard is the *proved* domain, not the observed one: `fixtures/lb_price_scan.py` walks every non-reverting id for `bin_step ∈ {1, 2, 10, 25, 100}` (≈2.9 M adjacent
pairs) and finds **0** non-monotone pairs, inside or outside the guard; `test_lb_price_is_monotone_over_the_whole_proved_window` re-runs the in-guard window in CI.

**Theorem LB.** `Σ g_i = x` for an `OK` quote (the loop ends only when `amountsLeft == 0`; `received == amount_in`), so with Lemma LB-1 and Lemma M (`p_i ≤ p_a` visited for
X→Y, `p_i ≥ p_a` for Y→X, `a = active_id`): `out_total ≤ (1−β/10¹⁸)·p_a/2¹²⁸·x` (X→Y) and `≤ (1−β/10¹⁸)·2¹²⁸/p_a·x` (Y→X); integrality gives `⌊r̄x⌋`. The variable fee,
`update_references`, the volatility cap, `protocol_share` (moved from the fee, never from the output) and bin crossings enter only through `φ_i ≥ β/10¹⁸`. ∎

## 5. Composition, same-direction validity, chunk marginals

### 5.1 Multi-hop composition
For a path of hops `k = 1..n` with `Q(x) = q_n(…q₁(x))`, apply the hop bound at the *actual* intermediate amount `y_k = q_k(y_{k−1}) ≤ ⌊r̄_k·y_{k−1}⌋`:
`Q(x) ≤ x·Πr̄_k`, and the tighter nested form `⌊r̄_n⌊r̄_{n−1}…⌊r̄₁x⌋…⌋⌋` is also valid. Only `r̄_k ≥ 0` is used. A hop that is not `OK` (or a dust leg) makes the plan
invalid. Test: `test_multi_hop_composition_needs_only_per_hop_bounds_on_random_states` and the corpus path replays.

### 5.2 Same direction and the opposite direction
**Same direction (cumulative).** `q_{s_k}(y) ≤ ⌊r̄(s₀)·y⌋` after any number of same-direction swaps through the pool, by induction on the swaps:
CPMM — `new_state` is `(R_i+x, R_o−q)`, so `r̄(s') ≤ r̄(s)`. CL — `ψ` only moves toward the limit and the final `tick` is `getTickAtSqrtRatio(ψ)` or
`tick_next(−1)`, so `G_CL` holds again and the proof of §4.2 applies to `s'` with `r₀' ≤ r₀`. LB — `new_state.active_id` is the last visited bin, so every bin a later same-direction swap visits
is on the same side of the *original* `active_id` and Lemma M (applied against the original `p_a`, whose guard already holds) gives `p_i ≤ p_a` / `p_i ≥ p_a`; `β` is unchanged because `swap` never
writes the static parameters. (`r̄(s')` itself can fall outside the LB guard; that only means the helper has no bound *for `s'`*, the original bound still dominates.) Tests: `test_same_direction_bound_from_the_original_state_dominates_later_swaps[cpmm|cl|lb]`, and on
the corpus.

**Opposite direction is not covered.** After an `A→B` swap the pool's `B→A` rate *improves*: the bound from the original state is exceeded
(`test_opposite_direction_reuse_breaks_the_bound_and_the_evaluator_excludes_it[cpmm|cl|lb]` finds counterexamples in every family). It is outside the feasible set: using a
pool in both directions puts `A→B` and `B→A` into the plan's token graph, and `_check_plan` rejects every cyclic plan **before any pool call** (`"economic token cycle: …"`).
The same test asserts this through `evaluate`, and that same-direction reuse of one pool is admitted and replays on the threaded state.

### 5.3 Chunk marginal (R2) and the slack
Claim: for all `x, m ≥ 0` with `x + m ≤ 2¹²⁷` where `q̃` is defined, `q̃(x+m) − q̃(x) ≤ r̄·m + s`, with `s` of §1.

*Common prefix.* A swap step is *full* when it reaches its target (CL `a ≥ in`; LB `g ≥ maxIn`); its result then does not depend on the remainder (the exactness argument of
`CLPrefixReuse`). Let `J` be the number of leading full steps for `x`; the swaps of `x` and `x' = x+m` are identical for those `J` steps. Let `R = x − C_J` be what `x` spends in step `J+1`
(partial for `x`; `R = 0` if `x` ended at a boundary). The steps `j ≥ J+2` of `x'` obey Lemma CL-1 / LB-1 (`out ≤ (1−φ)r₀g_j`, no floor deficit needed, only the upper bound), so only step `J+1` can carry
slack.

*CPMM.* `g(x) = kR_ox/(DR_i + kx)` is increasing and `g' ≤ r̄`, `q = ⌊g⌋`: `q(x+m) − q(x) < g(x+m) − g(x) + 1 ≤ r̄m + 1`. Exact form `≤ ⌈r̄m⌉`. `s = 1`.

*LB.* In bin `J+1` (same bin, same fee `φ`, same price): `out(x') ≤ (R+m₁)(1−φ)p/2¹²⁸` (Lemma LB-1, `m₁ ≤ m` the extra gross in this bin) and
`out(x) > (R(1−φ) − 1)·p/2¹²⁸ − 1` because `net = R − ⌈Rφ⌉ > R(1−φ) − 1` and the output floor. Difference `< m₁(1−φ)p/2¹²⁸ + p/2¹²⁸ + 1`; later bins add `≤ (1−φ_j)p_j/2¹²⁸` per unit with `p_j ≤ p_a`. `s_{X→Y} = p/2¹²⁸ + 1`; Y→X: replace `p/2¹²⁸` by `2¹²⁸/p`. The `+p/2¹²⁸` term is the price of the one *input* unit that the fee `ceil` can swallow.

*CL.* Write `Γ(a)` for the ideal continuous output of step `J+1` as a function of the net input `a` (0→1: `Γ = a·P_A/(1 + a√A/(LQ))`; 1→0: `Γ = a·ρ_A/(1 + aQ/(L√A))`); `Γ' ≤ r_A`. Then:
`out(x') ≤ Γ((1−φ)g')` (Lemma CL-1 proof: both the full and partial cases bound the output by `Γ` of at most `(1−φ)g'`), and
`out(x) ≥ Γ(a(R)) − ε` with `a(R) = ⌊(1−φ)R⌋ > (1−φ)R − 1`, `ε = 1 + L/Q` (0→1: one floor and `√n ≤ ideal + 1`, the round-up of `getNextSqrtPriceFromAmount0RoundingUp` branch 1)
or `ε = 1 + (L/Q)·ρ_A` (1→0: one floor, nested-floor identity `⌊⌊z/√n⌋/√A⌋ = ⌊z/(√n√A)⌋`, and `√n > ideal − 1` with `Λ' = LQ/y² ≤ (L/Q)ρ_A`). Hence the difference is
`≤ (1−φ)r_A(g'−R) + r_A + ε`, and `s = r₀ + 1 + L̂/Q` (0→1) / `r₀(1 + L̂/Q) + 1` (1→0), with `r₀ = P_0`/`ρ_0` and `L ≤ L̂` over the traversal.
*Domain.* In 0→1, branch 1 of the next-price function (no 256-bit overflow of `a·√A` or of `numerator1 + product`) holds when `x' ≤ 2¹²⁷` and `ψ < 2¹²⁸`; outside it the slack is "no bound" (the rate bound still holds).
*`L̂`.* Any value of the active liquidity the swap can take is `≤ liquidity + Σ|liquidity_net crossed| ≤ liquidity + Σ liquidity_gross`, and `add_delta` caps at `2¹²⁸−1`.

**Mutation checks.** Dropping the `+P` term, the `L̂/Q` term, the whole slack (CPMM `s = 0`; LB `s = 1`), or shrinking any `r̄` by one part in 10⁶ is caught by the randomized tests.

### 5.4 Multi-hop propagation of the slack
With `r̄_k, s_k` per hop and `d_{k−1} = q_{k−1}(x+m) − q_{k−1}(x) ≥ 0`: `q_k(y+d) − q_k(y) ≤ r̄_k d + s_k ≤ r̄_k(r̄_{k−1}…r̄₁m + s_{k−1}+…) + s_k`, so
`s_path = Σ_k s_k·Π_{j>k} r̄_j` and `Q(x+m) − Q(x) ≤ (Πr̄)·m + s_path`. This needs each `q_k` **non-decreasing** (`d ≥ 0`): CPMM (floor of an increasing function), LB (`net = R − ⌈Rφ⌉`
is non-decreasing because `φ ≤ 1`, the drained bin caps at its reserve, later bins add), CL (`a(R)` non-decreasing, partial → full continuous in the output, later steps add `≥ 0`). The tests
assert `q̃(x+m) ≥ q̃(x)` on every chunk pair they check.

## 6. Failure semantics

| Situation | Exact quote | Bound |
| --- | --- | --- |
| unknown/unadmitted source, wrong fixed fee, undeclared CL `lm_pool` | `UNSUPPORTED` | `None` (the proofs are about the admitted migrated code) |
| token not in pool | `UNSUPPORTED_TOKEN` | `None` |
| CPMM zero reserve | `INSUFFICIENT_LIQUIDITY` | `None` (never `0`) |
| CPMM/LB output floors to 0 | `INSUFFICIENT_OUTPUT_AMOUNT` (plan invalid) | unchanged; `floor(r̄x) ≥ 0` is trivially above it |
| CL partial fill / LB out of bins | `INSUFFICIENT_LIQUIDITY` | unchanged (no output claimed) |
| CL missing word/tick, LB missing bins or fee state | `INCOMPLETE_SNAPSHOT` | the CL bound reads only slot0/`fee`, so it is still sound and unchanged; the LB bound needs `static_fee` (else `None`) |
| revert (`uint112`, `SPL`, `FeeTooLarge`, `PowUnderflow`, …) | `REVERTED`/`INSUFFICIENT_LIQUIDITY` | unchanged, or `None` when it is an input of the rate (`β`, `p`, `tick`) |
| LB/CL guard fails (§4) | quote may still be `OK` | `None` |

A bound is never a guess: every `None` above is either a missing input or a state outside the proved domain, and `r̄` is never `0` for an admitted state.

## 7. Specification for WHI-1599 (the production helper)

Return, per `(state, token_in)`, either `None` or a rate with exact integer numerator/denominator (`fractions.Fraction` is acceptable); all arithmetic is integer/rational, **no floats**.

1. `bound_out(rate, x) = (num·x) // den`; the path bound is the product of rates (or the nested floors). The helper must be **pure** and read the *original frozen* state only.
2. CPMM: `(10⁴ − fee_bps)·R_out / (10⁴·R_in)`; gates as in `constant_product.quote_exact_in` (source, fixed fee, reserves `> 0`, `0 ≤ fee_bps < 10⁴`).
3. CL: gates `SOURCES`, the `lm_pool` rule of `concentrated._source`, `0 ≤ fee < 10⁶`; guard `G_CL` costs two `get_sqrt_ratio_at_tick` calls (compute once per state and direction). Rate as in §1.
4. LB: gates `SOURCES`, `static_fee is not None`, `bin_step ≥ 1`, `β ≤ MAX_FEE`; `p = get_price_from_id(active_id, bin_step)` — one `pow128` (~3 µs); `None` on `LBRevert` or outside `[2³⁸, 2²¹⁸]`.
5. Slack (WHI-1598 chunk searches): per §1/§5.3; `None` when the CL domain condition fails or any operand is `None`. Chunk claims are valid for `x + m ≤ 2¹²⁷`; a caller beyond that gets `None`.
6. A pool/direction with `None` is **never** pruned on (it may still be quoted exactly). The helper must not substitute `0`, infinity-as-a-number or a heuristic rate.
7. The bound holds for the *same state* only: after any `new_state`, use the bound of the original state (§5.2), or recompute from `new_state` (never larger).
8. `L̂` may be the universal `2¹²⁸−1` (so `L̂/2⁹⁶ < 2³²`, valid but loose); the tighter `min(2¹²⁸−1, liquidity + Σ liquidity_gross)` costs one pass over `ticks` and is cacheable per frozen state.

## 8. Evidence

Counts are from the recorded run (`ROUTER_CORPUS_BUNDLE` set to the primary clone's frozen corpus, bundle hash `717c21f3…3143`, 143 pools): 29 passed, 0 skipped.

| Criterion | Check |
| --- | --- |
| Hand-derived rates, R2 | `test_hand_derived_rates`, `test_r2_counterexample_reproduced_through_the_exact_seam` |
| Random both directions, boundaries, failure statuses | `test_cpmm_never_underestimates_random_and_boundary`, `test_cl_never_underestimates_random_both_directions`, `test_lb_never_underestimates_random_both_directions`, `test_*_failure_*` |
| Real corpus: every pool × both directions × sampled amounts | `test_corpus_pools_both_directions_never_underestimate[frozen-corpus|tracked-fixture]` |
| Chunk slack | the `Tally.chunk` check inside the random/corpus tests; R2 test |
| Same direction / opposite direction / evaluator | `test_same_direction_*`, `test_opposite_direction_*`, `test_corpus_same_direction_and_multi_hop_composition` |
| LB Lemma M | `test_lb_price_is_monotone_over_the_whole_proved_window`, `fixtures/lb_price_scan.py` |

| Run | Result |
| --- | --- |
| Real corpus, bound | 143 pools × 286 directions × 15 724 `(pool, direction, amount)` quotes: 8 070 `OK` (all bounded, 0 violations), 5 901 `insufficient_liquidity`, 1 349 `incomplete_snapshot`, 404 `insufficient_output_amount`; 8 412 chunk-marginal pairs |
| Tracked real fixture (always run) | 19 pools × 38 directions × 2 096 quotes: 1 133 `OK`; 1 212 chunk pairs |
| Randomized, `OK` quotes (0 violations) | CPMM 47 479; CL 4 943 (A→B) + 5 491 (B→A), 2 540 crossing ≥ 1 initialized tick; LB 6 952 + 6 940, 5 802 sweeping ≥ 2 bins; tightest `out/(r̄x)` = 1.000000000 in all families |
| Chunk pairs (random) | CPMM 63 634, CL 9 990, LB 18 546; the marginal exceeded `r̄·m` in 4 136 / 3 068 / 5 091 of them (so `s > 0` is exercised) |
| Same direction | corpus 1 605 + 208 later quotes after a swap; random 2 611 / 1 904 / 2 874 swaps (CPMM/CL/LB) |
| Opposite direction | counterexamples found: CPMM 367, CL 4, LB 120 (random states); the evaluator rejects the cyclic plan statically |
| Multi-hop | corpus 2 141 + 1 852 replays (2 072 + 1 839 chunk pairs); random 3 331 replays (3 277 chunk pairs) |
| LB price scan | 0 non-monotone adjacent pairs over ≈2.9 M pairs, `bin_step ∈ {1, 2, 10, 25, 100}` |

Corpus amounts per direction: `1, 2, 3, 7`, `2^k (k = 0, 4, …, 96)`, `10^k (k = 0, 2, …, 28)` and a subsample of the corpus cases' own `amount_in` for that token. The corpus is read from
the gitignored `data/` of the primary clone through `ROUTER_CORPUS_BUNDLE`; without it the test *skips* the frozen-corpus parameter (a skip is not evidence) and the tracked 19-pool real fixture
bundle (`tests/fixtures/corpus/bundle`, always run) is the fallback.

## 9. Limits and open questions

- **Looseness.** The bounds are spot-rate bounds: tight at small `x`, loose when price impact is large. A nonlinear (concave) CPMM envelope such as `g` is tighter; whether pruning needs it is WHI-1598/1599's call.
- **LB far from price 1.** Pools whose active price is outside `[2⁻⁹⁰, 2⁹⁰]` get no bound by guard (not observed in the corpus). Lemma M's guard is conservative (the scan finds no violation anywhere).
- **CL `x ≥ 2¹²⁷`.** The rate bound holds for all `x`; only the slack domain is restricted.
- **Chunk slack size.** For tokens with a large rate (≫1), `s` is large (≈ the rate): the slack is an honest bound on integer rounding, not a tuned one.
- **Not proved:** any bound for sources other than the four in §1; the Exact Output direction; non-simple (cyclic) plans — outside the evaluator's domain.
