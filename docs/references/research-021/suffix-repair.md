# Checkpoint-and-suffix repair for `incremental_graph_repair` (R021-P07, WHI-1553)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` ([`contract.md`](contract.md)); this memo fills the `incremental_graph_repair` row (§11 obligations) and changes no shared schema, vocabulary, example or check |
| Publication key | `R021-P07`, WHI-1553, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository base | `origin/dev` `1ce50763b84b7daf4eec844665848b5b1c27fb6a` (B `81559ab` + WHI-1547/1549/1557/1551 research), then merged with `origin/dev` `6284a9d4c8c1c77d18f2fd9a8cfe836f410c2c3f` (WHI-1560) and `7f127e2876d541fd2c5794abcc7774ea981e11f0` (WHI-1555); no file overlap. `routing/`, `pools/`, `snapshot/`, `benchmark/` unchanged since B |
| Revision | r2 (parent verification comment `7e509bdf`): stage-2 replay/accounting guard with fail-closed retention (§3.5, §5.1, §5.7) and the true all-stage `internal_evaluations` total with a narrow optional counting seam (§8, §8.1); probe provenance in §10.1 |
| Outcome | **`go`** (§11): implementable, bounded, proven conserving and exactly replayable on every admitted protocol; no quality, optimality or latency claim; the measured effect on the tuning split is small (§10) |
| Records | [`fixtures/suffix-repair.json`](fixtures/suffix-repair.json): trap, no-improvement, checkpoint-completeness and order fixtures, WHI-1549 cross-references, the pinned probe summary |
| Executable check | `uv run pytest tests/routing/test_suffix_repair_contract.py -q` |
| Downstream | WHI-1554 (`incremental_graph_repair` implementation); amendment text in §12 |

This memo defines a bounded repair of `incremental_graph`'s own greedy chunk trace: build the
ordinary incumbent, keep a complete checkpoint of the committed search prefix before every
decision, restore a registered set of those checkpoints, force a different first suffix
choice, rebuild the suffix with the unchanged reference rule, replay the complete candidate
from the original snapshot, and replace the incumbent only by a strictly better valid plan.
It implements no strategy. The normative algorithm is written out here and exists as an
executable specification, `repair_solve`, in `tests/routing/test_suffix_repair_contract.py`.
It runs on the actual `incremental_graph` pieces (`PoolFlow`, `chunk_amounts`,
`creates_cycle`, `merged_plan`), the embedded `path_split.solve`, the per-solve `QuoteCache`
behind the same guarded meter, and the unchanged evaluator. With `repair: false` it
reproduces `incremental_graph.solve` exactly (§9.1). `incremental_graph`, its profiles and all
historical records are unchanged; no trace seam was added to the runtime.

**What is claimed, and only this.** (1) A checkpoint as defined in §4 determines the rest of
the reference loop exactly: restoring it and resuming reproduces the incumbent byte for byte,
with no new executed quote (Lemma C, Lemma R). (2) Every complete repair candidate conserves
the whole input, satisfies full fill and the plan-token DAG, and its merged plan replays on
the evaluator to exactly its accounted gross (Theorem T1). (3) Without a hard kill, the
returned score is never below the repair-off control under the same budget (Proposition P1).
(4) A replay that contradicts its accounting is never accepted, published or returned; the
result is the best plan validated before it, or `algorithm_error` (Proposition P2). (5)
`internal_evaluations` is the exact total of every in-solve replay, all stages (§8.1).
**What is not claimed:** optimality in any domain (even the chunk-sequence domain the repair
searches, §9.3), a guaranteed improvement, a same-budget win, identical tie choices with any
other solver, or any latency effect. Same-budget comparisons stay empirical (WHI-1562).

Evidence classes (R021-C/1 §1) stay apart. *Repository evidence*: the actual
`incremental_graph`, `path_split`, `_ExactReuse` (read only), evaluator and committed fixture
bundles. *External proposal*: the archived report's N3 (rollback-and-replay suffix repair,
`sources/research-report.md`), which proposes the idea and states that no trap fixture was
built. *New reconstructions*: this issue's fixtures, independent oracle, stale-state
demonstrations and random suites. *Bounded probe*: the tuning split only (§10), a separate
diagnostic pass with no timing claim. The report's `verify_research.py`/`results.json` are
absent and were not run.

## 1. Sources

| Source | Pin | Used for |
| --- | --- | --- |
| `routing/algorithms/incremental_graph.py` | at base (unchanged since B) | `chunk_amounts`, `PoolFlow`, `creates_cycle`, `merged_plan`, the `marginal` rule and chunk loop of `solve`, `_ExactReuse` (read only) |
| `routing/algorithms/metis_inspired.py` | at base | `_Allocator.step/_marginal/choose_enumeration`: the same aggregate-flow rule, confirming the semantics are shared, not `incremental_graph`-specific |
| `routing/algorithms/path_split.py`, `routing/search.py` | at base | the retained simpler candidate, `QuoteCache` (pure original-state memo), `enumerate_paths` order |
| `routing/evaluator.py` | at base | whole-plan replay, full fill, plan-token DAG |
| `history-labels.md` §2.6–§2.7, `fixtures/history-labels.json` | WHI-1549 (`b9e7310`) | the `greedy_trap` and `tie_state` fixtures and their independently replayed values |
| `sources/research-report.md` N3 | SHA-256 `0588e83a…ba1d9` | the external proposal |
| `docs/handoff/algorithm-exploration-handoff.md` §5.1, §6.9 | at base | the observed non-monotone `graph.chunks` quality at 3 hops ("greedy chunks lock into a worse structure") |

## 2. The accounting model being repaired (pinned)

Everything below is `incremental_graph.solve` at the base with `graph_reuse=False` (the
registry, runner and every profile run that loop).

- **Chunks.** `chunk_amounts(A, K)[k] = ⌊A(k+1)/K⌋ − ⌊Ak/K⌋`; they sum to `A`. A zero chunk
  is skipped. A non-final chunk whose best admissible marginal is 0, or that has no
  admissible path, is **carried**: its amount is added to the next nonzero chunk. The final
  nonzero chunk commits even with a zero marginal; with no admissible path the incremental
  plan is abandoned (`chunk_<k>_no_admissible_path`).
- **Aggregate flows.** `flows: dict[pool_id, PoolFlow(edge, amount_in, amount_out, order)]`.
  `amount_in = x_p` is the pool's aggregate input in its single used direction and
  `amount_out = f_p(x_p)` is the exact quote of the whole aggregate on the pool's
  **original** bundle state. A chunk of `a` on path `e1..eh` is charged edge by edge:
  `m_0 = a`; if `m_{i−1} = 0` there is no call and the edge's flow is unchanged (a new pool
  is still recorded with `x = 0`); otherwise quote `f(x + m_{i−1})`, fail unless the status is
  `ok` with full consumption and output `≥ f(x)`, and set `m_i = f(x + m_{i−1}) − f(x)`. The
  chunk's value is `m_h`.
- **This is cumulative `f(x+m) − f(x)`, not one executed swap per chunk.** Chunks are search
  allocations on original-state aggregate quotes. R4 (contract §3.5): on one (1000, 1000)
  pool, two 100-unit chunks quoted on the initial state sum to 180, executed sequentially give
  165 and merged give 166; the merged figure is the one the accounting telescopes to.
  `tests/routing/test_incremental_graph.py` already checks that the sequential execution of
  the same chunks differs. A repair never executes, replays or scores a chunk as a swap.
- **Order metadata.** A new pool's `order` is `len(flows)` at scoring time, so every new pool
  of one path gets the same value and a zero-input record occupies a slot. The dict's
  insertion order is first-use order; replacing an existing key keeps its position.
- **Admission.** A path is admissible iff `creates_cycle(token_edges, path)` is false for the
  committed token-edge set; committing adds the path's token edges. This forbids economic
  token cycles, re-entering `token_in` and one pool in both directions.
- **Choice.** Paths in enumeration order (hop-major, then depth-first in bundle adjacency
  order); `Budget.max_candidates` caps the admissible paths scored per chunk; the choice is
  the **first** maximal marginal (ties keep the earlier path). A per-chunk `memo` of path
  prefixes lives for one chunk.
- **Budget.** `guarded` refuses a new (non-memoized) quote once `QuoteCache.misses ≥
  max_quotes`; exhaustion abandons the incremental plan (`truncated`, `truncated_by:
  max_quotes`).
- **Normalization.** `merged_plan` emits one step per positive-input pool, tokens in
  topological order (ties by first use), each token's funds split across its out-pools by
  explicit amounts in `order`, the last out-pool taking `ALL_REMAINING`. The complete plan is
  evaluated in solve on the guarded cache; only an `ok` evaluation is a candidate, and it must
  strictly beat the retained `path_split` result on `ObjectiveContext.score`.

## 3. Proof

### 3.1 Setting

Immutable inputs `I`: bundle, case, `K`, the chunk grid, the path list `P` in enumeration
order, the objective, the pure original-state quote `f_p`. The loop state before chunk
position `k` is `σ = (k, c, F, E, D)`: carry `c`, the ordered flow map `F` (records
`(pool, token_in, token_out, x, y, order)` in insertion order, zero-input records included),
the committed token edges `E` and the committed decisions `D`. The **chunk-sequence domain**
`𝒟(K, H)` is the set of complete runs in which every nonzero chunk (with its carry) commits to
an admissible path with a positive marginal, or is carried when none has one, and the final
nonzero chunk commits to any admissible successful path. The reference greedy picks one member
of `𝒟`; every repair candidate is a member of `𝒟`.

### 3.2 Lemma C — the checkpoint is complete

The loop's transition out of `σ` depends only on `σ` and `I`. The amount is `c + a_k`.
Admission reads `E` only. A marginal reads `F` (`x`, `y`, `order`, and `len(F)` for a new
pool) and `f_p`, which is pure; `QuoteCache` returns the identical result object for identical
`(pool, token, amount)` on an original state, so cache contents change only counters. The
per-chunk memo starts empty. The choice rule, the carry/final rule and the commit (replace
existing keys in place, append new keys in path order, add edges, append the decision) read
nothing else. The quote budget check reads the monotone executed count: it can only *cut* the
loop, never change a value. Hence `Checkpoint(k, c, F, E, D)` plus `I` determines the
continuation exactly (up to a budget cut).

### 3.3 Lemma R — exact round trip

Restoring checkpoint `i` into fresh objects and resuming unforced reproduces decisions
`i..n−1`, the final `F` (values, `order`, insertion order), `E` and the merged plan byte for
byte, and executes no new quote: every quote it asks was asked with the same arguments while
the incumbent was built and is memoized. (Lemma C plus determinism; checked from every
checkpoint of the fixtures, all `mantle_mixed` cases and 60 random graphs.)

### 3.4 Theorem T1 — whole-input conservation and exact final replay

Let a run start from any checkpoint of the incumbent, optionally with a forced first choice
taken from `score` on the restored state, and complete. Then (a) `Σ_{p out of S} x_p = A`; (b)
at every intermediate token the pool outputs into it equal the pool inputs out of it; (c)
`E` is acyclic, `S` is never re-entered and each pool has one direction; (d) `merged_plan`
of `F` replays on the evaluator with status `ok`, full fill, and gross `= Σ_{p into T} y_p`,
the accounted gross.

*Proof.* The prefix is the incumbent's prefix (Lemma C), which satisfies the invariants. By
induction over commits: a chunk of `a` on `e1..eh` adds `a` to `x_{e1}` and, for each `i`,
`m_i = f(x+m_{i−1}) − f(x)` to both `y_{e_i}` (new `y` minus old `y`, telescoping) and
`x_{e_{i+1}}`; a zero marginal adds nothing. So each commit adds equal inflow and outflow at
every intermediate token and `a` at `S`. Every nonzero chunk's amount is committed exactly
once (directly, or inside a later chunk's carry, and the final nonzero chunk commits or the run
fails), so the committed amounts sum to `Σ_k a_k = A`. A forced first choice is a scored path
at amount `c_i + a_{k_i}` on the restored state, so the same invariant holds. Every committed
path passed `creates_cycle` against the current `E` (the forced one too: alternatives come only
from `score`), so `E` stays acyclic; a pool in both directions would be a 2-cycle; paths never
return to `S`. For (d): each update sets `y = f_p(x)` from a full quote of the aggregate on
the original state (never a sum of marginals), with status `ok` and full consumption, so the
evaluator's single use of pool `p` on its original state with input `x_p` returns exactly
`y_p`; conservation makes every explicit split and `ALL_REMAINING` resolve to exactly the
pools' inputs, leaving no residual. ∎

The runtime check of T1 is part of the algorithm, **at stage 2 and stage 3 alike**:
`merged_plan` refuses non-conserved or cyclic flows, and a replay that is not `ok` or whose
gross differs from the accounted gross is a consistency failure (§5.7). T1 says this never
happens with a correct evaluator; the check exists so that a defect (or a corrupted replay)
fails closed instead of becoming a claimed score.

### 3.5 Proposition P1 — never below the control without a hard kill

Stages 1 and 2 equal the reference code on the same cache, meter and budget whenever the
replay agrees with the accounting (always, by T1, with a correct evaluator); the repair-off
control stops there. The incumbent is replaced only by a replayed candidate with a strictly
higher score, and a budget cut, failed rebuild, duplicate, tie or consistency failure leaves it
unchanged. So without a hard kill the returned score is `≥` the repair-off score and the plan
is the repair-off plan or a strictly better replayed one. Under a hard kill the runner keeps
the last published plan: publications happen only after a complete replay and acceptance, so
they form a strictly improving prefix of the uninterrupted sequence (§9.5). A hard wall kill
during repair still turns an otherwise `ok` record into `timeout` with that
`last_valid_candidate`; this is a measurable risk, not excluded by P1.

**Proposition P2 (fail closed).** A replay that contradicts its accounting (status not `ok`,
or evaluated gross ≠ accounted gross) is never the incumbent, never published and never the
returned plan or score, at stage 2 or 3. The returned plan is the best candidate validated
before the fault (the stage-1 plan, validated by the reference solver's own replay, or an
earlier stage-2/3 plan whose replay equalled its accounting); with none, the status is
`algorithm_error` with the provenance of §5.7. Checked by fault injection (+1, −1 and
`invalid_plan` at every own replay of `structural_trap`, and with no stage-1 plan, §9.5).
### 3.6 Lemma S — where admission lock-in can come from

A decision whose path adds no new token edge leaves `E` unchanged, and the admissibility of
every later path depends only on `E`. So a later path can only be forbidden by an edge a
**structural** decision (one that added at least one edge) committed. The number of structural
decisions is at most `|E_final|`, independent of `K` (observed 1–5 on the tuning split). This
is why the registered checkpoints are the structural ones. It is a targeting argument, not an
optimality one: capacity and chunk-granularity myopia at non-structural decisions are outside
the neighborhood except through the forced choice at a structural checkpoint.

### 3.7 Not claimed

- **Not optimal in `𝒟`.** `structural_trap` at 5 chunks: incumbent 92,911,795, repair
  111,172,169, exhaustive optimum of `𝒟` 111,193,897 (§9.3).
- **No improvement guarantee.** `twin_pools` and `tie_state` change nothing (§9.4).
- **No latency or same-budget claim.** Work grows (§10); a win at equal budget is WHI-1562's
  empirical question.

## 4. The checkpoint and what a restore invalidates

A checkpoint is taken immediately before each committed decision of the incumbent run (the
empty checkpoint precedes decision 0):

| Field | Content |
| --- | --- |
| `position` | the chunk index of the decision |
| `carry` | the carried amount entering it (`decision.amount − chunk_amounts[position]`) |
| `flows` | every `PoolFlow` as an immutable record `(pool_id, token_in, token_out, amount_in, amount_out, order)`, in dict insertion order, **zero-input records included** |
| `token_edges` | the committed token-edge set (frozen) |
| `decisions` | the committed prefix: `(position, amount incl. carry, path index, path, marginal, new edges)` per decision |

A restore builds **new** `PoolFlow` objects in the recorded order, a new edge set and a new
decision list. Nothing is subtracted: removing a chunk by subtracting its recorded deltas
while later chunks stay is wrong, because later deltas were computed on a state that included
it (§9.2, `test_subtracting_a_non_suffix_chunk_leaves_stale_aggregates`). Only whole suffixes
are rolled back, always by copy.

| State | Depends on committed state? | On restore |
| --- | --- | --- |
| bundle, case, chunk grid, path enumeration, objective | no | kept |
| `QuoteCache` memo (original-state quotes) | no (pure) | kept; rescoring a checkpoint is memo hits only |
| `QuoteCache.misses/hits`, worker meter, wall clock, all counters | ledger | **never reset**, never rolled back |
| the incumbent `(plan, evaluation, score)` and the set of replayed flow keys | no (complete, replayed) | kept |
| per-chunk prefix `memo` | yes (`F`) | dropped (a new one per chunk) |
| any kept chunk score, ranking or `Scored` result | yes (`F`, `E`, amount) | dropped; recomputed on the restored state |
| `_ExactReuse` scores, reach closure, `rejected`/`admitted`, `version` | yes (`F`, `E`; its rejections are monotone only while edges grow) | dropped; WHI-1554 runs the reference loop (`graph_reuse` off); a reuse accelerator would have to be rebuilt from the restored edges |
| `accounted_gross`, the merged plan | yes | recomputed from the candidate's final flows |
| per-chunk `max_candidates` count | per chunk | per chunk |

## 5. The algorithm (normative; `repair_solve` is the executable form)

### 5.1 Stages

1. **Retained simpler candidate.** `path_split.solve` on the per-solve `QuoteCache`, exactly
   as `incremental_graph` (publish its plan).
2. **Incumbent.** The reference chunk loop from the empty checkpoint, recording a checkpoint
   before every decision; `merged_plan`, in-solve `evaluate` on the guarded cache; the
   incremental plan counts as a candidate **only if the replay is `ok` and its gross equals
   the accounted gross** (else `incremental_status` is `invalid_plan` or
   `consistency_failure`, nothing is replaced or published, and the repair does not start);
   it replaces the stage-1 plan only if strictly better (publish). On every uncorrupted run
   the check holds (T1), so stages 1–2 (plans, statuses, counters, quotes, publications)
   equal `incremental_graph`; they differ only where `incremental_graph` would itself accept
   an inconsistent replay.
3. **Repair** (skipped when `repair: false`, `stop: disabled`), on the same cache, meter,
   budget and wall clock.

### 5.2 Registered neighborhood

- **Checkpoints:** the indices of structural decisions of the incumbent run (non-empty new
  edges), **latest first**, at most `max_checkpoints`. The earliest structural decision is
  decision 0 (the empty prefix).
- **Alternative first suffix choices** at checkpoint `i`: restore it, rescore decision `i`'s
  chunk (amount incl. carry) with the reference `score` and per-chunk cap; every admissible
  successfully scored path except the incumbent's path index, with a **positive** marginal,
  ordered by marginal descending then enumeration index; the first
  `alternatives_per_checkpoint`.
- **Round-trip guard:** the rescored first maximum must be the recorded decision (same path
  index and marginal); otherwise a consistency failure (§5.7).

### 5.3 Rebuild

For each alternative in order: if `max_repair_attempts` attempts were already made, stop
(`attempt_cap`); otherwise count an attempt and run the reference loop from the checkpoint with
the alternative forced as the first decision. Every later decision is the reference's first
maximum on the state the suffix itself built, with the reference carry and final-chunk rules
and the per-chunk `max_candidates` cap. A run that ends without an admissible path is a
failed candidate (counted, not replayed); the search continues.

### 5.4 Actual change

A candidate's identity is its **flow key**: the set of `(pool, token_in, token_out, x_p)` with
`x_p > 0`. Equal keys give the same merged steps up to order, hence the same replay output
(T1(d)). A candidate whose key equals the incumbent run's or an earlier candidate's is a
**duplicate**: counted and not replayed. Forced first choices often reconverge (the greedy
suffix re-chooses the incumbent's structure), so a different first choice alone is not a
change; a different flow key is, even when its score ties.

### 5.5 Replay and acceptance

`merged_plan` of the candidate's flows (a `ValueError` is a consistency failure); `evaluate`
on the guarded cache (metered; counted in `repair_evaluations` and in the all-stage
`internal_evaluations` total); status `ok` and evaluated
gross = accounted gross, else a consistency failure; `score = objective.score(evaluation)`.
Compared with the current best (the stage-1/stage-2 incumbent or an accepted candidate):
strictly greater → **accepted** (replaces the best, published with `report_candidate`);
equal → **tie** (the earlier incumbent stays, counted); less → **rejected_worse**. Chunk
choices stay gross marginals (as the reference); acceptance uses the objective's complete-plan
score, as the incumbent policy does.

### 5.6 Ties and determinism

Everything is deterministic: checkpoint order (latest structural first), alternative order
(marginal, then index), rebuild choices (first maximum), duplicate detection (flow key), the
strict acceptance rule (ties keep the earlier incumbent). No randomness, no seed, no
timing-dependent branch.

### 5.7 Stops, failures and retention

| `repair.stop` | When | Result |
| --- | --- | --- |
| `disabled` | `repair: false` | exactly `incremental_graph` |
| `no_trace` | no committed incremental decision (e.g. `no_paths`) | stage 1–2 result |
| `quote_budget` | the incumbent was truncated, or any repair quote hit `max_quotes` | best so far; `truncated_by: max_quotes`; the cut candidate is abandoned; no retry |
| `consistency_failure` | the incumbent's stage-2 replay or a candidate replay is not `ok` or differs from its accounting, `merged_plan` refuses a candidate, or the round-trip guard fails | the best plan validated before the fault (P2); the repair stops (or never starts); a defect alarm that every WHI-1554 check must show at 0 on uncorrupted runs |
| `attempt_cap` | an attempt was needed after `max_repair_attempts` | best so far |
| `complete` | every registered checkpoint and alternative was tried | best so far |

Statuses are `incremental_graph`'s: `ok` whenever a validated plan exists (a repair stop never
changes it); `timeout` (a declared budget cut with no valid plan, never `no_route`);
`incomplete_snapshot`; `no_route` (complete search only). A hard kill by the runner keeps the
last published plan as `last_valid_candidate`; the record is `timeout`. **A consistency
failure with no earlier validated plan is `algorithm_error`** (never `ok`, `no_route` or
`timeout`), with the error `consistency_failure in <stage> replay <n>: evaluated <gross|None>
!= accounted <gross>; no validated plan`. Every consistency failure also records
`search_stats["consistency_failure"]` = `{stage: incumbent | repair | repair_restore,
replay_index (1-based within the stage), accounted_gross, evaluation_status, evaluated_gross,
detail}` (decimal strings), and `repair.stop: consistency_failure`; it is `null` otherwise.

### 5.8 One attempt ledger

One `QuoteCache` (its `misses` is `quotes_executed` and equals the worker meter), one guarded
budget check, one runner quote meter and wall clock for stages 1–3: incumbent construction,
checkpoint restores and rescoring (memo hits), alternative rebuilds, candidate replays. No
external baseline result is reused, nothing is reset per attempt, and there is no hidden retry.
Work bound: the repair performs at most `max_checkpoints` checkpoint rescorings plus, per
attempt, at most `K` chunk scorings, each over at most `|P|` paths: at most
`(max_checkpoints + max_repair_attempts) · K` chunk scorings (`12·K` with the preset) against
the incumbent's at most `K` (observed ratios in §10).

## 6. Domain (`r021.domain/1`) and the §3.3 row

The incumbent's incremental candidate and every repair candidate share one feasible set, the
same as `incremental_graph`'s: `protocols` all admitted (`constant_product`, `concentrated`,
`liquidity_book`); `pool_order` bundle insertion order; `hops` `{max: search.max_hops, param:
"search.max_hops"}`; `splits` `{max: graph.chunks, param: "graph.chunks", governs:
"allocation"}`; `amount_grid` `{kind: "chunk_grid", chunks: graph.chunks, remainder:
"floor_chunks"}`; `zero_output_leg` `infeasible`; `token_reuse` `simple_path`; `pool_reuse`
`shared_merged`; `dag_admission` `plan_token_dag`; `full_fill` `v1_full_fill`. Repair on and
off have the same `candidate_domain_hash`, so `repair_off_on` is a `same_domain` comparison
(R021-C/1 §3.4). The retained `path_split` result keeps its own domain and is labeled
`fallback`.

§3.3 row (filled, same vocabulary): hop bound `search.max_hops`; splits `graph.chunks`
(allocation) for the incumbent and every rebuilt suffix, `search.max_splits` /
`search.percent_step` for the embedded `path_split` fallback only; `Budget.max_candidates` =
`paths_scored_per_chunk`, applied to every scored chunk (incumbent, checkpoint rescoring,
rebuilt suffix); separate caps `max_checkpoints`, `alternatives_per_checkpoint`,
`max_repair_attempts`.

## 7. Option schema (`algorithm_options.incremental_graph_repair`, WHI-1548 seam)

| Key | Type | Range | Meaning |
| --- | --- | --- | --- |
| `repair` | boolean | `true` \| `false` | the mechanism, or the repair-off control (exactly `incremental_graph`) |
| `max_checkpoints` | integer | 1 … 64 | structural checkpoints restored, latest first |
| `alternatives_per_checkpoint` | integer | 1 … 16 | alternative first suffix choices per checkpoint |
| `max_repair_attempts` | integer | 1 … 256 | total rebuilt candidates per solve |

All four are required after preset resolution; there are no solver defaults. The validator
refuses unknown keys, a non-boolean `repair`, booleans or non-integers for the integer keys,
out-of-range values and every R021-C/1 §9.1 reserved key (`chunks`, `max_hops`, `max_quotes`,
… stay profile values). `graph.chunks` and `search.*` are validated as for
`incremental_graph`.

**Bounded comparison preset** (version 1, path chosen by WHI-1548/1554):
`repair: true`, `max_checkpoints: 4`, `alternatives_per_checkpoint: 2`,
`max_repair_attempts: 8`. **Repair-off control** (`repair_off_on` arm): the same values with
`repair: false`. **Stress profile** (explicit `--strategies profile` only):
`max_checkpoints: 16`, `alternatives_per_checkpoint: 4`, `max_repair_attempts: 64`. Evidence
in §10.

## 8. Diagnostics, work units and counters

`search_stats`: every `incremental_graph` key keeps its meaning for stages 1–2 (so the repair-
off record equals `incremental_graph`'s); `quotes_executed`/`quotes_memoized` are the whole
solve; `chosen_source` is `incremental_graph_repair` for an accepted repair. A new
`search_stats["repair"]` object: `enabled`, `stop`, `checkpoint_restores`, `repair_attempts`,
`candidates_complete`, `candidates_failed`, `duplicates`, `rejected_worse`, `ties`,
`accepted`, `consistency_failures`, `repair_evaluations` (candidate replays only: a stage
extra, **not** the R021 total), `paths_scored`,
`paths_rejected_cycle`, `paths_truncated`, `marginal_failures`, `accepted_log` (checkpoint,
alternative index, score per acceptance). `search_stats["evaluations"]` =
`{fallback, incumbent, repair, total}`: every complete-plan `evaluate` call inside the solve by
stage (the embedded `path_split` with its `single_path`/`direct_split` replays, the incremental
plan's replay, the candidate replays) and their sum. `search_stats["consistency_failure"]` as
§5.7. `candidates_considered`/`candidates_truncated` add the repair's path counts.

`search_stats["r021"]` (`r021.diagnostics/1`): `algorithm: incremental_graph_repair`, the §6
domain and its hash, `certificate: null`, `certificate_unavailable_reason: not_produced`
(`hard_timeout` when killed), `max_candidates_unit: paths_scored_per_chunk`, optional
`fallback` (`used`, `source`, `reason`) and `repair` (the object above without the path
counters). `work` (§5.2 units only): `quotes_executed`, `quotes_memoized`,
`internal_evaluations` = `evaluations.total` (**every** complete-plan replay inside the solve,
all stages; on `structural_trap` 12 = 6 `single_path` + 1 `path_split` + 1 incumbent + 4
repair), `paths_scored` (incumbent + repair), `admission_checks` (every `creates_cycle` call:
scored plus cycle-rejected paths, incumbent + repair), `repair_attempts`,
`checkpoint_restores`. The record validates under the unchanged R021-C/1 validator
(`test_diagnostics_record_satisfies_…`), including `W_LEDGER` against the worker meter. No
bound kind other than none is emitted. The earlier r1 text, which excluded the fallback
stage's replays from the unit, was an undercount (5 reported vs 12 actual) and is withdrawn.

### 8.1 The counting seam (narrow, optional, default off)

The embedded reference stage calls `evaluate` through its own module aliases, so an exact
total needs a count inside the evaluator. The smallest seam, modelled on the existing quote
meter (`pools.quote.metered_quotes`), is a context-local counter in `routing/evaluator.py`:

```python
_ACTIVE_EVALUATION_COUNTER: ContextVar[EvaluationCounter | None] = ContextVar(
    "active_evaluation_counter", default=None
)

@dataclass
class EvaluationCounter:
    count: int = 0

@contextmanager
def counted_evaluations() -> Iterator[EvaluationCounter]:
    counter = EvaluationCounter()
    token = _ACTIVE_EVALUATION_COUNTER.set(counter)
    try:
        yield counter
    finally:
        _ACTIVE_EVALUATION_COUNTER.reset(token)

def evaluate(bundle, case, plan, objective, quote=quote_exact_in) -> Evaluation:
    counter = _ACTIVE_EVALUATION_COUNTER.get()
    if counter is not None:
        counter.count += 1          # every call, ok or invalid
    ...                             # unchanged body
```

With no active counter (every existing caller, the runner and every reference solver) the
behavior, outputs and signatures are unchanged. `incremental_graph_repair.solve` wraps its
whole body in `with counted_evaluations() as n:` and reads `n.count` at the stage boundaries;
the runner's own final evaluation is outside the solve and not counted. Nothing else changes:
no parameter threading through `path_split`/`single_path`/`direct_split`, no framework. This
research emulates the seam without a production edit (`EvaluationCounter` in the test
module wraps each loaded `routing.algorithms` module's `evaluate` alias for the duration of the
solve and counts its own replays), and independent wrappers confirm the total
(`test_internal_evaluations_is_the_total_…`). If WHI-1554 cannot land the seam, it must omit
`internal_evaluations` from `work` and state the unit as unavailable; a partial number is
never reported under the unit.

## 9. Independent evidence (this issue; WHI-1554 must port or re-run it)

The oracle in the test module has its own adjacency, simple-path enumeration, Kahn cycle test,
hand integer CPMM formula (fee-generalized UniswapV2 `getAmountOut`, dust → failure) and
aggregate-flow accounting, and enumerates every sequence of `𝒟(K, H)`. It imports no search,
solver or evaluator code.

### 9.1 Fidelity of the incumbent stage

`test_repair_off_is_incremental_graph_…`: with `repair: false`, status, plan, score, candidate
counts, 13 search counters, the published candidate sequence and the worker meter equal
`incremental_graph.solve` on the fixtures, every `mantle_mixed` case (real CL/LB/CPMM state)
and 80 random multigraphs with quote and candidate budgets (400 in development).

### 9.2 Checkpoint, restore and stale state

| Check | Shows |
| --- | --- |
| `test_checkpoint_round_trip_is_exact_everywhere` | Lemma R from every checkpoint: identical flows (values, `order`, insertion order), edges, decisions, merged plan; zero new executed quotes |
| `test_checkpoint_keeps_carry_zero_input_flows_and_order_slots` | `carry_and_zero_flow`: carries 62/63 enter checkpoints; `p1` (A→D, `x = 0`, `order` 3) is a committed record and edge though absent from the plan |
| `test_order_metadata_is_part_of_the_restored_state` | `order_metadata`: renumbered `order` values give a different merged plan (same gross 86,491): `order` is part of the plan identity |
| `test_subtracting_a_non_suffix_chunk_leaves_stale_aggregates` | subtracting chunk 1 of the trap while keeping chunks 2–3 leaves `y_p ≠ f_p(x_p)`; the evaluator exposes it |
| `test_stale_flows_are_detected_before_any_candidate` | flows not rolled back: the round-trip guard stops the repair (`consistency_failure`, 0 attempts, incumbent kept); unguarded, `merged_plan` refuses the doubled flows |
| `test_stale_token_edges_lose_the_valid_improvement` | edges not rolled back: the fix path is wrongly inadmissible, never built; result 94,153,558 < 111,178,819 |
| `test_reused_exact_closure_must_not_survive_a_rollback` | the actual `_ExactReuse` rejects the fix path after the incumbent's commits; a fresh closure admits it |
| `test_a_stale_score_as_first_choice_is_rejected_by_the_replay_check` | a `Scored` result computed on another state carries stale aggregates; conservation or replay = accounting catches it |

### 9.3 The greedy trap (`structural_trap`, NEW)

Six CPMM pools on S, B, E, D; 3,000,000 S→D; 3 chunks; 3 hops. `incremental_graph` returns
its incremental plan **90,545,314** (`path_split` 83,270,629): chunk 1 takes its greedy
maximum S–B–E–D (sb, eb, ed), committing B→E; chunks 2–3 take S–E–D and S–B–D. The oracle
reproduces 90,545,314 for that sequence and, over all 120 sequences of `𝒟(3, 3)`, finds the
unique optimum **111,178,819**: S–E–B–D (es, be, bd), then S–E–D twice. Its first path uses
E→B, which `creates_cycle` forbids after the incumbent's chunk 1. The repair visits
checkpoints 2, 1, 0: two rejected, two duplicates (forced choices that reconverge), then two
acceptances at checkpoint 0 (111,176,933, then 111,178,819). The plain evaluator replays the
returned plan to 111,178,819 with full fill, and the rebuilt accepted candidate's chunk
sequence is the oracle's optimum (`test_repair_finds_the_independent_optimum_…`).

At 3, 5 and 10 chunks every accepted candidate keeps the incumbent's decisions before its
checkpoint, changes the first suffix choice and the flow key, and its oracle value equals its
evaluated gross; at 10 chunks one acceptance keeps a non-empty prefix. At 4 chunks the repair
reaches the oracle optimum 111,109,071; at 5 chunks it improves 92,911,795 to 111,172,169 but
the optimum is **111,193,897** (`test_repair_is_not_optimal_even_in_its_own_domain`).

Cross-check with WHI-1549's independent evidence (`history-labels.json` `greedy_trap`, 4 hops,
2 chunks): the incumbent is `path_split`'s 10,020,569 (the incremental plan 9,943,929 loses
to it); the repair replays to exactly **12,757,712**, WHI-1549's `metis_inspired` L4 value.

### 9.4 Rejection, ties and stops

`twin_pools` (three identical S–D pools, 2 chunks): one duplicate (the forced a2-first
reconverges to the same flows) and one rejected candidate below the retained `direct_split`
plan; plan, score and publications equal the repair-off control. `tie_state` (WHI-1549): the
candidate replays to 9, equal to the retained `single_path` plan: `tie`, the earlier incumbent
stays. A single route has one checkpoint and no alternative. `max_repair_attempts: 1` stops
with `attempt_cap`; an unreachable pair is `no_route` with `no_trace`. Two runs are identical.

### 9.5 Ledger, budgets and interruption

`test_one_quote_ledger_covers_every_stage`: `quotes_executed` equals the worker meter with
repair on and off; the incumbent stage's counters are unchanged by the repair.
`test_the_budget_is_never_reset_for_the_repair`: for every `max_quotes` from the repair-off
solve's count to the repair-on count the meter never exceeds the limit, the result is `ok` and
never below the control; at the repair-off count the repair-off plan comes back
(`quote_budget`, `truncated_by: max_quotes`); one quote less cuts the incumbent and the repair
never starts. `test_interruption_leaves_only_complete_replayed_publications`: the runner's
hard quote meter kills the solve at every executed quote in turn; the publications so far are
always a prefix of the uninterrupted strictly improving sequence and the last is a complete
full-fill plan. `test_a_kill_inside_the_repair_stage_keeps_the_published_incumbent`: a kill
at the first repair quote leaves exactly the control's publications.
`test_candidate_cap_applies_to_every_rebuilt_chunk`.

### 9.7 Fail-closed replays and the evaluation total (r2)

`test_a_corrupted_in_solve_replay_is_never_accepted_or_published` (15 cases): the
specification's own `evaluate` is corrupted at replay 1 (stage 2) or 2–5 (the four repair
replays) of `structural_trap`, by gross +1, gross −1 or status `invalid_plan`. In every case the
returned score equals an independent reference replay of the returned plan, the publications
are a prefix of the uncorrupted ones, `repair.stop` is `consistency_failure` with the right
stage and index, and the result is the best plan validated before the fault. At replay 1 that
is the retained `path_split` plan, 83,270,629, with no repair attempt. The r1 specification
returned 90,545,315 there, with the corrupted incumbent published. That was the defect this
check fixes. `test_without_a_validated_plan_a_consistency_failure_is_an_algorithm_error`: with
stage 1 stubbed to `no_route`, a corrupted stage-2 replay gives `algorithm_error`, no plan, no
score and no publication, with the provenance in the error. Uncorrupted, the incumbent is the
first valid candidate, and a fault at the first repair replay keeps it. Repair-off fidelity
(§9.1) still holds on every uncorrupted run.

`test_internal_evaluations_is_the_total_of_every_in_solve_replay`: independent counting
wrappers on every `evaluate` alias (the specification module and every loaded
`routing.algorithms` module) count exactly `evaluations.total` on the fixtures, every
`mantle_mixed` case and 40 random graphs with budgets, repair on and off. On
`structural_trap` that is 6 `single_path` + 1 `path_split` + 5 own = 12; `repair_evaluations`
(4) is separate, and the diagnostics record's `internal_evaluations` is 12.

### 9.6 Real state and random graphs

Every `mantle_mixed` case with repair on: 0 consistency failures, every replayed candidate's
evaluated gross equals its accounted gross, meter equality, never below the control, full
fill. 200 random multigraphs (dust to large, fee 0–100 bps, 2–4 hops, 1–13 chunks, budgets):
the same invariants, and every accepted candidate's rebuilt sequence is valued identically by
the oracle.

## 10. Bounded tuning probe (tuning split only; evidence for the preset)

Probe: `PYTHONPATH=. uv run python tests/routing/test_suffix_repair_contract.py probe
<bundle_tuning> <out.json> [--hops H] [--chunks K] [--rule structural|trailing]
[--checkpoints N] [--alternatives N] [--attempts N] [--max-quotes N] [case ids]`. Per case it
runs the repair-off control and the repair-on specification with the same settings, objective
(`gross_only`) and quote budget. It is a separate diagnostic pass on a shared loaded host: no
timing is recorded or claimed, and the parallel shards are not clean measurements.
`trailing` (the last `max_checkpoints` decisions, structural or not) is a probe-only ablation
of the registered rule, not an option. Settings are the frozen profiles' values: `full`
(3 hops, 50 chunks, 300,000 quotes) and `daily` (2 hops, 200 chunks, 50,000 quotes),
`max_splits` 4, `percent_step` 5. Bundle hash `ee7afa7e…`. Every run in the table was
produced by the **r1** specification, hash `f90afb2e…` (source file `a1115206…`, committed
unchanged in `3d0afbd`, archived with the artifacts); they stay bound to it (§10.1). The
per-run summaries and shard file hashes are pinned in
[`fixtures/suffix-repair.json`](fixtures/suffix-repair.json) `probe`, the raw shards are in the
artifact directory. Gain is `(on − off)·10⁴/off` bps per
case; work ratios are on/off `quotes_executed` and (incumbent + repair)/incumbent
`paths_scored`.

| Run (96 cases unless noted) | Improved / equal / worse | Mean gain (all) | Max gain | Quotes ratio p50 / max | Paths ratio p50 / max | Outcomes (acc / tie / dup / worse) |
| --- | --- | --- | --- | --- | --- | --- |
| **3 hops, 50 chunks, preset** | 23 / 73 / **0** | 0.103 bps | 9.776 bps | 1.193 / 2.372 | 5.070 / 8.475 | 27 / 90 / 74 / 304 |
| 3 hops, 50 chunks, `trailing` ablation | 7 / 89 / 0 | 0.000144 bps | 0.0104 bps | 1.018 / 1.082 | 1.316 / 1.320 | 7 / 46 / 318 / 361 |
| 3 hops, 50 chunks, stress (24-case subset: every 4th case) | 7 / 17 / 0 | 0.443 bps | 10.112 bps | 1.372 / 2.275 | 9.516 / 19.772 | 10 / 36 / 39 / 180 |
| **2 hops, 200 chunks, preset** | 13 / 83 / **0** | 0.000203 bps | 0.0061 bps | 1.194 / 2.562 | 4.338 / 8.840 | 13 / 57 / 123 / 198 |
| 2 hops, 200 chunks, `trailing` ablation | 9 / 87 / 0 | 0.000511 bps | 0.0131 bps | 1.011 / 1.063 | 1.080 / 1.080 | 11 / 51 / 343 / 327 |
| 2 hops, 200 chunks, stress | 22 / 74 / 0 | 0.000472 bps | 0.0182 bps | 1.409 / 3.678 | 7.850 / 16.660 | 32 / 89 / 215 / 441 |

Every run: all cases `ok` with repair on, every repair `stop: complete`, **0 consistency
failures**, **0 cases below the control**, largest repair-on solve 99,412 quotes (3 hops) and
14,227 (2 hops), at most 7 structural decisions per case (3 hops; 5 at 2 hops).

Reading (descriptive tuning facts, not a verdict):
- (a) The mechanism is correct at corpus scale: no regression, no consistency failure, every
  accepted candidate replayed; this is the evidence for P1 and T1 beyond the fixtures.
- (b) The effect is small. At 3 hops 23/96 cases improve, but 22 of them by less than
  0.1 bps; one case (`emp-779ded-09bc4e-low-3`, incumbent `path_split`) gains 9.776 bps at
  decision 0. At 2 hops every gain is below 0.02 bps. Nothing here predicts a worthwhile
  report-split effect.
- (c) Structural checkpoints are the productive ones: at 3 hops the preset finds 23
  improvements and the 9.8 bps case where the equally capped `trailing` ablation finds 7, all
  below 0.011 bps. 12 of the preset's 27 acceptances are at decision 0 (the empty prefix).
- (d) Cost: the preset scores a median 5.1× (max 8.5×) the incumbent stage's paths and
  executes 1.19× (max 2.37×) its quotes. Checkpoint rescoring is memo hits; rebuilt suffixes
  are the cost. A time budget calibrated for `incremental_graph` may therefore not hold for the
  repair arm: WHI-1562 must report `timeout` counts, not assume headroom.
- (e) The stress profile, on the 24-case subset, finds 3 better plans than the preset
  (10.112 and 0.516 bps where the preset found nothing; 0.006 vs 0.001 bps), at about twice
  the preset's work (paths p50 9.5×, max 19.8×). Two were missed because the latest-first cap
  excluded decision 0 in a case with 5–6 structural decisions, one because the winning
  alternative ranked third at decision 0. This is recorded, not tuned away: the bounded preset
  stays `4 / 2 / 8` (bounded work, the only setting evaluated on all 96 cases at both depths),
  and a change follows the §12 rule.

### 10.1 Provenance after the r2 correction (no silent rebinding)

The r2 specification (hash `fe824058…`, asserted as the current `spec_sha256()`) differs from
the r1 one only as follows (the full diff of the specification section is archived as
`spec-section-r1-to-r2.diff`). It adds the stage-2 replay/accounting check, the consistency
provenance and the `algorithm_error` branch, and the all-stage evaluation counter. It also
renames `repair.internal_evaluations` to `repair.repair_evaluations`. These change a result
only when a replay disagrees with its accounting. The r1 records therefore keep their r1 hash,
and one focused rerun (not the campaign) shows that their numbers stand:

| r2 rerun (all 96 tuning cases, same settings and shards) | Per-case differences in the r1 fields | Stage-2 or repair consistency failures | `internal_evaluations` total (sum, on) | of which `repair_evaluations` (= r1's repair-only count) |
| --- | --- | --- | --- | --- |
| 3 hops, 50 chunks, preset | **none** (statuses, scores, sources, decisions, quotes, paths, repair counters, acceptances, outcomes) | 0 / 0 | 242,432 (fallback 241,915) | 421 |
| 2 hops, 200 chunks, preset | **none** | 0 / 0 | 14,699 (fallback 14,335) | 268 |

The ablation and stress runs were not rerun. Their per-case stages 1–2 are the same
computation as the preset runs at the same settings (identical `score_off`, `quotes_off` and
`incremental_status` in every case). The rerun shows that the new stage-2 guard never fires
there, and their repair stages recorded 0 consistency failures under the unchanged
repair-stage guard. Their outcomes therefore stand, bound to the r1 hash. The totals also show
the size of the r1 undercount: the fallback stage makes over 99% of the in-solve replays.

## 11. Outcome: `go`

**Supported (GO scope).** The algorithm of §5 with the schema of §7 is implementable on the
unchanged `incremental_graph` pieces without a trace seam, for every admitted protocol and
the objectives `incremental_graph` supports; exact all-stage `internal_evaluations` needs only
the default-off evaluator counter of §8.1. Its checkpoint is complete (Lemma C, R), every
complete candidate conserves the input and replays exactly (T1), without a hard kill it is
never below the repair-off control under the same budget (P1), and an inconsistent replay
fails closed (P2). A reproducible greedy trap shows
the neighborhood changing a valid final plan into the independently known optimum
(`structural_trap`), and the WHI-1549 trap is repaired to its independently replayed L4 plan.
The preset is finite and bounded (§5.8), and the tuning probe shows no regression and no
consistency failure.

**Not supported / not claimed.** No optimality (even in `𝒟`, §3.7), no guaranteed or
worthwhile improvement (the tuning effect is small, §10 (b)), no latency or same-budget win,
no coverage of non-structural lock-in, no protection against a hard wall kill turning an `ok`
record into `timeout` (§3.5, §10 (d)). `go` means "implement and measure as an experimental
identity", not a recommendation to adopt; the disposition is WHI-1562's under R021-C/1 §8.

## 12. Amendment text for WHI-1554 (for the parent to apply)

> **Contract:** implement `incremental_graph_repair` exactly as
> `docs/references/research-021/suffix-repair.md` §4–§8 (normative; executable form
> `repair_solve` in `tests/routing/test_suffix_repair_contract.py`) in a new
> `routing/algorithms/incremental_graph_repair.py`. Stages 1–2 re-express
> `incremental_graph.solve`'s reference loop (`graph_reuse` off) on its public `PoolFlow`,
> `chunk_amounts`, `creates_cycle`, `merged_plan`, `path_split.solve`, one per-solve
> `QuoteCache` and the guarded meter, recording the §4 checkpoint before every decision. No
> change to `incremental_graph.py` is needed (no trace seam). Stage 2 accepts the incremental
> plan only if its replay is `ok` **and** its gross equals the accounted gross; otherwise it
> neither replaces nor publishes, sets `incremental_status` (`invalid_plan` /
> `consistency_failure`) and does not start the repair (P2). With `repair: false` on
> uncorrupted runs the result must equal `incremental_graph.solve` in plan, evaluation,
> status, counters, publications and metered quotes; that differential is also the guard
> against drift. Stage 3: structural
> checkpoints latest first (≤ `max_checkpoints`); restore by copy into fresh objects (never
> subtract); rescore with the round-trip guard; alternatives with a positive marginal, by
> marginal then enumeration index, excluding the incumbent's path (≤
> `alternatives_per_checkpoint`); forced-first rebuild with the reference rule (≤
> `max_repair_attempts` in total); flow-key duplicates not replayed; `merged_plan`, in-solve
> `evaluate` on the guarded cache, evaluated gross = accounted gross; strict acceptance by
> `objective.score`, ties keep the earlier incumbent; publish only accepted complete replays.
> Drop every state-dependent cache on restore (§4 table); keep the pure quote memo and the
> ledger; never reset a budget or timer. Stops, retention, statuses and the
> `consistency_failure` provenance/error as §5.7: a consistency failure keeps the best plan
> validated before it, or is `algorithm_error` with no plan when none exists.
> **Evaluation seam (the one production edit outside the new module):** add the §8.1
> default-off `counted_evaluations()` context counter to `routing/evaluator.py` (a
> `ContextVar`, one increment at the top of `evaluate`, no signature or output change) and
> wrap the whole `incremental_graph_repair.solve` body in it. Its own tests: every existing
> evaluator/solver test unchanged; with no active counter nothing is counted; every call
> (ok and invalid) is counted inside the context; the previous counter is restored on exit and
> on exception. No other module, parameter or framework changes.
> **Options** (§7): `repair` bool, `max_checkpoints` 1…64, `alternatives_per_checkpoint`
> 1…16, `max_repair_attempts` 1…256; all required; reserved keys refused. Preset v1 =
> `{repair: true, max_checkpoints: 4, alternatives_per_checkpoint: 2, max_repair_attempts:
> 8}`; the `repair_off_on` control is the same with `repair: false`; stress
> `{true, 16, 4, 64}` only via `--strategies profile`.
> **Domain/row/diagnostics:** §6 domain (repair on and off share one hash), §6 row, §8
> `search_stats["repair"]` (with `repair_evaluations`, candidate replays only),
> `search_stats["evaluations"]` `{fallback, incumbent, repair, total}` and
> `search_stats["consistency_failure"]`; the `r021` record (certificate null, `not_produced`;
> unit `paths_scored_per_chunk`; `quotes_executed` = worker meter; `internal_evaluations` =
> the seam's all-stage total, never a partial count; omit the unit, stated unavailable, if the
> seam is absent).
> **Checks:** port §9 onto the registered factory: the fidelity differential (fixtures,
> `mantle_mixed`, random with budgets); the round trip from every checkpoint;
> `carry_and_zero_flow`, `order_metadata`; the §9.2 stale-state cases as mutation tests of the
> real restore (stale flows, stale edges, stale reuse closure, stale score, subtraction; each
> must fail or be caught); `structural_trap` (90,545,314 → oracle optimum 111,178,819, the
> outcome sequence), the 5-chunk non-optimality (111,172,169 < 111,193,897), the WHI-1549
> trap (12,757,712), `twin_pools`, `tie_state`, `attempt_cap`, `no_trace`, determinism; the
> ledger/meter, never-reset budget sweep and hard-meter interruption sweep; the candidate cap;
> `mantle_mixed` and random invariants with oracle recomputation; the unchanged R021-C/1
> validator; the §9.7 fault injections (+1, −1, `invalid_plan` at stage 2 and at every
> repair replay; no stage-1 plan → `algorithm_error`) against an independent reference
> replay; the evaluation total against independent wrappers on every `evaluate` alias
> (`structural_trap`: 12 = 6 + 1 + 1 + 4); case-order/state-leak; both CLI paths (`main.py run` and `main.py quote
> --details`) with the preset and the control, showing the distinct identity and repair
> provenance.
> **Tuning (before any report-split run, `bundle_tuning` only):** run preset and control at
> the profile's settings; report per case statuses (all scheduled), `repair.stop`, outcome
> counts, acceptances, `quotes_executed`, `paths_scored`, gross vs the control (wins / ties /
> losses) and `timeout` counts. Preset change rule: only with a new recorded tuning run of
> `max_checkpoints` ∈ {4, 8} × `alternatives_per_checkpoint` ∈ {2, 4} (attempts = product)
> on the same split, choosing the smallest setting whose improved-case count is maximal and
> whose largest work ratio keeps every tuning case inside the profile's time limit; record
> it and freeze the preset bytes/hash before the report comparison (R021-C/1 §6.3). No
> report-split tuning, no speed claim without L01's host rule.
> **Claims:** T1, P1 and P2 only; no optimality, improvement or latency claim.

## 13. Shared-contract impact

No shared schema change. The record validates under the unchanged R021-C/1 validator and
every unit, grid kind, pool-reuse value and comparison class used is registered. Notes for the
parent, not edits:
1. `contract-v1.json` `incremental_graph_repair.separate_caps` still reads "repair
   windows/attempts as algorithm_options (WHI-1553)". The filled values are
   `max_checkpoints`, `alternatives_per_checkpoint`, `max_repair_attempts` (§6–§7); a row fill
   like WHI-1557's is the parent's decision.
2. `internal_evaluations` is the all-stage total (R021-C/1 §5.2 unchanged). Reporting it
   exactly needs the §8.1 default-off counter in `routing/evaluator.py`. That is outside
   WHI-1554's current expected scope ("new module; a seam only in `incremental_graph` if
   needed"), so the §12 amendment names it explicitly. It is the narrowest option: one
   `ContextVar` in one file, with no parameter threading through `path_split` /
   `single_path` / `direct_split`. The same counter would also give exact totals to other
   identities (`metis_history`, `direct_split_certified`), which the parent may reuse. This
   memo does not require them to.

## 14. Reproduction

```bash
uv run pytest tests/routing/test_suffix_repair_contract.py -q
uv run pytest tests/routing/test_incremental_graph.py tests/routing/test_history_labels_contract.py tests/docs/test_research_021_contract.py -q
(cd docs/references/research-021/sources && shasum -a 256 -c SHA256SUMS)
C=…/router-algorithms-optimizer/data/corpus/mantle-5src-101082044
PYTHONPATH=. uv run python tests/routing/test_suffix_repair_contract.py probe \
  "$C/bundle_tuning" out.json --hops 3 --chunks 50 --max-quotes 300000 [case ids]
```
