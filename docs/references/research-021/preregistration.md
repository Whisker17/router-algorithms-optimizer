# WHI-1562 paired campaign: pre-registration (R021-P14, contract R021-C/1)

| Item | Value |
| --- | --- |
| Issue / key | WHI-1562, `R021-P14`, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Contract | [`contract.md`](contract.md) `R021-C/1` (§§3.4, 5, 6, 7, 8 bind this campaign) |
| Fixed release baseline B | `81559ab16376cf46416a69727c3ca0b45e72771a` |
| Campaign base | `dev` `26c0cd0c2008fe22b8ac3273c1360003bbd0c791` (all five identities merged) |
| Schedule (machine-readable) | [`config/research_021/campaign.yaml`](../../../config/research_021/campaign.yaml) |
| Driver and analysis | [`tools/research_021/campaign.py`](../../../tools/research_021/campaign.py), [`tools/research_021/analysis.py`](../../../tools/research_021/analysis.py) |
| Checks | `uv run pytest tests/research_021 -q`, `uv run python tools/research_021/campaign.py check` |
| Status | **Component A**: plumbing, registered tuning-only exploration, nominees and this pre-registration. **Component B (pending)**: the frozen report-split campaign, results and dispositions — nothing below is a report-split result. |

This document is written in two commits. The first (the tuning registration) commits the
driver, the manifest, every campaign profile and the nominee rule **before any tuning
observation**. The second adds the tuning evidence (§6), the nominees (§4.3) and the freeze
record (§8). No report-split solver was executed by component A.

## 1. What is compared, and what is not

- The ordinary roster is `main.py run|quote --strategies all` derived from the canonical base
  [`config/full_gross.yaml`](../../../config/full_gross.yaml) (sha256 `51a8a651…8177`): the six
  base references, `uni_sor_adaptive`, `uni_sor_optimized`, `metis_inspired`, then the five 0.2.1
  identities in contract order — 14 IDs, each once. It is derived from the unchanged base file
  every time, never re-derived from an already expanded effective profile (so no appended-order
  drift); `campaign.py check` asserts the derived order equals the registered `all14`.
- Every ablation / control profile is **generated** from the canonical base by
  `campaign.py profiles --write` (`config/research_021/profiles/*.yaml`) and differs from it only in
  the keys its `generate` spec lists; `check` refuses any byte drift. The WHI-1449 profiles
  `config/metis_challenge/m3.yaml` (= L3) and `m4_off.yaml` (= E4) are used as they are
  (sha256-pinned). No algorithm preset file, pinned profile, historical result or recipe is edited.
- Profile-mode runs record the source profile path (no `profile.yaml` is written); every run's
  replay is its manifest's `replay_command`, never a guessed path.
- Not in scope: default-router adoption, a loss tolerance (`heuristic_default_loss_tolerance:
  null`), a runtime/SLA ceiling, a sixth identity (`uni_sor_lb` is a future `narrow_go`, not
  implemented), new LB capability, any solver change.

## 2. Inputs (frozen, hash-verified)

| Key | Bundle | `bundle_hash` | cases | split / cohort |
| --- | --- | --- | ---: | --- |
| `tuning_full` | `bundle_tuning` | `ee7afa7e…279b` | 96 | tuning / full_source |
| `tuning_sor` | `sor_cohort_tuning` | `b900b866…7ade` | 96 | tuning / sor_compatible |
| `report_full` | `bundle_report` | `85202b20…71c0` | 302 | report / full_source |
| `report_sor` | `sor_cohort_report` | `8213b7b0…e640` | 302 | report / sor_compatible |
| `full` | `bundle` (parent corpus) | `717c21f3…3143` | 398 | quotes (sentinel), L01 parent |

`campaign.py inputs` copies each into the durable root
`…/router-algorithms-optimizer-artifacts/research-021/whi-1562/inputs/` and refuses a copy
whose `manifest.json` hash differs; `execute` re-verifies before every stage. The saved
pre-0.2.1 eight- and nine-strategy quote directories (`saved8`, `saved9`: profile, quote record,
derived bundle, original run) are copied the same way and pinned by sha256. Report results carry
`holdout_exposure: previously_exposed` (WHI-1447, WHI-1503…1510, WHI-1449; R021-C/1 §6.1); the
tuning split is exploration data. Report case IDs/hashes/metadata were inspected only to
register the schedule; the 12 known WHI-1447 `uni_sor_port` token-cycle cases are listed apart
as defect regressions (`known_report_defects`), never tuning or quality data.

## 3. Stages, order and host rules

| Stage | Owner | Split | Lanes | Content |
| --- | --- | --- | ---: | --- |
| **T** | A | tuning | 5 | the registered exploration of R021-C/1 §6.2 (§4) and a real-corpus quote/report/replay/order-check smoke on one tuning request |
| **L** | B, first, alone | L01 matrix | 1 | L02–L05 reassessment through the real L08 arm commands (§5.4) |
| **R** | B, after L | report | 3 | the frozen comparison: both roster runs and every ablation run on the report bundles (+ nominee arms, §4.3) |
| **M** | B, after R | L01 full-source matrix (24 cases) | 1 | the roster with the separate tracemalloc memory pass |
| **I** | B, last, alone | sentinel / saved quotes | 1 | the shared CLI invocation matrix (§5.5) |

`campaign.py schedule --stage X` prints the exact invocation list with each invocation's
algorithm order, case count and expected cell count (T: 26 invocations, 6,830 run cells;
R: 30 / 12,382; M: 2 / 336; I: 87 / 101; L: 16). `execute` samples the 1-minute load every 30 s
(`load.jsonl`), writes a start/end ledger (`ledger.jsonl`, argv, exit code, run directory), keeps
each invocation's stdout/stderr, refuses a dirty tree, a changed input or an existing slot, and on
SIGTERM/SIGINT stops its children and records them `interrupted`.

**Deviation rule (registered, the only re-execution).** An invocation whose process ends
without a complete manifest because of infrastructure (host restart, OOM kill, disk full,
tool/auth failure; evidence recorded) may be re-executed **once** into `<id>.retry1`
(`--retry-infrastructure ID`); the failed attempt stays in the ledger and is reported. A
completed run is never re-executed, whatever its statuses or timings; solver timeouts and
failures are outcomes; host load is never a retry reason. Stage L additionally follows L08's
launch gate (5 load samples 30 s apart, ≤ 3.0 headroom only; a busy host means not launched and
a reported blocker; no waiting loop, no retry; contamination alone never stops a session).

## 4. Registered exploration (stage T, tuning only)

All arms share `full_gross`'s objective (`gross_only`), `search` (3 hops, 4 splits, 5 %),
`graph.chunks` 50, budget (900 s / 300,000 quotes / no candidate cap), measurement (warmup 0,
repeats 1, seed 1447, fixed order) and worker; only the listed keys differ.

### 4.1 Arms

| Registered exploration | Arms (invocation / algorithm) | Class |
| --- | --- | --- |
| `E3`/`E4`, `L3`/`L4`, `S3`/`S4` (`tuning_full`) | E3 = `T-roster-full/incremental_graph`; E4 = `T-e4/metis_inspired` (`m4_off.yaml`); L3 = `T-l3/metis_inspired` (`m3.yaml`); L4 = `T-roster-full/metis_inspired` (`label_hops` 4, pruning on, from the pinned M4 file); S3 = `T-s3/metis_history`; S4 = `T-roster-full/metis_history`; controls S3-off, S4-off (`dominance: "off"`) | same-depth pairs `same_domain` per chunk |
| `same_grid_allocation` (both tuning bundles) | `direct_split` vs `direct_split_certified` in the same run | `same_domain` |
| `repair_off_on` (both) | `T-repair-off-*/incremental_graph_repair` (`repair: false`) vs the roster's preset | `same_domain` |
| `matched_sor_cycle_safe` (`tuning_sor`; full-source rows reported too) | `uni_sor_port` vs `uni_sor_cycle_safe`, same run | `same_domain` |
| `cfmm_cpmm_vs_cl` (both) | `T-cfmm-cpmm-*/cfmm_dual` (historical `cfmm_dual/1`) vs the roster's current `cfmm_dual/2` CP+CL; plus `cfmm_dual` vs `path_split` / `incremental_graph` | `expanded_protocol`; controls `incomparable_domain` (matched) / `expanded_protocol` (full source) |
| `max_splits_scan` 1/2/4/8 | `alloc_ms{1,2,8}` = `direct_split`, `path_split`, `direct_split_certified` (both bundles); `sor_ms{1,2,8}` = `uni_sor_port`, `uni_sor_adaptive`, `uni_sor_optimized`, `uni_sor_cycle_safe` (`tuning_sor`); value 4 = the roster runs | per value |
| unsupported scope | `net_empirical` = `direct_split_certified`, `cfmm_dual` under `full.yaml`'s empirical-cost objective (both) | every row `unsupported` |

Not scanned, by the §3.3 rows: graph solvers (`incremental_graph`, `metis_*`,
`incremental_graph_repair`: `max_splits` governs only their embedded fallback) and `cfmm_dual`
(`governs: none`). The historical CFMM v2 tuning observations (9/96 converged, 21 fallbacks,
worst ≈ −4557 bp; huge early "gains" from tiny bad-baseline denominators) are WHI-1559 evidence,
not results of this campaign.

### 4.2 Registered questions and metrics (both splits; stage T is exploration only)

For every arm: unconditional status counts over the full schedule (with unsupported reasons,
timeout limits, last-valid candidates); §5.2 work units per unit (never divided across units;
missing is never 0); descriptive timing (solve, evaluation, start-up incl. prepare, prepare) with
its host window; bound kinds and the runner's certificate-check codes. Paired quality: candidate
vs baseline over the identical schedule, common-OK only, exact relative bps (N/A at a zero
baseline), higher/equal/lower, worst losses and largest gains by case, per family (directed pair)
and per stratum beside the pooled distribution, every status transition. Identity/P1/equal-value
gates never pass a budget-bound cell (listed apart).

| Identity | Gates (a failure ⇒ `reject`) | Primary question (not a win requirement) |
| --- | --- | --- |
| `metis_history` | no `invalid_plan`/`algorithm_error`; runner certificate checks clean; S_H(off) ≡ E_H (status, score, evaluation) on every untruncated cell, H = 3, 4 | S_H vs L_H and vs E_H paired quality; depth decomposition `Q_S4/Q_E3 = (Q_E4/Q_E3)(Q_S4/Q_E4)`; capped chunks and frontier drops |
| `direct_split_certified` | equal value to `direct_split` on every supported, untruncated common-OK cell (P1); no `certified` claim fails the runner check; unsupported on every non-all-CPMM direct pair and every net objective | certificate kinds / terminations on supported cells (single-pool only in this corpus: a disclosed limitation, not a gate) |
| `incremental_graph_repair` | repair-off ≡ `incremental_graph` (status, score, evaluation, quotes counted) on untruncated cells; repair-on never below repair-off without a budget cut (P1); 0 unexplained consistency failures | on-vs-off paired quality, acceptances, stops, timeouts, same-unit quote and path ratios |
| `uni_sor_cycle_safe` | no variant `invalid_plan` (Theorem S); identity with `uni_sor_port` on the comparable identical cohort (zero rejections, completed selector and replay, reference not budget-cut); ledger = meter | rejection-cohort paired quality (losses possible, K5); CS-2 `withheld_by_cs2`, reference-only last valid candidates, cooperative replay cut vs hard kill; the 12 known defects apart |
| `cfmm_dual` | no `algorithm_error`/`invalid_plan`; never `certified`; an estimate only from the initial full-network converged solve without fallback; `unsupported (protocol_ceiling)` only by the stage rule; unsupported under net objectives | CP+CL vs CPMM-only (`expanded_protocol`); vs `path_split`/`incremental_graph` side by side; recovery failures, fallbacks, estimate coverage, recovered/estimate |

**Disposition mapping (B, R021-C/1 §8).** `reject` if a gate fails or a registered hypothesis
is refuted; `inconclusive` if every gate passes but a primary question cannot be evaluated (zero
eligible cells, missing/incomplete records, coverage); otherwise `keep_experimental`. A
functional method need not win; timing is never a disposition criterion here (§5.3).

### 4.3 max_splits nominee rule (registered before any tuning observation)

For each `kind: nominee` comparison, over every scheduled (case, algorithm) of its algorithms
on its tuning bundles:

- failures(v) = cells whose status is `timeout`, `invalid_plan`, `algorithm_error`,
  `model_error`, `incomplete_snapshot`, `cancelled` or missing;
- shortfall(v) = cells that are `ok` at some scanned value but at v are not `ok`, or are `ok`
  with a gross below that cell's best gross over all scanned values.

**Nominee = the smallest v with shortfall(v) = 0 and failures(v) = the minimum over the scan;
if no value qualifies, the canonical value 4.** No loss tolerance, no timing criterion; ties go to
the smaller value. Applied (`apply: true`) to `same_grid_allocation` (`direct_split` +
`direct_split_certified`) and `matched_sor_cycle_safe` (`uni_sor_port` + `uni_sor_cycle_safe`);
reported but not applied for `path_split` and the two pinned L08 recipes. A nominee ≠ 4 adds one
report-stage invocation per bundle of that pair at the nominee (the roster rows remain at 4); a
nominee = 4 adds nothing. Nominees never change a preset file.

*Stage-T nominees:* **pending** (filled in the second commit, §6).

## 5. The frozen report campaign (component B)

### 5.1 Stage R

`R-roster-full`, `R-roster-sor` (the 14-ID roster on each report bundle), `R-e4`, `R-l3`,
`R-s3`, `R-s3off`, `R-s4off` (report_full), `R-repair-off-*`, `R-cfmm-cpmm-*`, `R-net-*` (both),
each with a report regeneration; the `R-net-*` runs also replay and order-check. The registered
comparisons are the stage-T comparisons with every `T-*` invocation mapped to its `R-*` twin
(`report_mirror`), except the nominee rule; the two cycle-safe comparisons exclude the 12 known
defects from quality and list their statuses apart.

### 5.2 Measurement and statistics

Per-case exact integer gross; exact relative bps; per-family and per-stratum splits; the
decomposition works on per-case log ratios over one common-OK case set, never on added bps with
different denominators. No pooled p-value; no single-request percentile; batch distributions carry
their `n`.

### 5.3 Timing

L01 labels and boundaries. A window with any sampled 1-minute load > 0.5 × logical CPUs (5.0 on the
10-core host), or without a sample, is `inconclusive`. A batch run has one sample per case and no
A/A noise floor, so its timing is **descriptive only**; speed verdicts exist only in stage L via
`report.latency compare` (L01 rules verbatim). Complete-request timing is the quote CLI of stage I
(one process per invocation); a controlled arm's complete response is its cold per-algorithm charge
(L08 §2). No SLA, no total-campaign ceiling; long runs are execution control, not acceptance.

### 5.4 Stage L (L02–L05 reassessment)

Arms R, E1, E2, E3, E4 of the unchanged [`config/latency/l08.yaml`](../../../config/latency/l08.yaml)
(`e7add86a…eaba`), each `benchmark.latency run --protocol config/latency/l01.yaml --arms … --arm X`
followed by its own same-source `benchmark.latency sufficient` run, then `report.latency compare
--lane exact --sufficient …` for L02 (R→E1), L03 (E1→E2), L04 (E2→E3), L05 (E3→E4) and the
cumulative R→E3, R→E4. Verdicts and dispositions are L01/L08's verbatim; the prior L08 session and
the WHI-1449 campaign are untouched and not merged with it.

### 5.5 Stage I (shared CLI invocation matrix)

On the sentinel (USDC → USDT 1000, parent corpus): `quote --strategies all` with `--details` and
compact, `base`, `optimized`, `profile`, and `--strategies profile --details` for every campaign
profile and the pinned L3/E4 files; each quote gets `report`, its manifest `replay_command` and
`order-check` against it. The net profile's quote is a registered refusal (exit 1, empirical cost
is not supported by quote). The saved eight- and nine-strategy profiles are quoted literally with
`--strategies profile` and their recorded replay command is executed twice (relocated only into
the durable copy and a fresh results directory) with an order-check between the two; each quote
must show exactly one solve attempt per selected algorithm, one worker per algorithm in order and
no memory pass.

## 6. Tuning evidence (stage T)

**Pending** — filled in the second commit from the stage-T analysis.

## 7. Remaining work (component B) and reproduction

**Pending** — see the second commit.

## 8. Freeze record

**Pending** — see the second commit.
