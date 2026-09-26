# L02 — exact skipping of empty zero-liquidity CL spans (WHI-1504)

Status: **exactness established, performance adoption `inconclusive` (host-load blocker).**
Candidate source `8c7337a` (code); baseline `299b88a` (release 0.1.2 `dev` base, WHI-1503
merged). Protocol L01 v1 / L01-SB v1, unchanged. Research key L02
([latency-optimization-research.md](latency-optimization-research.md) §3.2).

## 1. Disposition

| Question | Answer | Evidence |
| --- | --- | --- |
| Is the change exact? | **Yes.** On every record in every L01 stage, the integer, state, status, failure, logical-feature and SOR-selection results are identical | §3 |
| Does it reduce executed work? | **Yes.** Across all 300 L01 records, the candidate runs 6.26 M of the 36.00 M logical CL iterations through `computeSwapStep` (−82.6 %) and makes 6.27 M instead of 36.00 M `getSqrtRatioAtTick` calls | §4 |
| Is it faster on the measured workload? | Every paired measurement points the same way (in-process ABBA held-out solves 27–88 % faster; L01 held-out solve improvements 44–81 %, cold charged `not_slower` everywhere). **But none of that is L01 adopt evidence**, because both L01 experiments are load-contaminated | §5 |
| L01 verdict | `inconclusive`. The only reason: `host load exceeded the protocol threshold in an experiment`. There are no coverage, determinism, semantic, sufficient-budget or work-counter problems | [comparison](latency-l02/compare-exact-08a7dfe8-e47f54fe.md) |

That is **not** "reject for performance". The protocol's minimum next step is one back-to-back
L01 pair on a quiet host (§6). No threshold was lowered and no run was repeated or discarded.

## 2. The change

`pools/concentrated.py:swap` got one fast path, used only while in-range liquidity is **0**.
In that state, a Solidity loop iteration whose target is an *uninitialized* tick strictly
before the price limit gets `(sqrtPriceNext, 0, 0, 0)` back from
`SwapMath.computeSwapStep`. Every delta is a multiple of zero liquidity, and
`fee < 1e6` keeps `mulDivRoundingUp(0, fee, 1e6 - fee)` from reverting. So the iteration
changes nothing but price and tick: no input, output, fee, protocol fee or fee growth, and
no crossing or LM-hook call. The fast path:

- runs the span's first iteration through the reference `nextInitializedTickWithinOneWord`,
  reading the same word. That iteration may start mid-word.
- steps over each later whole word only if it is **collected and zero**, and its far-end
  target tick is strictly before the limit. For that check the limit becomes one tick
  bound: `L = getTickAtSqrtRatio(limit)` for zeroForOne; for oneForZero, `L` or `L + 1`.
  `test_empty_span_limit_tick_matches_the_sqrt_comparison` checks this.
- adds every skipped iteration to `steps`, so the `swap_steps` quote feature, route
  features and evaluation traces stay the logical protocol counts.
- leaves the stopping iteration to the unchanged reference code. That covers the first
  word that is nonzero (an initialized tick), uncollected (the same read raises the same
  `MissingState`), or not strictly before the limit (limit, MIN/MAX tick clamp). That
  iteration always runs with zero liquidity, so it lands on its own target, and the
  skipped prices can never be observed.
- is disabled for `fee >= 1e6`. There every zero-liquidity iteration reverts. Skipping
  would change which error comes first when the span also meets an unknown word.

It never skips a positive-liquidity boundary. It adds no index, cache, preparation step or
retained memory; its extra state is a few per-call locals. Inputs stay immutable.
`swap(..., skip_empty_spans=False)` is the unmodified reference loop. It is kept only as the
differential oracle and paired-measurement control. `quote_exact_in` always uses the default.

An earlier word-by-word version (`3102755`) still called the bitmap search for every
skipped word. It was superseded before any L01 run. An instrumented cProfile showed that
per-word search taking most of the remaining solve CPU; the case was the held-out
`emp-78c1b0-deadde-large-1` / `uni_sor_port`, a structural diagnosis rather than parameter
tuning. The candidate has no tunable parameters.

## 3. Correctness evidence

**Deployed-bytecode fixtures.** The existing replays in `tests/pools/test_concentrated.py`
now run the fast path by default. They are independent Mantle-fork evidence for all three
admitted sources (Uniswap v3 / Agni / FusionX; real pools plus full-range controlled
pools), covering exhaustion to the bound both ways. The fixtures still match amounts,
next state, changed ticks and QuoterV2.

**Differential tests (new).** Each compares the complete `SwapOutcome` of the fast path and
the reference loop, or the identical exception type and message. The outcome covers
amounts, the new state (price, tick, liquidity, fee growth, protocol fees, crossed tick
data), crossed ticks, logical `steps` and LM-hook calls:

| Test | Covers |
| --- | --- |
| `test_empty_span_skip_matches_reference_on_contract_evidence` (6) | every fixture pre/post state × both directions × 6 amounts, plus every captured swap and limit. Asserts >1,000 skipped iterations on the controlled pools |
| `test_long_empty_span_then_initialized_liquidity` (2) | a 38-word gap then a liquidity band, both ways; Agni/FusionX hooks with non-zero/zero LM pool and both protocol-fee encodings |
| `test_exact_exhaustion_at_an_edge_then_zero_cost_transitions_both_ways` (2) | input exactly exhausted at a band edge (the loop must stop, not enter the span); +1 unit → partial fill to the bound; from the parked zero-liquidity state: onward (skip) and back (re-cross, no skip) |
| `test_price_limits_inside_an_empty_span` (2) | limits at word boundaries ±1 tick and ±1 sqrt unit, mid-word; final price == limit |
| `test_incomplete_range_inside_an_empty_span` (2) | the collected window ending inside the gap, and the first word itself uncollected. Same `MissingState` word/message; `INCOMPLETE_SNAPSHOT`; a limit before the unknown word is an ordinary fill |
| `test_span_ends_at_an_initialized_tick_on_the_word_edge` (2) | spacing 1, the next initialized tick on bit 255 / bit 0 of the next word |
| `test_dense_positive_liquidity_word_boundaries_are_never_skipped` | full-range liquidity: zero skipped iterations. Counterexample: one `computeSwapStep` across 30 uninitialized positive-liquidity boundaries gives different amounts from the per-word loop |
| `test_fee_at_the_pips_denominator_is_not_skipped` | `fee = 1e6`, with and without an unknown word in the span: the same revert |
| `test_quote_features_and_immutability_are_unchanged_by_skipping` | `quote_exact_in` results incl. `swap_steps` features identical; inputs unchanged; returned mappings read-only |
| `test_generated_sparse_and_dense_pools_match_reference` | seeded generator: 160 pools (spacing 1/10/60/200, 0–5 positions incl. full-range, all sources, fee protocols, LM pools, truncated windows), 640 swaps both ways, random limits, sequential state reuse. Guards on coverage: >300 fills, >60 with skips, >20 `MissingState`, >5 reverts |
| `test_empty_span_limit_tick_matches_the_sqrt_comparison` | the tick bound against the direct sqrt comparison at MIN/MAX/0 and 200 random ticks, limit ±1 |

**Mutation check.** 20 single-line mutants of the fast path were tried
([`latency-l02/mutants.sh.txt`](latency-l02/mutants.sh.txt)). Each broke one of: the
initialized/limit/range/nonzero conditions, the step count, the resulting tick, the span
start, the liquidity/fee gates, the oneForZero bound, the default, or the checked first
read. All 20 are killed by `tests/pools/test_concentrated.py` on `8c7337a`. In every probe,
pytest's summary line read `1 failed`, and none timed out under the per-probe
`timeout 120`. That run printed only the summary line and did not capture exit
statuses; the committed transcript now prints them.

Mutants of the earlier word-by-word variant are recorded separately. There, two mutants
never terminated: the oneForZero limit `>=` → `>`, and the zeroForOne span tick
`tick_next - 1` → `tick_next`. They were caught only by the per-probe timeout (the first,
unbounded probe was killed externally), so they count as timeout detections, not test
failures:

- In the first, a target clamped to `MAX_TICK == limit_tick` is "skipped". The next
  search overshoots and clamps back to `MAX_TICK`, so it loops forever.
- In the second, the search restarts from the same word.

Neither can occur in `8c7337a`. There the span loop has no clamp; it walks words
monotonically and is bounded by the collected range. The same probes also showed that
the variant's MIN/MAX clamp and the price assignment in the skip loop were redundant.
They were removed rather than kept untested.

**Complete-solve differential.** `tools/latency/l02_empty_spans.py work` covered all 300 L01
records: 4 derived bundles × 6 algorithms × (24 cases + sentinel). Each record ran once with
the reference loop and once with the candidate. It was then independently evaluated through
`benchmark.runner._independent_record` and compared on status, evaluation, score, error,
limit_hit, solver_reported, quotes, candidates and search. The result
([work-8c7337a.json](latency-l02/work-8c7337a.json)): `all_semantic_equal`,
`all_work_equal`, and both again **against the L01 baseline records of `bbda6e2`**
(`all_baseline_semantic_equal`, `all_baseline_work_equal`) — all `true`, including the
`incremental_graph` / `bnd-78c1b0-201eba-round_at` record that is truncated at 50,000 quotes.

**L01 driver-path exact lane** (spawned workers, both orders, both cohorts, cold, sentinel,
quote CLI): [comparison](latency-l02/compare-exact-08a7dfe8-e47f54fe.md).

- Coverage problems: none.
- Order/cold/repeat inconsistencies: none.
- Semantic mismatches: none.
- Work-counter differences: 0.
- Status regressions: 0.
- Budget-bound records: 1, established by same-source L01-SB on both sides
  ([baseline](latency-l02/20260926T125947937735Z-112be38a-baseline-sufficient.md),
  [candidate](latency-l02/20260926T131721593891Z-92de76bb-candidate-sufficient.md): `ok`,
  51,052 quotes, not bound, identical).
- Held-out regret against the reference: 0 losses, 0 gains, 0.00 bps.

The five `main.py quote` invocations exited 0 each, with one solve per algorithm.

## 4. Executed work (instrumented counters; not latency)

All 300 L01 records, reference → candidate
([work-8c7337a.json](latency-l02/work-8c7337a.json)):

| Counter | Reference | Candidate |
| --- | ---: | ---: |
| CL swaps | 269,351 | 269,351 |
| logical loop iterations (`swap_steps`) | 35,996,696 | 35,996,696 |
| executed `computeSwapStep` | 35,996,696 | 6,261,758 |
| executed zero-liquidity / uninitialized iterations | 29,757,171 (82.7 %) | 22,233 (stops at a limit) |
| `getSqrtRatioAtTick` calls | 35,996,696 | 6,269,443 |
| bitmap word searches | 35,996,696 | 6,302,265 |

The hypothesis is reconfirmed on the sentinel: `uni_sor_port` has 478,425
zero-liquidity/uninitialized iterations out of 908,828. Inside the solve the counts are
exactly the research's: 3,289 swaps, 908,823 steps and 478,425 empty iterations. The
final independent evaluation adds 4 swaps and 5 steps. The
work reduction varies by case. Executed/logical ratios per bundle and algorithm run from
0.05 (`direct_split` matrix) to 0.51 (`single_path` sentinel). The pool that the
research found traversed most is all positive liquidity. Its steps are unchanged and
belong to WHI-1505.

## 5. Measurements

### 5.1 In-process paired timing (uninstrumented; diagnostic)

`tools/latency/l02_empty_spans.py paired` runs one prepare, then 5 ABBA pairs of
reference/candidate solves in one process. Each solve gets a fresh `SolveContext` and quote
meter. Time is solve CPU/wall only, source `8c7337a` clean. Every case except the sentinel
is held-out. Semantics were identical and consistent in every attempt
([paired-8c7337a.json](latency-l02/paired-8c7337a.json)):

| Target (full source) | reference median s | candidate median s | ratio (wall) | max 1-min load |
| --- | ---: | ---: | ---: | ---: |
| sentinel `uni_sor_port` | 3.722 | 2.410 | 0.648 | 3.67 |
| `emp-78c1b0-deadde-large-1` `uni_sor_port` | 4.947 | 0.720 | 0.146 | 5.55 |
| `emp-09bc4e-201eba-large-1` `uni_sor_port` | 2.014 | 0.805 | 0.400 | 5.55 |
| `emp-779ded-78c1b0-large-1` `uni_sor_port` | 1.586 | 1.162 | 0.733 | 5.08 |
| `emp-78c1b0-deadde-large-1` `incremental_graph` | 1.549 | 0.604 | 0.390 | 4.45 |
| `emp-09bc4e-201eba-low-1` `path_split` | 0.868 | 0.251 | 0.289 | 4.00 |
| `emp-cda86a-deadde-low-1` `single_path` | 0.176 | 0.054 | 0.308 | 3.76 |
| `emp-09bc4e-201eba-low-1` `direct` | 0.0093 | 0.0011 | 0.118 | 3.76 |

Adjacent pairs balance slow drift. Still, three targets exceeded the 5.0 load bound, and
these samples do not follow the L01 procedure (in-process, no worker/IPC, no cold charge).
They support the direction and magnitude. They are not an adopt verdict.

### 5.2 L01 back-to-back pair (load-contaminated; timing is diagnostic only)

The pair ran back to back on one host, 2026-09-26 12:20–13:17 UTC, through
[`run-l01-pair.sh`](#7-reproduction). Stages:

1. Baseline `20260926T122008634477Z-08a7dfe8` on a measurement-only detached checkout of
   `299b88a`: max load **62.2**, 64 % of the 780 samples above 5.0.
2. Its L01-SB.
3. Candidate `20260926T125952820526Z-e47f54fe` on `8c7337a`: max load **24.2**, 2.4 %
   above 5.0.
4. Its L01-SB.

The load came from concurrent unrelated processes (rustc/cargo builds, a node publishing
job). Both experiments are complete, clean and fully covered. Summaries:
[baseline](latency-l02/20260926T122008634477Z-08a7dfe8-baseline-299b88a.md),
[candidate](latency-l02/20260926T125952820526Z-e47f54fe-candidate-8c7337a.md).

The comparator's held-out solve improvements (wall/CPU), with every cold charge
`not_slower`:

| Bundle | single_path | direct_split | path_split | incremental_graph | uni_sor_port |
| --- | --- | --- | --- | --- | --- |
| full source | 0.585 faster | 0.781 faster | 0.542 faster | 0.454 / 0.437 (threshold 0.510 from the baseline's A/A noise) → no_worthwhile_change | 0.714 faster |
| matched cohort | 0.721 / 0.688 (threshold 1.601) → no_worthwhile_change | 0.809 faster | 0.597 faster | 0.531 faster | 0.704 faster |

`direct` has no held-out case above the 0.02 s timing floor.

Sentinel warm medians, baseline → candidate:

| Algorithm | full source, s | matched cohort, s |
| --- | --- | --- |
| `uni_sor_port` | 4.107 → 2.432 | 3.749 → 2.432 |
| `incremental_graph` | 1.213 → 0.730 | — |
| `path_split` | 0.896 → 0.522 | — |

The `main.py quote` CLI median went from 7.713 s to 5.497 s.

The baseline ran under the heavier load, so these ratios may overstate the gain. That is
exactly why the protocol refuses them.

**Charged costs** (cold full-source medians, baseline → candidate):

| Bundle / algorithm | charged s | prepare ms | prepare peak B | solve peak max MiB |
| --- | --- | --- | --- | --- |
| matrix `uni_sor_port` | 0.776 → 0.364 | 17.09 → 17.16 | 680,350 → 680,350 | 30.39 → 30.39 |
| matrix `incremental_graph` | 0.782 → 0.516 | 0.103 → 0.104 | 36,824 → 36,824 | 34.63 → 34.63 |
| matrix `direct` | 0.120 → 0.123 | ≈0 | 92 → 92 | 0.03 → 0.03 |
| sentinel `uni_sor_port` | 3.855 → 2.648 | 17.25 → 18.63 | 681,601 → 681,497 | 298.65 → 298.65 |

Prepare and prepare memory are unchanged, since the candidate adds no preparation step.
Median solve peak grows by at most 49 bytes (per-call locals). The rest is in the
comparison JSON.

## 6. Limitations and next step

- **Blocker:** there is no uncontaminated baseline/candidate pair. The minimum run that
  can produce an `adopt_eligible` / `reject` verdict is exactly §7's
  `run-l01-pair.sh` on a quiesced host: baseline `299b88a` L01 + L01-SB, then candidate
  L01 + L01-SB, back to back. That took about 57 minutes here. It is not a repeated
  campaign.
- A/A noise floors of the contaminated baseline are inflated (incremental graph 0.255 wall);
  they are not valid noise estimates.
- One snapshot and block (101082044), the L01 matrix, and one shared M2 Pro host: this is
  not a universal speedup claim, a p95 or an SLA. Positive-liquidity traversal
  (e.g. `0xd145…ed09`) is unchanged and is WHI-1505's target. Word-level work in empty
  spans is now one mapping lookup per word; a nonzero-word index is a reuse question
  for WHI-1505.
- The counters in §4 come from wrapped functions, so the process CPU in those rows is
  instrumented.
- Held-out cases were used for one structural diagnostic profile (§2) and for the
  in-process pairs. No parameter was tuned; there is none.

## 7. Reproduction

Every raw artifact lives under the gitignored `data/latency-l02/` of the WHI-1504 worktree:
the L01 experiments, the L01-SB runs, `work-*.json`, `paired-*.json` and the pair script.
Read-only inputs:

- the parent bundle `data/corpus/mantle-5src-101082044/bundle` of the primary clone;
- for the work/paired diagnostics, the derived bundles and baseline records of the
  `bbda6e2` L01 experiment at `data/latency-012/whi-1503/20260926T090434484129Z-d5061563`.
  The driver refuses a derived bundle whose hash differs.

```bash
uv run pytest tests/pools/test_concentrated.py -q
uv run python tools/latency/l02_empty_spans.py work --experiment <bbda6e2 L01 dir> --out data/latency-l02/work.json
uv run python tools/latency/l02_empty_spans.py paired --experiment <bbda6e2 L01 dir> --pairs 5 \
  --target full_source/sentinel:uni_sor_port:quote-b5feb74821d5 --out data/latency-l02/paired.json
# run-l01-pair.sh: clean tree; detach to 299b88a; L01 run + L01-SB; back to the branch;
# L01 run + L01-SB (trap always returns to the branch):
uv run python -m benchmark.latency run --protocol config/latency/l01.yaml --bundle <parent bundle> --out data/latency-l02/l01
uv run python -m benchmark.latency sufficient --sufficient config/latency/l01-sufficient-budget.yaml --bundle <parent bundle> --out data/latency-l02/l01
uv run python -m report.latency compare <baseline> <candidate> --lane exact --sufficient <baseline SB> <candidate SB>
```
