# 0.2.1 strategy domains and experiment contract (R021-C/1)

| Item | Value |
| --- | --- |
| Contract | `R021-C/1` (this file is authoritative; [`contract-v1.json`](contract-v1.json) is its machine-readable vocabulary and must agree) |
| Publication key | `R021-P01`, WHI-1547, Release 0.2.1 (`ed16e106-fa3e-4b8a-b022-e7208eb8ef41`) |
| Repository baseline | `81559ab16376cf46416a69727c3ca0b45e72771a` (`dev`, fixed review baseline B of the release) |
| Sources and corrections | [`sources.md`](sources.md): pins, missing assets, code differences, D1–D5 dispositions |
| Example records | [`fixtures/examples.json`](fixtures/examples.json): positives and minimal negative mutations |
| New reconstructions | [`fixtures/reconstructions.json`](fixtures/reconstructions.json) (R1–R10) |
| Executable check | `uv run pytest tests/docs/test_research_021_contract.py -q` |

This contract freezes how the five 0.2.1 strategies are named, scoped, measured and judged.
It implements no solver, configuration runtime or campaign, and promises no performance
gain. The six base references, the two optimized recipes, `metis_inspired`, their frozen
profiles and every historical result are unchanged.

## 1. Scope and evidence classes

**Carried invariants.** Every new solver is separately named, experimental, runs offline in
the existing isolated Python worker and returns an ordinary `RoutePlan` that the common
evaluator (`routing/evaluator.py`) replays from the original snapshot with exact integer
protocol math. The v1 feasible set is unchanged: no economic token cycle (cyclic arbitrage),
no borrowed or external capital, no implicit dust disposal (full fill), and an unknown net
cost is never zero. There is no fixed total campaign runtime ceiling (DESIGN §1.4.8).

**Evidence classes** (never merged):

1. *Repository evidence*: committed code, tests and frozen artifacts at the baseline.
2. *External proposal*: the owner-supplied report archived in
   [`sources/research-report.md`](sources/research-report.md). It is mathematical and design
   evidence, not a repository audit. Its author's 200-case counts, `results.json` and
   `verify_research.py` are absent and are **not** our verification.
3. *New reconstructions* by WHI-1547 (R1–R10): independently recomputed, labeled as new.
4. *Future measured evidence*: only WHI-1562's frozen campaign under §6–§8.

**Delegation.** This contract fixes the shared seam. Each research ticket publishes its
algorithm-specific contract *inside these schemas* before its implementation is
dispatched (§11). A research ticket may narrow a domain, protocol ceiling or objective set;
widening one, renaming an identity or changing a vocabulary needs a new contract version (§13).

## 2. The five strategy identities (frozen)

| Order | ID | Research → implementation | Mechanism (one line) | Reference control | Protocol ceiling | Objectives |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `metis_history` | WHI-1549 → WHI-1550 | per-chunk label search with history/admission-aware dominance | `metis_inspired`, `incremental_graph` | all admitted | as `metis_inspired` |
| 2 | `direct_split_certified` | WHI-1551 → WHI-1552 | integer direct-split branch and bound with a validated same-domain bound | `direct_split` (same grid) | constant product | `gross_only` |
| 3 | `incremental_graph_repair` | WHI-1553 → WHI-1554 | complete incumbent plus bounded checkpoint/suffix repair | `incremental_graph` (repair off) | all admitted | as `incremental_graph` |
| 4 | `uni_sor_cycle_safe` | WHI-1555 → WHI-1556 | SOR selection that rejects plan-token-cycle combinations | `uni_sor_port` (same candidates/grid) | constant product + concentrated (V2/V3) | as `uni_sor_port` |
| 5 | `cfmm_dual` | WHI-1557 → WHI-1558 (+ WHI-1559 CL) | CFMM dual decomposition with integer plan recovery | `path_split`, `incremental_graph` (matched cohort) | constant product, then validated concentrated | `gross_only` |

Rules:

- All five are in the existing `custom` group ("Experimental and other strategies"). None
  is a base reference, an SOR optimization, a default router or Jupiter Metis.
- Under the ordinary `--strategies all`, an **implemented** identity is appended once, after
  `metis_inspired`, in the order above. The complete roster is 9 existing + 5 new = 14. An
  unimplemented identity is absent: no placeholder factory, no empty row. Inclusion is not
  adoption.
- `--strategies profile` and saved effective profiles (including the pre-0.2.1 eight- and
  nine-strategy ones) replay literally; they never gain a new identity.
- `direct_split_certified` joins `all` in its `repository_grid` mode only; a `raw_integer`
  configuration is a separately identified domain (§3.2) and, like every stress profile (§7),
  runs only by explicit profile selection.
- Protocols or objectives outside an identity's ceiling produce visible `unsupported` rows
  (§7.3), never silent omissions, and never an approximation of another protocol.

## 3. Comparison domains

### 3.1 The domain record (`r021.domain/1`)

A result that claims anything beyond "valid plan, evaluated output" names its feasible set.
The record's fields (all required):

| Field | Meaning | Repository values |
| --- | --- | --- |
| `schema` | `"r021.domain/1"` | — |
| `universe` | bundle hash, cohort (`full_source`, `sor_compatible`, fixture) and candidate pool set | e.g. `bundle_report`, `sor_cohort_report` |
| `protocols` | pool families the solver may route through | `constant_product`, `concentrated`, `liquidity_book` |
| `pool_order` | admitted pool order where it changes the feasible set or ties | bundle insertion order |
| `hops` | `{max, param}`: the bound on a route's pools and the parameter that sets it | `search.max_hops`, `graph.label_hops`, or null |
| `splits` | `{max, param, governs}`; `governs` is `allocation`, `fallback_only` or `none` | `search.max_splits`, `graph.chunks` |
| `amount_grid` | `kind` (§3.2) plus its parameters and remainder rule | — |
| `zero_output_leg` | a positive-input leg whose output floors to 0 | `infeasible` (quote status `insufficient_output_amount`, R2) |
| `token_reuse` | per-route token rule | `simple_path` (a repeated-token walk is outside the v1 set, R1) |
| `pool_reuse` | `single_pool`, `disjoint`, `shared_merged`, `shared_sequential` | §3.5 |
| `dag_admission` | plan-level token-graph rule | `plan_token_dag` (evaluator static check) |
| `full_fill` | residual rule | `v1_full_fill` (all input allocated, intermediates consumed) |

`candidate_domain_hash` = SHA-256 hex of `json.dumps(domain, sort_keys=True).encode()`, the
repository's canonical-JSON convention (`benchmark/results.py` `experiment_identity`).
Objective and budget are **not** part of the feasible set; they are part of the comparison
identity (§3.4) and of the certificate (§4).

### 3.2 Amount grids

| `amount_grid.kind` | Definition |
| --- | --- |
| `single_leg` | whole input on one route (`direct`, `single_path`) |
| `repository_grid` | `direct_split`'s rule: `N = 100 / percent_step` units; every leg but the last in admitted pool order gets `floor(A·u/N)`; the last nonzero leg takes `ALL_REMAINING`; a zero non-final leg is not emitted; at most `max_splits` nonzero legs |
| `chunk_grid` | chunk `k` of `K` is `floor(A·k/K) − floor(A·(k−1)/K)` (`incremental_graph`, `metis_inspired`) |
| `raw_integer` | every integer allocation of the declared legs (a strictly larger set than a grid) |
| `recovered_continuous` | integer plan recovered from a continuous solution under a declared recovery rule (`cfmm_dual`, WHI-1557) |

**Equal parameter values do not establish equal feasible sets.** R6 (new): two CPMM pools,
input 38, `percent_step` 5, `max_splits` 2. With admitted order (p1, p2) the repository grid
optimum is 58; with (p2, p1) — the same pools and the same parameter values — it is 59, and
`direct_split` itself returns exactly 58 and 59. The raw-integer optimum is 59 (x = 10 on p1).
The two grids are different domains with different hashes, so the grid certificate (upper
58) cannot be transferred to the raw domain (example `N-DOMAIN-TRANSFER`).

### 3.3 Which limits actually govern each solver

| ID | Hop bound | Split bound (`governs`) | `Budget.max_candidates` unit | Separate caps |
| --- | --- | --- | --- | --- |
| `direct` | 1 | none | `pools_evaluated` | — |
| `single_path` | `search.max_hops` | none | `paths_evaluated` | — |
| `direct_split` | 1 | `search.max_splits` (allocation), grid, pool order | `finalist_plans_evaluated` | — |
| `path_split` | `search.max_hops` | `search.max_splits` (allocation), grid | `per_stage_candidates_evaluated` | — |
| `incremental_graph` | `search.max_hops` | `graph.chunks` (allocation); `search.max_splits`/`percent_step` only for the embedded `path_split` fallback | `paths_scored_per_chunk` (the embedded `path_split` applies its own stage rule) | — |
| `uni_sor_port` | `search.max_hops` | `search.max_splits` (allocation), grid | `enumerated_routes_threshold` (fewer declared than enumerated → `timeout`) | — |
| `uni_sor_adaptive` / `uni_sor_optimized` | `search.max_hops` | as `uni_sor_port`, plus the pinned L08 recipe | as `uni_sor_port` | recipe caps |
| `metis_inspired` | `graph.label_hops` (chunk search), `search.max_hops` (fallback) | `graph.chunks`; `search.max_splits` fallback only | `label_relaxations_per_chunk` (label mode; paths per chunk with `label_pruning: false`) | — |
| `metis_history` | as `metis_inspired` | as `metis_inspired` | `label_relaxations_per_chunk` | state/frontier caps (WHI-1549) |
| `direct_split_certified` | 1 | `search.max_splits` (allocation), grid and pool order in `repository_grid` | `finalist_plans_evaluated` | `max_bound_nodes`, memory (WHI-1551) |
| `incremental_graph_repair` | as `incremental_graph` | `graph.chunks` for incumbent and rebuilt suffix; `max_splits` fallback only | `paths_scored_per_chunk` | repair windows/attempts (WHI-1553) |
| `uni_sor_cycle_safe` | as `uni_sor_port` | as `uni_sor_port` | `enumerated_routes_threshold` | none initially (WHI-1555) |
| `cfmm_dual` | `search.max_hops` bounds the market universe (pools on some simple path of ≤ `max_hops` admitted pools; the merged DAG may contain longer composite paths, as for `incremental_graph`) | none (`governs: none`; `search.max_splits`/`search.percent_step` unused; one merged step per market) | `fallback_paths_evaluated` (only the single-path fallback consumes it) | `max_iterations`, `max_function_evaluations` (one guarded numeric budget per solve attempt, shared by the re-solve), `max_recovery_attempts` ([`cfmm-dual.md`](cfmm-dual.md) §9) |

Consequences: a `search.max_splits` scan says nothing about a graph solver's own plan, only
about its fallback; a node, state, label, iteration or repair cap is its own option (§9) and
never a reinterpretation of `max_candidates` (example `N-WORK-MAXCAND`).

### 3.4 Comparison classes

| Class | Condition | What it may claim |
| --- | --- | --- |
| `same_domain` | identical `candidate_domain_hash`, objective and budget | mechanism / search-quality differences |
| `expanded_domain` | same protocols, one feasible set strictly larger (more hops, `raw_integer` vs grid, shared vs disjoint pools, more splits) | capability/domain gains, never a pure search improvement |
| `expanded_protocol` | different protocol sets (for example LB coverage) | coverage gains (the `⚑ coverage` rule of `v1-acceptance.md` §5.3) |
| `incomparable_domain` | neither contains the other | side-by-side only, unranked |
| `objective_mismatch` | different objectives | never ranked (`ranked: false`) |

Same-domain mechanism tests are reported apart from expanded-domain or expanded-protocol
comparisons. A comparison metric divides only equal units (`N-CMP-RATIO`).

### 3.5 Pool reuse

`single_pool` (one pool), `disjoint` (no physical pool on two routes: `path_split`, SOR),
`shared_merged` (one merged swap per physical pool: `incremental_graph`'s normalized plan)
and `shared_sequential` (several evaluator steps on one pool, each seeing the prior state).
R4 shows why they are different quantities: on one (1000, 1000) pool, two 100-unit chunks
quoted on the initial state sum to 180, executed sequentially give 165 and merged give 166;
the evaluator reproduces 165 and 166. `sum(path_quote(initial_state))` is not a shared-pool
objective.

## 4. Result metadata: incumbents, bounds and estimates

### 4.1 The certificate record (`r021.certificate/1`)

| Field | Rule |
| --- | --- |
| `schema` | `"r021.certificate/1"` |
| `candidate_domain_hash` | the domain the bound covers; equals the diagnostics' domain hash |
| `objective` | objective mode (`gross_only`, …); equals the run's objective |
| `source` | `git_revision`, `bundle_hash`, `algorithm`, `effective_settings_sha256`: all required and each **equal** to the identity the runner supplies independently for this record (its source revision, bundle, algorithm and effective-settings hash); `algorithm` also equals the diagnostics' `algorithm` |
| `request` | `case_id`, `token_in`, `token_out`, `amount_in` (decimal string): the exact request the proof is about, equal to the runner's case. One bundle and domain cover many requests; a proof never transfers to another request, even when the scores match |
| `lower_raw` | decimal string: objective score of the validated incumbent; equals the runner's independently evaluated score of the returned plan |
| `upper_raw` | decimal string only when `bound_kind` is `certified`; otherwise null (never `"0"`) |
| `gap_raw` | `upper_raw − lower_raw` when certified; otherwise null |
| `bound_kind` | `certified`, `estimate` or `unknown` |
| `upper_source` | for `certified`: `exhaustive`, `exact_rational` or `outward_rounded`; otherwise null |
| `estimate` | null or `{value, residual, tolerance}` decimal strings: a numerical estimate, never a bound |
| `optimality_proven` | true iff certified and `upper_raw == lower_raw` |
| `termination` | one value of §4.3 |

### 4.2 Bound kinds

- `certified`: `upper_raw` is proven to dominate every allocation of the declared domain
  under the declared objective, computed by exhaustive enumeration, exact rational
  arithmetic or rigorously outward-rounded arithmetic. Only this kind has a gap.
- `estimate`: a numerical value (continuous optimum, dual value, residual) whose inclusion
  in the discrete domain or outward safety is not demonstrated. It populates `estimate`,
  never `upper_raw`/`gap_raw` (`N-ESTIMATE-GAP`). A dual value becomes `certified` only after
  WHI-1557 proves feasible-set inclusion and outward-safe evaluation.
- `unknown`: no bound claimed, or the bound is inapplicable (unsupported protocol, cap
  before any bound). Unknown is null, never zero (`N-UNKNOWN-ZERO`).

### 4.3 Terminations

`complete` (the whole domain resolved: a certified gap is then zero), `node_cap`,
`state_cap`, `candidate_cap`, `quote_budget`, `wall_budget` (cooperative stops before the
hard limits), `iteration_cap`, `converged`, `not_converged`, `recovery_failed`,
`unsupported_scope`. A capped termination may still carry a certified nonzero gap; a
budget exhaustion is `timeout`/truncation with its available bounds, never `no_route`.

### 4.4 Invariants (validator codes of the executable check)

| Code | Rule |
| --- | --- |
| `C_AMOUNT_TYPE` | raw amounts are decimal strings (DESIGN §2.2) |
| `C_UNCERTIFIED_BOUND` | `estimate`/`unknown` carry no `upper_raw`/`gap_raw` |
| `C_CERTIFIED_SOURCE` | `certified` needs an upper bound and a certified `upper_source` (a float optimizer value is not one) |
| `C_ORDER` / `C_GAP` | certified `upper_raw ≥ lower_raw`; `gap_raw = upper_raw − lower_raw` |
| `C_OPTIMALITY` | `optimality_proven` exactly when certified with zero gap |
| `C_TERMINATION` | registered termination; `complete` closes a certified gap |
| `C_DOMAIN` | the certificate's domain hash is the record's domain hash (no transfer across domains) |
| `C_OBJECTIVE` | certificate objective = run objective, within the identity's objectives |
| `C_LOWER_EVAL` | `lower_raw` = the independently evaluated score |
| `C_IDENTITY` | source revision, bundle, algorithm and settings hash present **and equal** to the run's independently supplied identity (a nonempty wrong value fails) |
| `C_REQUEST` | the certificate names the exact request (case, input/output token, raw input) and it equals the runner's case (`N-REQUEST-TRANSFER`: the S→T 38 proof reused for the T→S 135 request whose grid optimum is also 58) |
| `C_KILLED` | no certificate for a hard-killed solve |
| `C_UNAVAILABLE_REASON` | an absent certificate states `hard_timeout`, `worker_error` or `not_produced` |
| `D_MISSING_FIELD`, `D_ENUM`, `D_POOL_ORDER`, `D_HASH` | complete, registered domain; `repository_grid` names its pool order; hash reproducible |
| `W_UNIT`, `W_TYPE`, `W_LEDGER`, `W_MAX_CANDIDATES`, `W_NUMERIC_REPLAY` | §5.2–§5.3 |
| `K_SAME_DOMAIN`, `K_OBJECTIVE`, `K_UNIT_RATIO` | §3.4 |
| `T_SINGLE_PERCENTILE`, `T_CONTAMINATED_VERDICT` | §5.1 |
| `V_VOCAB`, `V_EXPOSURE`, `V_NO_GO`, `V_TOLERANCE` | §6.1, §8 |

### 4.5 Unavailable certificates

A solve cut off by the runner's hard wall or quote limit returns no `SolveResult`; the
runner keeps only the separately labeled `last_valid_candidate` streamed through
`SolveContext.report_candidate`. Its certificate is **unavailable** (`hard_timeout`); it is
never reconstructed from logs, partial frontiers or worker output (`N-KILLED`). A crashed
worker is `worker_error`; a solver that ran but produced none is `not_produced`.

### 4.6 Example records

[`fixtures/examples.json`](fixtures/examples.json) holds positive records for a complete grid
certificate (58 = 58), a budget-truncated certified gap (incumbent 57, exact-rational
tangent bound 59), a raw-integer proof (59 = 59), a CFMM-style estimate (≈59.3676, no gap),
an unknown heuristic bound, a hard-timeout unavailable view, same-domain and
expanded-domain comparisons, single-quote and batch timing, and dispositions. Every
negative is a minimal mutation of a positive that must fail with exactly one code; each
diagnostics example's context carries the runner-supplied `run` identity and `request`, and
wrong nonempty values of every `source` field and of every `request` field are separate
negatives (`C_IDENTITY`, `C_REQUEST`). The certificate numbers are checked against
independent exhaustive enumeration of their domain (R6); they are illustrative records, not
outputs of any 0.2.1 implementation.

## 5. Timing and work units

### 5.1 Timing

Timing labels and boundaries are L01's (`latency-baseline.md` §4): `solve_wall_seconds`,
`solve_cpu_seconds`, `transport_seconds`, `startup_seconds` (including prepare),
`prepare_seconds`, `evaluation_seconds`, cold `charged_seconds` and quote `cli_wall_seconds`.
Everything a strategy does to build its incumbent, bound, repair, recover or internally
validate happens inside `solve` and is solve time; the runner's final evaluation stays
outside. Rules:

- `main.py quote` solves each selected strategy exactly once (no warmup, retry, repeat or
  memory pass). One call has one sample: no p95, percentile or SLA
  (`N-TIME-P95`). Optional per-stage seconds are observations, not budgets.
- Batch percentiles carry their sample count and underpowered flag (DESIGN §2.11).
- A speed verdict needs L01's host rule (1-minute load ≤ 0.5 × logical CPUs); a contaminated
  host yields `inconclusive`, never an invented clean speedup (`N-TIME-LOAD`).
- There is no fixed campaign runtime ceiling and no invented runtime/SLA threshold.

### 5.2 Registered work units

| Unit | Category | Meaning |
| --- | --- | --- |
| `quotes_executed` | quote | metered `pools.quote.quote_exact_in` calls of the attempt (the worker meter) |
| `quotes_memoized` | quote | per-solve cache hits (not metered) |
| `exact_replay_quotes` | quote | the subset of `quotes_executed` spent replaying numeric/continuous candidates exactly |
| `internal_evaluations` | validation | complete-plan evaluator replays inside solve |
| `paths_scored` | search | enumerated path evaluations (`incremental_graph`) |
| `label_relaxations` | search | label edge relaxations (`metis_inspired` family) |
| `labels_discarded_dominance` | search | labels removed by a proven dominance rule |
| `labels_retained_unknown` | search | labels kept because dominance was not provable |
| `state_comparisons` | search | label/state signature comparisons |
| `peak_frontier_labels` | memory | largest simultaneous label set |
| `bb_nodes_expanded` | search | branch-and-bound nodes expanded |
| `bound_evaluations` | search | bound computations (exact rational or outward-rounded) |
| `peak_open_nodes` | memory | largest open frontier |
| `admission_checks` | search | plan-token-DAG admission tests |
| `combinations_rejected_cycle` | search | combinations refused by admission |
| `repair_attempts` | search | repair neighborhood candidates built |
| `checkpoint_restores` | search | complete prefix restorations |
| `market_oracle_calls` | numeric | analytic per-market arbitrage/oracle evaluations |
| `objective_evaluations` | numeric | continuous objective evaluations |
| `gradient_evaluations` | numeric | gradient evaluations |
| `optimizer_iterations` | numeric | optimizer iterations |
| `recovery_attempts` | numeric | integer-plan recovery attempts |

Units are listed side by side and never divided by one another (`paths_scored` is not a
`label_relaxations`); the only cross-strategy work comparison in one unit is
`quotes_executed`, beside wall/CPU time and memory.

### 5.3 One attempt ledger

- Every exact protocol quote goes through the metered `pools.quote.quote_exact_in` (the
  evaluator already does). Calling protocol math directly to obtain free exact quotes is
  forbidden; analytic continuous oracles are reported as numeric units.
- Incumbent construction, bounds, repair, recovery and internal validation consume the same
  wall/quote attempt budget as the search (`N-WORK-LEDGER`); there is no free external
  baseline, no fresh budget per retry, no hidden retry and no timer reset.
- A record with numeric work also reports its `exact_replay_quotes` (`N-WORK-REPLAY`).
- The runner's hard limits keep their meaning: the hard quote cap and wall limit end the
  attempt (`timeout`), a cooperative cap is declared truncation (`truncated_by`).

## 6. Tuning, holdout exposure and noise

### 6.1 Splits and exposure

| Split | Bundles | Use | Exposure |
| --- | --- | --- | --- |
| tuning | `bundle_tuning`, `sor_cohort_tuning` (96 cases) | exploration, ablations, nominee selection | exploration data |
| report | `bundle_report`, `sor_cohort_report` (302 cases) | one frozen comparison per pre-registered claim | `previously_exposed`: WHI-1447 (v1 held-out runs), WHI-1503–WHI-1510 (the L01 matrix's held-out twins, through L06/L07/L08), WHI-1449 (H-M1b A0 vs M4) |

A new pre-registration does not make the report split unseen; every 0.2.1 report-split
result carries `holdout_exposure: previously_exposed` (`N-DISP-FRESH`). Independent
newer-block validation for generalization is a separate follow-up, not an implicit data
collection job of any 0.2.1 issue.

### 6.2 Registered exploration (tuning only)

- `E3`, `E4`: enumeration chunk selection at 3 / 4 hops (`E3` = `incremental_graph` at
  `max_hops` 3, the X7 identity; `E4` = `metis_inspired` with `label_pruning: false`,
  `label_hops` 4, the former M4-off).
- `L3`, `L4`: `metis_inspired` label search at 3 / 4 hops (former M3/M4).
- `S3`, `S4`: `metis_history` at 3 / 4 hops.
  All six share corpus, chunks, fallback `search.*`, objective, budgets and deterministic
  rules. Effects are decomposed by ratios (`Q_L4/Q_E3 = (Q_E4/Q_E3)(Q_L4/Q_E4)`) or logs,
  never by adding bps with different denominators.
- `same_grid_allocation`: `direct_split` vs `direct_split_certified` on one
  `candidate_domain_hash` (same pools, pool order, grid, `max_splits`).
- `repair_off_on`: `incremental_graph_repair` with repair disabled vs enabled, same incumbent
  policy and budget.
- `matched_sor_cycle_safe`: `uni_sor_cycle_safe` vs `uni_sor_port` on the same candidates,
  grid and V2/V3 cohort; known report-split `invalid_plan` cases are defect regressions, not
  tuning data.
- `cfmm_cpmm_vs_cl`: `cfmm_dual` CPMM-only vs CL-enabled on matched cohorts.
- `max_splits_scan`: values 1, 2, 4, 8, only where `search.max_splits` governs allocation
  (§3.3); for graph solvers the scan measures their fallback and is labeled so.

### 6.3 Frozen before the report comparison

Implementation SHA; effective settings and preset hashes; source/bundle/profile/domain
hashes; nominees; numerical tolerances; tie, stop, fallback and failure rules; case lists;
metrics; analysis script hash. Reported outcomes include every scheduled status
(`timeout`, `invalid_plan`, `no_route`, `unsupported`), not only common successes.

### 6.4 Noise and correlation

Timing follows §5.1; a loaded host yields `inconclusive`; no silent rerun until a desired
result appears. Cases sharing a token pair, direction or amount stratum are correlated:
report per-family gains/losses beside any pooled statistic, never one pooled p-value alone.

## 7. Bounded comparison presets versus stress profiles

1. **Bounded comparison preset.** Each implemented new ID has exactly one pinned preset
   (path, SHA-256, version) holding only its `algorithm_options`. Every cap in it is finite.
   The shared wall/quote/candidate budget and `search.*`/`graph.*` values still come from the
   profile. The preset is chosen on tuning, frozen before the report comparison and used by
   `--strategies all`.
2. **Stress profiles.** Separately named profile files with larger caps/budgets or expanded
   domains (for example `raw_integer`). They run only by explicit `--strategies profile`,
   are labeled stress, and appear in no `all` comparison or disposition except as
   budget/domain context.
3. **Unsupported scope.** A gross-only method (`direct_split_certified`, `cfmm_dual`) under
   `synthetic_fixed_cost` or `empirical_cost`, or any protocol outside an identity's
   ceiling, produces an `unsupported` row with its reason in every scheduled case.

## 8. Final evidence and disposition vocabulary

- **Research outcomes** (WHI-1549/1551/1553/1555/1557/1560): `go`, `narrow_go` (scoped
  domain), `blocked` (a missing input), `no_go`.
- **Implementation dispositions** (WHI-1562, per strategy): `keep_experimental` (functional,
  gates passed, measured evidence reported; remains opt-in/experimental), `reject` (a
  correctness, determinism or accounting gate failed, or the pre-registered hypothesis is
  refuted), `inconclusive` (gates passed but the decision metric could not be evaluated:
  contaminated timing, coverage problem, underpowered), `not_implemented` (the research
  outcome was `no_go` or `blocked`; never counted as done, `N-DISP-NOGO`).
- **Timing verdicts** keep L01's per-algorithm `faster`, `slower`, `no_worthwhile_change`,
  `insufficient_cases` and lane verdicts `adopt_eligible`, `opt_in_only`, `inconclusive`,
  `reject`.
- **No invented owner loss tolerance**: `heuristic_default_loss_tolerance` stays null
  (`N-DISP-TOL`). Default-router adoption is a separate owner decision outside this
  vocabulary (`N-DISP-ADOPT`). A functional implementation does not need a win; a GO may not
  be forced by relaxing exact math, the evaluator or the money rules.
- **Final evidence** (WHI-1562): raw per-case records, hashes, commands, environment, all
  registered ablations, unconditional statuses, matched common-success quality, per-family
  gains/losses, timing per §5.1, distinct work units, certificate scope versus estimates,
  recovery/fallback failures.

## 9. WHI-1548 configuration and diagnostics seam (implementation-ready)

### 9.1 Profile section

An optional top-level `algorithm_options` mapping keyed by registered algorithm ID:

```yaml
algorithm_options:
  <registered id>:          # must be a selected algorithm of this profile
    <option>: <value>       # keys, types and ranges owned by that factory's validator
```

- Absent section: the resolved profile, effective profile, replay command and every hash
  are byte-identical to today (the key is omitted, not an empty mapping).
- Refused before any worker starts or any result is written: an unknown ID, a registered but
  unselected ID, an ID whose factory has no validator, an unknown key, a boolean where an
  integer is expected, a non-finite number, an out-of-range value, and any **reserved** key
  that names a shared setting (`max_hops`, `max_splits`, `percent_step`, `chunks`,
  `label_hops`, `label_pruning`, `time_limit_seconds`, `max_quotes`, `max_candidates`,
  `shortlist`, `sampling`, `controls`, `recipe`, `objective`, `seed`). Shared search and
  budget values stay authoritative.
- The existing `strategies.<name>` SOR recipe checks are not reinterpreted or weakened.

### 9.2 Factory seam (minimal)

- `AlgorithmFactory.options_validator`: a module-level function
  `(Mapping[str, Any]) -> dict[str, Any]` returning the complete normalized options or
  raising a `ValueError` subclass naming the key. It is called by profile loading **and** by
  the factory's own `prepare` (public entrypoints cannot bypass it).
- `AlgorithmFactory.options_preset`: `{path, sha256, key, version}` of the bounded preset
  (§7.1), read only when that ID is selected and implemented, refused if its bytes changed.
- `AlgorithmConfig.options`: the validated options of that ID alone, separate from
  `params`; siblings never see each other's options.

### 9.3 Identity and replay

Each resolved entry records `options`, `source` (`{kind: "preset", path, sha256, key,
version}` or `{kind: "override"}`) and `settings_sha256` (the §3.1 hash rule over the
options). An override that differs from the preset is identified by its own hash and is
never labeled as the registered preset. Under `all`, an implemented new ID the source does
not configure receives its preset; a declared entry wins. Batch and quote save the resolved
entries in the effective profile and replay them literally with `--strategies profile`.

### 9.4 Diagnostics (`search_stats["r021"]`, `r021.diagnostics/1`)

One JSON object in the existing `SolveResult.search_stats`, no parallel result framework:
`schema`, `contract` (`"R021-C/1"`), `algorithm`, `domain` (§3.1), `candidate_domain_hash`,
`certificate` (§4.1) or null, `certificate_unavailable_reason`, `max_candidates_unit`,
`work` (§5.2 units only, non-negative integers), and optional `fallback` (`used`, `source`,
`reason`), `repair`, `scope` (`supported`, `reason`) and observational `stages` seconds.

After the independent final evaluation the runner applies §4.4, comparing the certificate against its own identities — never against the solver's claims: `C_LOWER_EVAL` against the evaluated score, `C_IDENTITY` against the run's source revision, bundle hash, algorithm and effective-settings hash, and `C_REQUEST` against the case being solved.
A violating certificate is rendered `invalid certificate (<codes>)`, counted as unknown,
and fails that strategy's own contract tests; it is never displayed as certified.

### 9.5 Rendering

`quote --details`, the terminal report and HTML show, per row with diagnostics: the domain
hash prefix and grid kind; the bound as `certified [lower, upper] gap g`, `estimate value
(not a bound)`, `unknown (no bound)` or `unavailable (<reason>)`; work counters with unit
names; fallback/repair; unsupported scope. A `timeout` row of a new identity without
diagnostics shows `unavailable (hard_timeout)`. Records without the key (every existing
algorithm and every old run) render exactly as before. No single-call percentile.

### 9.6 Tests WHI-1548 owns

Invalid/unknown/reserved/boolean/non-finite options refused before workers and results;
sibling isolation; absent-section identity of legacy resolved profiles and replay hashes;
unchanged nine-strategy selection and SOR recipe pins; batch and quote persist and replay
exact options and one solve per quote; a test-only diagnostics fixture rendering all four
bound states plus an invalid certificate, including wrong-run-identity and wrong-request
certificates whose other fields and score are valid; hard-timeout keeps only
`last_valid_candidate` and shows `unavailable`; old records render unchanged. The ordinary roster stays nine
until an implementation lands.

## 10. Deterministic rules shared by all five

- Deterministic order everywhere (admitted pool order, adjacency order, registered
  candidate order); no randomness unless a seeded rule is declared.
- The incumbent is replaced only by a complete, independently valid plan with a strictly
  better objective score; ties keep the earlier candidate and are counted.
- Equal objective values do not imply the same allocation as another solver; a certificate
  certifies the value, not a tie choice.
- Every candidate is replayed by the evaluator before it can become the incumbent; a failed
  candidate never erases the current valid incumbent.
- Status mapping: `ok`; `unsupported` (declared scope); `no_route` only after a complete
  search; `timeout` on a budget cut (with the last valid candidate if any); `invalid_plan`,
  `incomplete_snapshot`, `algorithm_error` as today.

## 11. Research ticket obligations

Before its implementation is dispatched, each research ticket publishes, in these schemas:
the exact algorithm; its `r021.domain/1` values and the §3.3 row (including every
"declared by" cell); the option schema with keys/types/ranges and reserved-key check; the
bounded preset values selected on tuning; tie/stop/failure rules; which `bound_kind` values
it can emit and the proof behind `certified`; its work units; and executable checks with
independent expectations. It records `go`, `narrow_go`, `blocked` or `no_go`.

## 12. Executable checks

```bash
uv run pytest tests/docs/test_research_021_contract.py -q
(cd docs/references/research-021/sources && shasum -a 256 -c SHA256SUMS)
```

The test checks the archived report hash, prose/JSON vocabulary agreement, the registry
(nine existing identities, none of the five registered yet), every example record, the
domain hashes and R1–R10.

## 13. Change control

`R021-C/1` is frozen at WHI-1547's merge. A research ticket may narrow its own row. Adding
or renaming an identity, a vocabulary value, a domain field or a work unit, or widening a
ceiling, requires `R021-C/2`: a new `contract-v2.json`, updated examples/checks and a
changelog entry here. Historical records keep the version they were produced under.

Row fills within `R021-C/1` (narrowing of a row the contract itself delegated, no new
vocabulary, domain field or work unit):

- WHI-1557 filled the `cfmm_dual` §3.3 row and the `cfmm_dual` identity of
  `contract-v1.json` (`max_candidates_unit`, `governing`, `separate_caps`; recorded in its
  `row_fill`), replacing the WHI-1547 placeholder `declared_by_research`. The example
  `P-CFMM-EST` was re-bound to the filled row with its WHI-1547 values kept in its
  `history`; the WHI-1547 domain `cfmm38` stays in `fixtures/examples.json` unchanged as a
  historical record, and `N-CFMM-PLACEHOLDER-UNIT` shows the old unit now fails
  `W_MAX_CANDIDATES`.
