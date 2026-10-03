# Exact upper-bound pruning contracts for path and chunk searches (WHI-1598)

| Item | Value |
| --- | --- |
| Contract | `R022-Q02/1` |
| Issue | WHI-1598, Release 0.2.2 (`fca63897-b103-46f4-a690-c297246ed1ff`) — consumed by WHI-1599 (`single_path_bounded`), WHI-1600 (`incremental_graph_bounded`, `metis_history_bounded`), WHI-1601 (documentation) and WHI-1602 (exactness and latency measurement) |
| Baseline | `origin/dev` `5aee97e265e5e1afc9948db4c86c6e06be28d84e` (WHI-1597 merged) |
| Consumes | [`output-bounds.md`](output-bounds.md) (`R022-Q01/1`, WHI-1597): `r̄`, chunk slack `s`, "no bound", §7 helper specification |
| Executable contract | [`tests/routing/test_pruning_contract.py`](../../../tests/routing/test_pruning_contract.py) — a test-local pruning model per target, driven against the **actual** reference solvers and the exact quote seam |
| Command | `uv run pytest tests/routing/test_pruning_contract.py -q` (tracked fixtures only; nothing depends on the gitignored `data/`) |

This document says, for `single_path`, `incremental_graph` and `metis_history`, **exactly which
candidates may be skipped before they are evaluated, and why the reference result cannot
change**. It adds no solver and no production module (implementation is WHI-1599/WHI-1600).
Every rule has an equality argument tied to the solver's own code: its tie rule, its caps,
its budgets and its memoization — not to an idealised algorithm. Nothing here is a
performance claim.

## 1. Result

| Target | Rule (skip a candidate when …) | Exact when | Net objective |
| --- | --- | --- | --- |
| `single_path` | **S1**: an incumbent exists and `UB(path) ≤ incumbent score`, `UB` = nested `⌊r̄·⌋` from the longest evaluated prefix (§4) | the bounded run is not budget-truncated (§8.2) | supported (`score ≤ gross` proved, §7) |
| `incremental_graph` | **I1**: the chunk already has a choice and the path's chain bound `⌊r̄·m + s⌋` from its longest memoized prefix is `≤` the chunk's best marginal so far (§5) | the simpler-candidate stage returned an OK plan (P0) and the bounded run is not budget-truncated | supported unchanged (chunk choices are gross marginals) |
| `metis_history` | **M1**: a relaxation **into the target** whose one-hop bound is `≤` the chunk's best marginal (§6.2); **M2**: a relaxation into another token whose `U_h` bound is `≤` the best marginal, **only behind the structural gate `G_M2`** (§6.3) | as I1; M1 is exact even on a chunk the reference itself caps; M2 is exact only where no dominance event and no cap can occur | supported unchanged |

Decisions this document takes (each argued below):

- **Original-state bounds are the right bounds; recomputing from the swapped state is not.**
  The reference chunk searches never score a candidate against committed flow in the opposite
  direction or around a token cycle (`creates_cycle` runs before every scored candidate), and
  they quote the pool's **original** state at the **aggregate** input `x_p + m`. The §5.2
  opposite-direction counterexamples of `output-bounds.md` are therefore unreachable, and its §7
  item 7 ("recompute from `new_state`, never larger") is *unsafe* for these solvers (§3.3).
- **`U_h(v)` is built only for `metis_history`, and only for M2.** In `incremental_graph` paths are
  enumerated, so the exact-path chain bound is never looser than any `U_h` bound and the table
  would be pure overhead. This narrows the wording of WHI-1600 step 1 (§5.3).
- **Label (non-target) pruning in `metis_history` changes the label population, and population
  is observable** through dominance (R2/R3) and both caps (R6/R7). M2 is allowed only where that
  is provably harmless; a frontier-cap counterexample is reproduced (§6.4).
- **No concave CPMM envelope in 0.2.2** (§12.1).
- **Chunk pruning is switched on only if the retained simpler candidate exists** (rule P0): a
  skipped candidate may have been the only source of an `incomplete_snapshot` disclosure, which
  decides the status when nothing else is valid (§5.2).

## 2. Premises that were checked

Each premise of the issue / orchestrator brief was re-derived from the code.

| Premise | Verdict | Evidence |
| --- | --- | --- |
| `single_path` replaces the incumbent only on strict `score > best` | **Holds** (`single_path.py:198`, `best is None or score > best[2]`). `score` is `ObjectiveContext.score(evaluation)`: gross output, net, or `gross − 2²⁵⁷` (§7). An equal-score later candidate changes nothing observable: it is not a new best, so no `report_candidate`; the only state it touches is `prefix_out`/`dead`, which only steer later *skips*. | `test_single_path_replaces_only_on_strictly_greater_score_…` |
| The quote-budget check (`single_path.py:174-176`) is unaffected by a skip | **Partly.** A skip never makes a quote, so the executed-quote *set* of the bounded run is a subset of the reference's, but `_new_quotes_needed` is an over-estimate that reads `prefix_out`, and a skipped candidate leaves no `prefix_out` for its prefixes (§8.2). | `test_single_path_budget_labels`, 3 000 seeded random budget cells |
| Net objective: `score ≤ gross bound` needs every cost term `≥ 0` | **Holds for every shipped mode, and must be guarded.** `synthetic_fixed_cost` rejects a negative cost in `__post_init__`; `empirical_cost` is `ceil(usd·10^d / price)` with `price > 0` enforced by `parse_price_context` and a non-negative fee; unranked plans score `gross − 2²⁵⁷`. A negative cost really does break the prune (negative control). | `benchmark/objective.py`, `snapshot/prices.py:193`, `test_single_path_unsafe_prunes_are_caught` (c) |
| `U_h` over relaxed constraints is a safe superset only if each `r̄` is valid for the state the reference quotes | **Holds, for a stronger reason than §5.2.** The reference quotes `bundle.pools[…]` (the *original* state) at `x_p + m`; the only state it has for a pool is the aggregate `(x_p, f(x_p))`. The opposite direction is excluded before scoring (§3.2). | `test_reference_chunk_searches_never_quote_a_pool_against_its_committed_direction` |
| The reference may score a candidate against earlier aggregate flow in the opposite direction, or around a token cycle | **It cannot.** `creates_cycle(token_edges, path)` (`incremental_graph.py:560`; `metis_history.py:356`, on every label prefix) runs *before* `chunk_scored += 1` / `relaxed += 1` / any quote. A committed `a→b` makes `b→a` close a 2-cycle; a longer cycle needs a path from a new edge's head to its tail. A pool carrying flow is therefore only ever quoted in that flow's direction. | same test (quotes on loaded pools > 0, rejections > 0, both choosers) |
| A bound recomputed from the current state (never larger) is preferable | **No — unsafe here.** The marginal charged is `f(x+m) − f(x)` on the *original* state; the `new_state` of a sequential swap has a smaller rate whose tangent lies *below* the aggregate marginal's slope by the factor `D(R_i+x)/(D·R_i+k·x)`. | `test_recomputing_the_bound_from_the_swapped_state_is_unsafe_…` (CPMM `10⁸/10⁸`, `x = 10⁸`, `m = 10⁵`: the marginal exceeds the recomputed bound by 24) |
| Slack is family-dependent and valid only for `x + m ≤ 2¹²⁷`; `None` = never prune | **Holds.** `r̄` and `s` are available **independently** (CL 0→1 with `ψ ≥ 2¹²⁸` has a rate but no slack; the tracked corpus has such a pool); `single_path` needs only the rate. | `test_a_hop_outside_the_slack_domain_has_no_bound`, `no_bound` counter in the I1 test |
| Removing a label in `metis_history` can change dominance or caps for *other* labels | **Holds, and is the reason M2 is gated.** Dominance (R2 equal amount, R3 strict upward-safe) and the two caps decide which labels exist; a removed label can resurrect one the reference discarded or refused. | `test_label_prune_outside_the_gate_is_unsafe_when_a_frontier_cap_binds` |
| The orchestrator's base | `dev` (title prefix `[0.2.2]` and Release `0.2.2 — Provable upper-bound pruning for exact routing` agree; no tag and no `origin/release/v*` exist, so the bootstrap clause resolves the version-scoped row to `dev`). | `git tag` empty, `git ls-remote --heads origin 'release/*'` empty |

## 3. Shared definitions

### 3.1 Runs, exactness, non-binding

`R` is the reference run (`bound_pruning` off), `B` the bounded run (same case, bundle, objective,
`Budget`, search/graph settings, options). `R∞` is `R` with no budget.

**Exact.** `B` is exact against `R` iff `status`, `plan` (every step, including fund ids),
`evaluation.to_dict()`, `score`, `error`, and the **identical-counter set** of §8.1 are equal.

**A run is budget-truncated** iff its `truncated_by` is `max_candidates` or `max_quotes`
(`single_path`, `incremental_graph`, `metis_history` all report it). `state_cap` (a
`metis_history` frontier/signature cap) is a **cap**, not a budget; M1 is exact across caps
(§6.2), M2 is gated (§6.3). The runner's time limit is outside this contract: a killed worker
yields `last_valid_candidate`, never an exactness claim.

**Claim (§8.2).** *If `B` is not budget-truncated then `B` equals `R∞`; hence `B` equals `R`
whenever `R` is not truncated either.* The converse ("`B` truncated ⇒ `R` truncated") is proved
for the chunk searches and holds up to conservative corners for `single_path` (§8.2); the label
always follows `B`'s own report.

### 3.2 Which bound, from which state

Per `(pool, token_in)`: `r̄` (output-bounds §1) and chunk slack `s` (§5.3), each an exact rational
or `None`. Both are functions of the **original frozen state** only.

- All three solvers quote on the original state (`QuoteCache` memoizes only calls whose state
  `is` the bundle's), so these are the bounds of the very function being quoted: `q(x) ≤ ⌊r̄x⌋` for
  an `OK` quote of `x`, and `q(x+m) − q(x) ≤ r̄m + s` for `x + m ≤ 2¹²⁷` (the chunk marginal).
- Within one path no pool repeats (a path is token-simple), so a pool is quoted at most once per
  path; across chunks it is quoted at the aggregate `x_p + m` in one fixed direction (§2).
- There is no same-direction-reuse step to prove and no opposite-direction case to exclude by
  argument: neither is ever executed by the reference.

### 3.3 Why not "recompute from the current state"

After a committed chunk, `PoolFlow(amount_in = x, amount_out = f(x))` is *accounting on the
original state*, not a swapped `new_state`. The marginal of `m` more input is `f(x+m) − f(x)`.
For CPMM `f'(x) = k·R_o·D·R_i/(D·R_i + k·x)²` while the swapped state's rate is
`k·R_o·D·R_i/((D·R_i+k·x)·D·(R_i+x))`; the former is larger by `D(R_i+x)/(D·R_i+k·x) ≥ 1`. A bound
recomputed from `new_state` underestimates the aggregate marginal as soon as the excess times `r̄·m`
exceeds the slack. It is reproduced exactly in the test (24 output units, with the helper's own slack).

### 3.4 What each bound may be used for

| Quantity | Used by | Needs | `None` when |
| --- | --- | --- | --- |
| `r̄` only | `single_path` S1 | nothing else (absolute quote `q(x) ≤ ⌊r̄x⌋`, no domain) | per output-bounds §1 |
| `r̄` and `s`, with `x_p + m ≤ 2¹²⁷` | I1, M1, M2 | `x_p` = the pool's committed aggregate input | either is `None`, or `x_p + m_ub > 2¹²⁷` (`m_ub` = the bound on the hop's input) |

**Gate rule (all rules).** A hop whose rate, slack or domain is unavailable makes the whole
candidate bound `None`; **`None` never prunes** and is never replaced by `0`, `∞`-as-a-number or a
guess. The number of such events is reported (`bound_no_bound`, §10.4).

### 3.5 Chain bound for a chunk path

For a path `e₁…e_n` and a known exact input marginal `m₀` into `e₁` (the chunk amount, or the exact
marginal of a memoized prefix), with `x_j` the committed aggregate input of `e_j`'s pool:

```
u₀ = m₀;     u_j = ⌊ r̄_j · u_{j−1} + s_j ⌋     (valid iff x_j + u_{j−1} ≤ 2¹²⁷)
```

`m_j ≤ u_j` for every `j`: by output-bounds §5.3 `m_j ≤ r̄_j m_{j−1} + s_j`, `m_j` is an integer,
and the right side is non-decreasing in `m_{j−1}` (`r̄_j ≥ 0`). This is the nested form of §5.4;
it is never looser than the product form `(Πr̄)·m + s_path` and is the one specified.

## 4. `single_path`: rule S1

**Rule.** Enumeration order is hop-major, then adjacency order (`routing.search.enumerate_paths`). For a
candidate `P` that is **not dead-pruned** and **while an incumbent exists**: let `k` be the length
of its longest *evaluated* prefix (the search `_new_quotes_needed` makes over `prefix_out`), `y` that
prefix's exact output (`case.amount_in` for `k = 0`); then

```
UB(P) = ⌊r̄_n · ⌊ … ⌊ r̄_{k+1} · y ⌋ … ⌋⌋        (nested floors; output-bounds §5.1)
skip P  iff  UB(P) is not None  and  UB(P) ≤ incumbent score
```

The skip is placed **after the dead-prefix check and before both budget checks**: a skipped
candidate is counted `pruned_bound`, is **not** `pruned` (dead prefix), is **not** `evaluated`, makes no
quote and records no `prefix_out`/`dead`. Candidates after a budget truncation are untouched
(`truncated_by` is already set).

**Proof of identity (incumbent induction).** Let `I_t` be the incumbent after candidate `t`. Claim:
`I^B_t = I^R_t` for every `t`. At `t`: if `B` skips, then
`score(P) ≤ gross(P) ≤ UB(P) ≤ I^B_{t−1}.score` (the first step is §7; the second is output-bounds
§5.1 on the exact integer outputs of a *valid* plan — a plan with a non-`OK` step has no score, and a
plan with a zero intermediate output is valid with output `0 ≤ UB`). The reference replaces only
on `score > best[2]` (strict), so it keeps `I^R_{t−1} = I^B_{t−1}`. If `B` does not skip, it
evaluates `P` exactly as `R` would — `quote_exact_in` is pure and the cache is a pure memo — and
takes the same decision. When `B` evaluates a candidate that `R` dead-pruned (the recorder of that dead
prefix was skipped in `B`), the candidate fails at the *same* step (a deterministic prefix failure)
and is counted in `failed_candidates`, never as a win. ∎

Consequences:

1. **`≤` is correct because the replacement is `>`.** A tie can never win; skipping on equality
   is what the strict rule licenses. A solver that let ties replace would be *unsafe* under the
   same rule (negative control: `replace_on_tie`).
2. **No incumbent ⇒ no skip.** The first valid candidate always becomes the incumbent (even with
   output `0` or a negative net score), so a bounded run that skips anything has status `ok`, the
   reference's status. A `no_route` / `timeout` / `incomplete_snapshot` result is reproduced by
   construction (it never had an incumbent to skip against).
3. **Direct-first enumeration.** Every 1-hop candidate precedes every 2-hop candidate, so the first
   incumbent is a direct pool when one exists; later parallel direct pools can be skipped against the
   best direct output. The `report_candidate` sequence (new bests) is identical because the incumbent
   sequence is.
4. **Dead prefixes and `prefix_out` are bookkeeping, not results.** A skipped candidate leaves a
   *shorter* evaluated prefix for later candidates (a looser `UB`, never an unsound one) and may
   leave a dead prefix unrecorded (a later candidate then fails instead of being dead-pruned).

## 5. `incremental_graph`: rule I1

### 5.1 Rule

Within a chunk of amount `a` (carry included), candidates are scored in `enumerate_paths` order
(`incremental_graph.py:554-571`): cycle admission, `chunk_scored += 1`, `marginal(path, a, memo)`, then
`if choice is None or m > choice[0]` (strict, first wins). **I1:** after cycle admission and
`chunk_scored += 1` (a skipped candidate is *scored* for budget purposes), if the chunk already
has a `choice` and the path's longest **successfully memoized** prefix gives a chain bound
(§3.5) with `UB ≤ choice[0]`, skip the candidate: no quote, no `memo` entry. A memoized *failure*
prefix is not bounded (the reference answers it for free).

### 5.2 Equality argument

*Chunk by chunk.* Induct on the chunk sequence: the committed flows `F`, token edges `E`, carry
and so the candidate list, admissibility and every `x_p` are equal in `B` and `R`. Within a chunk,
the processed-candidate prefix has the same `choice[0]` in both, by induction over candidates:
`B` skips `c` only if `UB(c) ≥ m(c)` (§3.5; `m(c)` is the exact marginal; valid because (i) `c` was
admitted, so every pool on it is quoted in its committed direction and on its original state
(§3.2), and (ii) the gates of §3.4 hold) and `UB(c) ≤ choice[0]`, so `m > choice[0]` is false in `R`
as well (or `c` fails in `R`, which changes nothing). `choice` is therefore identical, hence so are
the carry decision (`choice[0] == 0`), `commit`, `merged_plan`, the replay and the comparison with
the retained candidate (`score > best[3]`, strict). ∎

*Why `chunk_scored` includes skipped candidates.* `Budget.max_candidates` truncates when
`chunk_scored ≥ max_candidates` at the start of a path; counting a skipped candidate keeps the
truncation points **identical to the reference's**, even when the budget binds. `paths_scored`,
`paths_rejected_cycle` and `paths_truncated` are reference-identical.

*P0 (status).* A skipped candidate may have failed with `incomplete_snapshot` in the reference
and been the only entry of `incomplete`; `incomplete` decides the final status
(`INCOMPLETE_SNAPSHOT` vs `NO_ROUTE`) **only when `best is None`**. Chunk pruning is therefore
enabled for a solve only if `path_split` returned an `ok` plan (so `best` exists and the status is
`ok` in both runs). Otherwise the bounded strategy runs the reference loop.

### 5.3 What is not built, and what is excluded

- **No `U_h` table here.** I1's chain bound is over the *exact path* and the exact prefix marginal,
  so it is `≤` every `U_h`-based bound (which maximizes over a superset). WHI-1600 builds `U_h` for
  `metis_history` only.
- **`graph_reuse=True` is incompatible** with `bound_pruning=True` (`ValueError`): the L05 reuse
  keeps per-(amount, path) results across chunks and its exactness argument assumes every path is
  scored.
- **The embedded `path_split`** (and through it `single_path` and `direct_split`) is not touched:
  the parameter is **not forwarded**. It shares the solve's `QuoteCache` and meter, so any change
  there would move the chunk stage's budget accounting.

## 6. `metis_history`: label relaxation

### 6.1 The code that matters

`choose_history` (`metis_history.py:317-420`): per hop layer `k = 1..label_hops`, labels in
generation order, edges in adjacency order. For each admissible `(label, edge)`: `creates_cycle`
(before anything), `relaxed += 1` (the `max_candidates` unit), then one quote `alloc.step(e,
lab.amount, k)` (the pool's **original** state at `x_pool + lab.amount`). A relaxation into the
target updates `best` (`m > best[0]`, strict, first wins) and **creates no label**; any other
relaxation creates a label that goes through R1–R7:

| Rule | Effect |
| --- | --- |
| R2 | same `(layer, token, visited)` group, same amount ⇒ the **new** label is discarded |
| R3 | a *certified* (upward-safe CPMM region) group in a non-final chunk keeps one label: the larger amount (the older is removed) |
| R6 | `len(group) ≥ max_labels_per_signature`: the lowest amount (latest generation) of the group is dropped, or the new label if it is lowest |
| R7 | `size ≥ max_frontier_labels`: **the new label is refused** |

so which labels exist depends on **which other labels were created** — population is observable.
A chunk's result is `best`; the chunk is `state_capped` iff R6/R7 dropped anything.

### 6.2 Rule M1 (arrival prune) — population neutral, always on

**Rule.** For a relaxation `(lab, e)` with `e.token_out == target`, `m = lab.amount > 0`, and a
`best` already found in this chunk: let `ub = ⌊r̄_e m + s_e⌋` (valid iff `x_e + m ≤ 2¹²⁷`). Skip the
relaxation iff `ub ≤ best[0]`. The relaxation still counts in `relaxed`/`alloc.relaxations` (it is a
relaxation the reference makes); it makes no quote. A relaxation with `m = 0` makes no quote in the
reference either and is never skipped.

**Proof.** In the reference an arrival only (a) updates `best` if `m > best[0]`, (b) is counted. It
creates no label, never increments `order` and is never inserted into a group. Skipping an arrival
with `m_arrival ≤ ub ≤ best[0]` cannot change `best` (strict `>`). Hence every label created, every
R1–R7 decision, every cap drop, the layer order and every counter of the label search other than the
failure disclosures are **identical to the reference's**, including on chunks the reference caps.
The `max_candidates` truncation point is identical (the relaxation was counted). ∎
(Test: `test_arrival_prune_m1_is_population_neutral_…` for the bounded preset, generous caps,
`dominance: off` and a binding 3-label frontier; the preset leaves 14 of 32 reference runs
state-capped and the work counters are equal on all of them.)

### 6.3 Rule M2 (label prune) and the gate `G_M2`

**The table.** For a fixed `(source, target, hops)` and a rate/slack lookup:

```
U_0(v)      = no walk                                  (v ≠ target)
U_h(v)      = max over edges e = (v → w), w ≠ source, whose continuation can reach the target in h−1:
                 r(e) · U_{h−1}(w).r      ,   s(e) · U_{h−1}(w).r + U_{h−1}(w).s
              (maximized separately: r = max product, s ≥ every walk's s_path)
U_h(target) = (r = 1, s = 0)
U_h(v)      = None  if any edge on a walk that can reach the target has no bound.
```

Constraints are relaxed (tokens may repeat, committed-cycle admission is ignored), so every walk
the label search can take is a walk of the table. A second pair `(p, t)` bounds every **amount** that
enters a pool on the way (`p = max(1, r·p')`, `t = max(0, s·p' + t')`, maximized over edges) and is
used only for the domain gate `X_max + max(m, p·ub₁ + t) ≤ 2¹²⁷`, `X_max` the largest committed
aggregate input. A label of amount `m` relaxing `e` into a non-target `v` with `h` hops left has
`ub = ⌊ U_h(v).r · (r_e m + s_e) + U_h(v).s ⌋`, and M2 skips the relaxation iff `ub ≤ best[0]`.
Cost: one edge relaxation per `(token, h, edge)`, reported as `bound_table_cost`; built per solve (it
depends on the request), and only if `G_M2` is open.

**Why a gate.** Skipping a non-target relaxation removes a **label** and everything below it.
Dominance (R2/R3) and the caps (R6/R7) then see a different population; a label the reference
discarded or refused may survive and become the best (§6.4). `G_M2` is the structural condition
under which neither can happen. With `W[k][v]` the number of walks of exactly `k` edges from the
source to `v` that the search could create (never into the source, never out of the target, inside
the distance filter, tokens may repeat — an over-count):

```
G_M2  =   max_k Σ_v W[k][v]  ≤  max_frontier_labels                 (no R7 refusal in either run)
     and  ( dominance == "off"                                      (every label is its own group)
            or  max_{k,v} W[k][v] ≤ 1 )                             (history: every group is a singleton)
```

Under `dominance: off` a group is empty at every insertion (R2/R3/R6 never fire); under `history`
with `W ≤ 1` a (layer, token) is reached by a unique walk, so again no group ever holds two labels.
Both runs then create labels by the same tree rule: **a label exists iff its parent exists** (no
discards, no refusals).

**Proof of identity under `G_M2`.** Let `B` skip relaxation `ρ = (lab, e)` at time `τ` with
`ub ≤ best_B(τ)`. By the table, **every** target arrival below `ρ`'s child, in any run, has value
`≤ ub ≤ best_B(τ)`. The labels of `B` are the tree of `R` minus the subtrees of skipped relaxations,
in the same relative generation order, so the arrivals of `B` are a subsequence of those of `R`. Let
`a*` be `R`'s first maximal arrival (value `M`). If `a*` lay below a skipped `ρ`, then
`M ≤ best_B(τ)`; `best_B(τ)` is the value of an arrival `B` found before `τ`, which `R` found too and
earlier than every arrival below `ρ` — contradicting that `a*` is the *first* maximum. So `a*`
survives, every earlier arrival in `B` is an earlier arrival of `R` (value `< M`), and `B`'s first
maximum is `a*`: same marginal, path and updates. Quote work only shrinks, because `B`'s
relaxations are a subsequence of `R`'s. ∎

**What the gate leaves open.** Under the bounded preset v1 (`history / 1 / 1024`) `G_M2` is open
exactly for requests in which no non-target label is reached by two walks. On the tracked 96-case
corpus fixture that is 84 of 96 requests — and in **all 84 no non-target label exists at all** (they
are direct-pool requests), so M2 has nothing to skip; the 12 closed requests are the 12 no-direct-pool
requests with parallel routes, and run M1 only. M2 therefore matters in practice under `dominance:
"off"` and generous frontiers (WHI-1602 arm A4), not under the preset. A history-mode M2 outside the
gate is **not proved and not specified**: 1 200 seeded random CPMM runs and the tracked fixtures show
no counterexample (`test_history_mode_m2_has_no_counterexample_…`), which is evidence, not a proof.
WHI-1600 must not enable it without extending this contract with a proof.

### 6.4 The tempting unsafe prune (frontier cap)

`cpmm_graph`, `a_d_multi_hop`, `label_hops 3`, `chunks 24`, `dominance: off`, `max_frontier_labels 3`:
the reference fills the 3-slot frontier and chooses `ab_1>db_2` (marginal 862 for chunk 1), final gross
20 600. Forcing M2 skips labels, the frontier never fills, and the search finds `ac_1>cb_2>db_2` (902),
final gross 20 611 — **a better plan than the reference, i.e. not the reference**. M1 alone and M2
behind `G_M2` (closed here: `3 < Σ W`) reproduce the reference exactly.

### 6.5 Budgets, carry, cycle admission, fallback

- `Budget.max_candidates` counts `relaxed` (skipped relaxations included): truncation points are
  identical under M1; under M2 `B`'s relaxations are a subsequence, so `B` truncates only where `R`
  does.
- `Budget.max_quotes`: every quote `B` executes is a quote `R` executes at that time or earlier (same
  flows, same keys, `QuoteCache` memo), so at any moment the executed-key set of `B` ⊆ `R`'s.
  `guarded` raises iff `misses ≥ max_quotes` and the key is uncached. If `B` raises on key `κ`,
  `|B| ≥ max` and `κ ∉ B`; if `κ ∈ R` then `|R| ≥ max + 1`, impossible (`R` never exceeds the budget),
  so `R` had already raised; if `κ ∉ R`, `R` raises on this very call: **`B` truncates ⇒ `R` truncates**.
- Carry, commit, `merged_plan`, the in-solve replay, `creates_cycle` admission before every
  relaxation, the retained `path_split` candidate and its replacement rule are untouched (the skip
  is inside `step`). P0 applies as in §5.2.

## 7. Objectives

| Mode | `single_path_bounded` | `incremental_graph_bounded`, `metis_history_bounded` |
| --- | --- | --- |
| `gross_only` | supported (`score = gross`) | supported |
| `synthetic_fixed_cost` | supported: `score = gross − c`, `c ≥ 0` is enforced by `ObjectiveContext.__post_init__` ⇒ `score ≤ gross` | supported |
| `empirical_cost` | supported: ranked `score = gross − ⌈usd·10^d/price⌉ ≤ gross` (price `> 0`, fee `≥ 0`), unranked `gross − 2²⁵⁷ ≤ gross` | supported |
| any other mode / negative cost | **refused** (`SolveStatus.UNSUPPORTED`, the reason names the mode); a defensive check that every evaluated plan has `estimated_cost is None or ≥ 0` raises `ALGORITHM_ERROR` instead of absorbing a violation | n/a: chunk choices use gross marginals only; the net score enters only the final comparison, which is untouched |

Under a net objective S1 prunes less (a negative or low incumbent score is below every `UB ≥ 0`)
but never wrongly. Tests: shape-dependent cost objectives (`ShapeCost`, including unranked shapes)
match exactly; a **negative** cost (a 2-hop bonus) makes the gross-bound prune choose the wrong plan.

## 8. Exactness, counters and honest labelling

### 8.1 Counters

| | identical to the reference (non-truncated runs) | allowed to differ | new (`search_stats["bound_pruning"]`, §10.4) |
| --- | --- | --- | --- |
| `single_path_bounded` | `candidates_considered` (= evaluated + dead-pruned + skipped = enumerated − truncated), `candidates_truncated`, `paths_enumerated`, `direct_candidates`, `paths_truncated`, `truncated_by`, `best_hops`, `max_hops`, `min_hops_unbounded`, the `report_candidate` sequence | `paths_evaluated`, `paths_pruned`, `quotes_executed`, `quotes_memoized`, `failed_candidates`, `paths_incomplete`, `incomplete_example` | `pruned_bound`, `bound_evaluations`, `bound_no_bound` |
| `incremental_graph_bounded` | `candidates_considered`, `candidates_truncated`, `paths_enumerated`, `paths_scored`, `paths_rejected_cycle`, `paths_truncated`, `chunks*`, `incremental_*` (status, allocation, chunk sequence, shared pools, topology, gross, score, accounting), `path_split_*`, `single_path_score`, `direct_split_score`, `chosen_source`, `topology`, `truncated_by`, `truncated_stages` | `marginal_failures`, `marginal_incomplete`, `incomplete_example`, `quotes_executed`, `quotes_memoized` | as above |
| `metis_history_bounded`, **M1 only** | the chunk-search set above plus `label_relaxations`, `label_rejected_cycle`, `label_pruned_distance`, `label_skipped_revisit`, `label_truncated_chunks`, `labels_*`, `chunks_state_capped`, `peak_*`, `certified_strict_insertions`, `termination`, `evaluations`, `candidates_considered`, `candidates_truncated` | `marginal_failures`, `marginal_incomplete`, `incomplete_example`, `quotes_executed`, `quotes_memoized` (and the same `work` entries in `r021`) | as above, with `bound_table_cost = 0` |
| `metis_history_bounded`, **M2 active** (`bound_pruning.m2.active`) | the chunk-search set except the label-population counters; `evaluations` is unchanged | additionally `label_relaxations`, `label_rejected_cycle`, `label_pruned_distance`, `label_skipped_revisit`, `labels_*`, `peak_*`, `candidates_considered`, `candidates_truncated` | as above, `bound_table_cost > 0` |

Plan, evaluation, score, status and `error` are **never** in the allowed-to-differ column. Failure
disclosures (`failed_candidates`, `marginal_failures`, `paths_incomplete`, `marginal_incomplete`)
never contain a skipped candidate; `pruned_bound` is a separate counter and is never added to
`paths_pruned` (dead prefixes) or to any failure reason.

### 8.2 The budget lemma and the label

**Lemma B.** If `B` is not budget-truncated then `B = R∞` (plan, evaluation, score, status).
*Proof:* the identity arguments of §§4–6 never use a budget; an untruncated `B` is the identity
argument over the whole candidate set.

**Corollary.** `B = R` whenever `R` is untruncated, because then `R = R∞`.

**Converse (a warning, not a label input).** "`B` truncated ⇒ `R` truncated" is proved for the chunk
searches (§6.5: the `max_candidates` points are identical for I1/M1; the key-subset argument for
`max_quotes`) and for `single_path` `max_candidates`, where `E_B(c) ≤ E_R(c)` (count of evaluated
candidates; an injection from `B`'s extra evaluations — those `R` dead-pruned behind a skipped recorder
— to the skipped recorders). It has **two unproved corners** in `single_path`: (a) a candidate that
`R` dead-prunes but `B` reaches at a binding budget check is truncated in `B` only (a *conservative*
difference: `B` reports truncation where `R` would not); (b) `_new_quotes_needed` over-estimates from
`prefix_out`, and a skipped candidate leaves a shorter evaluated prefix, so the estimate can differ when
a quote key coincides across paths. Both can only make `B` **report** truncation that `R` did not;
neither can make `B` claim exactness it lacks. Neither occurred in the 3 000 seeded random cells nor on
the fixtures.

**Label** (`search_stats["bound_pruning"]["exactness"]`):

| `label` | when | meaning |
| --- | --- | --- |
| `exact` | `B.truncated_by` is `None` (for `metis_history_bounded` `state_cap` is allowed) | `B = R∞`, and `= R` whenever `R` is untruncated |
| `not_exact_budget_binding` | `B.truncated_by ∈ {max_candidates, max_quotes}`; `binding` lists which | no claim: a truncated bounded run and a truncated reference may stop at different points (the bounded run does less work per candidate, so it may go further) |

A missing bound (`bound_no_bound > 0`) does **not** change the label — it never prunes — and is
reported in the counter so a reader sees the pruning was partial. A case labelled
`not_exact_budget_binding` is **never** counted as exactness evidence, and WHI-1602 reports
`bounded truncated ∧ reference not truncated` as its own count (target `0`, else each case is listed).

## 9. Exclusions

| Excluded | Reason |
| --- | --- |
| SOR route × percent tables (`uni_sor_port`, `uni_sor_fast`, `uni_sor_adaptive`, `uni_sor_optimized`, `uni_sor_cycle_safe`) | Parity-tested ports and recipes; their candidates are route × percent samples ranked and then combined by a chooser, not a monotone max over independent candidates; the SOR parity contract forbids changing results; no equality argument against the pinned TypeScript is available. |
| `path_split` | Already carries its own sampled-output bound (`samples_bound_pruned`, `entries_rank_pruned`) and an exact branch and bound over the kept table; its bound rests on a concavity assumption and one unit of rounding — a different (non-`r̄`) argument. The parameter is not forwarded into the embedded `path_split` of the chunk strategies. |
| `direct_split`, `direct_split_certified` | Allocation over a grid of direct pools (`direct_split_certified` has its own certified bound and certificate). Not a max over independent paths. |
| `metis_inspired`, `incremental_graph_repair` | Out of scope of the release (WHI-1600 out of scope); `metis_inspired` shares `_Allocator.step` but `bound_pruning` is not offered there. |
| `cfmm_dual` | A convex solve, not a candidate search; there is no candidate to skip. |
| `graph_reuse=True` together with `bound_pruning=True` | Unproved interaction (§5.3); refused. |
| `metis_history` M2 outside `G_M2` | Not proved (§6.3) and unsafe under a binding frontier cap (§6.4). |
| Exact-output direction; non-simple (cyclic) plans; sources other than output-bounds §1 | Outside the bound's domain (output-bounds §9). |

## 10. The frozen interface (WHI-1599 – WHI-1602 follow it literally)

### 10.1 Strategy IDs

`single_path_bounded` (WHI-1599), `incremental_graph_bounded` and `metis_history_bounded` (WHI-1600).
Registered in `ALGORITHMS`; **not** in `BASE_STRATEGIES`/`OPTIMIZED_STRATEGIES`; `custom` group;
`--strategies all` appends them **after the existing roster, in this order** (a new `R022_ADDITIONS`
after `R021_ADDITIONS`): 14 → 17 IDs. `--strategies profile` replays literally; saved earlier profiles
never gain them. Capabilities, `search_params` and `graph_params` equal the reference's.

### 10.2 The default-off parameter

`bound_pruning: bool = False`, keyword-only, on `single_path.solve`, `incremental_graph.solve` and
`metis_history.solve` (the L05 `graph_reuse` precedent: selected only by an explicit caller; the
reference `FACTORY.solve`, the runner and every profile never pass it). A bounded strategy's factory is
a module-level function (workers pickle factories by reference) calling the reference solver with
`bound_pruning=True`.

With the parameter off: no `bound_pruning` key in `search_stats`, no new object in the prepared
result, no new import side effect — **records are byte-identical to today's** (the existing tests of
the three solvers and the 0.2.1 baseline records are the check). `bound_pruning=True` with a prepared
object that carries no bounds raises `TypeError`, like the reference's own prepared check. It is not
forwarded to the embedded `path_split`. `incremental_graph` raises `ValueError` for `bound_pruning`
together with `graph_reuse`.

### 10.3 Bound helper, preparation

Per output-bounds §7 plus: the result per `(pool_id, token_in)` is `OutputBound(rate: Fraction,
slack: Fraction | None)` or `None`; `rate` and `slack` are **independently** available (S1 uses the
rate alone). `L̂ = min(2¹²⁸−1, liquidity + Σ liquidity_gross)` (the tighter choice, one pass over
`ticks`, computed once). All of it is built **eagerly in `prepare`** from the frozen bundle (an
immutable `MappingProxyType`) and charged to the preparation step; `prepare` records
`{pool_directions, bounded, rate_only, no_bound, seconds}` in the prepared object, surfaced under
`bound_pruning.prepare`. `U_h` is per solve (§6.3).

### 10.4 Options, provenance, `search_stats["bound_pruning"]`

- **Options:** `single_path_bounded` and `incremental_graph_bounded` accept **none** (`refuse_options`,
  as their references). `metis_history_bounded` accepts exactly `metis_history`'s `algorithm_options`
  (same keys, ranges, validator), and the `settings_sha256` of the preset options is the **same**
  (`183bb1ff…`), so paired comparisons are same-settings. The pruning is **not** an option (it would
  change `settings_sha256`); the strategy ID carries it. Under `all` it receives the same preset values
  and the same `metis_graph_settings()` copy as `metis_history`.
- **Provenance** (`AlgorithmFactory.provenance`): `experimental: true`, `opt_in: true`, `issue`,
  `reference` (the reference ID), `contract: "docs/references/research-022/pruning-contract.md
  (R022-Q02/1)"`, `identity`: *"exact acceleration of `<reference>`; identical plan under non-binding
  budgets; not a new heuristic"*, `claims` (identity as in §8), `not_claimed`: any speedup, identical
  counters, exactness under a binding budget, M2 outside its gate, any objective other than §7,
  optimality.
- **`search_stats["bound_pruning"]`** (present for the bounded strategies only):

```
{
  "contract": "R022-Q02/1",
  "reference": "<reference id>",
  "rule": "S1" | "I1" | "M1" | "M1+M2",
  "pruned_bound": int,          # candidates (S1, I1) or relaxations (M1, M2) skipped
  "bound_evaluations": int,     # upper-bound computations made (a skipped candidate costs >= 1)
  "bound_no_bound": int,        # computations that had no bound (None / domain): never pruned
  "bound_table_cost": int,      # U_h edge relaxations (M2 only; 0 otherwise)
  "prepare": {...},             # §10.3, from `prepare`, constant per bundle
  "p0": bool,                   # I1 / M*: the retained simpler candidate existed (pruning enabled)
  "m2": {"active": bool, "gate": "open" | "closed:frontier" | "closed:dominance" | "n/a"},   # metis only
  "exactness": {"label": "exact" | "not_exact_budget_binding", "binding": [..]}
}
```

  `candidates_considered`, `candidates_truncated` and every reference key keep their reference
  meaning (§8.1).

### 10.5 Objective validation, runner, replay

Objective handling is §7. A bounded strategy appears in `main.py run|quote` (one solve per selected
strategy; `--details` shows `bound_pruning`), `report`, `replay` and `order-check` as a separate
strategy with its counters; a saved record of it replays literally.

## 11. WHI-1602 measurement protocol (pre-registration)

Registered here, before any bounded strategy exists. The WHI-1602 schedule file
(`config/research_022/…`) must be committed and pushed **before its first tuning observation**, and the
report stage must run from a clean clone of that commit. Changing a rule below is a documented
amendment made before analysis (the 0.2.1 rule, `research-021/preregistration.md` §3).

### 11.1 Roster, splits, exposure

- The roster is the 17 IDs of §10.1, derived from the unchanged `config/full_gross.yaml` (`gross_only`,
  `search` 3 hops / 4 splits / 5 %, `graph.chunks 50`, the 0.2.1 budget, seed 1447, fixed order). Each
  bounded strategy runs **in the same run, with the same profile values and budget** as its reference.
- **Tuning split** (`bundle_tuning`, `sor_cohort_tuning`; 96 cases each) is for checks: implementation
  defects, gate behaviour, budget choices. **Report split** (`bundle_report`, `sor_cohort_report`; 302
  cases each) is executed **once**, after the schedule is frozen, never tuned on, and labelled
  `previously_exposed` as in R021-C/1 §6.1. The only re-execution is the infrastructure retry of the
  0.2.1 deviation rule (a completed run is never repeated). The L01 matrix (9 tuning cases + 15
  held-out twins) and the sentinel (`USDC → USDT 1000`) serve latency.
- **Arms** (each on both splits): `A1` the roster, `gross_only`; `A2` the `config/full.yaml`
  empirical-cost objective (`single_path_bounded` supported; the two chunk strategies
  objective-independent; every identity gate evaluated); `A3` `metis_history` / `metis_history_bounded`
  with `{dominance: history, max_labels_per_signature: 1000000, max_frontier_labels: 10000000}`; `A4`
  the same with `dominance: "off"` (the arm where `G_M2` is open for requests with parallel routes, i.e.
  where M2 can act); `A5` binding-budget runs (§11.3).

### 11.2 Exactness (the gate; deterministic)

On every non-truncated cell of `A1–A4` (both runs `truncated_by` ∈ {`None`, `state_cap`}): status,
`plan` (canonical JSON), `evaluation`, `score`, `error` and the identical-counter set of §8.1 are equal.
**Any difference is a blocking defect**, filed against the strategy, never a tuning input. Reported per
strategy: cells compared, cells identical, cells excluded as truncated (listed), and `bounded truncated
∧ reference not truncated` (target `0`). The 14 reference IDs are additionally compared to the 0.2.1
baseline records (WHI-1562) on the report split: statuses, scores, plans and the `search` keys of the
references must be unchanged (the parameter-off guarantee).

### 11.3 Binding budgets

`A5` uses registered small `max_quotes` and `max_candidates` values chosen on the tuning split from the
observed reference counts (e.g. the 25th and 50th percentile of the reference's own `quotes_executed`);
the values are fixed in the schedule. Results are reported **separately** under
`not_exact_budget_binding`, never mixed into exactness evidence, and show what the bounded run did with
the budget the reference exhausted (it may reach a better plan).

### 11.4 Deterministic work (primary evidence)

Per strategy pair, per case family (cohort; direct-pool vs no-direct; pool-family mix of the plan;
stratum; pair), as sums **and** per-case distribution (n, p50, p90, max), each unit separately — units
are never added or divided across units, and missing is never `0`:

| Unit | Source |
| --- | --- |
| `quotes_executed` (reference, bounded, same-unit ratio) | solver `search_stats` (authoritative) |
| `paths_evaluated` / `paths_scored` / `label_relaxations` | `search_stats` |
| `pruned_bound`, `bound_evaluations`, `bound_no_bound`, `bound_table_cost`, `bound_prepare` | `bound_pruning` block |
| CL `swap_steps`, LB `lb_bins_swapped` summed over **executed** quotes | a separate instrumented *work pass* (not timed): a harness wrapper around the executed-quote call (`pools.quote.quote_exact_in` as bound in `routing.search` / `routing.evaluator`), summing each result's `features`; never inside a timed run |
| skipped fraction `pruned_bound / candidates_considered` | derived |

The headline work claim is the sign and size of `quotes_executed`, CL steps and LB bins saved **beside**
`bound_evaluations + bound_table_cost + bound_prepare` as separate columns (the cost of the bound is
reported, not netted against quotes). The bound's tightness is not measured; a low skip fraction with
many `bound_evaluations` is the registered trigger for the §12.1 follow-up, not a defect.

### 11.5 Timing (secondary, gated)

Timing is attempted only after §11.2 has passed and uses L01's rules (`config/latency/l01.yaml`,
`report.latency compare`) verbatim: warm process, 1 warm-up and 5 repeats, orders `fixed` and
`reverse`, the A/A noise floor of the same pair. **Quiet-host gate** (R021 §3, §5.3): the launch records
5 one-minute load samples 30 s apart and does not launch if any exceeds 3.0 (headroom only); the stage
holds a `caffeinate -i -m -s` assertion and captures the `pmset -g log` sleep/wake events. A timing
window with any sampled one-minute load `> 0.5 ×` logical CPUs (5.0 on the 10-core host), any host
`Sleep` transition, or no load sample or no sleep log is **inconclusive**. An inconclusive window yields
no speed claim and no timing disposition; a passed window yields the L01 verdict (`improvement` /
`no_difference` / `regression` against the noise floor).
**Cold vs warm:** *warm* = `solve` only in a warm worker (bound preparation not charged: it is
amortized); *cold* = a fresh spawned worker per `(strategy, case)` with `prepare` **plus** `solve`
charged (the single-request condition; `quote --details` checks, one solve per selected strategy).
`prepare` is reported as its own number for the bounded strategy and its reference; the extra
preparation, the per-request saving and the break-even request count are reported descriptively. A
separate `tracemalloc` memory pass is never headline latency.

### 11.6 Disposition

`reject` if §11.2 fails on any non-truncated cell, or a bounded strategy returns `invalid_plan` /
`algorithm_error` where its reference did not; `inconclusive` if §11.2 holds but a registered question
cannot be evaluated (no cell with `pruned_bound > 0`, missing records); otherwise `keep_experimental`
with the statement "work reduction only", unless §11.5 passed and the verdict is `improvement`, in which
case "work and latency". No default is changed and nothing is adopted.

## 12. Decisions, limits, open questions

### 12.1 The concave CPMM envelope: not wanted in 0.2.2

(`output-bounds.md` §9; `direct_split_certified.g`.) Reasons: **(1)** it exists only for CPMM; a path
mixing CL/LB hops needs the linear rate for those hops anyway; **(2)** for a CPMM hop the bound costs
about as much as the exact quote (`g` and `get_amount_out` are both O(1) integer arithmetic) — the
savings of bound pruning come from skipping *expensive* CL/LB quotes, which a tighter CPMM bound does
not touch; **(3)** a nonlinear envelope needs its own chunk-marginal and slack proof (§5.3 of
output-bounds is for the linear rate), and nested envelopes are not the spot rate that the helper
specifies; **(4)** it would add a second bound family to two contracts that are otherwise one formula.
Revisit only if WHI-1602 reports low skip fractions that the *looseness* of `r̄` (not the gates)
explains.

### 12.2 Limits

- **Spot-rate looseness.** `r̄` is tight at small `x` and loose under price impact: a candidate is
  skipped only if its spot-rate ceiling is below the incumbent's *realized* output, so the skip
  fraction rises as chunks shrink relative to pool depth. On the tracked `cpmm_graph`, `a_b_large`
  (150 M into 200 M reserves) skips 0 of 36 candidates at 6 chunks, 24 of 100 at 10, 188 of 1 000 at
  100; `a_b_small` skips 6 of 36, 50 of 100 and 600 of 1 000. Over the 48 fixture cells a third of the
  scored candidates are skipped (§13).
- **Slack carries the rate.** For a token with a large rate the chunk slack is large (output-bounds §9);
  small chunk amounts in raw units fail to prune for that reason (`a_b_dust`, 3 units: 0 skips with the
  slack; the slack-free variant, which §3.5 forbids, would skip 16).
- **M2 history-mode** is unspecified (§6.3). **`single_path` budget corners** (§8.2).

### 12.3 Open questions for the implementers

1. Does the profile loader accept two factories pinning the same preset file for `metis_history` /
   `metis_history_bounded`? If not, copy the file under a new key with identical options (the hash of the
   normalized options, hence `settings_sha256`, is unchanged).
2. WHI-1602 needs CL steps and LB bins over executed quotes: the wrapper of §11.4 is the proposed seam;
   a runner seam is acceptable if it adds no work to timed runs.

## 13. Evidence

All counts are from the tracked fixtures (`tests/fixtures/routing/cpmm_graph`, `mantle_mixed`,
`tests/fixtures/corpus/bundle`: 19 real pools, 96 cases); nothing skips in a worktree.

| Claim | Check |
| --- | --- |
| Tie rule and S1 exactness on every objective | `test_single_path_replaces_only_…`, `test_single_path_prune_is_exact_and_not_vacuous_on_every_objective` (192 cells: 1 376 candidates, 354 skipped, quotes 1 472 → 1 109) |
| Underestimate / tie-replace / negative-cost controls | `test_single_path_unsafe_prunes_are_caught` |
| Budget label (single) | `test_single_path_budget_labels`, `test_single_path_budget_labels_on_random_graphs` (3 000 seeded cells, > 300 binding, no violation in either direction) |
| The reference never scores against flow / a cycle | `test_reference_chunk_searches_never_quote_a_pool_against_its_committed_direction` |
| Driver fidelity | `test_the_reference_driver_reproduces_incremental_graph`, `…_metis_history` |
| I1 exact, carry and split exercised | `test_incremental_chunk_prune_equals_the_reference_and_is_not_vacuous` (48 cells: 6 710 scored, 2 213 skipped, quotes 2 636 → 2 210); `…_objective_independent`; `test_chunk_prune_with_an_underestimating_rate_is_caught` (43 of 48 cells differ) |
| Slack is required | `test_chunk_slack_is_required_a_bound_without_it_prunes_the_true_marginal` (R2) |
| M1 neutral under preset / generous / off / capped | `test_arrival_prune_m1_is_population_neutral_…` (4 option sets × 32 cells; 838 / 1 253 / 1 548 / 1 494 relaxations skipped; every work counter equal) |
| `G_M2`, M2 exact behind it, unsafe beyond | `test_m2_gate_…`, `test_label_prune_m2_is_exact_behind_its_gate_…`, `test_label_prune_outside_the_gate_is_unsafe_…`, `test_history_mode_m2_has_no_counterexample_…` (evidence only) |
| `U_h` exact maximum and dominance of real marginals | `test_u_table_matches_a_brute_force_walk_maximum`, `test_u_table_and_hop_bounds_dominate_every_real_path_marginal_…`, `test_the_dominance_check_catches_an_underestimating_rate_or_slack` |
| Budget label (chunk searches) | `test_chunk_search_budget_labels` |
| Original vs swapped state | `test_recomputing_the_bound_from_the_swapped_state_is_unsafe_…` |

The pass/skip counts of the final run are recorded in the pull request body.
