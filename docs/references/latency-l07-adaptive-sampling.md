# L07 — opt-in adaptive percentage sampling in `uni_sor_fast` (WHI-1509)

Status: **implemented as an explicit opt-in of the experimental `uni_sor_fast` identity,
for correctness, coverage and quality metadata, under the owner's development-first
amendment of 2026-09-26.**

- Adaptive sampling runs only when a profile declares the new `sampling` section. Without
  it `uni_sor_fast` is the L06 shortlist variant, unchanged (§4). No checked-in profile
  selects `uni_sor_fast` or declares `sampling`.
- It is a heuristic on top of a heuristic. Every sampled result says so
  (`search.sor_fast.sampling_approximation: true`, plus `search.sampling`).
- The six reference IDs, `uni_sor_port`, its contract, source pin and goldens, the grids
  and parity of `direct_split` / `path_split` / `uni_sor_port`, the evaluator, result
  schema and every default profile are unchanged.
- **The performance and quality trade-off decision, and any adoption, belong to
  WHI-1510.** No loss tolerance is assumed (`heuristic_default_loss_tolerance: null`).
  The timings below are single-shot in-process diagnostics on a loaded shared host. They
  are not an L01 run, a distribution or a speedup claim.

Research key L07 ([latency-optimization-research.md](latency-optimization-research.md)
§4, heuristic lane). Base `0d8ed87` (0.1.2 `dev`, WHI-1508 merged). The L01 protocol v1 and
the L06 registration are unchanged.

## 1. What the sampling does

Code: [`routing/algorithms/uni_sor_fast.py`](../../routing/algorithms/uni_sor_fast.py).
The port's translated core is called unchanged (`amount_distribution`,
`build_route_quotes`, `get_best_swap_route`, `integer_fill`). Only the set of
`(route, percent)` entries in the quote table changes. The L06 probe, shortlist and
fallback logic is unchanged and still decides the route scope.

Each table search (the shortlist, or the L06 full-cohort fallback) becomes:

1. **Coarse.** Every searched route is quoted at the grid percents that are multiples of
   `coarse_step` (100 is always one). The unchanged SOR core combines this partial table.
   It gets the full `percents` list, and a percent group with no entries is skipped, as
   B-S4/B-S6 already allow.
2. **Incumbent.** First, a simpler full-input incumbent is **seeded and validated before
   any refinement**: the sampled 100 % entries in B-S2 order, each as the core's
   single-route selection, until one replays valid (`search.sampling.seed`). A combined
   selection is not an incumbent by itself; its D-1 fill can fail where the full-input
   route does not (§7). Each candidate (seed or combined selection) is D-1 integer-filled, then built with
   `split_path_plan`, which refuses shared physical pools and disconnected legs. It is
   then replayed by the evaluator, and that replay is charged as `validation`. Only then,
   and only if it is valid and scores strictly better, is it published through
   `report_candidate`. A combined selection also replaces an equal-scoring seed. The funding check requires the integer allocation to sum to the
   input with every leg positive, and the last leg takes the explicit remainder. A
   selection that fails any check is counted in `rejected_incumbents` and is never
   published.
3. **Refine.** Take the percents of the incumbent and of the latest selection. Add the
   neighbours `p ± j·percent_step` and the freed shares `j·percent_step`, for
   `j ≤ refine_radius`, for every searched route. The core then re-combines the grown
   table, rebuilt in canonical B-Q1 order so ties resolve as in the reference. Stop when:
   - no new percent is proposed: `converged`, a local fixed point over the sampled table
     and **not** a global or full-grid optimum; or
   - before a round, the solve's counted quotes (all phases) have reached
     `soft_max_quotes` **and a valid incumbent is in hand**: `soft_limit`. Status stays
     `ok` with that incumbent, and it is labelled `truncated_by: soft_max_quotes`, so the
     report treats it as budget-bound. Without a valid incumbent the soft cap never stops
     the search.
4. **Grid completion** (`grid_completion.reason`). When the sampled table has no complete
   selection (`no_selection`), or refinement converged while every selection was rejected
   (`no_valid_incumbent`), the rest of the full grid is quoted, charged in the same phase,
   before anything is concluded.
   - No selection over the full grid: the L06 fallback and the "no `no_route` /
     `incomplete_snapshot` from a restricted scope" rules apply unchanged.
   - Selections but no valid incumbent over the full grid:
     `no_valid_incumbent_full_grid`, then `invalid_plan` for the last rejected plan. That
     plan is never published, and this is the same status the reference gives.
5. **Hard limits** stay truthful. `max_quotes` gives `timeout` with **no plan**; the
   runner would re-evaluate a returned plan as `ok`. The last valid incumbent is kept only
   as labelled metadata (`search.sampling.incumbent`) and through the candidate sink, so
   a worker kill yields the runner's separately labelled `last_valid_candidate`.

When `coarse_step == percent_step`, the first table is the full grid and the result equals
the L06 result, quotes included (tested).

### Settings

The profile section `sampling` (`benchmark/profile.py`) is declared through the new
`AlgorithmFactory.sampling_params` field. That field defaults to empty, so it is
positional- and keyword-compatible, and no reference algorithm declares it. It is an
**optional all-or-none group**:

- `coarse_step`: a multiple of `search.percent_step` that divides 100;
- `refine_radius`: at least 1;
- `soft_max_quotes`: at least 1, or an explicit `null`, meaning no soft cap is declared.

There are no defaults. Partial, unknown, boolean, out-of-range or non-dividing values are
refused by the loader and checked again by `prepare`. Without the section nothing is
handed over, and `PreparedUniSorFast.sampling` is `None`. `uni_sor_fast`'s `PROVENANCE` is
unchanged, so an L06-only profile resolves exactly as before.

### Exact controls

The quote path stays the ordinary default: L02, L03 and L04 are off, as in L06. Those
exact controls reduce work inside a fixed scope, while sampling changes the scope.
WHI-1510 decides whether and how they compose.

## 2. Accounting and metadata

Everything is inside the solve window: probes, sampled rounds, grid completion, fallback,
every incumbent validation and the ranking and combination CPU. Nothing moves into
`prepare`. The runner's independent final evaluation runs outside the solve, as for every
algorithm.

The quote phases are `probe`, `shortlist_table`, `fallback_table` and `validation`. In the
sampling path `validation` is the sum of all in-solve incumbent replays. The phases add up
to `quotes_executed` and to the worker meter (tested).

**Unlike L06 there is no general "never more quotes than the reference" bound.** Sampled
entries are a subset of the reference table, but several incumbents may be replayed. The
evidence therefore counts `quotes_exceed_reference` per row; it was 0 on every row (§3).

`search.sampling` describes the last table search started:

- `research_key`, `issue`, `approximation` and `settings`;
- `scope` and `completed`;
- entry coverage: `grid_percents`, `grid_entries`, `coarse_percents`,
  `sampled_percents`, `sampled_entries` and `skipped_entries`. These count **completed
  rounds only**, so an interrupted round's percents never count as sampled;
- `rounds`: each round's `kind` (`coarse` / `refine` / `grid_completion`), added percents
  and entries, table quotes, selection, and incumbent outcome (`improved` / `unchanged` /
  `not_better` / `rejected`);
- `stop_reason`: `converged`, `soft_limit`, `no_selection_full_grid`,
  `no_valid_incumbent_full_grid` or `hard_limit`;
- `soft_limit {max_quotes, reached, quotes_at_stop}`,
  `grid_completion {triggered, completed, reason}` and `seed {route_id, outcome}` (or
  `null` when no 100 % entry was sampled);
- `validations`, `rejected_incumbents` and `rejection_errors`;
- `incumbent`: round, `source` (`full_input_seed` / `sor_selection`), selection,
  allocation, evaluated gross, score and `validated: true`, or `null`.

The L06 keys keep their meaning. `search_scope` is the planned route scope and
`search_completed` says whether it completed. `searched_*` is the route coverage of the
last completed table. `candidates_truncated` counts skipped routes.

## 3. Correctness and adversarial evidence

[`tests/routing/test_uni_sor_fast.py`](../../tests/routing/test_uni_sor_fast.py) has 34 new
L07 tests: 31 original plus 3 from the acceptance correction in §7. The file totals 85
passing and 3 skipped. Expected CPMM values come from the
Solidity formula, and plans are replayed by a fresh evaluator.

| Test | Shows |
| --- | --- |
| `…sampling_settings_are_opt_in…`, `…rejects_partial_or_invalid_sampling` (×11 loader, ×5 `prepare`), `…no_checked_in_profile…` | Six references declare no `sampling_params`. Absent section: no `sampling` in resolved output or params, and `prepared.sampling is None`. Present section: handed only to `uni_sor_fast`, with explicit `null` allowed. Every refusal |
| `…refinement_proposes_neighbours…` | Neighbour/freed-share proposal stays inside `[step, 100]` |
| `…coarse_step_equal_to_the_grid_is_the_l06_result` (×2 fixture bundles) | Vacuous sampling equals L06 in status, plan, evaluation, score, selection, allocation, residual, quotes, entries and shortlist metadata |
| `…local_refinement_can_miss_a_narrow_optimum…` | **An asserted sampling loss.** Four CPMM routes (one two-hop) whose SOR optimum is `d1@90 + d0@5 + ax>xb@5`. From a 50 % coarse grid, radius 1 walks to `80/10/10`, where no one-step neighbour improves SOR's selection. It stops `converged`, and 90 is never sampled. The plan is valid and strictly worse (26.7 bps). Every published plan was valid and strictly improving. Radius 2 reaches the reference |
| `…small_profitable_share_is_reached…` | The thin pool is only profitable as a 10 % share, which the 50 % coarse grid never samples. Freed-share proposals reach it, and the result equals the reference |
| `…soft_cap_returns_the_valid_incumbent…` | Soft cap 20: `ok`, `truncated_by: soft_max_quotes`, `stop_reason: soft_limit`, `quotes_at_stop` 20, plan valid and equal to the last published one. Cap 1 still returns the coarse incumbent: the soft cap never precedes a first selection |
| `…hard_quote_limit_is_a_timeout…` | `max_quotes` 30 inside refinement: `timeout`, no plan and no score, `search_completed: false`, `stop_reason: hard_limit`. Sampled percents are only the completed rounds'. The sink's last plan replays `ok` and equals `sampling.incumbent`. Phases sum to 30 |
| `…no_incumbent_from_the_sampled_table_completes_the_grid…` | CL band case where the coarse grid {100} is all uncollected state. The grid completion finds the 50/50 plan after the L06 fallback, which equals the reference, and a soft cap of 1 does not interfere. The too-large case is `incomplete_snapshot` only after the full grid, with no incumbent |
| `…tiny_nondivisible_inputs_and_ties…` (×4) | Twin pools tie, and repeat runs are identical. Dust (1 wei) floors every entry: `no_route` only after the full grid and fallback. Inputs 13 and 21 fund fully. Input 100,001 splits `50000 / 50001`: the remainder goes on the last route |
| `…sampled_solves_are_valid_pool_disjoint_and_charged…` | Corpus fixture: meter equals phases; every `ok` plan replays independently and is pool-disjoint; entry coverage adds up; a non-`ok` status equals L06's |
| `…isolated_runner_records_the_sampling_metadata` | Spawned workers pickle the settings. The manifest's resolved profile has `sampling`, and the runner's independent replay agrees |

The unchanged-L06 claim is also backed by a one-off old-vs-new comparison. The base
`0d8ed87` `uni_sor_fast.py` and this branch's, run without `sampling`, gave identical full
`SolveResult` objects (all `search` keys) and sink publications on **945 solves**. Those
solves covered fixture, adversarial, CL-fallback and budget cases, 3 shortlist settings
and 3 budgets ([transcript](latency-l07/l06-identity-check.py.txt)). The `resolved()`
output of every checked-in run profile (11) is byte-identical to the base.

## 4. Pre-registered bounded quality diagnostics

The pre-registration is [`config/latency/l07.yaml`](../../config/latency/l07.yaml),
committed in `cd27439` **before** any L01-matrix or sentinel solve of the sampling path.
Before that commit it had only run on checked-in fixtures. The tool is
[`tools/latency/l07_adaptive_sampling.py`](../../tools/latency/l07_adaptive_sampling.py).
Inputs, profile (`daily_gross.yaml`: 2 hops, 4 splits, 5 % grid, 50,000 quotes) and L01
experiment `20260926T090434484129Z-d5061563` are identical to L06's.

**Prior visibility, disclosed.** The L06 tuning and held-out rows were visible when L07
was registered, including the held-out losses of the L06 nomination. The L06 shortlist
used here is that committed nomination (`p5.100-k8-d0`), fixed a priori and not re-tuned.
No L07 knob or rule was chosen from those losses.

**Arms.**

- `reference`: `uni_sor_port`.
- `l06_only`: the L06 nomination, without `sampling`.
- `combined`: the same shortlist with a sampling setting.
- `adaptive_only`: sampling over a vacuous shortlist. Probes are the coarse grid, so
  probing costs no extra quote, and 10⁶ routes per probe are kept. A route with no valid
  entry at any coarse percent is unranked and not searched, so this arm is adaptive-only
  *where applicable*. Its rows show `shortlist` scope for exactly that reason.

**Sweep** (12 combined, 6 adaptive-only): `coarse_step` {50, 25, 20} ×
`refine_radius` {1, 2} × `soft_max_quotes` {null, 250}. The adaptive-only arm uses `null`
only. The 250 cap is a fixed a-priori exercise value.

**Rules.**

- **Nomination** per sampling arm uses the tuning split only, with no tolerance, ranked
  in order by: fewest status regressions, smallest max regret, smallest summed regret,
  fewest quotes, sweep order.
- **Fixed aggressive setting:** `combined-c50-r1-s250`.
- **Held-out** runs once, after the tuning evidence was committed (`6c934c8`), for the
  reference, `l06_only`, both nominations and the aggressive setting. It refuses a changed
  code tree or registration.
- **Regret** is `(ref − cand) × 10,000 / ref` on gross, as in L06, and is N/A (never 0)
  when either side is not `ok`. Combined and adaptive-only rows also carry a paired
  regret against `l06_only`. For `combined` that isolates the sampling's own effect.
- **Checks on every row:**
  - the reference equals the L01 baseline record;
  - the runner's independent re-evaluation of every returned plan equals the solver's
    replay;
  - candidate quotes are compared with the reference's.

  All held on all 380 tuning+sentinel rows and 120 held-out rows: 0 mismatches, 0
  reference drift, 0 rows with candidate quotes above the reference, 0 rejected
  incumbents.

### 4.1 Tuning split (9 cases × 2 cohorts = 18 per setting; measured at `cd27439`)

[`latency-l07/tuning-cd27439.json`](latency-l07/tuning-cd27439.json). Every row went
ok → ok with no status regressions and no fallbacks.

| Setting | Loss cases | Max regret bps | Σ regret bps | Loss vs l06_only | Quotes vs reference | Stops |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| l06_only | 0 | 0 | 0 | — | 0.223 | — |
| combined-c50-r1-snone | 2 | 0.019 | 0.037 | 2 | 0.145 | 18 converged |
| **combined-c25-r1-snone (nominated)** | **0** | **0** | **0** | **0** | **0.153** | 18 converged |
| combined-c25-r2-snone | 0 | 0 | 0 | 0 | 0.168 | 18 converged |
| combined-c20-r2-snone | 0 | 0 | 0 | 0 | 0.175 | 18 converged |
| combined-c20-r1-s250 | 6 | 14.036 | 46.343 | 6 | 0.135 | 10 converged, 8 soft |
| combined-c50-r1-s250 (aggressive) | 6 | 27.010 | 100.329 | 6 | 0.119 | 10 converged, 8 soft |
| adaptive_only-c50-r1-snone | 2 | 0.019 | 0.037 | — | 0.380 | 18 converged |
| **adaptive_only-c25-r1-snone (nominated)** | **0** | **0** | **0** | — | **0.471** | 18 converged |
| adaptive_only-c20-r2-snone | 0 | 0 | 0 | — | 0.603 | 18 converged |

The other settings are in the JSON. Both radii tie at 25 % and 50 %, so the rule's quote
tie-break chose radius 1. Every soft-capped setting loses output, up to 27 bps. The
uncapped settings lose only 0.019 bps, on 2 rows, and only at the 50 % grid (both radii)
and at the 20 % grid with radius 1.

**Sentinel** (USDC → USDT 1000, both cohorts, reported apart; one case, not a
distribution):

| Setting | Regret bps | Quotes / reference | Sampled / grid entries |
| --- | ---: | ---: | ---: |
| `l06_only` | 0 | 684 / 3,734 | — |
| `combined-c25-r1` | 0 | 548 | 156 / 260 |
| `adaptive_only-c25-r1` | 0 | 1,998 | 1,656 / 2,760 |
| aggressive | 0.032 | 395 | 26 / 260, `soft_limit` |

### 4.2 Held-out split (15 cases × 2 cohorts = 30; run once at `6c934c8`)

[`latency-l07/held_out-6c934c8.json`](latency-l07/held_out-6c934c8.json). Full-source
and matched-cohort rows are identical, as in L06: SOR's candidates are the V2/V3 cohort
in both.

| Setting (role) | ok→ok | no_route→no_route | Status regr. | Loss / equal / gain vs ref | Max regret bps | Σ regret bps | vs l06_only loss / gain | Quotes vs ref | Stops |
| --- | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| l06_only | 26 | 4 (N/A) | 0 | 4 / 22 / 0 | 1.125 | 4.440 | — | 0.218 | — |
| combined-c25-r1-snone (nomination) | 26 | 4 | 0 | 4 / 22 / 0 | 1.125 | 4.440 | 0 / 0 | 0.152 | 26 converged, 4 no-selection |
| adaptive_only-c25-r1-snone (nomination) | 26 | 4 | 0 | 0 / 26 / 0 | 0 | 0 | 0 / 4 | 0.454 | 26 converged, 4 no-selection |
| combined-c50-r1-s250 (aggressive) | 26 | 4 | 0 | 8 / 18 / 0 | 35.744 | 206.181 | 8 / 0 | 0.114 | 14 converged, 12 soft, 4 no-selection |

**Retained losses and what they separate.**

- **Combined nomination.** Its ok plans equal `l06_only`'s on all 26 ok rows: the
  sampling added no loss on held-out. It inherits both L06 shortlist losses:
  `emp-09bc4e-201eba-low-1` at 1.125 bps and `bnd-09bc4e-201eba-liq_at` at 1.095 bps,
  per cohort. It does so at 15.2 % instead of 21.8 % of the reference quotes.
- **Adaptive-only nomination.** It equals the reference on all 26 ok rows, at 45.4 % of
  the reference quotes. Its 4 gains vs `l06_only` are exactly the L06 shortlist losses:
  the loss is the shortlist's, not the sampling's.
- **Aggressive setting.** It loses on 8 of 26 ok rows, up to **35.7 bps**. The soft cap
  of 250 stopped 12 rows. The worst cases:
  - `emp-09bc4e-201eba-low-1`, 35.7 bps: the incumbent was 50/50 on two pools versus the
    reference's four-route split;
  - `bnd-…-liq_at`, 35.1 bps;
  - `emp-78c1b0-deadde-large-1`, 30.4 bps;
  - `…201eba-large-1`, 1.8 bps: a single 100 % route versus the reference's
    80/15/5.

  These are soft-limit losses of valid incumbents, labelled `truncated_by:
  soft_max_quotes`.
- **Failures.** The 4 `no_route` rows (`bnd-1bdd88-78c1b0-dust` and `…-round_below`, each
  cohort) take the charged path: grid completion, then `no_selection_full_grid`, then the
  L06 full-table fallback. Their quotes equal the reference's, and `no_route` is stated
  only over the full table.

**Coverage limits.** The matrix has no `unsupported`, `timeout`, `invalid_plan` or
`incomplete_snapshot` SOR outcome, no hard-limit stop and no rejected incumbent. Those
paths are covered by unit tests only. Fifteen held-out cases (13 ok), several of the same
pair, do not support a tail estimate. The zero sampling loss on held-out is not a
guarantee: the narrow-optimum test shows a real miss, and the tuning split shows
0.019 bps misses at other coarse grids.

### 4.3 Time (diagnostic only)

These are single-shot in-process solve windows, with one `prepare` per bundle and setting
(at most 0.014 s) and no warmup. The 1-minute load reached 6.5 during tuning and 5.6
during held-out, above the L01 bound of 5.0, so **every timing is contaminated.**

Median per-case candidate/reference solve-wall ratio:

| Setting | Tuning | Held-out |
| --- | ---: | ---: |
| l06_only | 0.153 | 0.245 |
| combined nomination | 0.124 | 0.128 |
| adaptive-only nomination | 0.296 | 0.367 |
| aggressive | 0.108 | 0.112 |

On the sentinel, the combined nomination took 0.51 s against 0.66 s for `l06_only` and
3.72 s for the reference.

In-solve incumbent validation is inside those windows. Across all held-out rows of the
combined nomination it charged 30 quotes in 42 validations. The runner's independent
evaluation, outside the solve, had a median of 0.1 ms and a maximum of 0.9 ms per plan.

These are **not** speedups. Not measured here:

- cold process start and complete single-request response;
- peak memory;
- repeats and order balancing;
- a comparison with the post-L04 exact controls.

All of these are WHI-1510's.

## 5. Disposition

| Piece | Disposition |
| --- | --- |
| Opt-in `sampling` section, validated anytime incumbent, grid completion, soft/hard-limit and entry-coverage metadata | **Implemented, opt-in.** Not a default. Not an adoption |
| Quality/latency trade-off | **Pending WHI-1510.** Pre-registered bounded evidence above. The combined nomination added no held-out loss beyond L06's shortlist losses, at about 0.70× L06's quotes. Soft-capped and 50 %-grid settings do lose output. A synthetic narrow optimum shows radius-1 misses are real. No tolerance is chosen here |
| Composition with L02–L04 exact controls; cold/warm/memory/complete-response charges; quiet host; L01-style protocol listing `uni_sor_fast` | WHI-1510 |

WHI-1510 needs:

- an L01-style protocol version that lists `uni_sor_fast` with and without `sampling`;
- the paired end-to-end, cold and warm, and memory measurements on a quiet host;
- the owner's explicit decision on any loss, including whether soft caps are acceptable
  at all.

## 6. Reproduction

Read-only input: the L01 experiment directory
`data/latency-012/whi-1503/20260926T090434484129Z-d5061563` in the primary clone. Raw
outputs go to the gitignored `data/latency-l07/`. The committed JSON is reduced by
[`latency-l07/compact.py.txt`](latency-l07/compact.py.txt); `raw_sha256` names the raw
file.

```bash
uv run pytest tests/routing/test_uni_sor_fast.py -q
uv run python tools/latency/l07_adaptive_sampling.py tuning \
  --experiment <L01 dir> --out data/latency-l07/tuning-<sha>.json
uv run python tools/latency/l07_adaptive_sampling.py held_out --experiment <L01 dir> \
  --nomination data/latency-l07/tuning-<sha>.json --out data/latency-l07/held_out-<sha>.json
```

On this host, tuning took about 2.6 minutes and held-out about 0.8 minutes.

## 7. Acceptance correction after the evidence run: validated incumbent before any stop

The orchestrator's pre-merge verification of PR #39 at `fb4dbe9` found a real boundary
case with two pools:

- a fixture CL pool with liquidity 1,580, whose collected band fits an input of 50 but not
  51;
- a CPMM pool with reserves 101/101.

The case is input 101, 1 hop, 2 splits, 5 % grid, probes {50, 100}, 2 routes per probe,
2 direct routes, coarse step 50, radius 1, soft cap 1.

What went wrong at `fb4dbe9`:

- The coarse winner, CP@50 + CL@50, fills 50 + 51. CL@51 is `incomplete_snapshot`, so the
  plan was rejected.
- The soft cap then stopped with **no** incumbent, and the solve returned `invalid_plan`.
  The feasible full-input CP route (100 % quote ok) was ignored, 36 entries stayed
  unsampled, and nothing was published.
- The combinatorial selection had been treated as if it were the validated incumbent.

The fix is in `uni_sor_fast.py` only (§1 steps 2–4):

- **Seed.** The full-input incumbent is validated before refinement.
- **Soft cap.** It stops only with a valid incumbent.
- **No valid incumbent.** This is handled separately: grid completion with reason
  `no_valid_incumbent`, then `no_valid_incumbent_full_grid` and `invalid_plan` for the
  unpublished rejected plan.

Unchanged:

- ranking, probes, shortlist, fallback, the refinement proposal, every setting and
  tuning parameter;
- the L06 path when `sampling` is absent: the 945-solve identity check still reports
  "identical 945";
- the reference grids.

New tests:

| Test | Shows |
| --- | --- |
| `…boundary_fill_failure_keeps_the_validated_full_input_incumbent_before_a_soft_stop` | The real 101 case. The reference and L06 return `invalid_plan`. Sampling rejects the coarse CP@50 + CL@50 plan, seeds CP@100, and soft-stops with that valid incumbent: `ok`, `truncated_by: soft_max_quotes`. The single publication equals the returned plan. The meter equals the phases. Uncapped, it `converged` to the same plan |
| `…boundary_hard_cut_after_the_seed_is_a_timeout…` | `max_quotes` 4 cuts the combined plan's replay: `timeout`, no plan. Only the valid seed was published, and `sampling.incumbent` matches it |
| `…rejected_selection_without_any_valid_incumbent_is_never_a_soft_stop` | Two CL pools: no valid 100 % entry, so no seed, and the only split fails its fill. The soft cap does not stop. The grid completes with reason `no_valid_incumbent`, nothing is published, and the result is `invalid_plan` with the reference's error. No soft-limit label |

**Focused comparison with `fb4dbe9`.** 2,508 sampling solves: the fixture bundles plus the
thin, narrow, CL and boundary cases × 3 shortlists × 4 sampling settings × 2 budgets.

- Status, plan, score, selection and `quotes_executed` were identical everywhere except
  the boundary case. It changed `invalid_plan → ok` in 8 solves: the fix itself.
- Validation quotes were unchanged in all 2,508. Seed replays are memo hits, because the
  100 % entry was quoted at exactly the input on the same legs.
- `validations` rose by 1 in 96 solves, where the coarse selection is not the seed route.

**What this means for the committed evidence.** The tuning and held-out evidence
(`cd27439` / `6c934c8`) stays bound to its measured source and is not re-run.

- Every one of its rows had 0 rejected incumbents. Under the gross objective a valid
  combined selection always scores at least the seed: its head legs are the quoted
  entries and its last leg only gains the remainder. It therefore replaces the seed or
  equals it.
- So status, plan, score, quotes, soft stops and regret in those rows are unaffected by
  construction. Only these counters and keys would differ at the corrected source:
  - `validations`: +1 per table search whose coarse selection is not the seed route;
  - one extra sink publication in the same cases;
  - the new `seed`, `incumbent.source` and `grid_completion.reason` keys.
- Validation quotes would not change.
- Under a net objective with gas costs, the seed can outscore a multi-route selection. This
  is a behaviour difference from `fb4dbe9` that the gross-only evidence does not exercise.
  WHI-1510's measurements use the corrected source.
- A new status transition becomes possible: a case where the reference's full-grid
  selection fails replay but a full-input route is feasible now returns `ok`. It is
  reported as a status difference, and regret is N/A because the reference is not `ok`.
