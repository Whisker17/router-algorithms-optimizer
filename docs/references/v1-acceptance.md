# Core v1 acceptance experiment and report (WHI-1447 / I21)

Status: **final (2026-09-26).** Eleven held-out runs complete (exit 0, clean commits), four
order/rerun checks with 0 mismatches, reports and the write-once acceptance manifest
checked in (§12).

This is the evidence record for the 0.1.0 convergence gate (docs/DESIGN.md §1.4, §2.10–2.12,
§6 M4): the six mandatory algorithms run over the frozen five-source corpus, in declared
capability cohorts, under both objectives, with calibrated daily/full profiles, a
deterministic-replay / case-order check, measured wall time and environment, and an
offline HTML/CSV report. It links — and does not repeat — the evidence of the issues it
builds on. Nothing here is an optimality claim, a production-latency claim or a claim of
complete Mantle coverage.

**Summary.**

- Six algorithms × 302 held-out cases, two declared cohorts (matched V2/V3 = SOR's 98 pools;
  full five-source = 143 pools), two objectives, two calibrated profiles: 11 runs, every one
  of the 1,812 scheduled (algorithm, case) pairs per run recorded. 0 timeouts, 0
  `unsupported`/`incomplete_snapshot`/`model_error`/`algorithm_error`. Non-ok outcomes are
  `no_route` (capability: no direct pool; state-derived dust boundary) and, at 3 hops only,
  12 `uni_sor_port` `invalid_plan` (economic token cycle, kept for parity); the daily
  quote cap truncated 4 `incremental_graph` boundary searches (recorded, still `ok`) (§9).
- Deterministic replay: fixed vs reverse (full), fixed vs shuffle and fixed vs independent
  rerun (daily) — 0 mismatches over 1,812 records each (§6).
- Matched cohort, gross, full profile: `uni_sor_port` and `path_split` (same capability) are
  near parity (233/276 equal, `path_split` ahead on 34, SOR on 9); `incremental_graph` is
  never worse than either and gains +0.28 bps median / +14 bps p95 over SOR (§5.2). These
  are best-known comparisons on one snapshot, not optimality claims.
- Under `empirical_cost` split/shared-pool plans have no validated cost, so the split
  searches return rankable single-route plans; that is a declared cost-model limitation,
  not evidence about splitting (§5.4, §10.2).
- Wall times are recorded but contaminated by concurrent runs on a shared workstation;
  runtime is not an acceptance threshold (§7).

## 1. Acceptance criteria → evidence

| Criterion (WHI-1447) | Evidence | Where |
| --- | --- | --- |
| All five sources and all six mandatory algorithms are represented with explicit capability cohorts | Six algorithms × 302 held-out cases in every run; two declared cohorts (matched V2/V3 = SOR's 98 pools, full five-source = 143 pools); solved plans use all five sources; SOR's LB gap is labelled *coverage* | §4, §5.1, report *Source coverage* / *Status counts* |
| Protocol admission, shared-pool correctness, SOR parity and cost-model validation evidence are linked | WHI-1426/1428–1434, WHI-1435, WHI-1442–1444, WHI-1445, WHI-1436 records | §8 |
| Exact deterministic replay succeeds; every scheduled outcome remains in the report | 0 mismatches on fixed-vs-reverse (full), fixed-vs-shuffle and fixed-vs-rerun (daily); every one of the 6 × 302 scheduled pairs has a record in every run and in the report (non-ok outcomes listed, never dropped) | §6, §9, `v1-acceptance/manifest.json` `order_checks` |
| Daily/full wall time and environment are recorded; honest timeout reporting | Measured wall time per run and pass, per-algorithm solve sums, environment; recorded as contaminated by concurrency (decision in §7); per-case time/quote limits kept, 0 timeouts | §7, §3.3, §9 |
| Offline HTML/CSV and exact reproduction commands, held-out/exploratory labelling | Full and daily reports (HTML + CSV) checked in with hashes; every final run is on the report split and labelled *held-out*; calibration is tuning-split only and labelled exploratory | §12, §11, §3 |
| Immutable final manifest bound to the unchanged corpus/price/cost hashes | `main.py acceptance` (write-once) binds each run to its cut, the parent corpus hash, `experiment_id` (bundle + objective incl. cost-model and price-context hashes), profile hash, git revision and environment | §2, `v1-acceptance/manifest.json` |

## 2. Frozen inputs (unchanged) and the declared cuts

| Input | Identity | Record |
| --- | --- | --- |
| Corpus bundle `mantle-5src-101082044-091b0759` (WHI-1436) | `717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143`; block 101082044 `0x091b0759…65c4`; 143 pools (Agni 45, FusionX 26, Uniswap v3 13, Moe Classic 14, Moe LB 45); 398 cases (96 tuning / 302 report) | `docs/references/corpus.md`, `tests/fixtures/corpus/full_bundle.json` |
| Price context `price-context/1` (in the bundle) | `prices.json` of the bundle, 8/8 tokens priced (Dune `prices.minute`, 23:59 UTC bucket); sha256 recorded per empirical run as `experiment.objective.price_context_sha256` | `docs/references/corpus.md` §4 |
| Cost model `mantle-101082044-cost-v1` (WHI-1445) | `config/costs/mantle-101082044-cost-v1.json`, sha256 `50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71`, pinned by path + hash in `config/daily.yaml` / `config/full.yaml` | `docs/references/cost-model.md` |
| Uniswap SOR pin (WHI-1442/1443/1444) | `@uniswap/smart-order-router` 4.31.10 @ `04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647` | `docs/references/uni-sor-port-contract.md` |

The corpus bundle is never rewritten. The comparison bundles are **cuts** of it, written
by `main.py corpus cohort` (WHI-1444) and the new `main.py corpus split` (this issue):
every pool record is copied unchanged, the descriptor records `subset_of` (the parent's
hash), `cohort` and `split`, and the loader refuses a `split` cut containing a case of the
other split. Their identities are in [`v1-acceptance/bundles.json`](v1-acceptance/bundles.json)
and `tests/snapshot/test_corpus.py` re-cuts them from the local corpus and compares.

| Cut | Hash | Pools | Cases | Use |
| --- | --- | ---: | ---: | --- |
| `bundle_tuning` | `ee7afa7e…279b` | 143 | 96 | calibration only (exploratory) |
| `bundle_report` | `85202b20…71c0` | 143 | 302 | **held-out** full five-source coverage |
| `sor_cohort` | `7a4341dd…4c6f` | 98 | 398 | parent of the matched cuts (WHI-1444) |
| `sor_cohort_tuning` | `b900b866…7ade` | 98 | 96 | not used for measurement |
| `sor_cohort_report` | `8213b7b0…e640` | 98 | 302 | **held-out** matched V2/V3 cohort |

The final experiment manifest [`v1-acceptance/manifest.json`](v1-acceptance/manifest.json)
(`main.py acceptance`, write-once) binds each run to its cut, the parent corpus hash, its
`experiment_id` (bundle hash + objective incl. cost-model and price-context hashes), the
profile hash, the git revision and the recorded environment.

## 3. Profile calibration (tuning split only — exploratory)

All calibration ran on `bundle_tuning` (96 cases: one per (direction, stratum) cell, by
seeded hash; DESIGN §2.4). No report-split case was used to choose a value. The
calibration profiles are checked in under `config/calibration/` (the `sweep-*.yaml` grid
points are generated by `main.py calibrate profiles` from `sweep-base.yaml`), and
`main.py calibrate summarize` produced the tables below from the saved runs
(`v1-acceptance/calibration/*.json`). "Shortfall" is per case, in bps, against the **best
known** gross among the runs compared (a finite-grid reference, not an optimum), over the
variant's `ok` cases. All calibration runs are gross-only.

### 3.1 Uncapped 3-hop demand probe (`probe-h3-*.yaml`)

The five local algorithms and `uni_sor_port` at the DESIGN §2.12 trial values
(`max_hops 3`, `max_splits 4`, `percent_step 5`, `chunks 20`) with **no** quote/candidate
cap and a 1,800 s safety clock, so the recorded demand is unconstrained:

| algorithm | params | ok / scheduled | solve s p50 / p95 / max | quotes p50 / p95 / max | at best known | shortfall bps mean / p50 / p95 / max |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `direct` | – | 72/96 (no_route 24) | 0.00 / 0.01 / 0.02 | 3 / 16 / 16 | 19/72 | 24.30 / 7.25 / 88.18 / 259.60 |
| `single_path` | max_hops=3 | 96/96  | 1.19 / 4.72 / 5.04 | 2,960 / 5,498 / 6,482 | 26/96 | 10.07 / 5.90 / 36.32 / 61.36 |
| `direct_split` | max_splits=4 percent_step=5 | 72/96 (no_route 24) | 0.00 / 0.73 / 1.37 | 79 / 853 / 987 | 21/72 | 19.57 / 7.25 / 87.68 / 124.03 |
| `path_split` | max_hops=3 max_splits=4 percent_step=5 | 96/96  | 4.77 / 11.53 / 19.35 | 15,840 / 28,369 / 30,790 | 38/96 | 3.60 / 1.17 / 19.16 / 20.03 |
| `incremental_graph` | chunks=20 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 6.48 / 19.58 / 24.30 | 19,911 / 35,373 / 41,783 | 96/96 | 0.00 / 0.00 / 0.00 / 0.00 |
| `uni_sor_port` | max_hops=3 max_splits=4 percent_step=5 | 94/96 (invalid_plan 2) | 10.88 / 65.82 / 77.85 | 22,203 / 65,957 / 82,724 | 27/94 | 7.35 / 4.38 / 25.36 / 55.43 |

Runs `20260925T151109272576Z-4224ecdc` (five local algorithms) and
`20260925T151109272576Z-2ee19002` (`uni_sor_port`), [`calibration/probe-h3.json`](v1-acceptance/calibration/probe-h3.json).
The shortfall column here mixes capabilities (e.g. `uni_sor_port` cannot use LB on this
full five-source cut), so it is **not** a search-quality comparison; it only shows the
reference the others are measured against. What the probe established:

- **Demand at 3 hops.** The largest per-case solve was 77.9 s (`uni_sor_port`) and the
  largest quote count 82,724 (`uni_sor_port`); `path_split` needs ~16k quotes per case at
  the median (vs ~1.1k at 2 hops).
- **A real finding, kept as an outcome:** `uni_sor_port` returned 2 plans that the
  evaluator rejects as `invalid_plan` (`economic token cycle: mETH -> WETH -> mETH`,
  cases `emp-09bc4e-201eba-low-2` / `-medium-2`, USDC→USDT). At 3 hops upstream's
  pool-id-only conflict rule can combine USDC→mETH→WETH→USDT with USDC→WETH→mETH→USDT over
  different pools; the v1 feasible set (DESIGN §2.5) rejects any plan whose token graph
  has a cycle. The port keeps upstream's selection for parity, so these stay
  `invalid_plan` in every report (logged in `docs/DEFERRED_ISSUES.md`).

### 3.2 `graph.chunks` × `search.percent_step` sweep

Two-hop grid first (cheap), then the decisive axis at three hops. `percent_step` only
moves `path_split` (and the `path_split` finalist that `incremental_graph` embeds);
`graph.chunks` is `incremental_graph`'s own granularity.

**2 hops** (`sweep-ps-maxhops2-*`, `sweep-ig-maxhops2-*`; best known = the best of these 18 runs;
[`calibration/sweep-h2.json`](v1-acceptance/calibration/sweep-h2.json)):

| algorithm | params | ok / scheduled | solve s p50 / p95 / max | quotes p50 / p95 / max | at best known | shortfall bps mean / p50 / p95 / max |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `path_split` | max_hops=2 max_splits=4 percent_step=10 | 96/96  | 0.14 / 0.85 / 1.10 | 585 / 1,424 / 1,594 | 33/96 | 2.21 / 0.47 / 9.42 / 11.72 |
| `path_split` | max_hops=2 max_splits=4 percent_step=2 | 96/96  | 0.49 / 4.89 / 5.48 | 2,559 / 6,336 / 6,901 | 34/96 | 1.50 / 0.13 / 7.47 / 8.76 |
| `path_split` | max_hops=2 max_splits=4 percent_step=4 | 96/96  | 0.21 / 1.94 / 2.59 | 1,286 / 3,119 / 3,528 | 33/96 | 1.72 / 0.31 / 8.16 / 9.41 |
| `path_split` | max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.18 / 1.45 / 1.98 | 1,064 / 2,607 / 2,755 | 33/96 | 1.85 / 0.34 / 8.02 / 11.65 |
| `incremental_graph` | chunks=10 max_hops=2 max_splits=4 percent_step=10 | 96/96  | 0.16 / 1.06 / 1.27 | 718 / 1,752 / 1,856 | 33/96 | 1.59 / 0.29 / 7.40 / 11.04 |
| `incremental_graph` | chunks=20 max_hops=2 max_splits=4 percent_step=10 | 96/96  | 0.21 / 1.43 / 1.55 | 887 / 2,212 / 2,454 | 33/96 | 0.95 / 0.18 / 4.70 / 5.44 |
| `incremental_graph` | chunks=40 max_hops=2 max_splits=4 percent_step=10 | 96/96  | 0.22 / 1.49 / 2.06 | 1,002 / 2,534 / 2,911 | 33/96 | 0.43 / 0.09 / 2.14 / 3.66 |
| `incremental_graph` | chunks=50 max_hops=2 max_splits=4 percent_step=10 | 96/96  | 0.22 / 1.61 / 2.17 | 1,046 / 2,826 / 3,135 | 43/96 | 0.34 / 0.02 / 1.77 / 3.24 |
| `incremental_graph` | chunks=10 max_hops=2 max_splits=4 percent_step=2 | 96/96  | 0.54 / 5.31 / 5.66 | 2,726 / 6,773 / 7,393 | 34/96 | 1.08 / 0.13 / 6.46 / 7.97 |
| `incremental_graph` | chunks=20 max_hops=2 max_splits=4 percent_step=2 | 96/96  | 0.60 / 5.40 / 5.76 | 2,792 / 6,913 / 7,594 | 34/96 | 0.76 / 0.11 / 3.68 / 5.44 |
| `incremental_graph` | chunks=40 max_hops=2 max_splits=4 percent_step=2 | 96/96  | 0.59 / 5.33 / 5.87 | 2,843 / 7,241 / 7,868 | 34/96 | 0.42 / 0.08 / 2.14 / 3.66 |
| `incremental_graph` | chunks=50 max_hops=2 max_splits=4 percent_step=2 | 96/96  | 0.69 / 5.19 / 5.66 | 2,687 / 7,003 / 7,603 | 43/96 | 0.34 / 0.02 / 1.77 / 3.24 |
| `incremental_graph` | chunks=10 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.29 / 1.86 / 2.51 | 1,246 / 3,178 / 3,300 | 33/96 | 1.34 / 0.29 / 6.63 / 8.02 |
| `incremental_graph` | chunks=20 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.24 / 1.67 / 2.26 | 1,178 / 2,927 / 3,252 | 33/96 | 0.95 / 0.18 / 4.70 / 5.44 |
| `incremental_graph` | chunks=40 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.31 / 2.00 / 2.59 | 1,439 / 3,554 / 4,063 | 33/96 | 0.43 / 0.09 / 2.14 / 3.66 |
| `incremental_graph` | chunks=50 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.31 / 2.01 / 2.57 | 1,470 / 3,609 / 4,298 | 43/96 | 0.34 / 0.02 / 1.77 / 3.24 |
| `incremental_graph` | chunks=100 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.34 / 2.31 / 3.51 | 1,684 / 4,593 / 5,257 | 47/96 | 0.13 / 0.00 / 0.84 / 1.05 |
| `incremental_graph` | chunks=200 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.41 / 2.57 / 5.45 | 2,231 / 6,331 / 7,716 | 89/96 | 0.00 / 0.00 / 0.01 / 0.38 |

**3 hops** (`probe-h3-baselines` with `chunks 20` plus `sweep-ig-maxhops3-percentstep5-chunks{50,100,200}`;
best known = the best of these runs; [`calibration/probe-sweep-h3.json`](v1-acceptance/calibration/probe-sweep-h3.json)):

| algorithm | params | ok / scheduled | solve s p50 / p95 / max | quotes p50 / p95 / max | at best known | shortfall bps mean / p50 / p95 / max |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `path_split` | max_hops=3 max_splits=4 percent_step=5 | 96/96  | 4.77 / 11.53 / 19.35 | 15,840 / 28,369 / 30,790 | 18/96 | 5.35 / 3.09 / 20.51 / 25.10 |
| `incremental_graph` | chunks=20 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 6.48 / 19.58 / 24.30 | 19,911 / 35,373 / 41,783 | 21/96 | 1.75 / 0.99 / 5.70 / 13.42 |
| `incremental_graph` | chunks=50 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 9.25 / 35.88 / 76.27 | 26,358 / 48,708 / 58,955 | 25/96 | 0.82 / 0.29 / 3.68 / 7.54 |
| `incremental_graph` | chunks=100 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 11.48 / 41.74 / 94.48 | 33,256 / 62,287 / 75,639 | 29/96 | 0.71 / 0.12 / 4.00 / 13.64 |
| `incremental_graph` | chunks=200 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 13.84 / 52.17 / 99.21 | 41,783 / 85,898 / 99,061 | 79/96 | 0.95 / 0.00 / 7.76 / 17.40 |

**Hop bound** (the 2-hop and 3-hop runs of `path_split` / `incremental_graph` together;
[`calibration/hops-h2-vs-h3.json`](v1-acceptance/calibration/hops-h2-vs-h3.json)):

| algorithm | params | ok / scheduled | solve s p50 / p95 / max | quotes p50 / p95 / max | at best known | shortfall bps mean / p50 / p95 / max |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `path_split` | max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.18 / 1.45 / 1.98 | 1,064 / 2,607 / 2,755 | 18/96 | 15.31 / 8.64 / 50.34 / 101.21 |
| `incremental_graph` | chunks=50 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.31 / 2.01 / 2.57 | 1,470 / 3,609 / 4,298 | 18/96 | 13.80 / 5.34 / 45.91 / 101.20 |
| `incremental_graph` | chunks=200 max_hops=2 max_splits=4 percent_step=5 | 96/96  | 0.41 / 2.57 / 5.45 | 2,231 / 6,331 / 7,716 | 19/96 | 13.47 / 3.12 / 45.79 / 101.21 |
| `path_split` | max_hops=3 max_splits=4 percent_step=5 | 96/96  | 4.77 / 11.53 / 19.35 | 15,840 / 28,369 / 30,790 | 18/96 | 5.30 / 3.09 / 20.51 / 25.10 |
| `incremental_graph` | chunks=20 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 6.48 / 19.58 / 24.30 | 19,911 / 35,373 / 41,783 | 21/96 | 1.70 / 0.94 / 5.44 / 13.42 |
| `incremental_graph` | chunks=50 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 9.25 / 35.88 / 76.27 | 26,358 / 48,708 / 58,955 | 32/96 | 0.76 / 0.21 / 3.68 / 7.54 |
| `incremental_graph` | chunks=200 max_hops=3 max_splits=4 percent_step=5 | 96/96  | 13.84 / 52.17 / 99.21 | 41,783 / 85,898 / 99,061 | 81/96 | 0.89 / 0.00 / 6.74 / 15.74 |

Reading: at 2 hops, shortfall falls monotonically with `chunks` and `percent_step` barely
matters for `incremental_graph`; a finer 2% grid buys `path_split` ~0.35 bps mean at
~2.7× the median time, a coarser 10% grid costs ~0.36 bps. At 3 hops the chunk response is
**not monotonic** (the greedy chunk walk can lock into a worse structure: `chunks 200`
reaches the best known on 79/96 cases yet has the worst p95/max), and `chunks 50` has
the lowest mean and p95 shortfall. The hop bound dominates everything else: every 2-hop
split search falls 13–15 bps (mean; p95 ~46–50 bps) short of the 3-hop best known, at
~1/25–1/30 of the median solve time. 96 cases; differences below ~1 bps are within what
this sample can distinguish and are not claimed as significant.

### 3.3 Chosen values and their provenance (DESIGN §2.12)

| Parameter | `daily` / `daily_gross` | `full` / `full_gross` | Provenance |
| --- | --- | --- | --- |
| `algorithms` | all six | all six | DESIGN §2.6 (mandatory) |
| `objective` | `empirical_cost` (v1 model) / `gross_only` | same | DESIGN §2.9: core v1 needs the validated model; gross-only twins are the labeled development mode, identical otherwise (test-enforced) |
| `search.max_hops` | **2** (declared reduction) | **3** | full: DESIGN trial value, *kept on evidence* (§3.2 hop table); daily: a declared, cheaper search, not a calibrated optimum |
| `search.max_splits` | 4 | 4 | DESIGN trial value; **not swept — unvalidated** |
| `search.percent_step` | 5 | 5 | DESIGN trial value, kept (2-hop sweep: a 2% grid gains ~0.35 bps mean for `path_split` at 2.7× the median time; a 10% grid loses ~0.36 bps) |
| `graph.chunks` | **200** | **50** | calibrated at each profile's hop bound (§3.2) |
| `budget.time_limit_seconds` | 120 | 900 | calibrated: ~20× / ~9× the largest tuning-split solve at that hop bound (5.9 s / 99 s), headroom for the shared machine |
| `budget.max_quotes` | 50,000 | 300,000 | calibrated: ~6× / ~3.6× the largest tuning-split quote count (7,868 / 82,724) |
| `budget.max_candidates` | `null` (explicit) | `null` (explicit) | quotes and time bound every search; no candidate cap declared |
| `measurement.warmup` / `repeats` | 1 / 3 | 0 / 1 | daily: median of 3 latency samples and a per-case repeat-consistency check; full: one sample per case (a hop-3 pass is hours long) |
| `measurement.memory_pass` | `true` | `false` | memory measured on daily only (tracemalloc pass, separate from timing) |
| `measurement.seed` / `order` | 1447 / `fixed` | 1447 / `fixed` | declared; `--order reverse|shuffle` only for the order checks (§6) |
| `worker` | `spawn`, `algorithm` scope, 120 s prepare limit | same | WHI-1437 isolation; observed start-up + prepare < 0.2 s |

The profile headers carry the same provenance. One correction to them: they say the
3-hop search costs "~10x" the 2-hop one; the measured medians above are ~25–30× for the
split searches. The files are left byte-identical because the final runs record their
sha256. The daily 50,000-quote cap (6× the tuning maximum) was reached on 4 held-out
boundary cases by `incremental_graph` (§4, §9); it is kept as declared, not re-tuned on
the report split.

## 4. Final runs (held-out report split)

All final runs used clean commits of this branch (`git_dirty: false` in every
manifest): the full-profile runs and the first daily launch ran at `e64ce40`, the
order-check / rerun daily runs and the relaunched daily runs at `fba60cc`. Between the
two, only reporting, the manifest composer, tests and docs changed — no solver,
evaluator, pool, runner or profile file (`git diff --stat e64ce40 fba60cc`). Results are
in the primary clone's gitignored `data/results/<run id>`; logs in `data/logs/whi-1447/`
(the final runs' stdout is checked in under `v1-acceptance/stdout/`).

| Label | Run id | Profile | Bundle (cut) | Objective | Order | Wall time | Statuses (6 × 302 = 1,812) |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| F1 | `20260925T160801076875Z-32d039fb` | `config/full_gross.yaml` | `bundle_report` (five-source) | gross-only | fixed | 4 h 58 m | ok 1,745 · no_route 55 · invalid_plan 12 |
| F2 | `20260925T160801026614Z-8209a262` | `config/full_gross.yaml` | `sor_cohort_report` (matched V2/V3) | gross-only | fixed | 4 h 35 m | ok 1,740 · no_route 60 · invalid_plan 12 |
| F3 | `20260925T160801039139Z-d3e2144d` | `config/full.yaml` | `bundle_report` | empirical cost v1 | fixed | 4 h 58 m | ok 1,745 · no_route 55 · invalid_plan 12 |
| F4 | `20260925T160801118826Z-74e3e7e0` | `config/full.yaml` | `sor_cohort_report` | empirical cost v1 | fixed | 4 h 35 m | ok 1,740 · no_route 60 · invalid_plan 12 |
| F5 | `20260925T160801028971Z-38a62c54` | `config/full_gross.yaml` | `bundle_report` | gross-only | **reverse** | 4 h 57 m | ok 1,745 · no_route 55 · invalid_plan 12 |
| D1 | `20260925T210241655466Z-2efac37a` | `config/daily_gross.yaml` | `bundle_report` | gross-only | fixed | 3 h 30 m | ok 1,757 · no_route 55 |
| D2 | `20260925T210241565278Z-95556b57` | `config/daily_gross.yaml` | `sor_cohort_report` | gross-only | fixed | 3 h 13 m | ok 1,752 · no_route 60 |
| D3 | `20260925T210241633559Z-c3ccc41a` | `config/daily.yaml` | `bundle_report` | empirical cost v1 | fixed | 3 h 29 m | ok 1,757 · no_route 55 |
| D4 | `20260925T210241589234Z-64ab5e56` | `config/daily.yaml` | `sor_cohort_report` | empirical cost v1 | fixed | 3 h 13 m | ok 1,752 · no_route 60 |
| D5 | `20260925T185541322656Z-ae61cdb7` | `config/daily_gross.yaml` | `bundle_report` | gross-only | **shuffle** | 4 h 03 m | ok 1,757 · no_route 55 |
| D6 | `20260925T185543171981Z-6536a6e7` | `config/daily_gross.yaml` | `bundle_report` | gross-only | fixed (**rerun**) | 4 h 02 m | ok 1,757 · no_route 55 |

Daily wall time includes the separate tracemalloc memory pass (about two thirds of it).

Replay any of them with the manifest's `replay_command`, e.g.
`uv run python main.py run --bundle data/corpus/mantle-5src-101082044/bundle_report --profile config/full_gross.yaml --results-dir data/results`
(F1; add `--order reverse` for F5).

**No `timeout`, `unsupported`, `incomplete_snapshot`, `model_error` or
`algorithm_error` outcome occurred in any final run.** In the full runs no per-case limit
was reached (largest held-out solve 206 s against the 900 s limit, largest quote count
192,277 against 300,000 — `uni_sor_port` / `incremental_graph`, under a ~2–4× load
slowdown). In the daily runs the largest solve was 7.5 s against 120 s; the 50,000-quote
cap **was** reached by `incremental_graph` on 4 state-derived boundary cases of the
five-source cut (`bnd-{78c1b0,cda86a}-201eba-round_{at,below}`, in D1/D3/D5/D6): its
chunk walk stopped (`candidates_truncated: 1`, `search.truncated_by: max_quotes`) and it
returned its `path_split` finalist (a 1-raw-unit output, the same as every other
algorithm's there). These records stay `ok` with the truncation recorded (§9). Every
scheduled (case, algorithm) pair has a record.

## 5. Results

All numbers below are **held-out** (report split, 302 cases: 216 activity-pair, 24
no-direct-pool, 62 state-derived boundary) from the full profile unless marked daily.
Quality is the per-case relative gross difference in bps on cases where both algorithms
are `ok` ("+better / =equal / −worse"), as computed by `main.py report`
([`v1-acceptance/report-full/`](v1-acceptance/report-full/report.html), `paired_gross.csv`);
raw amounts of different assets are never averaged. "Best known" is the best among the
run's algorithms, not an optimum.

### 5.1 Coverage and statuses (every scheduled outcome)

| Algorithm | Capability | Matched V2/V3 (F2 = F4) | Full five-source (F1 = F3 = F5) |
| --- | --- | --- | --- |
| `direct` | single pool | ok 276 · no_route 26 | ok 277 · no_route 25 |
| `single_path` | multi-hop, no split | ok 300 · no_route 2 | ok 301 · no_route 1 |
| `direct_split` | direct split | ok 276 · no_route 26 | ok 277 · no_route 25 |
| `path_split` | multi-hop disjoint split | ok 300 · no_route 2 | ok 301 · no_route 1 |
| `incremental_graph` | shared-pool graph | ok 300 · no_route 2 | ok 301 · no_route 1 |
| `uni_sor_port` | SOR V2/V3 subset | ok 288 · no_route 2 · **invalid_plan 12** | ok 288 · no_route 2 · **invalid_plan 12** |

- `no_route` for `direct`/`direct_split`: the 24 no-direct-pool cases (capability, by
  design) plus the CATI→WMNT dust / round-below boundary cases, which no pool turns into
  a positive output (`insufficient_output_amount`); in the five-source universe an LB
  pool routes the round-below case, so only the dust case remains.
- `uni_sor_port` `invalid_plan` (12, identical in both cohorts and both objectives): the
  selected route set forms an economic token cycle (e.g. USDT→USDC→WETH→mETH +
  USDT→WMNT→USDC→mETH + USDT→WETH→WMNT→mETH ⇒ USDC→WETH→WMNT→USDC), which the v1 evaluator
  rejects (§3.1, §9). 10 empirical low/medium cases and 2 boundary cases.
- `unsupported`: 0 — every case is reachable in the matched cohort at this block
  (WHI-1436), so SOR's missing LB support shows up as a quality/coverage gap, not as
  `unsupported`.
- **All five sources are used by solved plans** (report *Source coverage*): on the
  five-source cut, e.g. `path_split` plans touch Agni 236 / FusionX 186 / Moe Classic 68 /
  Moe LB 187 / Uniswap v3 72 cases, `incremental_graph` 247 / 202 / 121 / 192 / 120;
  `uni_sor_port` never touches LB (0), as declared.

### 5.2 Matched V2/V3 cohort, gross (F2) — like-for-like with SOR

Every algorithm sees exactly SOR's 98 candidate pools.

| Algorithm vs baseline | Paired n | + / = / − | p5 / p50 / p95 bps | Notes |
| --- | ---: | --- | --- | --- |
| `uni_sor_port` vs `path_split` | 276 | 9 / 233 / 34 | −0.33 / 0.00 / 0.00 | same capability (disjoint split); near parity, `path_split` slightly ahead; +12 cases only `path_split` succeeds (SOR `invalid_plan`) |
| `incremental_graph` vs `uni_sor_port` | 276 | 176 / 100 / 0 | 0.00 / +0.28 / +14.03 | 138 like-for-like, 138 capability gains (shared-pool plans) |
| `incremental_graph` vs `path_split` | 288 | 186 / 102 / 0 | 0.00 / +0.38 / +14.03 | never worse (it keeps `path_split` as a finalist) |
| `path_split` vs `direct` | 252 | 159 / 93 / 0 | 0.00 / +5.36 / +122.67 | +24 cases only `path_split` succeeds (no direct pool) |
| `uni_sor_port` vs `direct` | 240 | 147 / 93 / 0 | 0.00 / +2.89 / +118.21 | |
| `single_path` vs `direct` | 252 | 96 / 156 / 0 | 0.00 / 0.00 / +110.71 | multi-hop only |
| `direct_split` vs `direct` | 252 | 87 / 165 / 0 | 0.00 / 0.00 / +55.82 | split only |

Per amount stratum (vs `direct`, matched, report split): `incremental_graph` median gain
low +28.6 / medium +5.4 / large +0.2 bps (80 cases each, 72 paired; p95 101.7 / 66.9 /
121.6); `uni_sor_port` +17.7 / +3.0 / 0.0. Boundary cases are their own stratum
(state-derived, not empirical trades).

### 5.3 Full five-source coverage, gross (F1) — coverage and capability gains

| Algorithm vs baseline | Paired n | + / = / − | p5 / p50 / p95 bps | Notes |
| --- | ---: | --- | --- | --- |
| `incremental_graph` vs `path_split` | 291 | 197 / 94 / 0 | 0.00 / +0.73 / +13.24 | 124 like-for-like, 167 capability |
| `path_split` vs `direct` | 255 | 185 / 70 / 0 | 0.00 / +4.73 / +131.23 | |
| `path_split` vs `uni_sor_port` ⚑ coverage | 276 | 158 / 108 / 10 | 0.00 / +0.17 / +31.31 | LB liquidity SOR cannot use |
| `incremental_graph` vs `uni_sor_port` ⚑ coverage | 276 | 196 / 80 / 0 | 0.00 / +4.41 / +38.34 | |

These are **not** search-quality differences: SOR is restricted to V2/V3 by declaration,
the others may use the 45 LB pools (report flag ⚑ *coverage*).

### 5.3a Daily profile (2 hops, D1/D2) — regression profile, held-out

Statuses are those of the full runs except that `uni_sor_port` has **no** `invalid_plan`
(ok 300 · no_route 2 on both cuts): a token cycle across routes needs 3-hop routes.

| Comparison (gross) | Cohort | Paired n | + / = / − | p5 / p50 / p95 bps |
| --- | --- | ---: | --- | --- |
| `uni_sor_port` vs `path_split` | matched (D2) | 284 | 10 / 248 / 26 | −0.10 / 0.00 / 0.00 |
| `incremental_graph` vs `uni_sor_port` | matched (D2) | 284 | 168 / 113 / 3 | 0.00 / +0.25 / +8.84 |
| `incremental_graph` vs `path_split` | matched (D2) | 284 | 168 / 116 / 0 | 0.00 / +0.25 / +8.39 |
| `path_split` vs `direct` | matched (D2) | 252 | 139 / 113 / 0 | 0.00 / +0.47 / +98.63 |
| `incremental_graph` vs `path_split` | five-source (D1) | 288 | 186 / 102 / 0 | 0.00 / +0.37 / +8.06 |
| `path_split` vs `uni_sor_port` ⚑ coverage | five-source (D1) | 284 | 128 / 156 / 0 | 0.00 / 0.00 / +20.43 |

**What the 2-hop reduction costs** (same algorithm, same case, full − daily gross, bps;
cases `ok` in both with a positive daily output):

| Algorithm | five-source F1 vs D1: n, full + / = / −, p50 / p95 | matched F2 vs D2 |
| --- | --- | --- |
| `direct`, `direct_split` | identical on every case (no hop parameter) | identical |
| `single_path` | 288: 77 / 211 / 0, 0.00 / +38.6 | 284: 68 / 216 / 0, 0.00 / +47.4 |
| `path_split` | 288: 174 / 111 / 3, +0.83 / +47.6 | 284: 134 / 143 / 7, 0.00 / +56.8 |
| `incremental_graph` | 288: 176 / 78 / 34, +1.27 / +53.3 | 284: 149 / 92 / 43, +0.22 / +67.2 |
| `uni_sor_port` | 272: 130 / 141 / 1, 0.00 / +53.8 (+12 cases only daily is `ok`: the 3-hop `invalid_plan`s) | same |

So the daily profile is a cheaper regression check (per measured attempt about 1/17 of
the full profile's summed solve time, §7), not the acceptance comparison: at the 95th percentile it gives up ~40–65 bps against
the 3-hop search. `incremental_graph` at 3 hops is worse than at 2 hops on 34/43 cases
(p5 −0.3 / −0.5 bps): the non-monotonic greedy chunk walk of §3.2, reported as is.

Daily empirical-cost runs (D3/D4) repeat the pattern of §5.4 at 2 hops: split and
shared-pool plans are unscored on net, so `direct_split` matches `direct`'s gross on all
252 paired matched cases and `path_split` matches `single_path`'s on every paired case
(276 matched / 277 five-source); `uni_sor_port`, selecting on raw
quotes, is ahead of `path_split` on gross on 135/276 matched cases (p95 +54.6 bps) but is
net-rankable on only 150/302 cases (`low_confidence` 106, `unsupported` 44).

Memory (daily only; tracemalloc peak of Python allocations, five-source D1, p50 / max):
`path_split` 25.8 / 229.9 MiB, `uni_sor_port` 26.9 / 289.5 MiB, `incremental_graph`
38.1 / 2,158.5 MiB (the largest on the quote-capped boundary cases above), the others
≤ 2 MiB median.

### 5.4 Estimated net output (F3, F4; cost model v1)

| Algorithm | Net-rankable plans, matched (F4) | Full (F3) | Unranked (reason) |
| --- | ---: | ---: | --- |
| `direct` | 276 | 277 | only no_route |
| `single_path` | 288 | 289 | `low_confidence` 12 / 8, `unsupported` 0 / 4 |
| `direct_split` | 276 | 273 | `low_confidence` 0 / 4 |
| `path_split` | 288 | 289 | `low_confidence` 12 / 8, `unsupported` 0 / 4 |
| `incremental_graph` | 288 | 289 | `low_confidence` 4 / 2, `unsupported` 8 / 10 |
| `uni_sor_port` | 108 | 108 | `low_confidence` 91, `unsupported` 89 (its split plans), plus 12 `invalid_plan` |

- Under `empirical_cost` split and shared-pool plans have **no validated cost**
  (WHI-1445), so the split searches return the best *rankable* plan: versus the gross
  twin (F1 vs F3, same bundle and search) `path_split` changed 207 plans,
  `incremental_graph` 211, `single_path` 111, `direct_split` 92, giving up a median
  14–22 bps of gross (among changed plans) to reach a rankable, cheaper shape.
  `uni_sor_port` changed none (it keeps zero gas scores, contract A-3).
- Consequently `single_path`, `path_split` and `incremental_graph` return the same net
  result on the net-rankable common cases (paired net p5 = p50 = p95 = 0 bps); vs
  `direct` they gain up to +52.3 bps (p95, matched) / +44.1 bps (full) at the nominal
  scenario, with 3 / 8 scenario rank reversals (low/high cost) and, for `direct_split` on
  the full cut, 22 gross-vs-net ordering reversals.
- 40–41 cases per algorithm have a non-positive net baseline (dust/small trades whose
  estimated execution cost exceeds the output) and are N/A, never scored as zero.
- **This is not evidence that splitting is worthless**; it is the declared effect of an
  uncalibrated split cost (§10.2).

## 6. Deterministic replay and case-order independence

`main.py order-check A B` (`benchmark.runner.compare_runs`, WHI-1437) compares, per
(algorithm, case), everything a run records that must not depend on timing or order:
status, the full evaluation (per-leg amounts, gross and — under `empirical_cost` — net
outputs and flags), score, error text, candidate counts, the solver's own search
counters, counted quotes, solver-reported fields, the per-case seed and the repeat
consistency flag. Only latency and memory are excluded. It exits non-zero on any
mismatch. The acceptance manifest re-runs each check and records the mismatch count.

| Check | Runs | Orders | Compared records | Mismatches |
| --- | --- | --- | ---: | ---: |
| `full-reverse` | F1 vs F5 (`full_gross`, five-source) | fixed vs **reverse** | 1,812 | **0** |
| `daily-shuffle` | D6 vs D5 (`daily_gross`, five-source) | fixed vs **shuffle** (seed 1447) | 1,812 | **0** |
| `daily-rerun` | D1 vs D6 (`daily_gross`, five-source, launched 2 h apart) | fixed vs fixed (independent rerun) | 1,812 | **0** |
| `daily-shuffle-vs-first` | D1 vs D5 | fixed vs **shuffle** | 1,812 | **0** |

In addition, every daily record ran 1 warm-up + 3 measured attempts inside one worker and
checked that all attempts returned the identical result (`attempts_consistent`): true on
all 1,812 records of every daily run. The four independent full runs agree with each
other where they must: F1 and F5 are identical (above), and F3/F4 (empirical cost) have
the same status counts as F1/F2 per cohort. The only nondeterminism a run records is
timing and memory.

These checks detect leakage through worker state, caches or case order; they do not
prove its absence for orders not tried.

## 7. Wall time and environment

**Decision (recorded, per DESIGN §2.10 and the owner's ruling that runtime is not an
acceptance threshold): the wall times below are from _concurrent_ runs on a shared
workstation and are contaminated. No clean sequential timing pass was made.** The
reference machine is the owner's everyday workstation (other agents, a browser and
messaging apps run on it), so even a sequential pass would not satisfy §2.10's
"otherwise idle" condition; a clean timing pass on a dedicated machine is logged in
`docs/DEFERRED_ISSUES.md`. The deterministic results (§5, §6) are unaffected by load;
only latency, wall time and the memory-pass duration are.

Environment (identical in every final manifest except the git revision): Apple M2 Pro,
10 cores (6 performance + 4 efficiency), macOS 27.0 (Darwin 27.0.0, arm64), CPython
3.13.13, `pyyaml` 6.0.3, `pycryptodome` 3.23.0, clock `perf_counter_ns` (monotonic,
41.7 ns resolution), workers `spawn` per algorithm with a 120 s prepare limit. Git
revision `e64ce40` (F1–F5) / `fba60cc` (daily), `git_dirty: false` everywhere.

Concurrency while the final runs executed (all on the 10-core machine):

| Interval (UTC, 2026-09-25/26) | Acceptance processes running |
| --- | ---: |
| 16:08 – 18:55 | 9 (F1–F5 + first daily launch D1–D4) |
| 18:55 – 20:08 | 11 (+ D5, D6) |
| 20:08 – 21:05 | 7 (first daily launch killed by its 4 h wrapper at 20:08; F2/F4 end 20:43) |
| 21:02 – 22:58 | 6–9 (daily relaunch D1–D4 + D5, D6 + F1/F3/F5 until 21:05) |
| 22:58 – 00:15 | 4 (D1–D4) |
| 00:15 – 00:32 | 2 (D1, D3) |

| Run | Timing pass | Memory pass | Total wall | Solve-time sum per algorithm (s): direct / single_path / direct_split / path_split / incremental_graph / uni_sor_port |
| --- | ---: | ---: | ---: | --- |
| F1 | 4 h 58 m | – | 4 h 58 m | 1 / 769 / 55 / 2,639 / 4,786 / 9,579 |
| F2 | 4 h 35 m | – | 4 h 35 m | 1 / 519 / 49 / 2,208 / 3,454 / 10,272 |
| F3 | 4 h 58 m | – | 4 h 58 m | 1 / 777 / 55 / 2,645 / 4,795 / 9,579 |
| F4 | 4 h 35 m | – | 4 h 35 m | 1 / 522 / 49 / 2,205 / 3,452 / 10,255 |
| F5 | 4 h 57 m | – | 4 h 57 m | 1 / 767 / 56 / 2,621 / 4,785 / 9,575 |
| D1 | 1 h 11 m | 2 h 19 m | 3 h 30 m | 4 / 136 / 122 / 454 / 931 / 1,503 |
| D2 | 1 h 06 m | 2 h 07 m | 3 h 13 m | 4 / 114 / 118 / 423 / 764 / 1,511 |
| D3 | 1 h 11 m | 2 h 19 m | 3 h 29 m | 4 / 138 / 121 / 456 / 932 / 1,504 |
| D4 | 1 h 06 m | 2 h 07 m | 3 h 13 m | 4 / 115 / 118 / 423 / 766 / 1,507 |
| D5 | 1 h 40 m | 2 h 22 m | 4 h 03 m | 5 / 269 / 189 / 697 / 1,427 / 1,883 |
| D6 | 1 h 40 m | 2 h 22 m | 4 h 02 m | 5 / 268 / 189 / 697 / 1,434 / 1,877 |

(Daily solve sums are over 3 measured attempts per case; full over one.) The
contamination is visible directly: D1 and D6 are the same profile over the same bundle and
produce identical deterministic output (§6), yet D6's timing pass took 1 h 40 m against
D1's 1 h 11 m (1.42×) and its `incremental_graph` solve sum 1,434 s against 931 s (1.54×),
only because more runs shared the machine while D6 ran. Per-case latency percentiles and
quote counts per algorithm are in each report's `latency.csv` and *Latency* section;
at the full profile the per-case p50 / p95 solve times on the five-source cut were
`path_split` 7.4 / 18.5 s, `incremental_graph` 12.8 / 43.9 s, `uni_sor_port` 19.5 /
104.2 s (F1) — observations under ~2–4× load, not latency estimates.

## 8. Linked evidence from earlier issues

| Evidence set | Issue(s) | Record | What it establishes |
| --- | --- | --- | --- |
| Source/deployment admission catalog and preflight | WHI-1426 | [`protocol-admission.md`](protocol-admission.md), `config/protocols.yaml`, [`preflight-evidence-2026-09-24.json`](preflight-evidence-2026-09-24.json) | Verified factories/code hashes/licenses for all five sources; code-hash-pinned preflight |
| Concentrated-liquidity integer math | WHI-1428 | [`concentrated-liquidity-migration.md`](concentrated-liquidity-migration.md) | Solidity→Python map, deployed-source diff for Agni/FusionX/Uniswap, fork differential evidence (exact integer outputs and next state) |
| Agni v3 fixed-block admission | WHI-1429 | [`agni-fixed-block-replay.md`](agni-fixed-block-replay.md) | Fixed-block collection and exact replay vs fork simulation |
| FusionX v3 fixed-block admission | WHI-1430 | [`fusionx-fixed-block-replay.md`](fusionx-fixed-block-replay.md) | Own fingerprint, fork + deployed QuoterV2 agreement, independent recompile |
| Uniswap v3 fixed-block admission | WHI-1431 | [`uniswap-v3-fixed-block-replay.md`](uniswap-v3-fixed-block-replay.md) | Same, for Uniswap v3 on Mantle |
| Merchant Moe Classic fixed-block admission | WHI-1432 | [`moe-classic-fixed-block-replay.md`](moe-classic-fixed-block-replay.md) | Fixed 0.3% fee, pair identity, fork + MoeRouter agreement |
| Liquidity Book v2.2 math with evolving fee state | WHI-1433 | [`liquidity-book-migration.md`](liquidity-book-migration.md) | Bin/fee/volatility-accumulator transitions vs fork evidence, swap-hook admission |
| Moe LB fixed-block admission | WHI-1434 | [`moe-lb-fixed-block-replay.md`](moe-lb-fixed-block-replay.md) | Complete bounded bin snapshot, verified hooks/rewarders, exact replay |
| Shared-pool / split / merge evaluator correctness | WHI-1435 | `routing/evaluator.py`, `tests/routing/test_plan_evaluation.py`, [`tests/fixtures/routing/README.md`](../../tests/fixtures/routing/README.md) | Hand-derived CPMM vectors (independent quoting of a shared suffix over-claims +5.65%) and fork-executed sequential CL/LB/Classic swaps through one physical pool; 20 exact-reason rejections |
| Frozen five-source corpus and price context | WHI-1436 | [`corpus.md`](corpus.md) | One common block, bounded Dune evidence, deterministic selection, envelope + single exclusion rule (no pool excluded), frozen prices |
| Isolated measured runner | WHI-1437 | `benchmark/runner.py`, `benchmark/worker.py` | Spawned workers, hard time/quote limits, honest timeout/crash records, order-leak check |
| SOR source/parity contract and upstream goldens | WHI-1442, WHI-1443 | [`uni-sor-port-contract.md`](uni-sor-port-contract.md), `tools/upstream/uni_sor/`, `tests/fixtures/uni_sor/` | Pinned upstream, 35 goldens generated by the actual pinned functions, byte-identical regeneration |
| SOR port parity | WHI-1444 | `routing/algorithms/uni_sor_port.py`, `tests/routing/test_uni_sor_parity.py` (221 tests, 35/35 goldens, no skips) | Exact selection/allocation parity on every golden; two mutation checks fail |
| Execution-cost holdout validation | WHI-1445 | [`cost-model.md`](cost-model.md) §§1–4 | Mantle fee formula verified on receipts/balances; 5 supported cohorts with 70/30 holdout error quantiles (scenarios, not CIs) |
| Offline report rules | WHI-1446 | [`report.md`](report.md) | Full-schedule denominators, paired per-case bps, cohort separation, net only from `estimated_net_output` |

The per-algorithm behaviour and tests are in `routing/algorithms/*.py` and
`tests/routing/test_{direct,single_path,direct_split,path_split,incremental_graph}.py`
(WHI-1427, WHI-1438–WHI-1441).

## 9. Failures, unsupported, timeouts

Every non-`ok` outcome of the final runs, grouped (full text per case in the report's
*All non-ok outcomes* section and `cases.csv`):

| Outcome | Algorithms | Cases (held-out) | Cause |
| --- | --- | --- | --- |
| `no_route` — no admitted direct pool | `direct`, `direct_split` | 24 per run | the no-direct-pool pairs (USDC/FBTC, FBTC/WETH, USDC/CATI, CATI/USDT0); capability, by design |
| `no_route` — every path/pool fails | all six | `bnd-1bdd88-78c1b0-dust` (both cuts); `bnd-1bdd88-78c1b0-round_below` (all six on the matched cut; only `uni_sor_port` on the five-source cut, where an LB pool routes it) | state-derived boundary: 1 raw CATI unit / one below the smallest input any V2/V3 pool turns into output (`insufficient_output_amount`; SOR B-S10) |
| `invalid_plan` — economic token cycle | `uni_sor_port` | 12 per full run (identical in F1–F5; 0 in the 2-hop daily runs): `emp-09bc4e-201eba-{low,medium}-{1,3}`, `emp-201eba-cda86a-low-{1,3}`, `emp-78c1b0-09bc4e-low-3`, `emp-78c1b0-deadde-low-{1,3,4}`, `bnd-09bc4e-201eba-liq_{at,above}` | upstream combines routes by pool id only; the combined token graph has a cycle, which DESIGN §2.5 excludes. Kept for parity; `docs/DEFERRED_ISSUES.md` |
| `ok`, search truncated by the quote cap | `incremental_graph` | 4 per daily five-source run (D1, D3, D5, D6): `bnd-{78c1b0,cda86a}-201eba-round_{at,below}` | the 50,000-quote daily cap stopped its chunk walk (`candidates_truncated: 1`, `truncated_by: max_quotes`); it returned its `path_split` finalist. Recorded, not hidden; none in the full runs (300,000 cap) |
| `timeout` | — | 0 | no per-case time limit was reached; no search was killed |
| `unsupported`, `incomplete_snapshot`, `model_error`, `algorithm_error`, `cancelled` | — | 0 | — |

Calibration (tuning split, exploratory) showed the same SOR cycle pattern on 2/96 cases
and nothing else.

**Aborted / incomplete launches (not part of the evidence, kept on disk):**

- `20260925T185438579303Z-06c9c815` (shuffle) and `20260925T185439520583Z-28d01b51`
  (rerun): started while untracked doc files made the tree dirty; stopped after a minute
  with SIGTERM (finalized `interrupted`, all unfinished cases `cancelled`) and relaunched
  from a clean commit.
- The first daily launch (`20260925T160801055293Z-b2763415` daily_gross/five-source,
  `…047973Z-e951c15f` daily_gross/matched, `…195893Z-fbf733c8` daily/five-source,
  `…101204Z-b87db69d` daily/matched; stdout `v1-acceptance/stdout/killed-by-wrapper-D*.txt`): its 4 h `timeout`
  wrapper, sized before the machine was this loaded, expired at 20:08 UTC while the runs
  were in the tracemalloc memory pass (timing pass complete, 1,591–1,664 of 1,812 memory
  records). The wrapper's SIGTERM reached both `uv` and the Python child; the second
  signal escaped the interrupt handler, so these manifests stay `state: running` and
  `load_manifest` refuses them (the new Low item in `docs/DEFERRED_ISSUES.md`). No result
  was taken from them; D1–D4 were relaunched from the same clean commit with a 12 h
  wrapper (`final-daily-rerun.sh`).

## 10. Uncertainty and limitations

### 10.1 Statistical and measurement uncertainty

- **One snapshot, one corpus.** 302 held-out cases (240 empirical + 62 boundary) at one
  block; no temporal generalization is claimed. Each amount stratum holds 80 held-out
  empirical cases but a per-direction cell only ~3–9; the report marks every paired
  sample below 30 cases ⚠ underpowered.
- **Best known is not optimal.** Every quality number is relative to another algorithm or
  to the best result among the run's algorithms under the recorded budget.
- **Calibration sample.** 96 tuning cases; sweep differences under ~1 bps are not
  distinguishable. `search.max_splits` was not swept.
- **Cost scenarios are not confidence intervals.** Low/high net outputs are the 5th/95th
  holdout-error quantiles of the v1 model (`cost-model.md` §3), one 7-day window, a
  single reference gas price; router overhead is included and averaged over the observed
  router mix.
- **Timing is an observation, not a guarantee.** The reference machine was shared with
  unrelated workloads and 2–11 acceptance runs ran concurrently (§7); latencies are
  inflated and noisy, the full profile has one sample per case, the per-case time limit
  covers IPC as well as `solve()`, and memory is tracemalloc peak (Python allocations,
  not RSS). No production-latency claim follows.

### 10.2 Deferred items (`docs/DEFERRED_ISSUES.md`, all still open)

| Item | Effect on this report |
| --- | --- |
| `uni_sor_port` can select a route set that forms an economic token cycle (WHI-1447, new) | Those cases are `invalid_plan` for SOR in every report (§9); not repaired, to keep parity |
| `uni_sor_port` ignores the empirical cost in its own selection (WHI-1445; contract A-3) | Under `empirical_cost` SOR still selects on raw quotes; its plan is net-evaluated and flagged like any other |
| `direct_split` rechecks the objective only over its per-leg-count finalists (WHI-1445) | A few `empirical_cost` plans fall back to a flagged unrankable split |
| Split / shared-graph and reverted-transaction costs not calibrated (WHI-1445) | Split and shared-pool plans are **unscored on net**; under `empirical_cost` the split searches settle for rankable single-route plans — visible against the gross twin, not a verdict on splitting |
| LB collector walks one bin per RPC round trip (WHI-1436) | Preparation cost only; the frozen bundle is complete within its declared bound |
| Temporary `release/v*` cut vs live branch; untracked port design record (governance) | None on the benchmark |
| Acceptance wall times from concurrent runs on a shared workstation (WHI-1447, new) | Latency/wall time are observations under load (§7); a clean timing pass on an idle machine is pending |
| A second SIGTERM during interrupt finalization leaves a run `running` (WHI-1447, new) | The first, wrapper-killed daily launch is unusable and was relaunched (§9); no result misreported |

### 10.3 Known limitations reported by earlier issues

- **Single exclusion ("thin-pool") rule and the envelope** (WHI-1436). A pool is excluded
  only if proving its envelope needs more than 8,192 bitmap words / 5,000 bins, so a thin
  pool is never dropped for being thin (none was excluded). The envelope is
  2 × the largest case notional per token; a multi-hop intermediate amount beyond a
  pool's collected state is `incomplete_snapshot` for that candidate (excluded and
  disclosed per case), never treated as real exhaustion.
- **LM-pool hook reverts are not modelled** (WHI-1428 §5): Agni/FusionX
  `accumulateReward`/`crossLmTick` are amount-neutral no-ops in the model; a revert
  inside the hook contract would revert the swap on chain.
- **Agni has no deployed QuoterV2**; its evidence is fork simulation only (WHI-1428 §8).
- **SOR is a scoped routing-core port** (WHI-1442/1444): candidate pools come from the
  frozen bundle (A-1), quotes from the benchmark simulator (A-2), gas scores are zero
  (A-3), `min_splits = 1`, final remainder per D-1. It is not the production
  `@uniswap/smart-order-router`, and LB is outside its protocols (LB-only cases would be
  `unsupported`; at this block every case is reachable in the matched cohort).
- **Corpus cases are historical swap legs**, not reconstructed user orders; the universe
  is the 8 tokens of the 12 most active pairs; boundary cases are state-derived and
  reported as their own stratum (WHI-1436).
- **No transaction executor**: plans are evaluated by exact pool math and state
  transitions, not executed as transactions; no executability claim (DESIGN §8).
- **`incremental_graph` is a heuristic** whose quality is not monotonic in
  `graph.chunks` at 3 hops (§3.2).
- **The daily profile is a smaller search** (2 hops): its split searches are measurably
  weaker than the full profile's (§3.2); it is a regression profile, not the
  acceptance comparison.

## 11. Reproduction

Everything below is offline except the (already done) corpus/cost preparation linked in
§8; run from the repository root with the frozen corpus at
`data/corpus/mantle-5src-101082044/bundle` (gitignored; its manifest is
`tests/fixtures/corpus/full_bundle.json`; rebuild recipe `corpus.md` §8).

```bash
C=data/corpus/mantle-5src-101082044
uv sync
uv run pytest && uv run ruff check . && uv run mypy          # code gates
uv run python main.py validate --bundle $C/bundle              # corpus bundle_hash 717c21f3…
uv run python main.py costs report --model config/costs/mantle-101082044-cost-v1.json

# 1. the declared cuts (deterministic; identities in docs/references/v1-acceptance/bundles.json)
uv run python main.py corpus cohort --bundle $C/bundle --output $C/sor_cohort
for b in bundle sor_cohort; do for s in tuning report; do
  uv run python main.py corpus split --bundle $C/$b --split $s --output $C/${b}_$s
done; done

# 2. calibration on the tuning split only (exploratory)
uv run python main.py run --bundle $C/bundle_tuning --profile config/calibration/probe-h3-baselines.yaml
uv run python main.py run --bundle $C/bundle_tuning --profile config/calibration/probe-h3-sor.yaml
for p in config/calibration/sweep-*.yaml; do
  uv run python main.py run --bundle $C/bundle_tuning --profile $p
done
uv run python main.py calibrate summarize data/results/<run> ...        # §3 tables
# (the sweep profiles are regenerated by, e.g.:
#  uv run python main.py calibrate profiles --base config/calibration/sweep-base.yaml \
#    --output config/calibration --prefix sweep-ig --algorithms incremental_graph \
#    --grid max_hops=3 --grid percent_step=5 --grid chunks=50,100,200)

# 3. held-out runs (report split). Long: run in the background with a timeout and a log.
for pr in full_gross full daily_gross daily; do for b in bundle_report sor_cohort_report; do
  timeout 43200 uv run python main.py run --bundle $C/$b --profile config/$pr.yaml \
    > data/logs/$pr-$b.log 2>&1 &
done; done; wait
uv run python main.py run --bundle $C/bundle_report --profile config/full_gross.yaml --order reverse
uv run python main.py run --bundle $C/bundle_report --profile config/daily_gross.yaml --order shuffle
uv run python main.py run --bundle $C/bundle_report --profile config/daily_gross.yaml   # rerun

# 4. deterministic replay / case-order independence (exit 0 = no mismatch); run ids of this record
R=data/results
F1=$R/20260925T160801076875Z-32d039fb F2=$R/20260925T160801026614Z-8209a262
F3=$R/20260925T160801039139Z-d3e2144d F4=$R/20260925T160801118826Z-74e3e7e0
F5=$R/20260925T160801028971Z-38a62c54
D1=$R/20260925T210241655466Z-2efac37a D2=$R/20260925T210241565278Z-95556b57
D3=$R/20260925T210241633559Z-c3ccc41a D4=$R/20260925T210241589234Z-64ab5e56
D5=$R/20260925T185541322656Z-ae61cdb7 D6=$R/20260925T185543171981Z-6536a6e7
uv run python main.py order-check $F1 $F5     # fixed vs reverse
uv run python main.py order-check $D6 $D5     # fixed vs shuffle
uv run python main.py order-check $D1 $D6     # independent rerun
uv run python main.py order-check $D1 $D5

# 5. offline reports (each run its own cohort section) and the final manifest
uv run python main.py report $F2 $F1 $F4 $F3 --output data/reports/whi-1447-full
uv run python main.py report $D2 $D1 $D4 $D3 --output data/reports/whi-1447-daily
uv run python main.py acceptance \
  --run F1=$F1 --run F2=$F2 --run F3=$F3 --run F4=$F4 --run F5=$F5 \
  --run D1=$D1 --run D2=$D2 --run D3=$D3 --run D4=$D4 --run D5=$D5 --run D6=$D6 \
  --order-check full-reverse=F1,F5 --order-check daily-shuffle=D6,D5 \
  --order-check daily-rerun=D1,D6 --order-check daily-shuffle-vs-first=D1,D5 \
  --report data/reports/whi-1447-full --report data/reports/whi-1447-daily \
  --output docs/references/v1-acceptance/manifest.json
```

The exact commands with the run ids of this record are in §4 and in each run manifest's
`replay_command`; every run manifest records its bundle/profile hashes, git revision
(clean) and environment. A rerun produces new run ids (never overwrites) and new wall
times; its deterministic outputs must agree (`order-check`).

## 12. Checked-in artifacts

All under [`v1-acceptance/`](v1-acceptance/); every file is listed with its sha256 in
[`v1-acceptance/SHA256SUMS`](v1-acceptance/SHA256SUMS) (`cd docs/references/v1-acceptance && shasum -a 256 -c SHA256SUMS`).

| Path | Content |
| --- | --- |
| `manifest.json` | the write-once acceptance manifest (`acceptance-manifest/1`): 11 runs with bundle/profile/objective/experiment identities, git revision, environment, status counts, wall time; 4 order checks (0 mismatches); sha256 of every rendered report file |
| `bundles.json` | identities of the corpus cuts (§2) |
| `calibration/*.json` | tuning-split calibration summaries (§3, exploratory) |
| `report-full/` | full-profile report of F2, F1, F4, F3 (`report.html` + CSVs), held-out |
| `report-daily/` | daily-profile report of D2, D1, D4, D3, held-out |
| `stdout/` | launch scripts and stdout/stderr of every final, killed and aborted launch (§4, §9) |

Not checked in (size): each report's per-record `cases.csv` (~1.5 MB; sha256 in
`manifest.json` → `reports`: full `1c952715…`, daily `33db8d96…`; regenerate with the §11
`main.py report` commands, byte-identical except that the embedded command line in
`report.html` records the `--output` path), and the raw run directories (`cases.jsonl`,
`memory.jsonl`, `manifest.json`; ~180 MB) that stay in the primary clone's gitignored
`data/results/`, with their sha256 in `manifest.json` → `runs[].{manifest,cases,memory}_sha256`.
