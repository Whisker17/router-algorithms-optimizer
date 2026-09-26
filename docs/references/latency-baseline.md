# L01 — reproducible latency and quality baseline protocol (WHI-1503)

Status: **pre-registered before any 0.1.2 candidate tuning.** The protocol is
[`config/latency/l01.yaml`](../../config/latency/l01.yaml) (protocol `L01`, version 1). The
measured baseline is recorded in §8. The research that motivated it is
[latency-optimization-research.md](latency-optimization-research.md) (WHI-1502). No
optimization has landed. The candidate issues WHI-1504/1505/1507/1508 use this protocol.

## 1. What this is, and what it is not

It fixes the inputs, measurement boundaries, run procedure and adopt/reject rules
**before** any candidate is tuned, so a later "speedup" cannot come from a changed workload,
moved costs, a luckier host or a looser quality rule.

The WHI-1502 observations of the owner's case (USDC → USDT, 1000 USDC; three
load-contaminated CLI runs at start load 61/33/19 and one cProfile-instrumented solve per
algorithm) are **diagnostics**. They are not a distribution, a noise estimate or a speedup,
and this protocol does not reuse them as a baseline. Nothing here derives a p95, a tail or
an SLA: each case has 2 × repeats = 10 samples per stage, which does not support that.
`report.latency` prints medians/min/max only, and a test fails if a percentile or SLA
appears in the rendered summary. There is no universal subsecond target and no default
quality-loss tolerance.

## 2. Frozen identity

| Input | Identity | Enforcement |
| --- | --- | --- |
| Parent corpus bundle | `mantle-5src-101082044-091b0759`, `bundle_hash` `717c21f3…3143` (block 101082044) | driver refuses another hash; bundle only read |
| Profile | `config/daily_gross.yaml`, sha256 `577b43eb…0c11`: six algorithms, gross-only, `max_hops 2`, `max_splits 4`, `percent_step 5`, `graph.chunks 200`, 120 s / 50,000-quote budget, seed 1447, `spawn` | driver refuses another sha256; the profile file is not edited. Its measurement section is replaced by the protocol's |
| Protocol | sha256 of `config/latency/l01.yaml` | recorded in every experiment; `compare` refuses two experiments with different protocol documents |
| Derived bundles | matrix and sentinel bundle per cohort, deterministic (same inputs → same `bundle_hash`) | `compare` refuses experiments whose derived bundle hashes differ |
| Source | `git_revision`, `git_dirty` and the committed tree/blob id of `benchmark`, `pools`, `routing`, `snapshot`, `report`, `config`, `main.py`, `pyproject.toml`, `uv.lock` | driver refuses a dirty tree unless `--allow-dirty`; then it records the changed paths and a patch identity covering tracked diffs **and** untracked file contents. `compare` treats dirty or partial evidence as `inconclusive` |

An experiment is bound to the source it ran on. A later commit inherits its results only if
every recorded code-path id is unchanged (a documentation or artifact-only commit keeps
them; any code or config change does not).

## 3. Workload matrix

The sentinel is the owner's case, run as the same derived single-request bundle
`main.py quote` builds (the full-source sentinel's `bundle_hash` equals the quote
command's). The corpus matrix has 24 frozen cases. Every tuning case has a **held-out twin**:
the lexicographically smallest report-split case of the same (pair, stratum) cell. The
driver refuses a case whose declared split differs from the corpus descriptor's split.
Candidates may be tuned on tuning cases only; adoption decisions use held-out cases only.

| Cell / case | Split | Covers |
| --- | --- | --- |
| USDC → USDT low: `emp-09bc4e-201eba-low-2` / `-low-1` | tuning / held-out | sentinel pair, small amount, dense 12-pool CL/CPMM/LB direct set |
| USDC → USDT large: `…-large-3` / `…-large-1` | tuning / held-out | sentinel pair, large amount |
| USDT0 → WMNT low: `emp-779ded-78c1b0-low-2` / `-low-1` | tuning / held-out | highest-activity pair, CL + LB |
| USDT0 → WMNT large: `…-large-2` / `…-large-1` | tuning / held-out | large amount |
| WMNT → WETH large: `emp-78c1b0-deadde-large-4` / `-large-1` | tuning / held-out | densest pair (16 pools, five sources), heaviest SOR work |
| METH → WETH low: `emp-cda86a-deadde-low-3` / `-low-1` | tuning / held-out | LST pair, CL-dominated |
| USDT0 → FBTC large: `emp-779ded-c96de2-large-2` / `-large-1` | tuning / held-out | sparse (one CL + one LB pool) |
| CATI → WMNT low: `emp-1bdd88-78c1b0-low-1` / `-low-2` | tuning / held-out | sparse, CPMM-selected |
| USDC → FBTC medium: `nod-09bc4e-c96de2-medium-2` / `-medium-1` | tuning / held-out | no direct pool: multi-hop only; `direct`/`direct_split` `no_route` |
| `bnd-09bc4e-201eba-dust` | held-out | dust boundary (all corpus boundary cases are report split) |
| `bnd-09bc4e-201eba-liq_at` | held-out | liquidity edge on the sentinel pair |
| `bnd-78c1b0-201eba-round_at` | held-out | CL tick rounding; `incremental_graph` stops at the 50,000-quote cap (fixed-budget truncation) |
| `bnd-201eba-779ded-round_at` | held-out | LB bin rounding |
| `bnd-1bdd88-78c1b0-dust` | held-out | failure: every algorithm `no_route` |
| `bnd-1bdd88-78c1b0-round_below` | held-out | only an LB pool routes it: `uni_sor_port` `no_route` on full source, all six on the matched cohort |

Both **cohorts** run the matrix and the sentinel: `full_source` (143 pools, five sources)
and `sor_compatible` (the matched V2/V3 universe `uni_sor_port` sees, no Liquidity Book).

Coverage limits, stated rather than hidden: on this 2-hop profile no final plan calls a
physical pool twice (§8 records `repeated_pool_calls`), so shared-pool state is exercised
during search (path-split conflict exclusion, incremental-graph tentative state) and by the
evaluator's synthetic shared-pool tests, not by a final matrix plan. No `unsupported`,
`timeout`, `invalid_plan` or `incomplete_snapshot` outcome occurs in this matrix at this
profile; the matrix does contain `no_route` failures and a quote-cap truncation. Every
scheduled outcome is retained whatever its status.

## 4. Timing boundaries and labels

| Label | Inside | Outside |
| --- | --- | --- |
| `solve_wall_seconds` | child `perf_counter_ns` around `factory.solve`: all search, quoting, in-solve evaluation, candidate-sink sends | worker start-up, `prepare`, request pickling, result transfer, final evaluation, rendering |
| `solve_cpu_seconds` | child `process_time_ns` over exactly the same window (new explicit worker metric; the ordinary runner does not record it) | as above |
| `transport_seconds` | parent request → answer minus the child's solve time: request/result pickling and pipe transfer, the child's per-attempt `SolveContext`/quote-meter setup, draining streamed candidates | solve (candidate-sink sends happen inside the solve window) |
| `startup_seconds` | worker process start → ready. The child reports ready **after** `prepare()`, so this **includes** `prepare_seconds` (spawn, unpickling of bundle/factory, prepare); charged per worker, including every replacement. Reports label it `startup_including_prepare_seconds` and derive `spawn_seconds` = startup − prepare | solve |
| `prepare_seconds` | `prepare()` alone (already inside `startup_seconds`) | solve |
| `evaluation_seconds` | the parent's independent `evaluate()` of the final plan | solve |
| cold `charged_seconds` | start-up (incl. prepare) + solve + transport + evaluation of a fresh worker's single attempt — each cost counted once, never prepare twice; not computed for a case missing a part (e.g. a failure without a solve) | — |
| quote `cli_wall_seconds` | `python main.py quote` process start → exit: imports, validation, request-bundle derivation (writes the 13 MB pool file), six sequential spawned workers each with start-up/prepare/one solve, evaluation, result writing, rendering | `uv run` resolution |

The six-algorithm CLI total is a different metric from any single solve and from the sum of
the six solves; both are reported. Instrumented measurement never feeds a headline: the
memory pass uses separate `tracemalloc` workers (timing workers report
`instrumented: false`), and this protocol runs no profiler. cProfile-style attribution, when
a candidate needs it, is a separate diagnostic.

Boundary tests (`tests/benchmark/test_latency.py`) place the same 0.3 s of work in `prepare`
or in `solve`, as burned CPU or as sleep, and slow the parent's evaluation by 0.2 s: each
cost appears only in its own stage, sleep is wall but not CPU time, and prepare + solve
always still contains the moved work. A cold-accounting regression fixes start-up 5 s
(incl. prepare 3 s) + solve 2 s + transport 0.25 s + evaluation 1 s at 8.25 s charged,
and a comparator regression rejects a candidate whose solve got faster only because 2 s
moved into prepare. `tests/benchmark/test_quote.py` keeps proving the quote
command solves each algorithm exactly once; the end-to-end latency test checks the same for
every `quote_cli` invocation.

## 5. Process and cache lifecycles

- **Warm process** (headline timing): one spawned worker per algorithm per run. The first
  case of an algorithm follows a fresh `prepare`; later cases reuse the worker. Per case,
  one warmup attempt (recorded with `phase: warmup`, excluded from samples) precedes five
  measured attempts. All current search caches, including `routing.search.QuoteCache`, are
  per solve (fresh `SolveContext` each attempt), so a warmup is not a cache hit today.
- **Cold process**: a fresh worker per (algorithm, case), one attempt — the single-request
  condition, with start-up and prepare charged every time.
- **Complete response**: each `quote_cli` invocation is a new Python process.

A candidate that adds any cache must declare its key and lifetime (per solve, per worker,
per prepared index), charge population to `prepare` or to the solve that fills it, expose
hit/miss counters in `search` statistics, and be judged on held-out cases whose warmup
attempt is a different solve from the measured ones. Cold and warm results are reported
separately and peak memory comes from the separate memory pass.

## 6. Run procedure and host control

1. Commit the source under test (clean tree). Run the driver; stages execute strictly
   sequentially and never overlap another benchmark.
2. `timing`: for each cohort, matrix then sentinel, in `fixed` order; then the same runs in
   `reverse` — bundle sequence reversed and the whole algorithm × case schedule reversed
   (ABBA, balancing drift and exposing order-dependent state).
3. `cold` (+ memory pass) over the full-source matrix and sentinel.
4. `quote_cli`: five sequential `main.py quote` invocations of the sentinel.
5. Host load: the 1-minute load average is sampled before/after every run and after every
   case. SIGTERM (like Ctrl-C) records the current run's unfinished cases as `cancelled`
   and finalizes the run and experiment as `interrupted` (exit 130); an interrupted or
   still-`running` experiment is refused by `report.latency`, so its samples are never
   read as, or mixed into, a complete experiment. A run is **load-contaminated** if any sample exceeds 0.5 × logical CPUs (5.0 on
   the 10-core reference machine). Contaminated timing is retained and labelled; it cannot
   support an adopt verdict. The reference machine is a shared Apple M2 Pro workstation
   (6 performance + 4 efficiency cores); core placement cannot be pinned without privileges
   and is a declared limitation, as are thermal state and background processes.
6. A candidate is compared with a baseline **re-measured on the same host in the same
   session**, baseline and candidate run back to back. The committed baseline (§8) is the
   semantic reference and a documented measurement; it is not a timing reference for a
   different day or machine.

## 7. Pre-registered acceptance rules

Applied by `uv run python -m report.latency compare BASELINE CANDIDATE --lane exact|heuristic`
(values in `acceptance:` of the protocol). Every summary and comparison records its own
report provenance (generating revision, dirty state, `report` tree id, time) beside the
measured source of the raw records; regenerating a report from saved records never
relabels those measurements as the new source's performance.

**Coverage (no vacuous pass).** Each experiment must hold every required run of the
protocol schedule for its requested stages (timing: every cohort × {matrix, sentinel} ×
order; cold: every cold cohort × {matrix, sentinel}), each `complete`, over the
experiment's derived bundle, with a schedule of every algorithm × case exactly once (the
matrix = the protocol's cases; the sentinel = one case), exactly one record per scheduled
pair, no `cancelled` record, the experiment's full algorithm set, and the protocol's number
of quote-CLI invocations. In a comparison both experiments must hold the same runs and
every record pairs with one on the other side. Algorithm pairing never drops an algorithm:
every candidate algorithm has a reference and every baseline algorithm is compared (the
exact lane pairs each algorithm with itself only). A missing run or record is a coverage
problem, never agreement.

**Determinism gates (both experiments).** Fixed == reverse order and cold == warm process
(`benchmark.runner`'s deterministic view: status, evaluation, score, error, work counters,
solver-reported fields, seed, attempt consistency), and every record's attempts
consistent (`attempts_consistent`, warmup included).

**Samples and statistics.** Per (bundle, algorithm, case): the median of the measured solve
samples pooled over both orders, wall and CPU separately. Timing ratios use the decision
split (held-out) only, and only cases whose baseline median is at least 0.02 s (shorter
solves are within scheduler jitter; they stay in every semantic and status check).
Improvement = 1 − geometric mean of candidate/baseline per-case medians. A held-out timed
baseline case the candidate did not time (it failed) is `lost_samples`, never skipped.

**A/A noise floor.** For each experiment and algorithm: exp|ln g| − 1, where g is the
geometric mean over the same cases of the reverse-order / fixed-order per-case median.

**Minimum worthwhile improvement.** A timing result is `faster` only if the improvement
reaches **both** 10 % **and** 2 × the larger A/A noise floor of the two experiments, for
wall **and** CPU. It is `slower` if either improvement is at or below minus that threshold,
otherwise `no_worthwhile_change` (or `insufficient_cases`).

**Charged costs.** Prepare, start-up (incl. prepare), cold charged time and peak memory
(solve and prepare) of both experiments are reported side by side. The cold charged time
(one fresh-worker attempt per case: start-up incl. prepare + solve + transport +
evaluation) is gated on the same held-out cases and threshold: charged `slower` rejects,
so work moved into `prepare`, start-up or the evaluation never counts as a solve-time
gain. Peak-memory growth has no pre-registered tolerance; it is reported and must be
justified in the adopt/reject record.

**Exact lane** (L02–L05). Every scheduled record of every stage (timing in both orders and
cold), cohort and bundle must keep identical `status`, `evaluation` (gross output, full
trace, route features, residuals), `score`, `error`, `limit_hit` and solver-reported
diagnostics. Work counters (`quotes counted`, candidates considered/truncated, `search`) may
differ and are listed. A difference on a **budget-bound** record — `timeout`, a
`limit_hit`, or a declared `search.truncated_by` (e.g. the `round_at` case where
`incremental_graph` stops at the 50,000-quote cap) — is a *fixed-budget completion
difference*: reported separately, not a same-scope exact mismatch, and `inconclusive` until
a sufficient-budget comparison (quote cap raised on those cases) shows semantic identity.

**Heuristic lane** (L06/L07, separately named opt-in variants only, e.g. `uni_sor_fast` via
`--pair uni_sor_fast=uni_sor_port`). Paired regret per case =
(reference score − candidate score) × 10,000 / reference score, against the **same-scope
best-known reference** — the reference algorithm's result on the same bundle, cohort,
objective and budget, never a claimed mathematical optimum — aggregated **per split**
(held-out decides; tuning and sentinel are reported apart, never pooled). A score that is
unknown (e.g. an unranked net score), or a reference that failed, is **N/A**, never zero.
Status transitions for every stage and ok → failure regressions are reported. There is no
default loss tolerance (`heuristic_default_loss_tolerance: null`, enforced by the loader),
so the best verdict is `opt_in_only`; becoming a default requires the owner's explicit
acceptance of the reported trade-off. `uni_sor_port` and its goldens stay unchanged.

**Verdict order.** Candidate determinism-gate failure → `reject`; exact-lane semantic
mismatch → `reject`; coverage problem → `inconclusive`; baseline determinism-gate failure
→ `inconclusive`; exact-lane fixed-budget difference → `inconclusive`; partial or dirty
evidence → `inconclusive`; contaminated host → `inconclusive`; `lost_samples`, any
algorithm `slower` or cold charged `slower` → `reject`; no algorithm `faster` → `reject`;
otherwise `adopt_eligible` (exact) or `opt_in_only` (heuristic). Adoption still needs the
full test suite, the SOR parity goldens and the orchestrator's decision.

`tests/benchmark/test_latency.py` builds complete paired experiments (five 1.0 s baseline
vs five 0.5 s candidate samples per case in both orders, identical semantics, clean,
uncontaminated) that are `adopt_eligible`, and checks that each single defect — a cold
`algorithm_error`, inconsistent attempts, an order-dependent result, a missing record or
run, a budget-bound difference, work moved into prepare, lost held-out samples — stops
that verdict, and that a tuning-only loss never enters held-out regret.

Every experiment ends with an explicit adopt or reject disposition. A rejected idea is
recorded as rejected, not described as a speedup.

## 8. Baseline results

No uncontaminated baseline exists yet; nothing below is performance, noise-floor or
speedup evidence.

**Experiment `20260926T090434484129Z-d5061563`** (complete; source `bbda6e2`, clean;
protocol sha256 `961fb522…7f1b`; all stages; 2026-09-26 09:04–10:04 UTC). Summary
regenerated from its saved records by the report fixed in this issue:
[`latency-baseline/20260926T090434484129Z-d5061563.md`](latency-baseline/20260926T090434484129Z-d5061563.md)
(its header names the measured and the generating source separately), and its A/A exact
comparison with itself:
[`latency-baseline/20260926T090434484129Z-d5061563-aa-exact.md`](latency-baseline/20260926T090434484129Z-d5061563-aa-exact.md).

- **Load-contaminated:** max sampled 1-minute load 161.4 against the 5.0 threshold (shared
  host running unrelated builds). Its timings, A/A noise floors and cold charged times are
  diagnostic only and cannot support any adopt verdict (the A/A comparison is
  `inconclusive` for that reason).
- **Semantic reference (load-independent):** complete coverage; fixed ≡ reverse in both
  cohorts, cold ≡ warm on the full-source matrix and sentinel, every record's attempts
  consistent; every scheduled status retained (`no_route` 11 of 144 full-source and 16 of
  144 matched-cohort matrix records); one budget-bound record
  (`incremental_graph` / `bnd-78c1b0-201eba-round_at`, `truncated_by: max_quotes`, full
  source); `repeated_pool_calls` = 0 in all 685 evaluated plans of its ten runs (the other
  65 records are `no_route`, without a plan); five `main.py quote` invocations, each
  exactly one solve per algorithm.
- Its measured source is `bbda6e2`; later commits change `benchmark/` and `report/` code
  ids, so they do not inherit its measurements (§2). The measurement path
  (`benchmark.runner`, `benchmark.worker`, `measure_run`) is unchanged since `bbda6e2`: the
  later driver changes are the SIGTERM handling and the recorded algorithm set.

**Experiment `20260926T100709788238Z-b23b2ea5`** — INTERRUPTED by a user-directed stop
during its fifth timing run (the `bbda6e2` driver had no SIGTERM handler, so its
`experiment.json` still reads `running`; a sidecar `INTERRUPTED.md` records the stop). Also
load-contaminated (max 125.5). Its four completed timing runs are retained as partial raw
evidence only; `report.latency` refuses the experiment and its samples are never mixed
with another.

Per §6, a candidate is judged against a baseline re-measured back to back with it on the
same host; the owner's quiet-host session is the prerequisite for the first adopt
decision, not a relaxed rule.

## 9. Reproduction

```bash
uv run python -m benchmark.latency run --protocol config/latency/l01.yaml \
  --bundle data/corpus/mantle-5src-101082044/bundle --out data/latency
uv run python -m report.latency summarize data/latency/<experiment id> \
  --json summary.json --markdown summary.md
uv run python -m report.latency compare data/latency/<baseline> data/latency/<candidate> \
  --lane exact
```

Experiments are written under the gitignored `data/`; small summaries are checked in under
[`latency-baseline/`](latency-baseline/). A summary or comparison can be regenerated from
saved records at any later source; it records that generating source separately. Offline, no credentials; the bundle is only read.
