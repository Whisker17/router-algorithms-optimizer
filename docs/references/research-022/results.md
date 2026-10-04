# WHI-1602 results: exactness, deterministic work and timing of the three bounded strategies

| Item | Value |
| --- | --- |
| Issue / key | WHI-1602, `R022-Q06`, Release 0.2.2 (`fca63897-b103-46f4-a690-c297246ed1ff`) |
| Protocol | [`pruning-contract.md`](pruning-contract.md) §11 (pre-registered) with the §14 amendments; schedule [`config/research_022/schedule.yaml`](../../../config/research_022/schedule.yaml) (rules and operational choices in its `rules:` section) |
| Schedule commit | `93a9db52f128594b0411e07a3fb1e23fbc1ef955`: committed **and pushed to `origin/feat/whi-1602-bounded-campaign` before the first tuning observation** (stage T started at 2026-10-03T15:10Z from a fresh clone of exactly this commit) |
| Freeze commit | `46d6e2e628b2768caab124986c3ce50d3c2f891d`: this schedule plus `a5.values` (the registered rule applied to stage T) and the 12 generated A5 profiles; pushed before any report-split solve. Stages R, M, I and L executed from a fresh clone of exactly this commit (R started 2026-10-03T18:05Z) |
| Tools of the final analysis | `04f5df62615109f44a7a1a24845c4fea7d0e0144` (analyses run from a clean clone of exactly this commit); `campaign/tables.md` is rendered by the PR head, whose only tool change since is the rendering of per-family distributions; the differences from the tools that executed the stages are disclosed in §1.3 and change no recorded value |
| Base | `dev` @ `86a6fd3`; PR base `dev` (bootstrap clause: no `release/v*`, no git tag) |
| Host | Apple M2 Pro, 10 logical CPUs, macOS (arm64), CPython 3.13.13, numpy 2.5.3, scipy 1.18.1; shared with other long-lived agent sessions (idle load ≈ 2.3–3.5) |
| Status | Exactness gate **passed** on every non-truncated cell; work evidence complete; **timing `inconclusive`** (§7); no default changed, nothing adopted, no production claim |

## 0. Summary

Exactness first. At the production grid (`config/full_gross.yaml`: 3 hops, 4 splits, 5 %,
`graph.chunks 50`, the M4 label settings, the 0.2.1 budget, seed 1447) each bounded strategy returned
its reference's status, plan replay, score, error and §8.1 counters on **every non-truncated cell**: on
the tuning split, and once on the report split, under the gross objective (A1), the empirical-cost
objective (A2) and — for `metis_history_bounded` — the `10^6 / 10^7` caps with dominance `history` (A3)
and `off` (A4). **There is no exactness difference to file** and `bounded truncated ∧ reference not
truncated` is empty everywhere.

| Strategy | cells compared (tuning + report) | identical | excluded as truncated | bounded cut ∧ reference not cut | differences |
| --- | ---: | ---: | ---: | ---: | ---: |
| `single_path_bounded` | 1,194 | 1,194 | 0 | 0 | 0 |
| `incremental_graph_bounded` | 1,194 | 1,194 | 0 | 0 | 0 |
| `metis_history_bounded` | 1,978 | 1,978 | 12 | 0 | 0 |

Deterministic work on the report split, full-source bundle (`R-WP-A1-*-full`, the untimed work pass; reference → bounded) with the bound's cost beside it, never netted:

| Strategy | quotes executed | CL `swap_steps` | LB `lb_bins_swapped` | `pruned_bound` | `bound_evaluations` | `bound_table_cost` |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `single_path_bounded` | 865,583 → 166,847 (−80.7 %) | 152,084,351 → 77,115,730 (−49.3 %) | 1,031,145 → 135,862 (−86.8 %) | 1,055,521 | 1,169,449 | 0 |
| `incremental_graph_bounded` | 8,591,807 → 6,134,446 (−28.6 %) | 685,258,563 → 628,513,651 (−8.3 %) | 8,818,897 → 7,662,515 (−13.1 %) | 33,350,477 | 41,929,259 | 0 |
| `metis_history_bounded` | 5,562,023 → 5,253,049 (−5.6 %) | 467,065,161 → 456,539,073 (−2.3 %) | 7,908,935 → 7,727,016 (−2.3 %) | 1,723,290 | 2,253,310 | 0 |

| Strategy | disposition (§11.6) | statement | timing verdict |
| --- | --- | --- | --- |
| `single_path_bounded` | **keep_experimental** | work reduction only | inconclusive |
| `incremental_graph_bounded` | **keep_experimental** | work reduction only | inconclusive |
| `metis_history_bounded` | **keep_experimental** | work reduction only | inconclusive |

Timing is **`inconclusive`**: a sampled one-minute load above `0.5 × 10` CPUs fell inside the timing
window (§7), so the registered rule yields no speed claim and no timing disposition. The window was
not repeated. Every disposition below is "work reduction only".

## 1. What was run, and what was disclosed

### 1.1 Registration order

| Step | Commit / time | Evidence |
| --- | --- | --- |
| Pre-registration of the §11 protocol | `64f2f86` (WHI-1598) | `pruning-contract.md` §11, before any bounded strategy existed |
| Schedule, generated profiles, harness, tests | `93a9db5` | pushed before the first tuning observation: GitHub's `CreateEvent` for the branch (head `93a9db5`) is 2026-10-03T15:10:22Z, the stage-T ledger starts 15:10:59Z; `git ls-remote origin refs/heads/feat/whi-1602-bounded-campaign` then returned `93a9db52…`. `campaign/freeze.json` pins the files of the freeze commit |
| Stage T (tuning, checks) | from `93a9db5`, 2026-10-03 15:10Z → 17:55Z | 18/18 invocations `ok`, 0 reconciliation problems (`campaign/tuning-analysis.json`, `tuning-ledger.jsonl`) |
| A5 values, freeze | `46d6e2e` | `a5-values` over the stage-T reference records; written once into the schedule; GitHub `PushEvent` 2026-10-03T18:05:36Z, before the first report-split solve (stage-R ledger starts 18:06:04Z) |
| Stages R, M (report split, once; A5 on both splits), I | from `46d6e2e`: R 2026-10-03 18:06Z → 2026-10-04 05:47Z, M 18:06Z → 00:00Z, I 03:10Z → 03:24Z | R: 32/32 `ok`; M: 24/24; I: 12/12; each analysis reconciled with 0 problems |
| Stage L (timing) | from `46d6e2e`, launched 2026-10-04 07:04Z after the launch gate passed on attempt 10; ended 13:56Z | §7 |

No infrastructure retry occurred and no completed run was repeated; no solver, no bound and no
reference default was touched (`git diff 86a6fd3..HEAD -- routing pools snapshot report main.py` is
empty; `benchmark/` changes by the one disclosed line in §1.3).

### 1.2 The roster and how it was split

The 17 IDs are those `--strategies all` derives from the unchanged `config/full_gross.yaml`
(`campaign.py check` asserts the derivation equals the registered `all17`). So that the heavy strategies
run in parallel lanes, the roster ran as **group runs** (`--strategies profile` over generated profiles
that equal the derived roster's per-strategy values; `check` and
`test_group_profiles_change_only_the_algorithm_list_and_the_registered_sets` enforce it; the batch
test additionally compares each group's resolved settings with the recorded 17-ID run). Every bounded
strategy ran **in the same run as its reference**. A2–A5 ran on the full five-source bundle only; A1 on
both cohorts. Inputs (hash-verified copies of the frozen corpus bundles):

| Key | `bundle_hash` | cases | split / cohort |
| --- | --- | ---: | --- |
| `tuning_full` | `ee7afa7e…279b` | 96 | tuning / full_source |
| `tuning_sor` | `b900b866…7ade` | 96 | tuning / sor_compatible |
| `report_full` | `85202b20…71c0` | 302 | report / full_source (`previously_exposed`) |
| `report_sor` | `8213b7b0…e640` | 302 | report / sor_compatible (`previously_exposed`) |
| `full` | `717c21f3…3143` | 398 | all (parent of the L01 matrix and the sentinel) |
| `fixture` | `5401b1de…9ad0` | 96 | tracked fixture (CLI matrix) |

### 1.3 Disclosures (everything that was not as registered, or was found after observing)

1. **`benchmark/latency.py::arm_profile`** (one change, needed): an L08-style arm that selects a subset
   of the pinned profile now keeps only the `algorithm_options` of the algorithms it runs. Without it
   the loader refuses the REF arm (its profile also declares `metis_history_bounded`'s options), so a
   reference and its bounded strategy could not be compared by `report.latency compare`. Every L08 arm
   so far has no `algorithm_options`; `tests/benchmark/test_latency.py` passes unchanged. One more
   existing test needed one line: the legacy-profile guard `tests/benchmark/test_algorithm_options.py`
   globs every `config/**/*.yaml`, so `NEW_PROFILES` now lists the generated `config/research_022/profiles/`
   (the WHI-1562 precedent for `config/research_021/profiles/`). The first full-suite run on this branch
   failed exactly there; it passes with that line.
2. **The baseline comparison was refined after the first partial report-split analysis.** The first
   rule compared the order-check's whole deterministic view and flagged five references on every
   cell. The only differences were provenance stamps: `git_revision` inside `diagnostics` and
   `search.r021` (the 0.2.1 baseline was produced at `aa5726c`), plus — for the SOR cohort only —
   the `universe.cohort` label and the domain hash of `metis_history` and `incremental_graph_repair`
   (the 0.2.1 baseline recorded `full_source`; WHI-1606, merged after the baseline, records
   `sor_compatible`). The comparison now has three tiers (§3): the outcome and the strategy's own
   `search` counters (the contract's claim) are compared strictly; the research block is compared with
   the stamps removed and its remaining differences are listed. This was decided after seeing the
   first report-split analysis, not before; it changes no recorded value.
3. **The `mix` family label was wrong in the first analyses**: pool entries without a `family` key are
   constant-product and were labelled `?`. Fixed (`_pool_families`), re-run on every stage; it touches
   no exactness or work total.
4. **Post-observation tooling** (no computed value changes): the compact analysis output, richer
   tables and the per-run `manifest_sha256` / environment columns. The stage-T analysis regenerated
   with these tools equals the one produced from the schedule commit, modulo the tool hashes.
5. **A5 `max_candidates` does not bind the chunk strategies** (§5): the registered rule took the
   percentile of the reference's `candidates_considered`, but `max_candidates` for
   `incremental_graph` / `metis_history` is counted per chunk (`max_candidates_unit:
   label_relaxations_per_chunk` in the diagnostics block); the resulting budgets bound nothing. This is
   reported as a limit of the registered A5 design, not repaired (no report-split run is repeated).
6. **Host load.** Stages T, R, M and I ran several lanes on a shared host: their timings are
   descriptive only and every host window is `contaminated` by the registered rule (statuses and
   outputs are load-independent). Stage L is §7.

### 1.4 Premises checked

| Premise of the dispatch | What the records show |
| --- | --- |
| Tuning differentials used reduced grids; production-grid exactness unproven | Shown now: 96/96 per run on tuning, 302/302 on report, at the unchanged full-gross values (§2) |
| §11.2: the 14 references are unchanged versus the 0.2.1 baseline | Outcome and `search` identical on all 8 456 cells; the research block differs only by stamps and, on SOR, the WHI-1606 cohort label (§3) |
| M2 is active 0/96 under the preset on tuning (WHI-1600) | Re-derived: 0/96 (A1 full) and 0/96 (A1 SOR); gates `closed:frontier` 93 + `closed:dominance` 3 (full), 87 + 3 + 6 `open` (SOR). Under `dominance: off` (A4) M2 is active on **96/96** tuning cells and 301/302 report cells (the guide's 12/96 is its 96 *fixture* cases) |
| The §11 real-state request skips nothing | True of the guide's $10 000 USDC→USDT0 fixture request; the sentinel $1 000 USDC→USDT on the full bundle skips plenty (§6) |

## 2. Exactness (§11.2)

Per strategy and split, over A1–A4 (A5 is §5). *Compared* = neither side budget-truncated; *identical* = every §11.2 field and every §8.1 counter equal. `runs` counts the pair runs summed.

| Strategy | split | runs | scheduled | compared | identical | excluded as truncated | bounded cut ∧ reference not cut | differences | label disagrees | new failure status |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_path_bounded` | tuning | 3 | 288 | 288 | 288 | 0 | 0 | 0 | 0 | 0 |
| `single_path_bounded` | report | 3 | 906 | 906 | 906 | 0 | 0 | 0 | 0 | 0 |
| `incremental_graph_bounded` | tuning | 3 | 288 | 288 | 288 | 0 | 0 | 0 | 0 | 0 |
| `incremental_graph_bounded` | report | 3 | 906 | 906 | 906 | 0 | 0 | 0 | 0 | 0 |
| `metis_history_bounded` | tuning | 5 | 480 | 480 | 480 | 0 | 0 | 0 | 0 | 0 |
| `metis_history_bounded` | report | 5 | 1,510 | 1,498 | 1,498 | 12 | 0 | 0 | 0 | 0 |

**Cells excluded as truncated (listed).** All 12 are boundary cases of the `metis_history` pair in the generous-cap arms A3 and A4, where **both** sides stopped at `max_quotes` (the 300 000 budget): not exactness evidence, and `bounded truncated ∧ reference not truncated` stays empty.
- `R-A3-mh-full` (6): `bnd-779ded-c96de2-round_at`, `bnd-779ded-c96de2-round_below`, `bnd-cda86a-201eba-round_at`, `bnd-cda86a-201eba-round_below`, `bnd-cda86a-779ded-round_at`, `bnd-cda86a-779ded-round_below` — reference `truncated_by:max_quotes`, bounded `truncated_by:max_quotes`
- `R-A4-mh-full` (6): `bnd-779ded-c96de2-round_at`, `bnd-779ded-c96de2-round_below`, `bnd-cda86a-201eba-round_at`, `bnd-cda86a-201eba-round_below`, `bnd-cda86a-779ded-round_at`, `bnd-cda86a-779ded-round_below` — reference `truncated_by:max_quotes`, bounded `truncated_by:max_quotes`

No other cell of any of the 11 tuning and 11 report pair runs is truncated, timed out or missing; every tuning status is `ok`, and on the report split `no_route` is the only other status (1 case on the full-source bundle, 2 on the matched cohort), identical in reference and bounded. The per-run tables (scheduled / compared / identical / excluded per run, with each run's replay command) are in [`campaign/tables.md`](campaign/tables.md) and [`campaign/report-analysis.json`](campaign/report-analysis.json).

**What identical means here** (schedule `rules.exactness`): status, error, score, the runner's independent replay of the submitted plan (`evaluation`, i.e. every step's pool, token pair, input funds and amounts), the solver-reported outcome, `candidates_considered` / `candidates_truncated` (not under an active M2), every `search` key outside the §8.1 allowed-to-differ column, the `diagnostics` block without executed-quote work, and the key set (`search` of the bounded run is the reference's plus `bound_pruning`). The work pass additionally records a SHA-256 of the canonical submitted plan; its outcome equals the ordinary run's on all 5,572 records of the 14 work-pass runs (7 tuning, 7 report), 0 differences, 0 meter / wrapper / `search.quotes_executed` mismatches).

**Gate states, re-derived from the records** (`bound_pruning.m2`): under the preset (A1) `G_M2` is closed on every full-source cell that has labels (tuning: `closed:frontier` 93 + `closed:dominance` 3; report: `closed:frontier` 290 + `closed:dominance` 11) and M2 is active on **0** cells; on the matched cohort 20 report cells have the gate `open` (tuning 6) and M2 still acts on none; with dominance `history` and the generous caps (A3) the gate is `closed:dominance` (M2 inactive); with dominance `off` (A4) it is `open` on 96/96 tuning and 301/302 report cells and **M2 is active on 96/96 and 301/302**, still without a difference (the §8.1 population counters are the only ones allowed to differ then). Every bounded cell carries the label `exact` except the 6 + 6 truncated cells above, whose label is `not_exact_budget_binding`; the label and my truncation rule agree on every cell.

## 3. The 14 references against the 0.2.1 baseline records (§11.2, last sentence)

Run once on the report split. For every reference ID the record at HEAD is compared with the 0.2.1 baseline record of the same (algorithm, case) (WHI-1562, `aa5726c…`, `R-roster-full` run `20261002T012141213128Z-e1515ec9`, manifest sha256 `fb62e42d…7a95`, cases sha256 `3a8e0f3a…215c`; `R-roster-sor` run `20261002T012141203437Z-8519bba8`, manifest `a9bbb850…87fb`, cases `d0735a1c…12b3`; both pinned in the schedule and verified before comparing; read-only, never modified). **Tier 1** is the outcome (status, replay, score, error, candidates, solver-reported outcome, quotes counted); **tier 2** is the strategy's own `search` counters; **tier 3** is the research block (`search.r021`, `diagnostics`) with `git_revision` and wall-clock `stages` removed. §11.2's claim is tiers 1 and 2.

| Algorithm | full_source: cells | outcome + `search` identical | tier-3 differs | sor_compatible: cells | outcome + `search` identical | tier-3 differs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `cfmm_dual` | 302 | 302 | 0 | 302 | 302 | 0 |
| `direct` | 302 | 302 | 0 | 302 | 302 | 0 |
| `direct_split` | 302 | 302 | 0 | 302 | 302 | 0 |
| `direct_split_certified` | 302 | 302 | 0 | 302 | 302 | 0 |
| `incremental_graph` | 302 | 302 | 0 | 302 | 302 | 0 |
| `incremental_graph_repair` | 302 | 302 | 0 | 302 | 302 | 302 |
| `metis_history` | 302 | 302 | 0 | 302 | 302 | 302 |
| `metis_inspired` | 302 | 302 | 0 | 302 | 302 | 0 |
| `path_split` | 302 | 302 | 0 | 302 | 302 | 0 |
| `single_path` | 302 | 302 | 0 | 302 | 302 | 0 |
| `uni_sor_adaptive` | 302 | 302 | 0 | 302 | 302 | 0 |
| `uni_sor_cycle_safe` | 302 | 302 | 0 | 302 | 302 | 0 |
| `uni_sor_optimized` | 302 | 302 | 0 | 302 | 302 | 0 |
| `uni_sor_port` | 302 | 302 | 0 | 302 | 302 | 0 |

**8,456 of 8,456 cells have identical outcome and `search`; none differs in a status, a score, a plan or a counter; none is missing.** The 14 references are unchanged by the default-off parameter. The tier-3 differences are only on the matched SOR cohort, for `metis_history` and `incremental_graph_repair` (302 cells each): `search.r021.domain.universe.cohort` (`full_source` in the 0.2.1 baseline, `sor_compatible` now), `search.r021.candidate_domain_hash` and `diagnostics.domain.hash`. That is the WHI-1606 fix (matched cohort carried into the domain identity, merged after the baseline), not a behaviour change of this release.
- `metis_history` (SOR): {'/diagnostics/domain/hash': 302, '/r021/candidate_domain_hash': 302, '/r021/domain/universe/cohort': 302}
- `incremental_graph_repair` (SOR): {'/diagnostics/domain/hash': 302, '/r021/candidate_domain_hash': 302, '/r021/domain/universe/cohort': 302}

## 4. Deterministic work (§11.4, primary evidence)

Units are never added or divided across units; a missing value is counted, never 0; ratios are bounded ÷ reference of the **same** unit. *Reference sum* and *bounded sum* are over the 302 cases of the bundle; the distribution columns are `n / p50 / p90 / max` per case. Quotes, paths and relaxations come from the ordinary run's `search_stats`; CL `swap_steps` and LB `lb_bins_swapped` from the work pass (executed quotes only).

### 4.1 A1, report split (gross objective): reference → bounded

| Pair | bundle | unit | reference sum | reference n/p50/p90/max | bounded sum | bounded n/p50/p90/max | bounded ÷ reference | saved |
| --- | --- | --- | ---: | --- | ---: | --- | ---: | ---: |
| `single_path` | full | quotes_executed | 865,583 | 302/3,134/5,075/6,479 | 166,847 | 302/504/1,094/2,350 | 0.193 | 698,736 |
| `single_path` | full | paths_evaluated | 904,423 | 302/2,990/5,021/6,778 | 114,453 | 302/329/775/1,743 | 0.127 | 789,970 |
| `single_path` | full | cl_swap_steps | 152,084,351 | 302/364,584/1,281,818/1,502,213 | 77,115,730 | 302/104,442/680,713/1,469,595 | 0.507 | 74,968,621 |
| `single_path` | full | lb_bins_swapped | 1,031,145 | 302/1,605/9,887/19,418 | 135,862 | 302/188/1,430/2,743 | 0.132 | 895,283 |
| `single_path` | sor | quotes_executed | 461,193 | 302/1,201/3,079/4,045 | 71,885 | 302/196/500/943 | 0.156 | 389,308 |
| `single_path` | sor | paths_evaluated | 468,005 | 302/1,000/2,935/4,145 | 42,903 | 302/118/315/607 | 0.092 | 425,102 |
| `single_path` | sor | cl_swap_steps | 113,921,750 | 302/172,897/984,596/1,214,558 | 58,005,655 | 302/87,329/558,968/1,184,833 | 0.509 | 55,916,095 |
| `single_path` | sor | lb_bins_swapped | 0 | 302/0/0/0 | 0 | 302/0/0/0 | — | 0 |
| `incremental_graph` | full | quotes_executed | 8,591,807 | 302/26,815/50,781/192,277 | 6,134,446 | 302/19,184/32,786/189,823 | 0.714 | 2,457,361 |
| `incremental_graph` | full | paths_scored | 67,646,824 | 302/231,398/404,948/502,100 | 67,646,824 | 302/231,398/404,948/502,100 | 1.000 | 0 |
| `incremental_graph` | full | cl_swap_steps | 685,258,563 | 302/1,493,025/5,132,567/11,928,188 | 628,513,651 | 302/1,327,366/4,697,189/11,835,356 | 0.917 | 56,744,912 |
| `incremental_graph` | full | lb_bins_swapped | 8,818,897 | 302/15,515/70,929/167,496 | 7,662,515 | 302/13,908/65,842/166,273 | 0.869 | 1,156,382 |
| `incremental_graph` | sor | quotes_executed | 5,870,694 | 302/19,305/35,375/105,302 | 4,393,081 | 302/14,489/24,849/103,806 | 0.748 | 1,477,613 |
| `incremental_graph` | sor | paths_scored | 25,362,955 | 302/54,150/186,784/216,700 | 25,362,955 | 302/54,150/186,784/216,700 | 1.000 | 0 |
| `incremental_graph` | sor | cl_swap_steps | 556,725,689 | 302/1,522,957/4,424,238/7,515,036 | 511,929,910 | 302/1,284,025/4,107,289/7,443,687 | 0.920 | 44,795,779 |
| `incremental_graph` | sor | lb_bins_swapped | 0 | 302/0/0/0 | 0 | 302/0/0/0 | — | 0 |
| `metis_history` | full | quotes_executed | 5,562,023 | 302/20,633/30,914/61,125 | 5,253,049 | 302/19,325/29,899/61,083 | 0.944 | 308,974 |
| `metis_history` | full | label_relaxations | 5,814,060 | 302/19,714/31,200/41,000 | 5,814,060 | 302/19,714/31,200/41,000 | 1.000 | 0 |
| `metis_history` | full | cl_swap_steps | 467,065,161 | 302/1,302,133/3,283,113/5,171,464 | 456,539,073 | 302/1,275,910/3,124,960/5,151,512 | 0.977 | 10,526,088 |
| `metis_history` | full | lb_bins_swapped | 7,908,935 | 302/14,085/66,740/166,338 | 7,727,016 | 302/13,851/66,296/166,246 | 0.977 | 181,919 |
| `metis_history` | sor | quotes_executed | 4,257,614 | 302/16,151/23,444/39,815 | 4,038,684 | 302/14,411/23,291/39,785 | 0.949 | 218,930 |
| `metis_history` | sor | label_relaxations | 3,956,285 | 302/13,723/21,250/29,400 | 3,956,285 | 302/13,723/21,250/29,400 | 1.000 | 0 |
| `metis_history` | sor | cl_swap_steps | 418,063,681 | 302/1,337,583/2,750,700/4,353,731 | 405,738,865 | 302/1,280,058/2,667,676/4,350,922 | 0.971 | 12,324,816 |
| `metis_history` | sor | lb_bins_swapped | 0 | 302/0/0/0 | 0 | 302/0/0/0 | — | 0 |

`paths_scored` (incremental_graph) and `label_relaxations` (metis_history, M1 only) are **equal** by construction (§8.1: identical counters; M1 is population-neutral), so they are shown as such, not as savings; the bound's gain is in `quotes_executed` and, through it, CL steps and LB bins. On the matched SOR cohort the bundle has no Liquidity Book pool (`lb_bins_swapped` is 0 on both sides).

### 4.2 The bound's own cost, beside the savings (never netted)

| Pair | bundle | `pruned_bound` | `bound_evaluations` | `bound_no_bound` | `bound_table_cost` | skipped ÷ `candidates_considered` | cases with a skip | `bound_prepare` seconds (runner prepare events: reference; bounded) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `single_path` | full | 1,055,521 | 1,169,449 | 0 | 0 | 0.567 | 292/302 | [0.00011]; [0.00453] |
| `single_path` | sor | 504,935 | 547,442 | 0 | 0 | 0.738 | 280/302 | [8e-05]; [0.00387] |
| `incremental_graph` | full | 33,350,477 | 41,929,259 | 348,938 | 0 | 0.468 | 280/302 | [0.00013]; [0.00438] |
| `incremental_graph` | sor | 16,997,715 | 20,803,276 | 279,844 | 0 | 0.610 | 268/302 | [9e-05]; [0.00388] |
| `metis_history` | full | 1,723,290 | 2,253,310 | 15,145 | 0 | 0.184 | 276/302 | [0.00033]; [0.00474] |
| `metis_history` | sor | 1,284,522 | 1,583,092 | 15,408 | 0 | 0.199 | 266/302 | [0.00056]; [0.00384] |

- **`bound_prepare`** is one number per run from the runner's prepare events (§14 A1.1): ≈ 4 ms for each bounded strategy against 0.1–0.6 ms for its reference (the bound table is built once per bundle in `prepare`).
- **Overhead-only cases exist** (cases with no skip): single_path 10/302 (full), 22/302 (SOR); incremental_graph 22/302 and 34/302; metis_history 26/302 and 36/302. On those cases the bound only costs `bound_evaluations`.
- **The registered §12.1 trigger** (a low skip fraction with many `bound_evaluations`) is present for `metis_history_bounded` under the preset: 1 723 290 relaxations skipped with 2 253 310 bound evaluations, yet only 5.6 % of the quotes and 2.3 % of the CL steps avoided. For `incremental_graph_bounded` the bound avoids 28.6 % of the quotes and 8.3 % of the CL steps for 41 929 259 evaluations (about 17 evaluations per quote avoided). `single_path_bounded` is the opposite case: 80.7 % of the quotes and 49.3 % of the CL steps for 1 169 449 evaluations. Whether that is worth the evaluations in seconds is §7's question and is **not** answered here.

### 4.3 By case family (report split, full-source bundle, work pass; quotes · CL steps · LB bins, bounded ÷ reference)

Families are cohort, direct-pool vs no-direct (`nod-` case ids), the pool-family mix of the reference plan (`cp` constant-product, `cl`, `lb`), stratum and directed pair (the pair level is in `campaign/report-analysis.json` only as a hash; the full file is in the artifacts). CL-step and LB-bin sums with their `n / p50 / p90 / max` per family, and the same tables for the SOR cohort and for tuning, are in [`campaign/tables.md`](campaign/tables.md) ("Work by case family").

#### `single_path_bounded`

| family | value | cases | quotes ref → bnd | ref p50 / p90 / max | bnd p50 / p90 / max | quotes ÷ | CL steps ÷ | LB bins ÷ |
| --- | --- | ---: | --- | --- | --- | ---: | ---: | ---: |
| cohort | full_source | 302 | 865,583 → 166,847 | 3,134 / 5,075 / 6,479 | 504 / 1,094 / 2,350 | 0.193 | 0.507 | 0.132 |
| direct | has_direct | 278 | 834,409 → 157,731 | 3,262 / 5,087 / 6,479 | 533 / 1,084 / 2,350 | 0.189 | 0.508 | 0.127 |
| direct | no_direct | 24 | 31,174 → 9,116 | 876 / 3,176 / 3,410 | 110 / 1,317 / 1,522 | 0.292 | 0.463 | 0.252 |
| stratum | boundary | 62 | 122,214 → 19,693 | 1,094 / 4,819 / 5,767 | 91 / 1,038 / 1,231 | 0.161 | 0.453 | 0.092 |
| stratum | large | 80 | 234,142 → 40,206 | 3,105 / 4,601 / 5,924 | 446 / 864 / 1,931 | 0.172 | 0.442 | 0.173 |
| stratum | low | 80 | 262,842 → 57,757 | 3,551 / 5,131 / 6,479 | 719 / 1,360 / 2,350 | 0.220 | 0.607 | 0.092 |
| stratum | medium | 80 | 246,385 → 49,191 | 3,312 / 5,081 / 5,956 | 584 / 1,168 / 2,042 | 0.200 | 0.507 | 0.137 |
| mix | cl | 161 | 512,250 → 99,796 | 3,551 / 4,886 / 6,479 | 591 / 1,120 / 2,350 | 0.195 | 0.493 | 0.144 |
| mix | cl+cp | 16 | 13,041 → 2,491 | 354 / 2,891 / 2,994 | 75 / 576 / 602 | 0.191 | 0.592 | 0.537 |
| mix | cl+cp+lb | 6 | 5,328 → 468 | 527 / 1,463 / 1,463 | 89 / 121 / 121 | 0.088 | 0.849 | 0.002 |
| mix | cl+lb | 54 | 221,343 → 45,402 | 4,373 / 5,503 / 5,939 | 782 / 1,255 / 2,014 | 0.205 | 0.562 | 0.096 |
| mix | cp | 24 | 8,704 → 293 | 67 / 536 / 3,273 | 6 / 12 / 91 | 0.034 | 0.126 | 0.001 |
| mix | cp+lb | 5 | 992 → 252 | 97 / 358 / 358 | 23 / 104 / 104 | 0.254 | 0.969 | 0.194 |
| mix | lb | 35 | 103,921 → 18,141 | 2,801 / 5,075 / 5,087 | 469 / 946 / 981 | 0.175 | 0.449 | 0.129 |
| mix | none | 1 | 4 → 4 | 4 / 4 / 4 | 4 / 4 / 4 | 1.000 | — | — |

#### `incremental_graph_bounded`

| family | value | cases | quotes ref → bnd | ref p50 / p90 / max | bnd p50 / p90 / max | quotes ÷ | CL steps ÷ | LB bins ÷ |
| --- | --- | ---: | --- | --- | --- | ---: | ---: | ---: |
| cohort | full_source | 302 | 8,591,807 → 6,134,446 | 26,815 / 50,781 / 192,277 | 19,184 / 32,786 / 189,823 | 0.714 | 0.917 | 0.869 |
| direct | has_direct | 278 | 8,028,007 → 5,765,134 | 27,931 / 51,918 / 192,277 | 19,864 / 33,087 / 189,823 | 0.718 | 0.925 | 0.864 |
| direct | no_direct | 24 | 563,800 → 369,312 | 22,474 / 35,787 / 44,439 | 15,176 / 29,766 / 33,258 | 0.655 | 0.757 | 0.929 |
| stratum | boundary | 62 | 1,884,855 → 1,611,297 | 21,214 / 83,322 / 192,277 | 12,876 / 82,984 / 189,823 | 0.855 | 0.930 | 0.893 |
| stratum | large | 80 | 1,848,272 → 1,301,871 | 20,759 / 40,877 / 61,177 | 16,067 / 26,681 / 39,078 | 0.704 | 0.892 | 0.897 |
| stratum | low | 80 | 2,630,160 → 1,735,535 | 36,298 / 50,633 / 60,729 | 23,588 / 33,471 / 43,127 | 0.660 | 0.937 | 0.815 |
| stratum | medium | 80 | 2,228,520 → 1,485,743 | 29,650 / 48,021 / 62,964 | 19,864 / 29,168 / 37,968 | 0.667 | 0.917 | 0.863 |
| mix | cl | 67 | 1,624,330 → 1,372,206 | 19,112 / 42,594 / 192,163 | 15,056 / 29,493 / 189,709 | 0.845 | 0.860 | 0.963 |
| mix | cl+cp | 18 | 529,767 → 375,954 | 37,682 / 56,988 / 83,321 | 22,958 / 56,412 / 82,983 | 0.710 | 0.980 | 0.542 |
| mix | cl+cp+lb | 74 | 2,683,929 → 1,686,994 | 37,166 / 50,633 / 60,729 | 23,828 / 32,063 / 43,127 | 0.629 | 0.938 | 0.676 |
| mix | cl+lb | 107 | 3,366,235 → 2,328,562 | 29,650 / 52,056 / 62,964 | 20,953 / 35,754 / 39,563 | 0.692 | 0.909 | 0.905 |
| mix | cp | 24 | 211,189 → 202,794 | 105 / 1,981 / 192,277 | 105 / 1,295 / 189,823 | 0.960 | 0.927 | 0.898 |
| mix | cp+lb | 5 | 5,434 → 5,434 | 358 / 2,350 / 2,350 | 358 / 2,350 / 2,350 | 1.000 | 1.000 | 1.000 |
| mix | lb | 6 | 170,919 → 162,498 | 15,206 / 83,322 / 83,322 | 11,472 / 82,984 / 82,984 | 0.951 | 0.861 | 0.912 |
| mix | none | 1 | 4 → 4 | 4 / 4 / 4 | 4 / 4 / 4 | 1.000 | — | — |

#### `metis_history_bounded`

| family | value | cases | quotes ref → bnd | ref p50 / p90 / max | bnd p50 / p90 / max | quotes ÷ | CL steps ÷ | LB bins ÷ |
| --- | --- | ---: | --- | --- | --- | ---: | ---: | ---: |
| cohort | full_source | 302 | 5,562,023 → 5,253,049 | 20,633 / 30,914 / 61,125 | 19,325 / 29,899 / 61,083 | 0.944 | 0.977 | 0.977 |
| direct | has_direct | 278 | 5,078,728 → 4,822,724 | 20,515 / 30,980 / 61,125 | 19,325 / 30,009 / 61,083 | 0.950 | 0.986 | 0.978 |
| direct | no_direct | 24 | 483,295 → 430,325 | 21,968 / 29,333 / 34,451 | 19,307 / 28,797 / 33,411 | 0.890 | 0.874 | 0.965 |
| stratum | boundary | 62 | 903,066 → 872,357 | 16,262 / 34,505 / 61,125 | 13,837 / 34,505 / 61,083 | 0.966 | 0.984 | 0.985 |
| stratum | large | 80 | 1,333,406 → 1,265,247 | 17,123 / 27,005 / 35,170 | 16,496 / 25,831 / 33,490 | 0.949 | 0.974 | 0.982 |
| stratum | low | 80 | 1,793,730 → 1,677,370 | 24,451 / 33,914 / 42,034 | 22,434 / 32,799 / 41,178 | 0.935 | 0.980 | 0.963 |
| stratum | medium | 80 | 1,531,821 → 1,438,075 | 21,565 / 29,083 / 32,953 | 19,918 / 28,627 / 32,072 | 0.939 | 0.975 | 0.977 |
| mix | cl | 45 | 670,017 → 662,983 | 14,064 / 34,505 / 61,011 | 13,924 / 34,505 / 60,969 | 0.990 | 0.992 | 0.999 |
| mix | cl+cp | 22 | 389,963 → 365,420 | 20,306 / 28,306 / 30,058 | 20,013 / 26,623 / 27,031 | 0.937 | 0.980 | 0.983 |
| mix | cl+cp+lb | 74 | 1,787,483 → 1,647,723 | 24,259 / 31,684 / 42,034 | 22,042 / 31,174 / 41,178 | 0.922 | 0.987 | 0.909 |
| mix | cl+lb | 125 | 2,560,210 → 2,423,906 | 21,565 / 28,822 / 38,551 | 19,895 / 27,397 / 37,762 | 0.947 | 0.967 | 0.985 |
| mix | cp | 24 | 77,593 → 77,402 | 105 / 1,685 / 61,125 | 105 / 1,669 / 61,083 | 0.998 | 1.000 | 0.970 |
| mix | cp+lb | 5 | 5,569 → 5,534 | 441 / 2,349 / 2,349 | 418 / 2,349 / 2,349 | 0.994 | 0.999 | 0.983 |
| mix | lb | 6 | 71,184 → 70,077 | 11,982 / 27,037 / 27,037 | 11,439 / 27,032 / 27,032 | 0.984 | 0.969 | 0.982 |
| mix | none | 1 | 4 → 4 | 4 / 4 / 4 | 4 / 4 / 4 | 1.000 | — | — |

### 4.4 `metis_history_bounded` with generous caps: M1 only (A3) versus M1 + M2 (A4)

Report split, full-source, same 302 cases; reference → bounded, sums. M2 acts only when the gate is open (dominance `off`); then it changes the label population (`label_relaxations`) and builds the `U_h` table (`bound_table_cost`, relaxations), reported beside the savings.

| arm | quotes executed | `label_relaxations` | CL steps | LB bins | `pruned_bound` | `bound_evaluations` | `bound_no_bound` | `bound_table_cost` |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| A3 (dominance history; M2 closed) | 31,298,629 → 13,351,500 (0.427) | 237,481,432 → 237,481,432 (1.000) | not measured (no work pass) | not measured (no work pass) | 162,608,813 | 184,124,455 | 758,857 | 0 |
| A4 (dominance off; M2 active 301/302) | 31,298,629 → 11,510,394 (0.368) | 294,577,223 → 218,471,568 (0.742) | 2,745,567,684 → 2,041,895,184 | 17,667,863 → 8,441,868 | 129,111,730 | 182,280,065 | 10,318,821 | 142,774 |

The M1 saving does not change the label population (A3: 237 481 432 relaxations both sides) while removing 57 % of the quotes at these caps; M2 additionally removes 26 % of the relaxations (294 577 223 → 218 471 568) and a further share of quotes, at a `U_h` table cost of 142 774 edge relaxations. **Do not attribute M1 savings to M2**: M2 is inactive in A1 and A3. The preset's `G_M2` is closed on the shipped configuration, so M2 changes nothing there. (A3/A4 figures are with `max_labels_per_signature 10^6` and `max_frontier_labels 10^7`, not the preset's 1 / 1 024.)

### 4.5 A2: the empirical-cost objective

`single_path_bounded` prunes less under a net objective (a low incumbent score is below every `UB ≥ 0`): quotes 865,583 → 220,413 (÷ 0.255) against ÷ 0.193 under gross-only. The two chunk strategies are objective-independent: their A2 counters equal their A1 counters exactly (`incremental_graph` 8 591 807 → 6 134 446, `metis_history` 5 562 023 → 5 253 049).

## 5. Binding budgets (A5, §11.3; not exactness evidence)

Registered rule (schedule `a5.rule`): the nearest-rank p25 and p50 of the reference's own `quotes_executed` (→ `max_quotes`) and `candidates_considered` (→ `max_candidates`) over its tuning `T-A1-<pair>-full` cells; one `budget` key replaced per run; reference and bounded in the same invocation; full-source bundle, both splits. Values (frozen in `46d6e2e`):

| pair | `max_quotes` p25 / p50 | `max_candidates` p25 / p50 |
| --- | --- | --- |
| `single_path` | 911 / 2,960 | 1,714 / 4,840 |
| `incremental_graph` | 15,456 / 26,358 | 90,527 / 229,704 |
| `metis_history` | 12,733 / 21,559 | 24,513 / 30,026 |

Results are **`not_exact_budget_binding`**, never exactness evidence. Quadrants are (reference, bounded) truncated or clean; *higher / equal / lower* compares the bounded score with the reference's on cells where both are `ok`.

| run | budget | cells | ref cut ∧ bnd cut | ref cut ∧ bnd clean | ref clean ∧ bnd cut | neither | bounded higher / equal / lower |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `M-A5-ig-c25-report` | max_candidates p25 | 302 | 0 | 0 | 0 | 302 | 0 / 301 / 0 |
| `M-A5-ig-c25-tuning` | max_candidates p25 | 96 | 0 | 0 | 0 | 96 | 0 / 96 / 0 |
| `M-A5-ig-c50-report` | max_candidates p50 | 302 | 0 | 0 | 0 | 302 | 0 / 301 / 0 |
| `M-A5-ig-c50-tuning` | max_candidates p50 | 96 | 0 | 0 | 0 | 96 | 0 / 96 / 0 |
| `M-A5-ig-q25-report` | max_quotes p25 | 302 | 192 | 21 | 0 | 89 | 15 / 286 / 0 |
| `M-A5-ig-q25-tuning` | max_quotes p25 | 96 | 60 | 12 | 0 | 24 | 10 / 86 / 0 |
| `M-A5-ig-q50-report` | max_quotes p50 | 302 | 64 | 91 | 0 | 147 | 88 / 213 / 0 |
| `M-A5-ig-q50-tuning` | max_quotes p50 | 96 | 19 | 29 | 0 | 48 | 27 / 69 / 0 |
| `M-A5-mh-c25-report` | max_candidates p25 | 302 | 0 | 0 | 0 | 302 | 0 / 301 / 0 |
| `M-A5-mh-c25-tuning` | max_candidates p25 | 96 | 0 | 0 | 0 | 96 | 0 / 96 / 0 |
| `M-A5-mh-c50-report` | max_candidates p50 | 302 | 0 | 0 | 0 | 302 | 0 / 301 / 0 |
| `M-A5-mh-c50-tuning` | max_candidates p50 | 96 | 0 | 0 | 0 | 96 | 0 / 96 / 0 |
| `M-A5-mh-q25-report` | max_quotes p25 | 302 | 206 | 6 | 0 | 90 | 6 / 295 / 0 |
| `M-A5-mh-q25-tuning` | max_quotes p25 | 96 | 68 | 4 | 0 | 24 | 4 / 92 / 0 |
| `M-A5-mh-q50-report` | max_quotes p50 | 302 | 111 | 30 | 0 | 161 | 27 / 274 / 0 |
| `M-A5-mh-q50-tuning` | max_quotes p50 | 96 | 35 | 13 | 0 | 48 | 12 / 84 / 0 |
| `M-A5-sp-c25-report` | max_candidates p25 | 302 | 2 | 234 | 0 | 66 | 37 / 264 / 0 |
| `M-A5-sp-c25-tuning` | max_candidates p25 | 96 | 0 | 66 | 0 | 30 | 9 / 87 / 0 |
| `M-A5-sp-c50-report` | max_candidates p50 | 302 | 0 | 31 | 0 | 271 | 0 / 301 / 0 |
| `M-A5-sp-c50-tuning` | max_candidates p50 | 96 | 0 | 6 | 0 | 90 | 0 / 96 / 0 |
| `M-A5-sp-q25-report` | max_quotes p25 | 302 | 50 | 181 | 0 | 71 | 51 / 250 / 0 |
| `M-A5-sp-q25-tuning` | max_quotes p25 | 96 | 18 | 54 | 0 | 24 | 15 / 81 / 0 |
| `M-A5-sp-q50-report` | max_quotes p50 | 302 | 0 | 159 | 0 | 143 | 22 / 279 / 0 |
| `M-A5-sp-q50-tuning` | max_quotes p50 | 96 | 0 | 49 | 0 | 47 | 6 / 90 / 0 |

- **`bounded truncated ∧ reference not truncated`: 0 in all 24 runs** (target 0 met).
- **The bounded run is never worse than its reference** (`lower` = 0 in all 24 runs) and, where the reference exhausts the budget, it is often better: it reaches a better plan on up to 88 of 302 report cells (`incremental_graph`, `max_quotes` p50) and on 51 (`single_path`, `max_quotes` p25) because pruned candidates cost no quotes.
- **`max_candidates` is not a binding budget for the chunk strategies under the registered rule**: for `incremental_graph` and `metis_history` every cell of the four `max_candidates` runs on both splits is clean on both sides. The cap is counted per chunk (the diagnostics unit is `label_relaxations_per_chunk`) while the rule took percentiles of `candidates_considered`. The `single_path` `max_candidates` budgets do bind (p25 cuts 236 of 302 report references).

## 6. CLI matrix (stage I) and single-request checks

Stage I runs the CLI matrix over all 17 IDs (`config/research_022/schedule.yaml`), alone, from the freeze commit; every command ended `ok` and `analyze --stage I` reconciled with 0 problems.

| Invocation | command | result |
| --- | --- | --- |
| `I-batch17` | `main.py run --strategies all` over the tracked 96-case fixture (17 IDs × 96 cases) | ok |
| `I-batch17.report` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-batch17.replay` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-batch17.order` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-details` | `main.py quote --strategies all --details` (sentinel USDC → USDT 1000, full bundle) | ok |
| `I-quote-details.report` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-details.replay` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-details.order` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-compact` | `main.py quote --strategies all` (compact) | ok |
| `I-quote-compact.report` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-compact.replay` | derived: `report` / the recorded replay command / `order-check` | ok |
| `I-quote-compact.order` | derived: `report` / the recorded replay command / `order-check` | ok |

- **Batch.** `I-batch17` has all 17 IDs in the registered order; the three bounded rows are `ok` on 96/96 cases, their `bound_pruning` blocks present; `direct_split_certified` is `unsupported` on 84 (visible, as registered).
- **Report.** The HTML report of both runs lists the 17 rows with the bounded strategies' counters.
- **Replay and order-check.** Each replay re-ran the recorded command with the same 17 algorithms and the same case count; `order-check` found no difference.
- **Single request** (`I-quote-details`, one solve per strategy; the sentinel is the §11.5 single-request condition; **descriptive, one run**): all three bounded strategies return their references' plan and score (1 000 230 567 / 1 000 237 432 / 1 000 237 432).

| strategy | solve s (quote) | quotes | `pruned_bound` | `bound_evaluations` | prepare s |
| --- | ---: | ---: | ---: | ---: | ---: |
| `single_path` | 2.663 | 4,413 | — | — | 0.00011 |
| `single_path_bounded` | 0.427 | 217 | 5,870 | 6,033 | 0.00438 |
| `incremental_graph` | 14.705 | 24,194 | — | — | 0.00012 |
| `incremental_graph_bounded` | 13.782 | 18,831 | 275,237 | 301,050 | 0.00435 |
| `metis_history` | 11.350 | 20,023 | — | — | 0.00037 |
| `metis_history_bounded` | 11.369 | 19,287 | 11,638 | 15,650 | 0.00458 |

Five further single-request `quote --details` runs are in §7 (stage L).

## 7. Timing (§11.5)

Attempted only after §11.2 had passed (it had, on both splits). The rules are L01's, verbatim, run by `report.latency compare` over `config/research_022/l01-r022.yaml` (protocol L01-R022: `config/latency/l01.yaml` with only `key` and the pinned profile — the six pair strategies over the unchanged full-gross values — replaced; a test enforces it): warm process, 1 warm-up + 5 repeats, orders `fixed` and `reverse`, the A/A noise floor of each experiment, the 24-case L01 matrix (9 tuning + 15 held-out twins) and the sentinel on both cohorts, a cold stage (fresh worker per (strategy, case), prepare + solve charged) with the separate tracemalloc pass. Baseline experiment `L-ref` = the three references (arm `REF`), candidate `L-bnd` = the three bounded strategies (arm `BND`), compared with `--lane heuristic --pair BOUNDED=REFERENCE` (the only lane that pairs different IDs; the lane label carries no quality claim: exactness is §2).

**Gate evidence.**

- Launch gate (5 one-minute load samples 30 s apart, none above 3.0): passed on attempt 10; attempts 1–9 failed (their maxima: 5.80, 4.59, 3.08, 3.49, 3.94, 4.63, 4.36, 3.13, 3.58); attempt 10: 1.48, 2.85, 1.94, 1.85, 1.83. The launch policy waited (schedule `stages.L.launch_window`: re-sample every 300 s, at most 6 h). `caffeinate -i -m -s` held for the whole stage; `pmset -g log` captured: **0 sleep/wake transitions** (`campaign/timing-pmset-sleep-wake.txt`, empty).
- `L-ref` window: 453 samples, **max one-minute load 5.99** (5 over the 5.0 threshold) → `contaminated`; the experiment's own tracker agrees (`contaminated: true`, max 5.52). `L-bnd`: 360 samples, max 5.21 (2 over) → `contaminated` (own tracker max 5.79). The five single-request quotes each ran in a `clean` window (max 3.10).
- The over-threshold samples (5.4 – 6.0, at 16:48 and 17:01 local time and later) fell on the host's usual agent load; a `find / -name …` of another session (38.9 % CPU) was running when I checked at about 17:10. The campaign itself held one lane (one warm worker) and I ran nothing else during the window.

**Verdict: `inconclusive`.** Per §11.5 a window with any sampled one-minute load above `0.5 ×` the logical CPUs yields **no speed claim and no timing disposition**. Timing was not repeated (§11.1 retry rule; host load is never a retry reason). `report.latency` itself reports `inconclusive`: 'host load exceeded the protocol threshold in an experiment'.

**What the records show, descriptively and without a verdict** (held-out decision cases; improvement = 1 − geometric mean of per-case median candidate ÷ baseline; A/A noise = |ln geomean(reverse ÷ fixed)| of the experiment; the comparison's own threshold = max(10 %, 2 × noise). The label column is the comparison's, shown only so that nothing is hidden: it is **not** a claim.)

| cohort / pair | cases | wall improvement | CPU improvement | A/A noise (ref wall; bnd wall) | threshold | comparison label |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix incremental_graph_bounded | 13 | 14.4 % | 14.3 % | 0.257; 0.005 | 51.4 % | no_worthwhile_change |
| full_source/matrix metis_history_bounded | 12 | 18.2 % | 18.2 % | 0.416; 0.002 | 83.1 % | no_worthwhile_change |
| full_source/matrix single_path_bounded | 12 | 60.7 % | 60.7 % | 0.026; 0.006 | 10.0 % | faster |
| sor_compatible/matrix incremental_graph_bounded | 12 | 21.1 % | 20.9 % | 0.432; 0.002 | 86.4 % | no_worthwhile_change |
| sor_compatible/matrix metis_history_bounded | 12 | 19.6 % | 19.6 % | 0.436; 0.003 | 87.2 % | no_worthwhile_change |
| sor_compatible/matrix single_path_bounded | 12 | 69.3 % | 69.2 % | 0.426; 0.005 | 85.3 % | no_worthwhile_change |

The reference experiment's A/A floor is 26 – 44 % between its `fixed` and `reverse` orders for five of its six series (2.6 % for `single_path` on the full-source cohort): on this shared host the order-to-order drift alone is as large as the work the bound removes for `incremental_graph` and `metis_history`. Only `single_path_bounded` on the full-source cohort has an effect (60.7 %) that exceeds its own threshold, and it sits inside a contaminated window.

**Cold charge and memory** (full-source matrix, median over the 24 cases; the cold stage charges `prepare` + solve; descriptive):

| pair | `prepare` s (ref → bnd) | charged s (ref → bnd) | solve peak MiB (ref → bnd) |
| --- | --- | --- | --- |
| `single_path` | 0.00010 → 0.00422 | 1.056 → 0.529 | 32.2 → 5.6 |
| `incremental_graph` | 0.00011 → 0.00427 | 9.409 → 8.005 | 415.5 → 378.5 |
| `metis_history` | 0.00029 → 0.00439 | 5.584 → 5.534 | 375.5 → 364.3 |

The extra preparation of a bounded strategy is ≈ 4 ms per worker (the bound table). It is amortized in the warm process; in the cold single-request condition it is charged to the request and is small against a solve of 0.4 – 14 s. Per sentinel request the bounded solve was shorter by 2.16 s (`single_path`) and 0.73 s (`incremental_graph`) against 4 ms of extra preparation, i.e. a break-even below one request; for `metis_history` it was shorter by 0.03 s, which is inside the run-to-run variation, so no break-even can be stated.

**Single-request `quote --details` checks** (five separate processes, sentinel USDC → USDT 1000, full bundle; each in a clean one-minute-load window but with no A/A floor, so descriptive):

| strategy | solve s, five runs | median | quotes | `pruned_bound` / `bound_evaluations` |
| --- | --- | ---: | ---: | --- |
| `single_path` | 2.576, 2.573, 2.582, 2.586, 2.576 | 2.576 | 4,413 | — |
| `single_path_bounded` | 0.415, 0.413, 0.428, 0.418, 0.412 | 0.415 | 217 | 5,870 / 6,033 |
| `incremental_graph` | 14.113, 14.153, 14.118, 14.171, 14.117 | 14.118 | 24,194 | — |
| `incremental_graph_bounded` | 13.448, 13.389, 13.327, 13.415, 13.325 | 13.389 | 18,831 | 275,237 / 301,050 |
| `metis_history` | 11.017, 11.019, 11.149, 11.027, 11.112 | 11.027 | 20,023 | — |
| `metis_history_bounded` | 11.004, 10.999, 10.965, 10.965, 10.997 | 10.997 | 19,287 | 11,638 / 15,650 |

Read as measurements of single runs on a host that was not quiet for the comparison: `single_path_bounded` solved the sentinel in 0.415 s against 2.576 s (−84 %); `incremental_graph_bounded` in 13.39 s against 14.12 s (−5 %); `metis_history_bounded` in 11.00 s against 11.03 s (no difference). They agree in sign and rough size with the work counters of §4 (quotes −95 %, −22 %, −4 %) and are **not** a timing verdict.

## 8. Disposition (§11.6)

| Strategy | exactness (§11.2) | invalid_plan / algorithm_error where the reference had none | question not evaluable | timing (§11.5) | **disposition** |
| --- | --- | --- | --- | --- | --- |
| `single_path_bounded` | passes: 1,194 cells compared, 0 differences | 0 | no (`pruned_bound` total 1,952,682 on A1) | inconclusive | **keep_experimental** — "work reduction only" |
| `incremental_graph_bounded` | passes: 1,194 cells compared, 0 differences | 0 | no (`pruned_bound` total 66,087,383 on A1) | inconclusive | **keep_experimental** — "work reduction only" |
| `metis_history_bounded` | passes: 1,978 cells compared, 0 differences | 0 | no (`pruned_bound` total 4,037,274 on A1) | inconclusive | **keep_experimental** — "work reduction only" |

`keep_experimental` means exactly what §11.6 says: the identity holds on the frozen corpus and the work reduction is real and reported with its cost; **no default is changed, nothing is adopted, no latency or production claim is made**. `metis_history_bounded` under the shipped preset avoids only 5.6 % of the quotes; its stronger effect (A4) needs `dominance: off`, which is not the preset.

## 9. Limits and open questions

- **Timing is not evaluated.** The only evidence is descriptive (§7). A quiet, long window on a host that is not
  shared is the missing input; the pre-registered gate would have to be re-registered for a repeat.
- **Exactness is shown on the frozen corpus and the tracked fixtures, not proved by the campaign.** The proofs are
  in `pruning-contract.md`; this release adds evidence that nothing differs on 4 366 non-truncated production-grid cells.
- **Budget-truncated cells prove nothing** (12 cells here, all both-sides `max_quotes`). The converse of §8.2
  ("bounded truncated ⇒ reference truncated") held on every cell of A1–A5 (0 violations in 24 A5 runs and in all
  exactness runs) but remains a corner-case argument, not a theorem, for `single_path`.
- **A5's `max_candidates` rule did not bind the chunk strategies** (§1.3 item 5, §5). A follow-up that registers
  the per-chunk unit would need a new report-split execution (a documented amendment before it).
- **SOR cohort and A2–A5**: A2–A5 ran on the full-source bundle only; A1 ran on both cohorts.
- **A4/A3 use generous caps** (10^6 / 10^7); under them 6 boundary cells exhaust the 300 000-quote budget on both sides.
- **The bound's looseness is not measured** (§11.4): the skip fractions are reported, not explained. The registered
  §12.1 follow-up trigger (low skip fraction with many `bound_evaluations`) holds for `metis_history_bounded`
  under the preset (§4.2) and for `incremental_graph_bounded` in CL steps (§4.2); that is a recommendation to
  read §12.1 again, not a defect.
- **Open questions for the owner / orchestrator:** (1) schedule a quiet-host timing window for the REF / BND arms
  (the harness and a fixture test exist; `stages.L` is reusable); (2) decide whether A5 for the chunk strategies
  should be re-registered with a per-chunk `max_candidates`; (3) the §12.1 concave-envelope question for
  `metis_history_bounded`.

## 10. Reproduction

All commands run from a fresh clean clone of the commit named; outputs outside the repository under `/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-artifacts/research-022/whi-1602/` (`inputs/` copies of the corpus bundles, hash-verified; one directory per commit; `analysis-<commit>/out/` the analyses). Compact copies of every analysis are committed under [`campaign/`](campaign/); each invocation's run directory, `manifest.json` sha256, `cases.jsonl` sha256, profile sha256, bundle hash, replay command and environment are in the `invocations` object of the matching analysis file.

```bash
uv run python tools/research_022/pruning_campaign.py check                      # 0 problems
uv run python tools/research_022/pruning_campaign.py inputs --primary <clone> --root <artifacts>/inputs
uv run python tools/research_022/pruning_campaign.py execute --stage T --inputs <artifacts>/inputs --out <artifacts>/93a9db52f128594b0411e07a3fb1e23fbc1ef955/T
uv run python tools/research_022/pruning_campaign.py a5-values --tuning <artifacts>/93a9db52f128594b0411e07a3fb1e23fbc1ef955/T --write   # the freeze commit
uv run python tools/research_022/pruning_campaign.py execute --stage R --inputs … --out <artifacts>/46d6e2e628b2768caab124986c3ce50d3c2f891d/R   # 5 lanes
uv run python tools/research_022/pruning_campaign.py execute --stage M --inputs … --out <artifacts>/46d6e2e628b2768caab124986c3ce50d3c2f891d/M --lanes 3
uv run python tools/research_022/pruning_campaign.py execute --stage I --inputs … --out <artifacts>/46d6e2e628b2768caab124986c3ce50d3c2f891d/I
uv run python tools/research_022/pruning_campaign.py execute --stage L --inputs … --out <artifacts>/46d6e2e628b2768caab124986c3ce50d3c2f891d/L   # waits for a quiet host
uv run python tools/research_022/pruning_campaign.py analyze --stage R --inputs … --out <artifacts>/46d6e2e628b2768caab124986c3ce50d3c2f891d/R --baseline-root <artifacts>
uv run python tools/research_022/pruning_campaign.py tables --analysis T=… R=… M=… --out campaign/tables.md
```

Every ordinary run is a literal `main.py run --strategies profile --profile config/research_022/profiles/<key>.yaml`; the work pass is the same command through `tools/research_022/pruning_work.py` (counting twins around the family quote functions, inside the worker's solve window only; never an exactness or timing record).

| Key | sha256 |
| --- | --- |
| `config/research_022/schedule.yaml` | `becf5ba653257214ad21fe0b7352c812f0cacfebc62b91ab707719de015fad81` |
| `config/research_022/l01-r022.yaml` | `f34475cdff54713f4641e2ac4c32c7cab41eefcdc7ffb79a4ba6cdc149350400` |
| `config/research_022/latency-arms.yaml` | `661ee7977150b9502c5f3dcd8160252c5195f2ff2647e29079f7b6f9c71d334b` |
| `config/research_022/profiles/timing_pairs.yaml` | `86cf3cbf493771fc43cb96654ce9364e210d4766fd15ea8680f2d4140e01d852` |
| `docs/references/research-022/campaign/freeze.json` | `f7a286b3a56aa50c3b5ab3dfaf466f3baf8b89e030a8728b7aafdfafd51f4f86` |
| `docs/references/research-022/campaign/report-analysis.json` | `468c4645e3c71600f59dbf938296999712703cd8eaed81d985b90322ee166f91` |
| `docs/references/research-022/campaign/tuning-analysis.json` | `e7882dd0f6a5ad4908795ca21c09518bf1d60990d50fe5cd78694827dce7bd65` |
| `docs/references/research-022/campaign/binding-budget-analysis.json` | `fe4d15e5d5b1764e51963db5663ec8630a24b43f7ea4205895dc0d199bf89a30` |
| `docs/references/research-022/campaign/cli-analysis.json` | `18363a517de7a7434f3143d6330ec80060edd33c51e09ab30a7e310ebbee481b` |
| `docs/references/research-022/campaign/timing-analysis.json` | `4fa275317801a94ad912404e0cccad06299a9a642a8d962cc6a9dfa265752ed0` |
| `docs/references/research-022/campaign/tables.md` | `e1d50aa94b96bd8e9311017c1e6297c5d83ac3d23440e60bb72070843f1b5747` |
