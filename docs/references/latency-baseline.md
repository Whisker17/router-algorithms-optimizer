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
| `transport_seconds` | parent request → answer minus the child's solve time: request/result pickling and pipe transfer | solve |
| `startup_seconds` / `prepare_seconds` | spawn + unpickling of bundle/factory until ready / `prepare()` alone; charged per worker, including every replacement | solve |
| `evaluation_seconds` | the parent's independent `evaluate()` of the final plan | solve |
| cold `charged_seconds` | startup + prepare + solve + transport + evaluation of a fresh worker's single attempt | — |
| quote `cli_wall_seconds` | `python main.py quote` process start → exit: imports, validation, request-bundle derivation (writes the 13 MB pool file), six sequential spawned workers each with start-up/prepare/one solve, evaluation, result writing, rendering | `uv run` resolution |

The six-algorithm CLI total is a different metric from any single solve and from the sum of
the six solves; both are reported. Instrumented measurement never feeds a headline: the
memory pass uses separate `tracemalloc` workers (timing workers report
`instrumented: false`), and this protocol runs no profiler. cProfile-style attribution, when
a candidate needs it, is a separate diagnostic.

Boundary tests (`tests/benchmark/test_latency.py`) place the same 0.3 s of work in `prepare`
or in `solve`, as burned CPU or as sleep, and slow the parent's evaluation by 0.2 s: each
cost appears only in its own stage, sleep is wall but not CPU time, and prepare + solve
always still contains the moved work. `tests/benchmark/test_quote.py` keeps proving the quote
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
   case. A run is **load-contaminated** if any sample exceeds 0.5 × logical CPUs (5.0 on
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
(values in `acceptance:` of the protocol).

**Samples and statistics.** Per (bundle, algorithm, case): the median of the measured solve
samples pooled over both orders, wall and CPU separately. Timing ratios use the decision
split (held-out) only, and only cases whose baseline median is at least 0.02 s (shorter
solves are within scheduler jitter; they stay in every semantic and status check).
Improvement = 1 − geometric mean of candidate/baseline per-case medians.

**A/A noise floor.** For each experiment and algorithm: exp|ln g| − 1, where g is the
geometric mean over the same cases of the reverse-order / fixed-order per-case median.

**Minimum worthwhile improvement.** A timing result is `faster` only if the improvement
reaches **both** 10 % **and** 2 × the larger A/A noise floor of the two experiments, for
wall **and** CPU. It is `slower` if either improvement is at or below minus that threshold,
otherwise `no_worthwhile_change` (or `insufficient_cases`).

**Exact lane** (L02–L05). Every scheduled record of every order and cohort must keep
identical `status`, `evaluation` (gross output, full trace, route features, residuals),
`score`, `error`, `limit_hit` and solver-reported diagnostics. Work counters (`quotes
counted`, candidates considered/truncated, `search`) may differ and are listed. A candidate
that changes how much work a search needs must also show semantic identity with the quote
cap raised on the affected cases (a sufficient budget); what it completes under the fixed
budget (the `round_at` truncation case) is reported separately, not treated as the same
search scope. Verdict order: any semantic mismatch → `reject`; partial or dirty evidence →
`inconclusive`; contaminated host → `inconclusive`; any algorithm `slower` → `reject`; no
algorithm `faster` → `reject`; otherwise `adopt_eligible`. Adoption still needs the full test
suite, the SOR parity goldens and the orchestrator's decision.

**Heuristic lane** (L06/L07, separately named opt-in variants only, e.g. `uni_sor_fast` via
`--pair uni_sor_fast=uni_sor_port`). Paired regret per held-out case =
(reference score − candidate score) × 10,000 / reference score, against the **same-scope
best-known reference** — the reference algorithm's result on the same bundle, cohort,
objective and budget, never a claimed mathematical optimum. A score that is unknown (e.g. an
unranked net score), or a reference that failed, is **N/A**, never zero. Status transitions
and searched-versus-eligible scope are reported. There is no default loss tolerance
(`heuristic_default_loss_tolerance: null`, enforced by the loader), so the best verdict is
`opt_in_only`; becoming a default requires the owner's explicit acceptance of the reported
trade-off. `uni_sor_port` and its goldens stay unchanged.

**Charged costs.** Prepare, cold charged time and peak memory (solve and prepare) of both
experiments are reported side by side. Work moved into `prepare`, a cache or the
evaluation is visible there and must be justified in the adopt/reject record; it never
counts as a solve-time gain.

Every experiment ends with an explicit adopt or reject disposition. A rejected idea is
recorded as rejected, not described as a speedup.

## 8. Baseline results

Recorded with the baseline run (next section of this document's history).

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
[`latency-baseline/`](latency-baseline/). Offline, no credentials; the bundle is only read.
