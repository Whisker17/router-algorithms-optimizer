# History/admission-aware label pruning for `metis_history` (R021-P03, WHI-1549)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` ([`contract.md`](contract.md)); this memo narrows the `metis_history` row (§11 obligations) and changes no shared schema |
| Publication key | `R021-P03`, WHI-1549, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository base | `origin/dev` `42cbf595593b996d2f15d4d7176521a93f6ba241` (B = `81559ab` plus WHI-1547's contract; `routing/`, `pools/` unchanged since B) |
| Outcome | **`narrow_go`** (§10) |
| Records | [`fixtures/history-labels.json`](fixtures/history-labels.json) (new counterexamples, the pinned real-corpus H4 classification, bounded probe summary) |
| Executable check | `uv run pytest tests/routing/test_history_labels_contract.py -q` |
| Downstream | WHI-1550 (`metis_history` implementation); amendment text in §11 |

This memo replaces `metis_inspired`'s amount-only "one label per (layer, token)" rule with a
selector whose discards are proven, whose unknowns are retained and whose resource caps are
visible. It implements no strategy: the normative selector lives as an executable
specification in `tests/routing/test_history_labels_contract.py` (`choose_history`), on the
actual `metis_inspired._Allocator` step/commit semantics. `metis_inspired`, its profiles and
all historical records are unchanged. Nothing here is Jupiter Metis.

**What is claimed (only this).** For one chunk, on an identical committed state, with no cap
or budget truncation in that chunk, the selector's chosen marginal equals the maximum over
every admissible simple path within `graph.label_hops` (the same-depth enumeration `E_H`),
in every admitted protocol (Theorem T1, §3). **What is not claimed:** whole-plan optimality,
improvement over `metis_inspired` (`L_H`) or `incremental_graph`, identical tie choices or
trajectories with `E_H`, any speedup, or exactness of a capped chunk (§3.6, §2.6–§2.7).

Evidence classes (R021-C/1 §1) stay apart: *repository evidence* (actual solver, evaluator,
frozen WHI-1449 artifacts), the *external proposal* (archived report, §2.1/§2.2/N1),
*new reconstructions* (this issue's fixtures, independent oracle) and a *bounded tuning
probe* (§9.4; a separate diagnostic pass, never a measured solve, no timing claim).

## 1. Sources

| Source | Pin | Used for |
| --- | --- | --- |
| `routing/algorithms/metis_inspired.py` | at base | `_Allocator.step/commit/choose_labels/choose_enumeration`, `hops_to_target`, `diagnose_case` (read-only) |
| `routing/algorithms/incremental_graph.py` | at base | `creates_cycle`, `PoolFlow`, `chunk_amounts`, `merged_plan` |
| `routing/evaluator.py` | at base | whole-plan replay and the plan-token-DAG check |
| `pools/constant_product.py` | at base | CPMM failure rules used by lemma L3 (dust floor, `uint112` overflow revert, input-independent failures) |
| `pools/concentrated.py`, `pools/liquidity_book.py` | at base | failure statuses that make CL/LB not upward closed (`incomplete_snapshot`, `insufficient_liquidity`) |
| External report | [`sources/research-report.md`](sources/research-report.md), SHA-256 `0588e83a…ba1d9` | §2.1 (R1), §2.2 (amount top-k), N1 (signature proposal) — external claims only; its `verify_research.py`/`results.json` are absent and were not run |
| WHI-1547 reconstructions | [`fixtures/reconstructions.json`](fixtures/reconstructions.json) R1, R7 (X4), R8 (X4b) | pools and amounts |
| WHI-1449 H4 diagnostic | `…/router-algorithms-optimizer-artifacts/metis-challenge/campaign-h4diag-446674e-20260928T185905Z/h4-diagnostic.json`, SHA-256 `a0306f02…96d9a` (read-only, re-hashed) | real-corpus L4 loss classes |
| `bundle_tuning` | `ee7afa7e…279b` (96 cases, 143 pools: 84 CL, 45 LB, 14 CPMM; 8 tokens), copy at `…/metis-challenge/inputs/bundle_tuning` | bounded probe (§9.4) |

## 2. Counterexamples and their actual classifications

Each row is reproduced by a named test on the actual allocator/solver and the unchanged
evaluator; "oracle" is this issue's independent exhaustive enumerator (§9.3).

### 2.1 R1 (external report §2.1) — legal prefix loss, the walk is out of domain

Input 10,000 A→T, 4 hops, fee 30. Actual `metis_inspired` (L4, 1 chunk): incremental gross
**9,938** (A–B–T); at C the via-B label (11,926) replaced via-D (9,840) and via-B cannot
continue C→B (`label_skipped_revisit > 0`); `diagnose_case`: `{token_revisit: 1}`. E4
(`label_pruning: false`) and the history selector: **19,560** on A–D–C–B–T, which repeats no
token and is evaluator `ok`. The 23,708 walk A–B–C–B–T is an evaluator `invalid_plan`
(economic token cycle): a genuinely cyclic, out-of-domain candidate, never a target.
Test: `test_r1_actual_label_search_discards_the_legal_prefix_and_the_walk_is_invalid`.

### 2.2 X4 (R7) — token-revisit loss of a simple path

`diagnose_case` at label_hops 4: `{token_revisit: 1}`; enumeration maximum
S–A–X–Y–D (1,995,991,039), label choice S–Y–D (998,998,253). The lost path is simple; the
dominant prefix S–Y–X had visited Y. History selector: S–A–X–Y–D, oracle `agree`.
Test: `test_x4_token_revisit_is_a_legal_prefix_loss_the_signature_recovers`.

### 2.3 X4b (R8) — prefix-dependent admission loss of a legal continuation

Two chunks. `diagnose_case`: `{agree: 1, prefix_admission: 1}`. Chunk 2's dominant label
S–A–V (visited {S, A, V}) cannot continue V→X (it would close the committed X→A→V→X cycle);
the dominated S–B–V (visited {S, B, V}) continues to S–B–V–X–D. L4 puts both chunks on
S–X–A–D; the history selector and E4 put chunk 2 on S–B–V–X–D (higher evaluated gross;
oracle `agree` on both chunks, L4 `miss` on chunk 2). With the selector mutated to
metis_inspired's own rule (one label per token, amount-only dominance) its trajectory is
exactly L4's. Test: `test_x4b_prefix_admission_is_a_legal_prefix_loss_the_visited_set_recovers`.

**Classification (D3 settled).** R1, X4 and X4b are all legal-prefix losses; none is a
cyclic optimum. X4 and R1 are token-revisit (the continuation needs a token the dominant
prefix visited); X4b is prefix admission. Both are repaired by the visited-set signature
alone (lemma L2).

### 2.4 Failure domains: a larger amount is not always more quotable

| Record | Actual behavior | Selector | Unsafe rule |
| --- | --- | --- | --- |
| X2 (real Uniswap v3 USDT/WMNT state from `mantle_mixed`) | the larger S→USDT label's continuation is `incomplete_snapshot` (collected tick range); the smaller fits | CL is not certified: both labels retained (`labels_retained_unknown`), oracle `agree` | forced strict dominance and L3: `miss` |
| `overflow` (NEW, Moe Classic v1 sourced pair near `uint112`) | `quote_exact_in` on `xd` is `ok` at 9,871,580 X and `reverted` at 9,969,900,600 X | `xd` fails the static overflow bound (§3.4), not certified; gross **4,920,982** = E3 | forced strict and L3: **9,871** (weak direct pool) |
| dust | a positive input whose output floors to 0 is `insufficient_output_amount` (R2) while a 0-marginal step is a no-call success | rule R5 (§4) | — |

Tests: `test_x2_larger_amount_fails_on_real_cl_state_and_the_smaller_label_is_retained`,
`test_sourced_cpmm_overflow_makes_a_larger_amount_fail`,
`test_final_chunk_uses_equal_amount_dominance_only`.

### 2.5 Amount-only top-k is not a repair (external §2.2, reconstructed NEW)

The report did not specify its construction. Reconstruction: R1 plus k−1 parallel A→B
pools `ab_i` with reserves (10⁹ + i·10⁶, 10⁹). An amount-only top-k beam per (layer, token)
fills C with k via-B labels, all of which visited B: it returns **9,938** for k = 1, 2, 4, 8.
The history selector returns **19,560** with one label per signature, oracle `agree`.
Test: `test_amount_only_top_k_is_not_a_repair_but_the_signature_is`.

### 2.6 Equal chunk value, different state (`tie_state`, NEW)

Pools p1, p3 (S→X, p3 slightly deeper) and a thin q (X→D) that rounds both chunk-1
amounts to the same output; 20,000 S→D, 2 chunks, 2 hops. Certified strict dominance keeps
p3's larger X label; enumeration keeps the first maximal path via p1. Chunk 1: equal
marginal (`tie`); chunk 2 agrees on each trajectory's own state; final evaluated gross
**9** (selector) vs **8** (enumeration). A per-chunk value claim says nothing about the
tie choice or the next chunk's state.
Test: `test_equal_chunk_value_different_state_changes_the_next_chunk`.

### 2.7 Per-chunk exact is not whole-plan better (`greedy_trap`, NEW, and real corpus)

Synthetic (8 CPMM pools, 100,000 S→D, 2 chunks, 4 hops): S4 and E4 both take chunk 1's
exhaustive maximum S–E–B–A–D (oracle `agree` 2/2); its committed B→A edge blocks chunk 2's
S–E–A–B–D. L4 misses chunk 1 (`token_revisit`) with S–E–A–D and can then use S–E–A–B–D:
L4 **12,757,712** vs S4 = E4 **9,943,929**.
Test: `test_per_chunk_exact_choice_is_not_whole_plan_optimal_or_better`.

Real corpus (frozen H4 diagnostic, pinned in the records file): on the 12 tuning cases
where L4 and E4 differ, 600 chunks classify as agree 398, tie 174, token_revisit 22,
prefix_admission 6. The L4 − E4 whole-plan gross is **positive on 7 of 12** cases, including
`emp-09bc4e-78c1b0-low-1` (+97,234,747,446,109 raw) and `emp-09bc4e-78c1b0-medium-3`
(+1,472,592,160,599 raw), which have only `agree`/`tie`/`token_revisit` chunks: missing
chunk maxima produced a better whole plan. Repairing the chunk choice can therefore lower a
whole plan; WHI-1550/1562 must measure, not assume, the whole-plan effect.

## 3. Proof

### 3.1 Setting (the repository's semantics, not the report's model)

One chunk of amount `a > 0` (carry included) on a fixed committed state: aggregate flows
`F` (per pool: used direction, `x_p`, `f_p(x_p)` on the pool's **original** state) and the
committed token edges `E`, acyclic. A candidate is a path `S = t_0 → … → t_h = T`,
`1 ≤ h ≤ H = graph.label_hops`, token-simple, never re-entering `S`, never passing through
`T`, admissible iff `E ∪ edges(path)` is acyclic (`creates_cycle`). Its marginal is computed
edge by edge exactly as `_Allocator.step`: `m_0 = a`; for `m_{i−1} = 0` the step makes no
call and `m_i = 0`; otherwise quote `x_p + m_{i−1}`, fail unless the status is `ok` with full
consumption and output `≥ f_p(x_p)`, and set `m_i = out − f_p(x_p)`. The chunk objective is
the maximal `m_h`; the reference `E_H` takes the first maximum in enumeration order
(hop-major, then depth-first in bundle adjacency order). A **label** is a successful prefix
`P` ending at `v ≠ T` with amount `a_P`; its visited set is `V_P`.

### 3.2 L0 — suffix pools do not depend on the prefix

Every continuation `Q` of `P` (from `v` to `T`) visits only tokens outside `V_P` after `v`.
Each pool of `P` joins two tokens of `V_P`; each edge of `Q` enters a token outside `V_P`.
So `pools(P) ∩ pools(Q) = ∅`: a continuation's quotes see only `F`, never the prefix's own
tentative flows. (This is where "relevant PoolFlow/original-state identity" is settled:
inside a chunk the relevant state is `F`, common to all labels; prefix flows matter only
after commit, i.e. across chunks — §2.6.) The distance prune (`hops_to_target`) and the
source/target filters are prefix-independent.

### 3.3 L1, L2 — which labels have identical futures

**L1 (equal amount).** Two labels with the same layer, token, visited set and amount have
identical continuation sets (L2) and, by L0, identical quote sequences on every
continuation: identical values and failures, **for every protocol** (no monotonicity used).

**L2 (admission depends only on the visited set).** Fix `E`. For a label `P` at `v` and a
continuation `Q`, `E ∪ edges(P) ∪ edges(Q)` is cyclic iff `E ∪ edges(Q)` is cyclic or some
token of `Q` after `v` reaches, in `E ∪ edges(Q)` and without passing through `V_P`, a token
of `V_P`. *Proof.* `E ∪ edges(P)` is acyclic (`P` was admitted), so a cycle uses a `Q` edge.
If it never touches `V_P` it lies in `E ∪ edges(Q)`. Otherwise it enters `V_P` from outside;
edges of `P` stay inside `V_P` and `Q`'s edges leave `V_P` only at `v`, so the entering edge
is an `E` edge into some `u ∈ V_P`, reached from a `Q` token. Conversely, if a `Q` token
`q_j` reaches `u ∈ V_P`, then `u` reaches `v` along `P` (every token of a path reaches its
end), and `v` reaches `q_j` along `Q`: a closed walk, hence a cycle. The condition names
only `E`, `Q` and the set `V_P` — not the order of `P`'s tokens or its pools. ∎

Consequence: the report's suggestion that prefix-dependent admission may need a
reachability/forbidden-relation signature beyond `S` (N1) is not needed in this
repository's model — the visited set is enough, because prefixes are paths from the source
and continuations use fresh tokens. The signature is therefore **`(layer, token, visited
set)`**, compared by exact equality (a frozenset key; a hash is only an index, never an
equality proof). Checked exhaustively: `test_lemma_l2_admission_depends_only_on_the_visited_set`
(200 random multigraphs, committed DAGs from multi-chunk trajectories, > 1,000
continuation verdicts, > 20 reordered-prefix groups with committed edges, independent
acyclicity test).

### 3.4 L3 — when a larger amount provably dominates (certified upward safety)

A directed edge `(p, u→w)` is **certified upward safe** iff `p` is constant product and
one of: its source has no overflow rule (`source_key` null), its failure is input
independent (unknown source, fee mismatch, an empty side), or, for a sourced pair,
`reserve_u(p) + Σ_{q ∋ u, q ≠ p} reserve_u(q) < 2^reserve_bits` with **every** other pool
holding `u` constant product. Justification: `getAmountOut` is nondecreasing in the input;
its input-dependent failures are the dust floor (only below a threshold) and the sourced
overflow revert (only above one); every unit entering `p` from `u` is some other pool's
aggregate output of `u`, which for a CPMM is below its `u` reserve. Concentrated and
liquidity-book pools are **not certified**: their exact-input quotes have upward failures
(`incomplete_snapshot`, `insufficient_liquidity`, X2) and no monotonicity proof of the
migrated code is offered here (unknown ⇒ not certified).

The **continuation region** of `(v, r)` is every directed edge on a walk of at most `r`
pools from `v` that never enters `S` and never leaves `T` (a superset of the token-simple
continuations). It is **certified** iff all its edges are certified.

**L3.** If the region of `(v, H − k)` is certified, labels `A`, `B` share a signature at
layer `k` and `a_A ≥ a_B`, then for every continuation `Q` with `B·Q` succeeding with value
`≥ 1`, `A·Q` succeeds with value `≥` that of `B·Q`. *Proof.* By L2 `Q` is admissible for
both; by L0 both see `F`. Since `B·Q`'s value is `≥ 1`, each of its marginals is `≥ 1`
(a zero stays zero). Inductively `m_A ≥ m_B ≥ 1` into a certified CPMM edge with base `x`:
`B`'s quote at `x + m_B` succeeded, so `out(x + m_A) ≥ out(x + m_B) ≥ max(1, f(x))` (dust and
nonmonotone impossible), no overflow (static bound), full consumption (CPMM), and the new
marginal is `≥` `B`'s. ∎ A value-0 continuation is not covered: `B` may continue with
no-call zero steps where `A`'s positive dust fails.

### 3.5 T1 — per-chunk value preservation

Rules R1–R5 (§4) discard a label only by L1 (equal amount, any protocol, any chunk) or by
L3 (strictly larger amount, certified region, **non-final** chunk). For every admissible
successful path `Z` of value `z` there is then a retained path of value `≥ z` (induction
over layers, using transitivity of the two rules). Hence, when no cap or budget drops a
label in that chunk:

- if the chunk maximum is `≥ 1`, the selector's marginal equals it;
- if it is `0` in a non-final chunk, both sides carry the chunk (the allocator carries a
  chunk whose best is absent or zero), so no state differs;
- in the final chunk only L1 is used, so the full value set, including zeros and the
  first-found choice, is the reference's (R5 is the retain-on-unknown answer to the gap in
  L3; no counterexample is claimed, no proof of the omission is offered either).

Quotes are pure (`QuoteCache` on original states), so the selector and the oracle compute
the same numbers. T1 is checked on every chunk of the selector's own trajectory against the
independent oracle (§9.3).

### 3.6 Ties and multi-chunk effects (separate from T1)

Ties are decided independently of dominance: within a layer, labels are expanded in
generation order, which equals enumeration order; among target relaxations the first
strictly greater marginal wins (`metis_inspired`'s rule). An L1 discard keeps the earlier
label, so it never changes a tie. An L3 discard may remove the enumeration-first prefix of
a maximal path when a strictly larger label reaches an equal value (floor plateau): the
selector then returns **the same value on another path** (`tie`, §2.6). No stronger tie
claim is made. `dominance: off` (retain everything) chooses exactly `E_H`'s first maximum
and reproduces its whole trajectory when neither side truncates
(`test_every_chunk_matches_the_exhaustive_oracle_on_random_cpmm_graphs`).

Across chunks nothing is claimed: a different tie choice or a different (better) chunk
choice changes committed flows and edges and can raise or lower the final gross (§2.6,
§2.7). One-chunk results are never reported as whole-plan optimality or improvement.

## 4. The selector (normative; `choose_history` is the executable form)

Per chunk, given `(amount, H, dist, budget, options, final, certified edges)` and the
allocator's `F`, `E`, index:

```text
layer_0 = [label(S, amount, path=(), visited={S})];  best = None;  relaxed = 0
for k in 1..H:
    groups = {}                     # signature (token, visited set) -> {amount: (order, label)}
    for lab in layer_{k-1} (generation order):
        for e in index.edges_from(lab.token)  (adjacency order):
            v = e.token_out
            skip if v == S                                  # as metis_inspired
            skip if v != T and dist[v] > H - k              # exact structural prune
            skip if v in lab.visited                        # token-simple
            admission_checks += 1; skip if creates_cycle(E, lab.path + e)
            if budget.max_candidates is not None and relaxed >= it:
                truncated_by = "max_candidates"; return best          # declared truncation
            relaxed += 1; label_relaxations += 1
            r = allocator.step(e, lab.amount, k); on failure count reason, continue
            if v == T: best = r if best is None or r.m > best.m (strict); continue
            insert(label(r), groups[(v, lab.visited | {v})])
    layer_k = retained labels sorted by generation order
    peak_frontier_labels = max(peak_frontier_labels, len(layer_k))
return best
```

`insert` into group `g` (with `strict` = not `final` and region `(v, H − k)` certified —
constant for the whole group):

- **R1** `g` non-empty: `state_comparisons += 1`.
- **R2** an entry with the same amount exists: discard the new label
  (`labels_discarded_dominance`).
- **R3** `strict`: `g` holds at most one label; the larger amount survives, the other is
  discarded (`labels_discarded_dominance`).
- **R4** otherwise the new label is **retained** beside the others (`labels_retained_unknown`).
- **R5** the final non-empty chunk never applies R3.
- **R6** signature cap: if `g` already holds `max_labels_per_signature` labels, the lowest
  (amount, then latest generation) of `g ∪ {new}` is dropped (`labels_dropped_signature_cap`).
- **R7** frontier cap: if the layer already holds `max_frontier_labels` labels, the new
  label is dropped (`labels_dropped_frontier_cap`); nothing is evicted across tokens
  (amounts of different tokens are not comparable).
- **R8** any R6/R7 drop marks the chunk **capped**: T1 does not apply to it, and the solve
  records `state_cap` (§7).

`dominance: "off"` uses a unique key per label (no R2–R3; caps still apply).
Everything outside the chunk choice is `metis_inspired.solve` unchanged: the `path_split`
candidate at `search.max_hops` first on the same `QuoteCache`, the guarded quote meter,
chunk carry, `commit`, `merged_plan`, the in-solve `evaluate`, strict incumbent
replacement, statuses.

## 5. Domain (`r021.domain/1`) and the §3.3 row

`metis_history`'s incremental candidate: `protocols` all admitted
(`constant_product`, `concentrated`, `liquidity_book`; strict pruning only on certified
CPMM edges, correctness everywhere); `pool_order` bundle insertion order; `hops`
`{max: graph.label_hops, param: "graph.label_hops"}`; `splits`
`{max: graph.chunks, param: "graph.chunks", governs: "allocation"}`; `amount_grid`
`{kind: "chunk_grid", chunks: graph.chunks}`; `zero_output_leg` `infeasible`; `token_reuse`
`simple_path`; `pool_reuse` `shared_merged`; `dag_admission` `plan_token_dag`; `full_fill`
`v1_full_fill`. The retained `path_split` candidate keeps its own domain at
`search.max_hops` and is labeled `fallback` (never counted as the mechanism's result).
§3.3 row (narrowed, same vocabulary): hop bound `graph.label_hops` (fallback
`search.max_hops`); splits `graph.chunks` (allocation), `search.max_splits` fallback only;
`Budget.max_candidates` = `label_relaxations` per chunk; separate caps
`max_labels_per_signature`, `max_frontier_labels`.

## 6. Option schema (`algorithm_options.metis_history`, WHI-1548 seam)

| Key | Type | Range | Meaning |
| --- | --- | --- | --- |
| `dominance` | string | `"history"` \| `"off"` | the mechanism, or the disabled-mechanism control (every label kept) |
| `max_labels_per_signature` | integer | 1 … 1,000,000 | R6 |
| `max_frontier_labels` | integer | 1 … 10,000,000 | R7 |

All three are required after preset resolution; there are no defaults inside the solver.
The validator refuses unknown keys, booleans for integers, non-integers, out-of-range
values and every R021-C/1 §9.1 reserved key (`label_hops`, `chunks`, `label_pruning`, …
stay `graph.*`/`search.*` values of the profile). `graph.label_hops ≥ search.max_hops` and
`graph.chunks` are validated as for `metis_inspired`. `graph.label_pruning` is **not** read
by `metis_history` (its label search is always on; the control is `dominance: "off"`).
Uncapped selection is not expressible in the bounded preset; a stress profile may raise
the caps (R021-C/1 §7.2).

**Bounded comparison preset** (`config/strategy_options/metis_history.yaml` or WHI-1548's
chosen path, version 1): `dominance: history`, `max_labels_per_signature: 1`,
`max_frontier_labels: 1024`. Evidence and reasoning in §9.4. With cap 1 the preset is a
**declared approximation** on uncertified (CL/LB) regions: one label per history
signature, chosen by amount (the external report's `(h, v, S)` history rule), visibly
capped. It is exact wherever strict pruning is certified.

## 7. Stops, failures, statuses, bound kind

- Per chunk: `Budget.max_candidates` stops the chunk search at the relaxation limit and
  keeps its best (declared, `truncated_by: max_candidates`); caps drop labels and continue
  (R6–R8). A failing quote yields no label and is counted in `marginal_failures`.
- Per solve: `Budget.max_quotes` is checked before every new quote (as `metis_inspired`);
  exhaustion abandons the incremental plan (`truncated_by: max_quotes`,
  `incremental_status: truncated`). A final chunk without an admissible path abandons the
  incremental plan; the retained `path_split` plan stands.
- `truncated_by` precedence: `max_quotes`, then `max_candidates`, then `state_cap`.
  `state_cap` never makes a result `timeout`; a complete valid plan stays `ok` and is
  labeled capped.
- Statuses are `metis_inspired`'s: `ok`, `timeout` (a declared budget cut with no valid
  plan; never `no_route`), `incomplete_snapshot`, `no_route` (complete search only).
- Diagnostics (`search_stats["r021"]`): `certificate: null`,
  `certificate_unavailable_reason: "not_produced"`; the method has no whole-plan bound
  (`bound_kind` would be `unknown`). Termination vocabulary for the per-chunk mechanism:
  `complete`, `state_cap`, `candidate_cap`, `quote_budget`.

## 8. Work units and counters

`search_stats["r021"]["work"]` (§5.2 units only): `quotes_executed`, `quotes_memoized`,
`internal_evaluations` (every `evaluate` call inside `solve`), `label_relaxations`,
`labels_discarded_dominance`, `labels_retained_unknown`, `state_comparisons`,
`peak_frontier_labels` (largest single layer of any chunk), `admission_checks`. Additional
plain `search_stats` keys (not §5.2 units): `labels_dropped_signature_cap`,
`labels_dropped_frontier_cap`, `chunks_state_capped`, `peak_signature_labels`,
`certified_strict_insertions`, `dominance`, `candidate_unit: "label_relaxation"`, and
`metis_inspired`'s existing keys with their meanings. Units are never divided by each
other; `paths_scored` (E arms) and `label_relaxations` (L/S arms) are different units.

## 9. Same-depth diagnostics, checks and bounded tuning

### 9.1 Arms (R021-C/1 §6.2)

`E3`/`E4` enumeration, `L3`/`L4` `metis_inspired` labels, `S3`/`S4` `metis_history` preset,
all at the same corpus, `graph.chunks`, `search.*`, objective, budgets and rules. Required
same-depth contrasts: `S_H` vs `L_H` (mechanism), `S_H` vs `E_H` (pruning cost/quality),
and `S_H(dominance: off)` vs `E_H` (control; must be plan-identical unless one truncates).
Depth effects use ratios (`Q_S4/Q_E3 = (Q_E4/Q_E3)(Q_S4/Q_E4)`).

### 9.2 Diagnostic pass (separate from the measured solve)

WHI-1550 adds `diagnose_history(case, bundle, prepared)` (never called by `solve`, the
registry or a profile). It replays `metis_history`'s own chunk trajectory unbudgeted on its
own `QuoteCache` and, per chunk, re-scores the exhaustive same-depth enumeration on the
identical committed flows, edges and carried amount with a second cache and own counters.
Classes: `agree`, `tie` (equal marginal, other path); on a capped chunk also `miss_capped`
(allowed, visible approximation); anything else — `miss` on an uncapped chunk, `above`,
`extra` — is `unattributed` and fails the gate. `dominance: off` must show only `agree`.

### 9.3 Independent checks (this issue; WHI-1550 must port or re-run them)

`tests/routing/test_history_labels_contract.py`: the oracle has its own adjacency,
depth-first enumeration, acyclicity test and hand CPMM formula (non-CPMM via the pure
`quote_exact_in`). Every chunk of the selector's trajectory is audited:
random CPMM multigraphs at 3, 4 and 5 hops (1 and 7 chunks, dust to large, fee 0–100 bps,
some sourced pairs) → only `agree`/`tie`, with certified strict pruning exercised; the real
`mantle_mixed` CL/LB/CPMM states at 3 and 4 hops → only `agree`/`tie`; the lemma-L2 check;
the §2 counterexamples; caps visible (`miss_capped`); relaxation budget declared. Whole
plans are evaluator-replayed.

### 9.4 Bounded probe (tuning split only; evidence for the preset)

Probe: `PYTHONPATH=. uv run python tests/routing/test_history_labels_contract.py probe
<bundle_tuning> <out.json> [--cap N] [--frontier N] [--oracle] [case ids]`, S4 settings
(chunks 50, label_hops 4), the selector trajectory only (no fallback, no timing claim, a
loaded shared host). Artifacts under
`…/router-algorithms-optimizer-artifacts/research-021/whi-1549/probe-<source sha>/`
(frozen source copy, wrapper, outputs; the spec hash is recorded and pinned in
[`fixtures/history-labels.json`](fixtures/history-labels.json) `probe`).

Source commit `91702da3e79bad69bbbb2f5c5cae3ef3e264ddfb`, spec SHA-256 `4f2b5df5…ddee4`
(equal to this HEAD's `spec_sha256()`, asserted by the test), `summary.json` SHA-256
`01421022…6da2`; comparisons use the frozen WHI-1449 S4-campaign records (M4 = L4, M4-off
= E4; `cases.jsonl` `ce8395de…` / `1c3d1d23…`), incremental plan gross only, descriptive.

| Run | Cases | Result |
| --- | --- | --- |
| **Uncapped exact selector** (no oracle), the 12 H4 cases | 12 | `label_relaxations` p50 815,108 / max 1,379,299; `quotes_executed` p50 169,518 / max 236,781; `peak_frontier_labels` p50 7,254 / max 12,165; largest signature group 1,666; `certified_strict_insertions` 0 — enumeration scale (E4 median 1,778,207.5 `paths_scored`, 116,745 quotes, WHI-1449 §4; different units) |
| **Preset-shaped selector** (`max_labels_per_signature: 1`, frontier uncapped) with the per-chunk oracle audit, all of `bundle_tuning` | 96 (all `ok`) | 4,800 chunks: **4,595 `agree`, 205 `tie`, 0 `miss`**; 4,650 chunks capped (649,628 signature-cap drops), 0 frontier drops, `peak_frontier_labels` max **27**; `label_relaxations` p50 19,490.5 / max 41,000; `quotes_executed` p50 3,120.5 / max 14,997; incremental gross vs E4: 7 higher / 89 equal / 0 lower; vs L4: 6 higher / 87 equal / 3 lower |

Reading: (a) without CL/LB certification, exactness costs enumeration-scale work on this
corpus, so an uncapped preset is not a bounded comparison; (b) with one label per
signature every tuning chunk still reached the exhaustive same-state maximum (ties only),
which is the bounded evidence for `max_labels_per_signature: 1` — evidence on one block's
tuning split, not a proof (X2 shows the cap can lose; such a chunk is reported
`miss_capped`); (c) the frontier cap never bound (max 27 labels per layer), so
`max_frontier_labels: 1024` is a memory guard with ≥ 37× headroom; (d) the gross
comparisons are descriptive tuning facts (3 cases below L4 illustrate §2.7), not a verdict.

### 9.5 Bounded tuning recipe for WHI-1550 (before any report-split run)

1. Implement; pass the ported §9.3 checks, `dominance: off` ≡ `E_H` identity on the
   metis fixtures and `mantle_mixed`, and `metis_inspired`'s unchanged tests.
2. On `bundle_tuning` only: run `S4` (preset) and `S4` with `dominance: off`; run
   `diagnose_history` on every case where `S4` and `E4` gross differ (E4 records may be the
   frozen WHI-1449 M4-off run, labeled as such) and on the chunks `S4` marks capped.
   Gate: 0 `unattributed`; `dominance: off` identical to `E4`.
3. Report per case: statuses (all scheduled), `chunks_state_capped`, cap drops,
   `peak_frontier_labels`, `label_relaxations`, `quotes_executed`, final-plan source, and
   gross vs `L4`/`E4` with wins/ties/losses (descriptive).
4. Preset change rule: only if a cap binds on the frontier (`labels_dropped_frontier_cap`
   > 0 on tuning) may `max_frontier_labels` be raised (×4, at most twice), recorded with the
   evidence; `max_labels_per_signature` changes only with a new recorded tuning run of
   values {1, 2, 4} on the same split, choosing the smallest value whose `miss_capped`
   count is minimal. Freeze preset bytes/hash before the report comparison (§6.3).
5. No report-split tuning, no speed claim without L01's host rule.

## 10. Outcome: `narrow_go`

**Supported (GO scope).** The selector of §4 with the schema of §6 is implementable on the
existing allocator and gives, per chunk and on identical state, the exact same-depth
maximum whenever no cap/budget drops a label (T1), for every admitted protocol, with strict
pruning only on certified CPMM regions in non-final chunks and equal-amount pruning
everywhere. It structurally removes both known L4 loss classes (token revisit, prefix
admission; §2.1–§2.3, lemma L2) and the failure-domain loss where no cap binds (§2.4).

**Narrowed / not supported.**
- On the 5-source corpus (90 % CL/LB pools) strict pruning is almost never certified: the
  uncapped exact selector degenerates to enumeration scale (§9.4). The bounded preset is
  therefore a **visibly capped approximation** there; exactness is claimed only for
  uncapped chunks.
- Certifying CL/LB monotonicity and failure domains (which would allow shadow/lazy
  dominance) is **blocked** on a separate proof of the migrated `pools/concentrated.py`
  and `pools/liquidity_book.py` code; it is not assumed.
- No whole-plan, tie-identity, improvement or speed claim (§3.6, §2.6, §2.7).

## 11. Amendment text for WHI-1550 (for the parent to apply)

> **Contract:** implement `metis_history` exactly as `docs/references/research-021/history-labels.md`
> §4 (normative; executable form `choose_history` in
> `tests/routing/test_history_labels_contract.py`), inside a new
> `routing/algorithms/metis_history.py` that reuses `metis_inspired`'s solve structure
> (fallback `path_split`, guarded meter, carry, commit, `merged_plan`, in-solve `evaluate`,
> statuses) without changing `metis_inspired`'s behavior. Signature `(layer, token, visited
> set)`; equal-amount discard always; strict amount discard only in non-final chunks whose
> continuation region is certified upward safe (§3.4 rule, precomputed once per worker from
> the bundle); every other same-signature label retained; caps R6/R7 visible.
> **Options** (§6): `dominance` {history, off}, `max_labels_per_signature` 1…1e6,
> `max_frontier_labels` 1…1e7, all required, reserved keys refused; preset v1 =
> `{dominance: history, max_labels_per_signature: 1, max_frontier_labels: 1024}`.
> **Domain/row/units/stops/statuses/diagnostics:** §5, §7, §8; certificate null,
> `not_produced`.
> **Checks:** port §9.3 (independent oracle audits: random CPMM 3–5 hops, `mantle_mixed`
> 3–4 hops, lemma L2, R1/X4/X4b/X2/`overflow`/`tie_state`/`greedy_trap`, top-k), assert on
> the real implementation; `dominance: off` plan-identical to `metis_inspired`
> (`label_pruning: false`, same `label_hops`) when untruncated; `metis_inspired` tests
> unchanged; multi-chunk, case-order/state-leak, cap and budget tests; `diagnose_history`
> separate pass (§9.2). **Tuning:** §9.5 on `bundle_tuning` only.
> **Claims:** per-chunk same-state value exactness on uncapped chunks only; no whole-plan,
> tie, improvement or speed claim.

## 12. Reproduction

```bash
uv run pytest tests/routing/test_history_labels_contract.py -q
uv run pytest tests/routing/test_metis_inspired.py tests/docs/test_research_021_contract.py -q
(cd docs/references/research-021/sources && shasum -a 256 -c SHA256SUMS)
shasum -a 256 …/metis-challenge/campaign-h4diag-446674e-20260928T185905Z/h4-diagnostic.json  # a0306f02…
```
