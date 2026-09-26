# L06 — opt-in SOR candidate-shortlist variant `uni_sor_fast` (WHI-1508)

Status: **integrated as an opt-in, separately named experimental identity, for correctness
and coverage metadata, under the owner's development-first amendment of 2026-09-26.**

- `uni_sor_fast` is a heuristic. Its search is an approximation of `uni_sor_port`'s, and
  every result says so (`search.sor_fast.search_approximation: true`).
- It is registered as a seventh ID and selected only by a profile that names it. No
  checked-in profile does. The six reference IDs, their factories and search policies,
  `uni_sor_port`, its contract, source pin and goldens, the evaluator and every default
  profile are unchanged.
- The **performance and quality trade-off decision, and any adoption, belong to
  WHI-1510.** No loss tolerance is assumed (`heuristic_default_loss_tolerance: null`). The
  timings below are single-shot in-process diagnostics on a loaded shared host. They are
  not an L01 run, a distribution or a speedup claim.

Research key L06 ([latency-optimization-research.md](latency-optimization-research.md)
§4, heuristic lane). Base `317106e` (0.1.2 `dev`, WHI-1507 merged). The L01 protocol v1 is
unchanged.

## 1. What the variant does

Code: [`routing/algorithms/uni_sor_fast.py`](../../routing/algorithms/uni_sor_fast.py). It
calls the port's translated core unchanged: `compute_family_routes`,
`amount_distribution`, `build_route_quotes`, `get_best_swap_route` and `integer_fill`. It
reuses the port's `prepare` for the cohort, universe and parameters.

What stays the reference's:

- the frozen V2/V3 cohort in A-1 order;
- route enumeration, so `unsupported` / `no_route` from enumeration are the reference's
  own;
- the grid, the A-2 null rules, the zero A-3 gas scores and the SOR combination and final
  order (B-S*, B-F*);
- the D-1 integer fill and the D-3 replay.

Only the set of routes that enters the quote table changes:

1. **Probe.** Every enumerated route is quoted at each probe percent. Probe percents are
   grid percents, and 100 is mandatory.
2. **Rank.** Per probe percent, routes are ranked by `quoteAdjustedForGas` descending.
   Ties go to the reference quote-list order (B-Q1). A route with no valid probe entry is
   unranked.
3. **Shortlist.** Take the union of the `routes_per_probe` best routes at every probe.
   Add the `direct_routes` best one-hop routes, ranked by their best rank over the probes.
   Because 100 is always probed, the best full-input single route (the simplest
   incumbent) is always searched, and the B-S3 baseline keeps it.
4. **Search.** The unchanged core runs on the shortlisted routes, in quote-list order, over
   the full grid.
5. **Fallback.** A deterministic full-table fallback runs when no route was ranked, or when
   the shortlist has no complete selection. Its quotes are charged in the same solve. After
   a fallback the result is exactly the reference's. A shortlist failure is never reported
   as `no_route`: `no_route` and `incomplete_snapshot` are only stated over the full cohort
   table.

### Upstream lead, and why ranking uses quotes

The lead is the pinned `get-candidate-pools.ts` (`getV3CandidatePools` /
`getV2CandidatePools`: `topNDirectSwaps`, `topNTokenInOut`, `topNSecondHop` and
base-token TVL budgets). It was read from the pinned npm build in the local npm cache.
The tarball's sha256 `647fb12b…24e4` equals the inventory's, so it is source-equivalent to
pin `04c7c0b4`.

It is a lead, not translated and not reproduced:

- The frozen bundle has no pool TVL in a common unit.
- TVL or full-input ranking can miss profitable small splits (§3).

So ranking uses exact probe quotes at several amounts. This is not upstream
candidate-selection parity, and it is not hosted-API equivalence.

### Settings

Profile section `shortlist` (`benchmark/profile.py`, via the new
`AlgorithmFactory.shortlist_params`):

- `probe_percents`: a non-empty list of distinct `search.percent_step` multiples up to 100,
  including 100;
- `routes_per_probe`: at least 1;
- `direct_routes`: at least 0.

There are no defaults. Unknown keys, booleans, out-of-grid, duplicate or missing values are
refused by the loader and re-checked by `prepare`. A profile without the section resolves
byte-identically to before. This was checked over every `config/*.yaml` against `317106e`.

### Exact controls versus this heuristic

- The quote path is the ordinary default, with L02, L03 and L04 all off.
- Those exact controls reduce work inside the same search scope. This variant changes the
  scope.
- Comparisons must keep the two apart. A post-L04 control is **not** the default
  reference. WHI-1510 decides whether and how they compose. By L04 §6, a shortlist gain
  should be weighed against the post-L04 exact cost too.

## 2. Accounting and metadata

Every probe, shortlist and fallback entry is an entry of the reference's own table, quoted
through the same per-solve memo. **Quotes executed therefore never exceed `uni_sor_port`'s
on the same case.** They are equal after a fallback. This is checked in tests and in every
diagnostic row (`quotes_exceed_reference` = 0).

Everything is inside the solve window: ranking, sorting, the fallback and the in-solve
replay. `prepare` is the port's `prepare` plus a settings check, so no work moves into
preparation. The runner's independent final evaluation is outside the solve, as for
every algorithm.

Per-case `search` metadata:

- `sor_fast`: reference, upstream pin, contract, gas provider, quote path, B-S12 params
  and `search_approximation: true`;
- `search_scope`: the **planned** scope of the last table search started — `shortlist`
  (restricted), `full_cohort` (the shortlist kept every route, identical to the reference)
  or `full_cohort_fallback`;
- `search_completed`: whether that planned table was quoted in full and combined. A
  budget-interrupted table is `false`: planned scope is not completed coverage;
- `shortlist`:
  - settings, eligible routes by family, eligible pools;
  - probe entries, null and incomplete;
  - ranked routes, routes contributed per probe, direct routes retained;
  - `shortlisted` `{routes, pools, route_ids}`: the **initial** shortlist chosen by the
    probes, before any table search;
  - `searched_routes` / `skipped_routes` by family, `searched_pools` / `skipped_pools`,
    `searched_route_ids`: the coverage of the last **completed** table search. After a
    completed fallback this is the whole eligible set (nothing skipped). After an
    interrupted fallback it stays the completed shortlist table, and it is `null` if no
    table search completed;
  - fallback `{triggered, reason, completed}`: `completed` is `null` without a fallback and
    `false` if the budget ran out inside it;
  - quotes by phase (`probe`, `shortlist_table`, `fallback_table`, `validation`), which sum
    to `quotes_executed`.

Plus every key `uni_sor_port` records: coverage mode, selection, allocation, D-1 residual,
cached and evaluated gross, requote delta and `truncated_by`.

`candidates_truncated` is the number of skipped routes of a shortlist search (0 after a
fallback). The existing report shows it next to the declared limits.

## 3. Correctness and adversarial evidence

[`tests/routing/test_uni_sor_fast.py`](../../tests/routing/test_uni_sor_fast.py): 51 tests,
plus 3 skipped non-profile YAML files. Expected CPMM values come from the Solidity
formula, and plans are replayed by a fresh evaluator.

| Test | Shows |
| --- | --- |
| `…seventh_identity_leaves_the_six_references_untouched`, `…no_checked_in_profile_selects_the_variant` (×11) | Registry order and identity of the six. Empty `shortlist_params` on each. Every run profile loads without the variant and without a `shortlist` key |
| `…profile_hands_the_validated_settings…`, `…rejects_missing_or_invalid…` (×16), `…needs_the_declared_percent_step`, `test_prepare_rejects…` (×7) | Settings reach only the declaring factory, as immutable tuples. Every refusal |
| `…keeps_every_route_is_the_reference` (×2) | Fixture bundles, where every route is kept: status, plan, evaluation, score, selection, allocation, residual and entry counts all equal `uni_sor_port`. Restricted-input equality is claimed **only** where the restriction is vacuous |
| `…thin_pool_is_worst_at_full_input…`, `…full_input_ranking_misses_the_thin_split_and_loses_output`, `…small_probe_catches_the_thin_split` | Adversarial low-TVL case. A thin but better-priced pool is last at 100 % and best at 5 %, and the reference uses it. Probing only at 100 % with 3 routes skips it. The result is valid and has **strictly lower output** (an asserted loss). A 5 % probe catches it |
| `…unusual_intermediate_route…` | No base-token list: a two-hop route through an arbitrary token ranks first on quotes. `direct_routes` keeps the direct incumbent |
| `…ties_keep_the_reference_quote_list_order` | Deterministic tie-break and repeat identity |
| `…no_ranked_route_falls_back…`, `…restricted_failure_is_never_no_route…`, `…enumeration_statuses…` | CL pools with a bounded collected band. At 100 % every route needs uncollected state, but 50/50 fits: the fallback is charged and its status, plan and quotes equal the reference's, and its completed coverage is every eligible route and pool while `shortlisted` stays empty. `incomplete_snapshot` and dust `no_route` also come only after the full table. LB-only cases are `unsupported` and disconnected cases `no_route`, as in the reference |
| `…interrupted_fallback_is_planned_scope_not_completed_coverage` | The shortlist (`cl1` only) completes without a selection. The completed fallback covers both routes and equals the reference. With a quote budget that runs out inside the fallback: `timeout`, `search_completed: false`, fallback `completed: false`, and the searched coverage stays the completed shortlist table (`cl1`), never the planned full table |
| `…quote_budget_is_a_timeout…`, `…accounting_matches_the_meter…`, `…isolated_runner_records…` | Budgets during the probes or the table give `timeout`, never `no_route`. Phase quotes sum to the worker meter. Quotes stay at or below the reference's. Spawned workers pickle the factory and settings, and the runner's independent replay agrees |

Unchanged reference behaviour is covered by the existing SOR goldens and parity, profile,
registry, runner and quote tests, which still pass (§6).

## 4. Pre-registered bounded quality diagnostics

The pre-registration is [`config/latency/l06.yaml`](../../config/latency/l06.yaml),
committed at `9cfb76a` **before** any L01-matrix or sentinel solve of `uni_sor_fast`.
Earlier it had only run on checked-in test fixtures. The tool is
[`tools/latency/l06_sor_shortlist.py`](../../tools/latency/l06_sor_shortlist.py). It
refuses an L01 protocol, profile, experiment or derived bundle that differs.

The registration fixes:

- **Inputs:** the L01 v1 matrix/splits, the derived bundles of L01 experiment
  `20260926T090434484129Z-d5061563`, and `daily_gross.yaml` (2 hops, 4 splits, 5 % grid,
  50,000 quotes).
- **Sweep:** 18 settings — probe sets {100}, {5, 100}, {5, 25, 100}, × `routes_per_probe`
  {2, 4, 8} × `direct_routes` {0, 4}.
- **Nomination, from the tuning split only.** Lexicographic, with no tolerance: fewest
  status regressions, then smallest max regret, then summed regret, then quotes, then sweep
  order.
- **A fixed aggressive setting**, `p100-k2-d0`, chosen a priori.
- **Held-out:** run once, for those two settings only, after the tuning results were
  committed. The held-out command refuses a changed code tree or registration, so held-out
  cannot change either choice.

**Regret** is `(ref − cand) × 10,000 / ref` on the gross score, against `uni_sor_port`
re-solved on the same bundle, cohort, objective and budget.

- It is N/A, never 0, when the reference is not ok (or not positive) or the candidate is
  not ok. A candidate that is not ok counts as a status regression.
- Negative regret is not clamped.
- Tuning, sentinel and held-out are never pooled.

**Checks on every row:**

- the reference re-solved at the current source equals the L01 baseline record (`bbda6e2`)
  on every semantic field;
- the runner's independent re-evaluation of every returned plan is ok and its gross output
  equals the solver's in-solve replay;
- candidate quotes ≤ reference quotes.

All three held on all 360 tuning and 60 held-out rows.

### 4.1 Tuning split (9 cases × 2 cohorts = 18 per setting; measured at `9cfb76a`)

Every record is ok → ok. There were no status regressions and no fallbacks.

| Setting | Loss cases | Max regret bps | Σ regret bps | Quotes vs reference |
| --- | ---: | ---: | ---: | ---: |
| p100-k2-d0 (aggressive) | 6 | 27.010 | 110.923 | 0.074 |
| p100-k8-d4 | 6 | 25.234 | 64.426 | 0.142 |
| p5.100-k2-d0 | 6 | 10.172 | 34.761 | 0.135 |
| p5.100-k4-d4 | 4 | 10.172 | 20.445 | 0.173 |
| **p5.100-k8-d0 (nominated)** | **0** | **0** | **0** | **0.223** |
| p5.100-k8-d4 | 0 | 0 | 0 | 0.225 |
| p5.25.100-k2-d0 | 4 | 2.907 | 5.915 | 0.186 |
| p5.25.100-k8-d0 | 0 | 0 | 0 | 0.274 |

The other ten settings are in
[`latency-l06/tuning-9cfb76a.json`](latency-l06/tuning-9cfb76a.json). Across the whole
sweep, more routes per probe and a small probe cut losses, while the 100 %-only probe keeps
losing up to 25–27 bps.

Sentinel (USDC → USDT 1000, both cohorts, reported apart), for the two settings carried
to held-out:

- `p100-k2-d0` loses 0.020 bps on each cohort;
- `p5.100-k8-d0` loses 0. It searches 13 of 210 routes, with 684 vs 3,734 quotes.

The sentinel is one case, not a distribution.

### 4.2 Held-out split (15 cases × 2 cohorts = 30; run once at `05a640a`)

[`latency-l06/held_out-05a640a.json`](latency-l06/held_out-05a640a.json).

| Setting (role) | ok→ok | no_route→no_route | Status regressions | Loss / equal / gain | Max regret bps | Σ regret bps | Fallbacks | Quotes vs reference |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| p5.100-k8-d0 (nomination) | 26 | 4 (N/A) | 0 | 4 / 22 / 0 | 1.125 | 4.440 | 4 | 0.218 |
| p100-k2-d0 (aggressive) | 26 | 4 (N/A) | 0 | 8 / 18 / 0 | 35.048 | 207.682 | 4 | 0.078 |

**Retained losses.**

- **Nomination.** The zero-loss tuning result did **not** carry to held-out. USDC → USDT
  `emp-09bc4e-201eba-low-1` loses 1.125 bps (241 raw) and the liquidity-edge boundary
  case `bnd-09bc4e-201eba-liq_at` loses 1.095 bps (238 raw), in each cohort. The reference
  adds a fourth, 5 % route (`0x9cae…>0xd145…`) that was ranked outside the top 8 at both
  probes, and it puts 55 % instead of 60 % on the first route.
- **Aggressive setting.** It loses up to 35.0 bps. The worst cases are the same two, plus
  USDC → USDT `large-1` (1.76 bps) and WMNT → WETH `emp-78c1b0-deadde-large-1`
  (32.5 bps).

**Failures and fallbacks.**

- Both `no_route` failures are retained in each cohort: `bnd-1bdd88-78c1b0-dust` and
  `…-round_below`. In the latter only an LB pool routes the case; the single V2/V3 route
  SOR enumerates has no valid quote, so the reference says `no_route`, as in L01.
- Each triggers `no_ranked_route` → full-table fallback, with quotes equal to the
  reference's (1 and 11), and states `no_route` only over the full table.
- **Metadata caveat for these fallback rows.** The committed raw evidence was measured at
  `9cfb76a` / `05a640a`, before the coverage fix of §7. It records the *initial* (empty)
  shortlist as `searched_routes` / `searched_pools` (0) for these 8 rows (4 per setting),
  although the completed fallback searched the whole eligible set (1 route each).
  Status, regret, scope, fallback reason and quotes in those rows are unaffected. No
  tuning row had a fallback. The files are kept as measured, not rewritten.
- The single-route CPMM case (`emp-1bdd88-78c1b0-low-2`) keeps every route
  (`full_cohort`) and is identical to the reference.

**Cohorts.** Full-source and matched-cohort rows are identical, because SOR's candidates
are the V2/V3 cohort in both. LB coverage differences stay visible through the
reference's `coverage_mode` and `excluded_pools`, not through this variant.

**Coverage limits.** The matrix contains no `unsupported`, `timeout`, `invalid_plan` or
`incomplete_snapshot` SOR outcome. Those paths are covered by the unit tests only.
Fifteen held-out cases (13 ok), several of the same pair, do not support a tail estimate.

### 4.3 Time (diagnostic only)

These are single-shot in-process solve windows, with one prepare per bundle and no warmup.

Load exceeded the L01 5.0 bound in both phases (max 1-minute load 5.6 in tuning, 12.4 in
held-out), so every timing is contaminated. The median per-case candidate/reference
solve-wall ratio was:

- tuning: 0.154 nominated, 0.068 aggressive;
- held-out: 0.235 nominated, 0.073 aggressive;
- sentinel, nominated: 0.64 s vs 3.76 s.

These are **not** speedups. Not measured here:

- cold start-up / prepare / complete-response charges and memory;
- repeats and order balancing;
- a comparison with the post-L04 exact controls.

All of these are WHI-1510's.

## 5. Disposition

| Piece | Disposition |
| --- | --- |
| Opt-in `uni_sor_fast`, exact plans, complete scope/fallback/phase metadata, charged fallback | **Implemented and integrated opt-in.** Not a default. Not an adoption |
| Quality/latency trade-off | **Pending WHI-1510.** Pre-registered bounded evidence above: every setting that reduces quotes loses output on some held-out or tuning case. The nomination loses ≤ 1.13 bps on 2 of 13 held-out ok cases per cohort at 22 % of the reference quotes. No loss tolerance is chosen here |
| Pool-level TVL/base-token budgets (upstream lead) | Not implemented: no frozen TVL. Multi-amount quote ranking instead |
| Coarse-to-fine grid / anytime refinement | Not here: WHI-1509 (L07) |

For WHI-1509 and WHI-1510:

- **WHI-1509** can layer its grid refinement on `uni_sor_fast`'s searched routes, or on
  its own identity. It must keep the probe/fallback charging and the "no `no_route` from a
  restricted scope" rule.
- **WHI-1510** needs:
  - an L01-style protocol version that lists `uni_sor_fast`. The L01 v1 driver pins the
    six-algorithm profile, so `report.latency compare --lane heuristic --pair
    uni_sor_fast=uni_sor_port` cannot run on v1 experiments;
  - cold/warm/complete-response charges and memory;
  - a quiet host;
  - comparison against both the default reference and the adopted exact controls;
  - the owner's explicit decision on any loss.

## 6. Reproduction

Read-only inputs: the L01 experiment directory
`data/latency-012/whi-1503/20260926T090434484129Z-d5061563` in the primary clone. Raw
outputs go to the gitignored `data/latency-l06/`. The committed JSON is reduced by the
transcript [`latency-l06/compact.py.txt`](latency-l06/compact.py.txt), which drops fields
and copies numbers; `raw_sha256` names the raw file.

```bash
uv run pytest tests/routing/test_uni_sor_fast.py -q
uv run python tools/latency/l06_sor_shortlist.py tuning \
  --experiment <L01 dir> --out data/latency-l06/tuning-<sha>.json
uv run python tools/latency/l06_sor_shortlist.py held_out --experiment <L01 dir> \
  --nomination data/latency-l06/tuning-<sha>.json --out data/latency-l06/held_out-<sha>.json
```

Both phases took about 2 minutes on this host.

## 7. Coverage-metadata fix after the evidence run

The orchestrator's review of PR #38 found that after a fallback, `searched_*` described the
initial shortlist rather than the completed full-table search. The fix (after `f46403c`):

- keeps the initial shortlist as `shortlist.shortlisted`;
- makes `searched_*` / `skipped_*` / `searched_route_ids` the coverage of the last completed
  table search;
- adds `search_completed` and fallback `completed`, so an interrupted fallback is never read
  as completed full-cohort coverage.

It is metadata only. On 963 old-vs-new solves (the fixture bundles, the CL fallback and
budget cases and the thin-pool case, with three settings and three budgets), status, plan,
evaluation, score, selection, allocation, quotes, scope and truncation were identical. The
check was a one-off script, not committed. No ranking, probe, selection or tuning parameter
changed, and nothing was re-swept. The pre-registered evidence keeps its measured sources
with the caveat in §4.2. WHI-1510 uses the final schema.

