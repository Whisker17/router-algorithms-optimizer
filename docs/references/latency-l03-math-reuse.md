# L03 — bounded exact reuse of tick and bin price math (WHI-1505)

Status: **integrated default-off, for correctness, under the owner's development-first
amendment of 2026-09-26.**

- The reuse is `pools.cl_math.TickMathReuse` and `pools.liquidity_book.BinMathReuse`. It is
  selected only by passing an instance explicitly: `concentrated.swap(..., math_reuse=...)`
  or `liquidity_book.swap(..., math_reuse=...)`.
- Every ordinary caller runs the unchanged reference code with no memo and L02 off. That
  covers `quote_exact_in`, the `pools.quote` dispatcher, the evaluator and all six solvers.
- The **performance adopt/reject decision is deferred to WHI-1510**. This document makes no
  adoption claim and no speedup claim. Every timing here is an in-process diagnostic on a
  loaded shared host. None of it is an L01 run.

Research key L03 ([latency-optimization-research.md](latency-optimization-research.md)
§§3.2, 4). Base `2cb7a60` (release 0.1.2 `dev`, WHI-1504 merged). Protocol L01 v1 is
unchanged.

## 1. Disposition

| Piece | Disposition | Evidence |
| --- | --- | --- |
| Exact memo of `TickMath.getSqrtRatioAtTick(tick)` in the CL swap loop | **Implemented, explicit and off by default.** Exact on all 300 L01 records; performance pending WHI-1510 | §§3–5 |
| Exact memo of `PriceHelper.getPriceFromId(id, binStep)` in LB `getAmounts` | **Implemented, explicit and off by default.** Exact; the remaining work it removes is small on this workload | §§3–5 |
| Reusing LB's sorted bin-tree index across swaps of one state | **Not selected in this stage.** Re-sorting took 0.2–0.7 % of instrumented solve wall time on the measured bundles, and a reuse would need a state-identity key and invalidation | §6 |
| A nonzero-bitmap-word index for L02 empty spans | **Deferred.** It only matters if L02 is adopted (WHI-1510), and it was not measured here | §6 |

## 2. What remained after L02 (post-L02 re-profile)

All counts below come from a post-L02 solve that sets `skip_empty_spans=True`
**explicitly**. They are not from the default-off reference. Setting: sentinel
`quote-b5feb74821d5`, full-source derived bundle of L01 experiment `d5061563`, cProfile
([`reprofile.py.txt`](latency-l03/reprofile.py.txt)). The profile is instrumented, so
its shares are indicative only.

- **`uni_sor_port`:** 434,869 `getSqrtRatioAtTick` calls take 0.94 s of 6.27 s profiled
  tottime (15 %). The swap loop makes 431,374 of them, over only **6,790 distinct ticks**.
  The research's pre-L02 figure was 908,823 calls over 13,131 ticks. L02 roughly halves the
  calls, but the repetition stays.
- **`incremental_graph`:** `getSqrtRatioAtTick` takes 0.150 s of 2.34 s (6.4 %). There are
  12,677 `getPriceFromId`/`pow128` calls over 349 distinct `(id, binStep)` pairs, taking
  0.059 s (2.5 %).
- **Isolated microcheck:** the 431,374-tick sequence was replayed through the reference
  function and then through `lru_cache`.
  - Reference: 0.83 s.
  - Capacity 8192 or 65536: 0.048 s, including the 6,790 cold misses.
  - Capacity 1024 thrashes: 0.50 s.

  This is only a kernel microcheck. Solve-level numbers are in §5.

## 3. The change

**`TickMathReuse(capacity)` / `BinMathReuse(capacity)`.** Each instance owns one
`functools.lru_cache(maxsize=capacity, typed=True)` over the **unchanged** reference
function. There is no module-level cache: a test asserts that no module in
`pools.cl_math`, `pools.concentrated` or `pools.liquidity_book` holds a `cache_info`
object.

- **Key = the complete input.** For CL that is the tick. For LB it is `(bin_id, bin_step)`,
  so the same id under another bin step is a different key. Both functions are pure, so a
  hit is the reference result in any pool or snapshot. No pool- or state-derived value is
  cached.
- **Invalid input keeps its behavior.** Only plain `int` arguments reach the memo. Any
  other argument calls the reference function directly, so its result or exception is
  unchanged: `bool`, an `int` subclass with a custom hash, `float`, `str`, `None` or an
  unhashable list. Nothing is hashed first.
  - Out-of-range ticks (`'T'`) and underflowing bin prices
    (`Uint128x128Math__PowUnderflow`) run the reference function on every call.
    `lru_cache` never stores an exception.
- **Bounded and lazy.** `capacity` must be a positive plain `int`. `None` would be
  unbounded, and 0 or `True` would silently disable the memo or make it degenerate. Entries
  are added only on first use, with LRU eviction. Nothing is prepared per pool or per
  direction.
- **Accounting.** `stats()` returns `{capacity, entries, hits, misses}`, taken from the
  native `cache_info()`. `clear()` empties the memo.
- **Capacities are experiment settings, not defaults.** Each class takes `capacity` with no
  default. The evidence tool declares tick 16,384 and bin 4,096.

**The swap plumbing** is a keyword-only argument, `math_reuse=None`:

- In `concentrated.swap`, only the loop's `getSqrtRatioAtTick(tickNext)` goes through the
  memo. Traversal, search, the L02 fast path, the limit-tick bound and `getTickAtSqrtRatio`
  are unchanged.
- In `liquidity_book.swap` and `get_amounts`, only the bin price goes through the memo.
- No serialized schema, snapshot model, profile or config changes.

**Selecting L02 and L03.** L02 and L03 are independent keywords. The evidence tool
`tools/latency/l03_math_reuse.py` sets **both** of its variants explicitly:

| Variant | CL swap | LB swap |
| --- | --- | --- |
| `l02_only` (the post-L02 control) | `skip_empty_spans=True, math_reuse=None` | `math_reuse=None` |
| `l02_l03` | `skip_empty_spans=True, math_reuse=TickMathReuse(...)` | `math_reuse=BinMathReuse(...)` |

The default reference (L02 off, no memo) is **not** measured as "post-L02". Its outputs are
the L01 baseline records that every work row is compared against. The tool patches
`swap` only around `factory.solve` and restores it in `finally`. The independent final
evaluation then runs on the ordinary default path.

## 4. Correctness evidence

**Tests** (`tests/pools/test_concentrated.py`, `tests/pools/test_liquidity_book.py`):

| Test | Covers |
| --- | --- |
| `test_tick_math_reuse_vectors_are_exact_and_lru_bounded` | MIN/MAX/0/±1 and 400 random ticks equal the reference; exact hit/miss/entry counts at capacity 64 with eviction; an exact LRU order at capacity 2; `clear()` |
| `test_tick_math_reuse_invalid_and_non_int_inputs_behave_like_reference` (11) | out-of-range, huge, float, str, None, list, bool, a hash-colliding int subclass: identical result, or identical exception type and message, twice on a populated memo; nothing stored |
| `test_*_capacity_must_be_a_positive_int` (6 + 6) | 0, -1, None, 1.5, True and "8" are refused |
| `test_tick_math_reuse_swaps_match_reference_with_l02_off_and_on` | 80 generated pools × 3 sequential swaps (random limits, truncated windows, all sources). Complete `SwapOutcome` or identical error for reuse vs `None`, **separately with L02 off and L02 on**. Instances: one shared across all pools (fills and evicts at 4,096), a thrashing capacity-1 instance and a fresh one. Inputs unchanged |
| `test_tick_math_reuse_replays_contract_evidence` (6) | every deployed-bytecode fixture swap (Uniswap v3 / Agni / FusionX, real and controlled) with reuse, L02 off and on, equals the fixture-validated reference outcome |
| `test_ordinary_callers_never_use_tick_math_reuse` | default `swap`, the CL `quote_exact_in` and the `pools.quote` dispatcher call the reference `getSqrtRatioAtTick` once per logical step. With an explicit instance the reference is never called, `hits + misses == steps`, and the outcome is identical. No module holds a cache |
| `test_bin_price_reuse_vectors_are_exact_and_keyed_by_id_and_step` | 9 bin steps × about 90 ids, including ±(2^20 − 1) from 2^23: reverts are recomputed and never stored; the same id under another bin step gives another key and value; eviction |
| `test_bin_price_reuse_invalid_and_non_int_inputs_behave_like_reference` (9) | PowUnderflow ids, float/str/None/bool/list arguments |
| `test_bin_price_reuse_swaps_match_reference_on_contract_evidence` | every LB fixture sequence (4 real pairs + controlled) with shared, capacity-1 and fresh instances: complete outcome or identical revert; inputs unchanged |
| `test_ordinary_lb_callers_never_use_bin_price_reuse` | default LB `swap` and the dispatcher call the reference price once per swapped bin; an explicit instance replaces those calls and gives the identical outcome |

**Bounded manual probes.** Four single-line mutants were run once each against the new
tests, and each file was restored from its copy afterwards. This was not a mutation
campaign. Every mutant failed a test:

1. LB key without `bin_step`.
2. Dropping the non-int bypass.
3. `concentrated.swap` ignoring `math_reuse`.
4. Accepting capacity ≤ 0.

**Complete-solve differential over all 300 L01 records.** Setup: 4 derived bundles × 6
algorithms × (24 cases + sentinel), capacities tick 16,384 / bin 4,096, and a fresh (cold)
instance per solve. Result file:
[work-summary-8dfae64.json](latency-l03/work-summary-8dfae64.json).

- **`l02_only` vs `l02_l03`:** `all_semantic_equal` and `all_work_equal` are both **true**.
  The compared fields are status, evaluation, score, error, limit_hit, solver_reported,
  quotes, candidates and search.
- **`l02_l03` vs the L01 baseline records of `bbda6e2`** (the default reference):
  `all_baseline_semantic_equal` and `all_baseline_work_equal` are both **true**.
- This includes the `incremental_graph` record that is truncated at 50,000 quotes, and every
  `uni_sor_port` record. So the pinned SOR-port outputs and the other five algorithms are
  unchanged.

## 5. Accounting (diagnostic; the performance decision is WHI-1510's)

**Memo work across all 300 records** (cold per solve):

| Memo | Hits | Misses | Hit rate | Max entries in one solve | Records with eviction |
| --- | ---: | ---: | ---: | ---: | ---: |
| tick | 5,936,056 | 323,960 | 94.8 % | 8,577 | 0 |
| bin | 239,272 | 13,789 | 94.6 % | 1,079 | 0 |

- **Hit rates vary by algorithm.** Examples: `uni_sor_port` 97–98 %, `path_split` and
  `incremental_graph` about 90–96 %, `single_path` 60–73 %, and `direct` only **18–24 %**. For
  `direct` the cold misses make up most of the memo work.
- **The capacities were never reached** on this workload, so no eviction happened. That
  is one snapshot. The tests cover eviction.

**In-process paired timing.** Tool:
[paired-866392a.json](latency-l03/paired-866392a.json). Sentinel only, so no held-out case
was used. One prepare per target, then one charged warm-population solve. Three rotated
rounds follow, each with an `l02_only` solve, a `cold` solve (fresh instances, so first-use
population is inside the timed solve) and a `warm` solve (instances kept across solves).
Times are solve wall medians:

| Target (full source) | l02_only s | cold s (ratio) | warm s (ratio) | warm-population solve s | max 1-min load |
| --- | ---: | ---: | ---: | ---: | ---: |
| `uni_sor_port` | 3.463 | 2.745 (0.79) | 2.683 (0.77) | 2.685 | 11.98 |
| `incremental_graph` | 1.064 | 0.907 (0.85) | 0.888 (0.83) | 0.890 | 7.74 |
| `single_path` | 0.2256 | 0.1941 (0.86) | 0.1782 (0.79) | 0.1905 | 6.29 |
| `direct` | 0.00465 | 0.00470 (1.01) | 0.00367 (0.79) | 0.00481 | 6.27 |

- **Load:** every target ran above the 5.0 load bound, so these are contaminated
  diagnostics, not an adopt verdict.
- **`direct`:** the cold ratio of 1.01 shows the cold population cost is not hidden. Where
  keys barely repeat, a per-solve memo gains nothing. The warm figures depend on reuse
  that outlives a solve, and ordinary quotes do not have that today (no process-global
  state).
- **Single-shot CPU in the work run** (`l02_only` → `l02_l03`, loaded host):
  - full-source matrix `uni_sor_port` 12.55 → 10.56 s;
  - `incremental_graph` 12.86 → 11.74 s;
  - `direct` about 0.03 s both ways.

**Memory and setup** (tracemalloc pass, instrumented, `uni_sor_port` sentinel):

- **Constructor:** about 40 µs and 2.5 KB.
- **Solve peak:** 314.76 MB vs 313.19 MB for `l02_only`, so +1.57 MB.
- **Memo retained after the solve:** about 1.57 MB for 6,789 tick entries, roughly
  230 B per entry. This is the released-bytes difference against the control, which itself
  released 0.40 MB of other objects.
- **Other targets:** `incremental_graph` +1.65 MB peak (6,789 tick + 349 bin entries),
  `single_path` +1.65 MB, `direct` +0.11 MB.
- **Capacity bound:** at the declared capacities, a completely full memo is on the order of
  4 MB for ticks plus 1 MB for bins. That is an estimate from the per-entry size, not a
  measurement.

## 6. Not selected / deferred

- **LB sorted-tree reuse.** `liquidity_book.swap` rebuilds `tuple(sorted(state.bins))` on
  every swap. The diagnostic ([lb_resort.py.txt](latency-l03/lb_resort.py.txt),
  [lb-resort-diagnostic.json](latency-l03/lb-resort-diagnostic.json)) timed one extra
  identical sort per LB swap on the full-source bundles, measured at 866392a:
  - `single_path`, `path_split` and `incremental_graph` spend 0.2–0.7 % of their
    instrumented solve wall re-sorting;
  - the largest share is matrix `incremental_graph`: 26,647 LB swaps, 0.23 s of 34.2 s.

  A reuse would have to be keyed on state identity. The state's `bins` proxy wraps a
  private copy (`snapshot.models._freeze_mapping`), so that key is possible. But it adds
  lifetime and invalidation machinery, and cross-snapshot leakage risk, for a share that is
  small on this workload. It was **not selected in this stage**. That is not a universal
  claim: a pool universe with much larger books could change the balance.
- **Nonzero-bitmap-word index for L02.** With L02 on, each skipped empty word costs one
  mapping lookup. An index would only pay off if L02 is adopted, which WHI-1510 decides.
  It was not measured here and is **deferred**.
- **The existing exact-amount `QuoteCache`** (per solve, already present) is unchanged.
  It is not part of this work.

## 7. What WHI-1510 must measure

- **What an ordinary L01 run measures.** On the default-off integration it measures the
  reference path: L02 off, no memo. A valid measurement of L03 must compare **L02-only**
  against **L02+L03**, with both explicitly selected in a declared experiment identity or
  on candidate sources. It must not compare the reference against the reference.
- **Setup:** back to back, each side with its own L01-SB, on a quiet host.
- **Lifetime is a policy choice.** A per-solve memo charges its cold population inside
  every solve, as the `cold` column does. A longer lifetime, for example per process or per
  bundle, would need an explicit owner decision, and so would any default capacity.
- **Implication for WHI-1506 (prefix reuse across amounts).** L03 keeps the traversal
  unchanged and removes only repeated pure math inside it. Prefix reuse would remove whole
  repeated steps, so it overlaps with this memo. WHI-1506 should measure against
  L02+L03 (or whatever WHI-1510 adopts), not against the reference.

## 8. Reproduction

Read-only inputs: the derived bundles and baseline records of L01 experiment
`data/latency-012/whi-1503/20260926T090434484129Z-d5061563` in the primary clone. The raw
outputs are kept under the gitignored `data/latency-l03/` of the WHI-1505 worktree.

```bash
uv run pytest tests/pools -q
uv run python tools/latency/l03_math_reuse.py work --experiment <L01 dir> --out data/latency-l03/work.json
uv run python tools/latency/l03_math_reuse.py paired --experiment <L01 dir> --pairs 3 \
  --target full_source/sentinel:uni_sor_port:quote-b5feb74821d5 \
  --target full_source/sentinel:incremental_graph:quote-b5feb74821d5 \
  --target full_source/sentinel:single_path:quote-b5feb74821d5 \
  --target full_source/sentinel:direct:quote-b5feb74821d5 --out data/latency-l03/paired.json
```

The work run's source was commit `747382a`, with tree `8dfae64`. That commit was reworded
to `866392a`, which has the identical tree and is the paired run's source. Later commits
change only docs and evidence.
