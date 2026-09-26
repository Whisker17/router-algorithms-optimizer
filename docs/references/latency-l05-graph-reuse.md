# L05 — exact graph marginal-score and cycle-admission reuse (WHI-1507)

Status: **integrated default-off, for correctness, under the owner's development-first
amendment of 2026-09-26.**

- The reuse is selected only by passing `graph_reuse=True` explicitly:
  `routing.algorithms.incremental_graph.solve(case, context, budget, graph_reuse=True)`.
- Every ordinary caller runs the unchanged reference chunk loop: the registry's
  `FACTORY.solve`, the runner, the worker and every profile call `solve(case, context,
  budget)`. No profile, schema, search policy, registry entry or default changed. The
  other five algorithms are untouched (`path_split`/`direct`/`single_path`/
  `direct_split`/`uni_sor_port` code is not in the diff).
- The **performance adopt/reject decision is deferred to WHI-1510**. This note makes no
  adoption claim and no speedup claim. Every timing here is an in-process diagnostic,
  not an L01 run.

Research key L05 ([latency-optimization-research.md](latency-optimization-research.md)
§3.3). Base `700daef` (0.1.2 `dev`, WHI-1506 merged). Protocol L01 v1 is unchanged.

## 1. Disposition

| Piece | Disposition | Evidence |
| --- | --- | --- |
| Reuse of a path's chunk result while its pools' aggregate state and the actual chunk amount are unchanged | **Implemented, explicit and off by default.** Exact on unit/random/fixture evidence and on all 50 L01 `incremental_graph` records in both case orders. Performance pending WHI-1510 | §§3–5 |
| Incremental cycle admission (transitive closure of committed edges, monotone rejections) | **Implemented, same switch.** Exact against `creates_cycle` on 12,000 random growing-DAG checks and in every solve | §§3–4 |
| Skipping the per-path loop entirely (heap/argmax index over kept scores) | **Not built.** Stable first-wins ties, per-chunk `max_candidates` truncation and failure counting are defined by the ordered loop; after reuse the loop is a few dict lookups per path | §5 |

## 2. Remaining repeated work after L02 + L03 + L04

Re-profiled on the explicitly selected post-L04 control (`l04`: WHI-1506's
`l02_l03_l04`, fresh per solve), sentinel `full_source` USDC→USDT 1000, `daily_gross`
(2 hops, 200 chunks): 340 paths × 200 chunks. cProfile, instrumented, one solve:
[profile-0963f4d.json](latency-l05/profile-0963f4d.json).

- `marginal` **68,000 calls, 0.37 s** and `creates_cycle` **68,000 calls, 0.25 s**
  cumulative within a 1.11 s solve. The embedded `path_split` is 0.45 s, the final
  evaluations 0.18 s. Quote-kernel work is small after L04: the whole solve executes
  only 1,918 quotes, while its `QuoteCache` answers **58,613 lookups from memo**. 57,012
  of those are the chunk loop's re-walks that L05 removes; the rest belong to
  `path_split` and the evaluations. The remaining cost is path-level rescanning, as the
  research predicted.
- The reference loop has no memo across chunks, so a path whose pools no chunk touched
  is re-walked every chunk with the same amount.

## 3. The change and its exactness contract

**Score dependency.** For a chunk of actual amount `A` (carry included), a path's
result -- marginal output plus pool updates, or its first failing prefix with the
counted reason -- is a pure function of `A` and the committed aggregate
`(amount_in, amount_out)` of each pool on the path. Quotes are on the pools' original
bundle states through the solve's `QuoteCache` (`quote_exact_in` is pure). Pools on a
path are distinct (a path never revisits a token).

**Kept results.** `_ExactReuse` keeps results per `(A, path index)`:

- A committed chunk drops the kept results of every path containing any pool of the
  committed path (`pool_paths` index), even if a pool's aggregate did not change.
- Another amount is another key, so a chunk-size change (floor/ceiling sizes of a
  nondivisible input) or a carried sum never answers from a different amount.
- Only the two most recently used amounts are kept (uncarried chunks have at most two
  sizes). At most `2 × paths` results are held. Eviction only loses reuse, never
  exactness. On the corpus it lost none (§5).
- A kept success's pool updates are rebuilt at commit with the first-use `order` the
  reference assigns at scoring time (`len(flows)` before the commit), as fresh objects.

**Budgets and counters.** A reused result makes no quote call. The reference would have
answered every quote of that result from the `QuoteCache`: same pool, direction and
`x + m` keys. So the meter, the `max_quotes` pre-check (which exempts memo hits),
`quotes_executed` and truncation points are unchanged. The loop itself is unchanged:
hop-major order, first-wins ties (`m > best`), `max_candidates` per chunk,
`paths_truncated`, carries and the final-chunk zero-marginal commit. Logical counters
(`paths_scored`, `paths_rejected_cycle`, `marginal_failures`, `marginal_incomplete`,
`incomplete_example`) equal the reference's. A kept failure is counted where the
reference counts it: by the first scored path of the chunk that reaches the failing
prefix, with that path's label. It is then marked in the chunk's prefix memo so a
recomputed path sharing the prefix does not count it again. The only field that
differs is the physical `quotes_memoized` (fewer cache lookups). The run adds
`search_stats["graph_reuse"]` with the physical counters (reused / recomputed scores,
invalidations, peak entries, evicted amounts, reused / executed cycle decisions,
closure edges). These include the chunk a budget aborted, which `paths_scored` does not.

**Admission.** Committed token edges only grow within a solve.

- A path once found to close a cycle stays rejected (a superset of a cyclic graph is
  cyclic). An admitted path is re-checked only when an edge was added since.
- The check covers the whole proposed path at once, against the transitive closure of
  the committed (acyclic) edges. Path `t0 → … → th` is cycle-free by itself, and
  `committed ∪ path` is cyclic iff some `t_i` is reachable from a later `t_j` in the
  committed graph. That is exactly `creates_cycle`. It includes a cycle closed only by
  several new edges together (committed `C → A`, path `A → B → C`: each edge alone
  is safe) and a pool proposed against its committed direction.
- The check reads only. A rejected proposal changes nothing. Only a committed chunk
  extends the closure (`u → v`: every `w` with `w = u` or `u ∈ reach(w)` gains `v`
  and `reach(v)`).

**Evaluation.** Unchanged: `merged_plan` of the committed flows, `evaluate` of the
complete plan through the same seam, and a strict-better comparison against the
retained `path_split` candidate. The runner's independent evaluation re-checks it.

**Lifetime.** One instance per solve, created inside `solve`. There is no module state,
and nothing is written to the prepared object, the bundle or the context.

## 4. Correctness evidence

**Tests** (`tests/routing/test_incremental_graph.py`, 17 new). Each compares
`graph_reuse=True` with the reference solve for plan, evaluation, score, status, error,
candidate counters, published candidates, metered quote count and every search counter
except the two physical fields. They also replay the returned plan independently.

| Test | Covers |
| --- | --- |
| `test_reuse_matches_the_reference_on_random_graphs` | 40 random CPMM graphs (3–5 tokens, 4–9 pools, parallel pools, both directions, fees 0–100 bps) × dust / mid / nondivisible large amounts × (2 hops, 1 chunk), (3, 7), (4, 20). Asserts the run exercised reuse, invalidation, amount eviction, closure growth, cycle rejections and carries |
| `test_reuse_preserves_declared_budgets` (×3) | `max_candidates` 1 and 3, `max_quotes` 40 on 12 random graphs. A `max_quotes` truncation of the chunk allocation on the shared-prefix fixture |
| `test_reuse_matches_the_reference_on_real_mixed_state` (×8) | Real CL/LB/CPMM `mantle_mixed` state, every case, 2 and 3 hops, 37 chunks |
| `test_reuse_preserves_fixtures_fallback_and_statuses` | Shared-prefix / shared-suffix / single-pool fixtures at dust and nondivisible amounts. Fallback to `path_split` under a per-call cost. `no_route`. `incomplete_snapshot` only |
| `test_reuse_counts_shared_failing_prefixes_where_the_reference_does` | Two paths behind one incomplete-state CL hop: the failure is counted once per chunk (5), with the first path's label |
| `test_reuse_is_explicit_and_per_solve` | Default `False`, the registry's `solve` is the plain function, no `graph_reuse` key by default. Forward, reverse and fresh case orders on one prepared object give equal results. The bundle and prepared object are unchanged |
| `test_closure_admission_equals_creates_cycle_and_is_atomic` | The multi-edge cycle and reverse-direction cases. A rejected trial leaves the closure/version unchanged. 200 random growing DAGs × 60 checks against `creates_cycle` |
| `test_differentials_catch_dropped_invalidation_rules` | Three in-test mutants (no pool invalidation; amount-blind keys; admitted paths never re-checked) each make the random differential fail. So those tests would catch a broken rule |

The relevant suites pass: `tests/routing`, `tests/benchmark`, `tests/docs` and
`tests/test_synthetic_run.py` (1,008 tests, including the new ones, the `uni_sor_port`
parity goldens and the worked-example goldens).

**Complete-solve differential, all 50 L01 `incremental_graph` records** (4 derived
bundles × (24 matrix cases + sentinel)). Tool `work` at `0963f4d`, both `--order fixed`
and `--order reverse`, clean tree. Raw data is gitignored under `data/latency-l05/`;
the compact rows are in [work-summary-0963f4d.json](latency-l05/work-summary-0963f4d.json).

- Variants: `ref` (everything default, re-run at this HEAD), `ref_l05` (default quote
  path + L05), `l04` (post-L04 control), `l04_l05`.
- **Per-call equality:** every CL swap the solver executed, in call order, has the same
  arguments and outcome in `l04` and `l04_l05` (`all_cl_calls_equal`).
- **Record equality:** all four variants equal the L01 default-reference baseline records
  of `bbda6e2` in every semantic and work field except the two physical fields
  (`all_baseline_equal`, `all_control_equal`). That covers 47 `ok` and 3 `no_route`
  records and the record truncated at 50,000 quotes (`bnd-78c1b0-201eba-round_at`).
- The final evaluation ran on the ordinary default path after the variant was removed.
- Reverse order gives the same per-case counters as fixed order
  (`rows_counters_equal_to_fixed`). The `graph_reuse` counters of `ref_l05` and `l04_l05`
  are identical: the reuse does not depend on the quote path.
- Limit: `daily_gross` has 2 hops, so no corpus path can close a cycle
  (`paths_rejected_cycle` 0). Rejections are exercised by the 3–4-hop random tests.

## 5. Accounting (diagnostic; the performance decision is WHI-1510's)

**Work**, 50 records, cold per solve, `l04` → `l04_l05`:

| Counter | l04 | l04_l05 |
| --- | ---: | ---: |
| Logical scored paths (`paths_scored`, unchanged) | 1,536,836 | 1,536,836 |
| `marginal` recomputations | 1,536,836 | 202,209 (1,334,843 reused) |
| `creates_cycle` calls / closure checks | 1,537,052 | 0 / 28,157 (1,508,895 reused) |
| `QuoteCache` lookups answered by memo (`quotes_memoized`) | 1,249,288 | 78,242 |
| Executed quotes (`quotes_executed`, unchanged) | 173,396 | 173,396 |
| Kept results, largest solve | — | 938 (`2 × 469` paths) |
| Instrumented CPU s, sum (single-shot) | 14.70 | 12.70 |

Sentinel `full_source`: 68,000 → 739 recomputations. There are 68,000 → 1,020 cycle
checks and 58,613 → 1,601 memo lookups.

- The unbounded first variant (`e848988`, same reuse count) held **91,201** results in
  the `round_at` record. In that record 194 chunks carry, so every chunk amount is new.
  The two-amount bound brought the largest solve to 938 entries with no lost reuse.
- **No-reuse case:** in `round_at`, no score is reusable (0 reused, 91,202 recomputed),
  so the reuse is pure overhead there.

**In-process paired timing.** Tool `paired` at `0963f4d`, raw data in
[paired-0963f4d.json](latency-l05/paired-0963f4d.json). Setup: 3 rotated pairs, cold per
solve, solve-window medians, `l04` vs `l04_l05`.

| Target | l04 s | l04_l05 s | ratio | max 1-min load |
| --- | ---: | ---: | ---: | ---: |
| sentinel `full_source` | 0.437 | 0.300 | 0.69 | 4.7 |
| matrix `emp-78c1b0-deadde-large-1` | 0.412 | 0.327 | 0.79 | 4.6 |
| matrix `bnd-78c1b0-201eba-round_at` (no reuse, quote-truncated) | 1.636 | 1.670 | 1.02 | 4.7 |

These are three samples on a shared 10-core host that was loaded minutes earlier (1-min
load under 5 during the samples, 15-min load about 30). They are not a distribution and
not a verdict.

**Memory** (tracemalloc solve + evaluation peak, instrumented): +57 KB (sentinel),
+207 KB (`large-1`) and +179 KB (`round_at`) over the control's 82 / 62 / 509 MB peaks.
Kept results are bounded by `2 × paths` entries plus one closure set per token and one
admission entry per path. There is no preparation cost: everything is built per solve
inside the measured window.

## 6. Effects on WHI-1508 / WHI-1510

- **WHI-1508 (L06):** unaffected. `uni_sor_port` and its goldens are untouched.
- **WHI-1510:** compare `l04` (or whatever quote-layer combination it adopts) with the
  same plus `graph_reuse=True`, both explicitly selected, back to back on a quiet host
  with L01-SB. Measuring the default-off integration is measuring the reference. The
  graph-only share is small next to `path_split` and the final evaluations. It must
  also decide:
  - whether the `round_at`-like no-reuse overhead is acceptable;
  - whether a profile-level switch is wanted (none exists; adoption would need a
    reviewed seam, not a new tuning framework).

## 7. Reproduction

Read-only inputs: the derived bundles and baseline records of L01 experiment
`data/latency-012/whi-1503/20260926T090434484129Z-d5061563` in the primary clone.

```bash
uv run pytest tests/routing/test_incremental_graph.py -q
uv run python tools/latency/l05_graph_reuse.py work --experiment <L01 dir> --out data/latency-l05/work-fixed.json
uv run python tools/latency/l05_graph_reuse.py work --experiment <L01 dir> --order reverse \
  --out data/latency-l05/work-reverse.json
uv run python tools/latency/l05_graph_reuse.py profile --experiment <L01 dir> \
  --target full_source/sentinel:quote-b5feb74821d5 --out data/latency-l05/profile.json
uv run python tools/latency/l05_graph_reuse.py paired --experiment <L01 dir> --pairs 3 \
  --target full_source/sentinel:quote-b5feb74821d5 \
  --target full_source/matrix:bnd-78c1b0-201eba-round_at \
  --target full_source/matrix:emp-78c1b0-deadde-large-1 --out data/latency-l05/paired.json
```
