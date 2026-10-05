# Research contract `R023-C/1`: Jupiter-inspired split polishing (E1) and marginal activation (E2)

| Item | Value |
| --- | --- |
| Issue / Release | WHI-1622, Release 0.2.3 (`430fe77b-107a-41d8-86f2-9ae0b5ffe991`) |
| Downstream | WHI-1623 `split_polish` (E1), WHI-1624 `marginal_activation` (E2, conditional) |
| Repository base of the research | `dev` @ `95d0744db23d19d14f65eafd8512b05d84ec9858` |
| Published on | `release/v0.2.3` @ `abd66fbc84708646e238cbc4dceec8b13932c0a9`; fixtures (27/27) and the analysis identity re-verified there (WHI-1622) |
| Domain | frozen corpus `mantle-5src-101082044` (block 101082044, bundle_hash `717c21f3…3143`), exact input, **gross-only** |
| Label | "Jupiter-inspired (Ultra V3 / Metis v7 Brent splitting), **NOT Jupiter Metis**" |
| Probe (validation only, never imported by runtime code) | [`probe/`](probe/), `polish4.py(.txt)` sha256 `b508bcc48d520ba56840e8e9bc4274d548c6d0d962fe2607adfc66c7fc9111b3`; pins in [`probe/SHA256SUMS`](probe/SHA256SUMS) (arm results, uncompressed: [`probe/results/SHA256SUMS`](probe/results/SHA256SUMS)) |
| Executable check | see [`probe/README.md`](probe/README.md) (`fixtures4`: 27/27, exits non-zero on failure); publication check `tests/docs/test_research_023_contract.py` |
| Independent review | four rounds with `mantle/gpt-6-astra` (different vendor); final verdict **AGREE**; reports in [`reviews/`](reviews/); dispositions in §12 |

This contract fixes the mechanisms, nominees, questions, arms, controls, gates and dispositions of two
new experimental strategies. It implements no runtime code and changes no existing strategy, profile,
strategy group or historical record. Nothing here claims optimality, Jupiter equivalence, a speed
gain, statistical significance or default adoption.

## 1. Sources

Retrieved 2026-10-05 UTC by the independent reviewer. URLs, displayed dates, SHA-256 pins of the
retrieved pages and the verbatim quotations relied on are in [`sources.md`](sources.md). Only short
attributed quotations are used.

| ID | Source (displayed date) | Verified content used |
| --- | --- | --- |
| U1 | [Ultra V3](https://developers.jup.ag/blog/ultra-v3) (2025-10-15) | Iris "utilzies … Golden-section and Brent's method"; "uses Brent's method to optimize route splitting"; splitting "up to 0.01%"; "100x performance improvements … compared to our sunsetted Metis router" |
| M7 | [Metis v7](https://developers.jup.ag/blog/metis-v7) (2025-11-17) | "Re-engineered splitting algorithm from GGS to Brent Op Splitting … down to 1 BPS precision"; GGS is not defined |
| MU | [Metis Update](https://developers.jup.ag/blog/metis-update) (2026-04-23) | RTSE-aware routing, JIT routing (splits finalised on-chain), Fast Mode, dynamic intermediates, JupiterZ V2 as a hop, Lend mint/redeem |
| CL5 | [Changelog May 2026](https://developers.jup.ag/changelog/2026-05) (2026-05-31) | router name migrates `iris` → `metis` ("legacy name"); Metis V8 upgrade (naming/lineage, not version identity) |
| JS | [JIT Swap](https://developers.jup.ag/blog/jit-swap) (2026-06-11) | off-chain splits at single-bps resolution; on-chain re-split in 5 % increments over six venues, ~600k CU; 26.89 % pool-set changes; "57.82% of same-pool swaps" rebalanced (> 0.5 % share change) |
| QE | [Why you don't get what you were quoted](https://developers.jup.ag/blog/why-you-dont-get-what-you-were-quoted) (2026-05-25) | RTSE per-market EMA, predictive simulation, +0.63 bps (same figure as U1's Oct-2025 window) |
| MC | [metis.builders changelog](https://metis.builders/changelog) (entries undated) | v7.0.1 "Feat: Brent optimization for routing"; v7.0.4 top-3 intermediates; v7.0.5 "Fix: Quote multiple use bellman logic", "Perf: Optimize routing allocations and lock usage", "Chore: Run Metis Using Snapshot", "Feat: DLMM cache liquidity available per tick to bypass heavy math" — one-line strings; no mechanism is inferred |
| P1 | Weiye Xi & Ciamac C. Moallemi, [arXiv 2607.20762](https://arxiv.org/abs/2607.20762) (2026-07-22) | same-pair pools only; SCO (within-support mis-split) mean 0.05467 bps; total 2.02 bps; limited activation dominates; one-block staleness FVO +1.28739 / G-FVO +1.77525 bps |
| P2 | PRIME, [arXiv 2603.08337](https://arxiv.org/abs/2603.08337) (v1 2026-03-09, v2 2026-07-17) | pool-disjoint paths; ASGM with backtracking line search. **PRIME-Flow**: "permits overlapping paths"; "ternary search … split ratio … between the current flow and the new path"; stops when no better marginal path |
| P3 | [arXiv 2204.05238](https://arxiv.org/abs/2204.05238); [arXiv 2302.04938](https://arxiv.org/abs/2302.04938) | convex CFMM routing; decomposition; aggregate CFMMs (already used by `cfmm_dual`) |
| B | Brent, *Algorithms for Minimization without Derivatives* (1973), ch. 5 | golden section + successive parabolic interpolation; a **local** minimiser |

**Reading "100x".** A vendor claim attributed only to U1's contemporaneous changes (Iris, golden/Brent,
0.01 % splitting); the comparator is chronologically pre-v7 Metis (inference); the metric is undefined.
It is motivation, never a target. Transferable content: precision decoupled from evaluation count —
golden section needs 20 contractions for a 10⁻⁴ bracket, while one evaluation here is a full exact plan
replay. Measured as Q5, no numeric target.

**Mantle mapping.** Brent/golden splitting at 1 bps → E1. Candidate activation (P1: activation
dominates) → E2. Fast Mode, exact per-tick caching, charged meta-aggregation → deferred static
follow-ups (§11). RTSE, predictive execution, JIT on-chain re-split, Beam/MEV, RFQ/Prop AMM,
dynamic intermediates → out of scope (dynamic state, execution layer or absent venues).

## 2. Distinctness from prior work

| Existing | Mechanism | Difference |
| --- | --- | --- |
| `incremental_graph`, `metis_inspired`, `metis_history` | irreversible greedy chunks; per-chunk path choice | E1 re-allocates continuous shares of a finished plan; E2 adds branches and re-allocates |
| `incremental_graph_repair` | checkpoint restore + greedy suffix re-run | no continuous share optimisation; used as comparator |
| `cfmm_dual` | continuous dual (L-BFGS-B) + share projection, CP+CL | E1 optimises against exact integer replay of the actual plan |
| `direct_split_certified` | exact certificate, single-hop CPMM grid | E1 uncertified, all five protocols, intermediate splits |
| L07 `uni_sor_fast` adaptive sampling | coarse-to-fine percentages inside SOR | E1 is solver-agnostic post-processing on every split fund |
| PRIME (P2) | pool-disjoint paths, ASGM backtracking | E1 handles shared pools and intermediate splits by exact sequential replay |
| PRIME-Flow (P2) | overlapping paths, ternary split current-vs-new | E2-PF is PRIME-Flow-like with DAG admission and exact replay; E2-full adds full pairwise re-polish |
| WHI-1448 §8 rejection of Brent splitting | source did not define the method | the method is now fully self-specified; Jupiter only motivates |

## 3. Domains

- **E1 — fixed funding topology.** The input is the base strategy's evaluator-valid integer plan,
  canonicalised (zero references and zero-input steps removed). Every consumed fund must be fully
  consumed; otherwise the case is refused as `unsupported_topology` and the base plan is returned with
  its true gross (Σ over target-token funds of produced − consumed). Variables: exact rational shares
  of every multiply-consumed fund. Pools, directions, step order and fund wiring are fixed. This is a
  subset of all allocations over the same pool support — **not** SCO, FVO or an optimum.
- **E2.** E1 plus appended branches admitted under the all-steps DAG invariant (§5.1); a reused pool
  executes as a new sequential call (`shared_sequential`, R021 vocabulary). Merge-by-token
  reparameterisation and cycle unlock by deactivation are out of scope.

## 4. E1 — `split_polish` (normative)

1. **Reconstruction.** Shares `Fraction(amount, produced)` from the canonical plan; simulate and assert
   that every allocation and the gross equal the base plan before any optimisation.
2. **Simulation.** Evaluator-equivalent forward pass: for each fund, every consumer except the **last
   consumer with a positive share** gets `floor(total · share)`; that consumer takes the remainder;
   zero-share consumers get exactly 0. Steps with zero input are skipped. Merged inputs are summed;
   physical pools use sequential state. A positive unconsumed non-target fund makes the candidate
   infeasible at scoring time.
3. **Outer loop.** R rounds over split funds in order of first consumption, consumer pairs (i < j) in
   index order; stop early when a round accepts nothing.
4. **Line search.** Transfer t ∈ [−s_j, s_i] moves share from i to j. Cache keys are exact rational t;
   t = 0, t = s_i and t = −s_j are always evaluated (distinct when distinct); interior points lie on the
   grid D = 10⁹ (share units); grid index 0 maps to t = 0. Infeasible = −∞ (golden) or a finite relative
   penalty +1.0 (Brent: SciPy `minimize_scalar(method="bounded")`, objective −(v − v₀)/v₀, `xatol` =
   max(1, tol/2), MAXITER 60). The final choice is always the best **exact cached** integer output.
   Tie rule: higher gross, then t = 0, then smaller |t|, then smaller t. Local heuristic only.
5. **Acceptance.** Strictly higher gross only; each accepted exchange is canonicalised, structurally
   checked by the evaluator's plan check (no quotes) and stored immediately in the incumbent holder.
   After each polish call the topology is rebuilt from the canonical plan (zero steps removed).
6. **Budget.** One attempt ledger shared with the base solve: `max_quotes` (300,000 in
   `full_gross.yaml`) minus the base's quotes, and a cooperative wall deadline (900 s minus the base's
   time). A cooperative stop returns the last validated incumbent with `truncated_by` ∈ {`max_quotes`,
   `time`}; no free re-quoting is used to materialise the result. Budget 0 returns the base plan.
7. **Nominee.** Brent, R = 2, share tolerance 10⁻⁴ of a fund; golden R = 2 is the solver control.

## 5. E2 — `marginal_activation` (normative, conditional on E1 gates)

1. **Iteration** (≤ K): post-plan pool states by sequential simulation of the incumbent; δ =
   max(1, A/10⁴); hop-layered token-simple label search (one label per (layer, token), H =
   `search.max_hops`) from `token_in` with fresh δ quotes on post-states; keep the top-3 terminal
   paths. For each in rank order:
   - build the union of the incumbent's canonical steps and the new path with a collision-checked fund
     prefix; **admit only if the token graph over all union steps is acyclic** (then no reallocation
     inside the union can create a cycle);
   - seed: PF mode = proportional (`w_orig · (1 − s₀) ⊕ s₀`, s₀ = 10⁻⁴); full mode = donor (subtract s₀
     from the largest REQUEST consumer, lowest index on ties);
   - optimise: PF = one grouped 1-D search over t ∈ [0, 1] of `w_orig · (1 − t) ⊕ t` (t = 0 reproduces
     the original canonical plan exactly; intermediate funds fixed); full = E1 polish over all funds;
   - accept iff the new branch's first hop has positive integer input, the gross strictly improves, and
     an evaluator replay charged to the attempt ledger equals the simulated gross; then rebuild.
   - otherwise try the next candidate. Stop when K is reached, no candidate is accepted, or the budget
     ends (the caught reason is preserved).
2. **Nominee.** PF, K = 2, top-3, δ = max(1, A/10⁴), seed 10⁻⁴; full mode is an ablation.
3. **Controls** (campaign only, every E2 arm, including truncated treatments):
   - *work-matched* (primary): E1 polish calls until the treatment's activation-stage quote spend —
     a hard `work_target` stop inside the control's own ledger — or until a call improves nothing;
   - *call-matched*: exactly the treatment's number of activation-stage optimiser invocations, each a
     full E1 polish call (heavier per call than a PF invocation; reported as invocations started and
     completed calls separately);
   - each control: own quote ledger pre-charged with the shared E1 spend, the same global cap, its own
     wall allowance = treatment allowance after base + E1.
   - If the budget ends during reconstruction or E1, the row is labelled "activation/control not
     reached: E1 truncated", stays in every denominator and is never counted as a matched comparison.

## 6. Identities, options and status mapping

- New ids `split_polish` (wrapper over a declared base strategy) and `marginal_activation`; custom
  group; selectable in `main.py run` and `main.py quote`; existing ids, profiles and groups unchanged;
  saved profiles replay literally.
- Objective: gross-only; any other objective → `unsupported` (`objective`), as for `cfmm_dual`.
- Status mapping: `unsupported_topology` → `ok`, base plan passed through, diagnostic
  `search.<id>.scope = "unsupported_topology"`; cooperative stop → `ok` with `search.<id>.truncated_by`,
  never `no_route`; a runner hard limit keeps the existing `timeout`; base-strategy statuses
  (`no_route`, `unsupported`, `incomplete_snapshot`, …) pass through and their denominators are kept.

## 7. Tuning evidence (probe v4; descriptive, not preregistered)

Tuning split (96 cases, 32 directed-pair families), base records from the 0.1.0 calibration runs
(IG c50 `20260925T153506524692Z-09f63aea`, IG c100 `…937800ce`, IG c200 `…a8a2dd9f`, `path_split`
`20260925T151109272576Z-4224ecdc`); "best known" from the frozen 23-run list
[`probe/reference-runs.frozen.json`](probe/reference-runs.frozen.json). Every arm: reconstruction
identity, independent replay `ok` and equal, never-worse 96/96; no unsupported topology. Overhead =
paired probe quotes / base quotes. Work ratio = paired (base + probe) / comparator quotes. Nearest-rank
percentiles. Full output: [`probe/analysis.txt`](probe/analysis.txt), [`probe/c100_summary.txt`](probe/c100_summary.txt).
E1's qualitative conclusions are unchanged from the earlier probe; all tables were rerun under the
last-positive-share remainder rule (§4.2), and these v4 values and pins are authoritative.

| Arm | gain vs own base mean / p50 / p95 (bps) | improved | overhead p50 / p95 | vs C100 H/E/L; family net ±; work p50 | vs C200 H/E/L; family net ±; work p50 |
| --- | --- | --- | --- | --- | --- |
| A0 (IG c50) + golden R2 | 0.466 / 0.086 / 2.079 | 73 | 7.0 % / 159 % | 29/20/47; 13/14; 0.985 | 24/18/54; 8/18; 0.788 |
| **A0 + Brent R2 (E1 nominee)** | **0.473 / 0.086 / 2.076** | 72 | **5.6 % / 125 %** | 30/19/47; 13/14; 0.952 | 24/18/54; 8/18; 0.788 |
| C100 + Brent R2 | 0.321 / 0.086 / 1.447 | 75 | 6.1 % / 153 % | — | 37/18/41; 12/13; 0.932 |
| PS + Brent R2 | 0.300 / 0.018 / 1.306 | 58 | 1.1 % / 10 % | — | — |
| E2-full (A0) | 0.740 / 0.378 / 2.601 | 85 | 44 % / 681 % | 68/11/17; 25/4; 1.263 | 51/11/34; 19/10; 1.053 |
| E2-PF (A0) | 0.675 / 0.352 / 2.638 | 85 | 13 % / 130 % | 68/11/17; 25/4; 1.018 | 44/11/41; 15/14; 0.803 |
| **E2-PF (C100) (E2 nominee composition)** | 0.476 / 0.223 / 1.661 | 85 | 13 % / 157 % | — | **68/11/17; 25/4; 0.972** |

Activation over controls (own ledgers): full (A0) +0.248 over call-matched (74 better / 1 worse),
+0.247 over work-matched (74 / 3); PF (A0) +0.184 (71 / 4) and +0.199 (74 / 3); PF (C100) +0.145
(73 / 1) and +0.152 (73 / 2). Controls' own gains over E1 ≤ 0.019 mean. PF keeps ≈ 76 % of the full
mode's activation-stage gain (0.202 / 0.267) at ≈ a quarter of total probe quotes (p50 2,013 vs
7,451). Full mode truncated 3/96 at `max_quotes`; PF never truncated. One call-matched control
(`c100_actpf`, `emp-78c1b0-deadde-medium-4`) stops at `max_quotes` in its last invocation.

**Readings.** E1 removes real allocation error but, as a stand-alone alternative, loses to finer
chunking case by case (vs C100 30/47 at about equal work) — it is an additive post-processor. An
earlier "order-of-magnitude cheaper precision" statement compared unpaired medians and is withdrawn.
Activation is the larger lever and survives both controls. These nominees were chosen on the tuning
split after inspecting the probes.

## 8. Campaign plan (report split; one frozen pass per arm)

**Questions** (descriptive; no p-values — cases share directed pairs and one block):
- Q1 E1 additive value: gain distribution and prevalence of `split_polish(B)` over B for B ∈ {A0 = IG c50,
  C100, M4 `metis_inspired`, S4 `metis_history`, REP `incremental_graph_repair`, PS `path_split`}.
- Q2 E1 vs finer granularity: `split_polish(A0)` vs C100, C200 (expected from tuning: loses on counts).
- Q3 E2 after controls: E2-PF and E2-full vs work- and call-matched controls.
- Q4 E2 vs references: `marginal_activation(A0)`, `marginal_activation(C100)` vs C100, C200, M4, S4, REP.
- Q5 solver work: Brent vs golden, paired polish quotes and gross.

**Arms.** References A0, C100, C200, M4, S4, REP, PS · E1-b on A0, C100, M4, S4, REP, PS · E1-g on A0 ·
E2-PF on A0 and C100 · E2-full on A0 · both controls for every E2 arm · optional CFMM + E1-b (CP+CL
diagnostic, fallbacks separate) · matched SOR cohort only for cross-router statements.

**Metrics.** Exact gross; every status and denominator; zero-baseline rows counted apart and excluded
from bps; per-case H/E/L; bps distributions; per directed-pair family net-win / net-loss; per stratum;
paired overhead and work ratios; polish calls and accepted exchanges; Brent status counts; activation
outcomes (accepted, `dag_cycle`, no gain / zero flow, stop, budget before validation); control stop
reasons and the `q ≤ target` audit; physical CL/LB work; wall time secondary (L01 rule only).

**Evidence boundaries.** Report split `previously_exposed` (0.1.0, 0.2.0, 0.2.1, 0.2.2); tuning-informed
nominees; pinned source/settings/analysis hashes; every scheduled status kept; single block; no speed
claim without L01; no new-block generalisation. The campaign is published as its own issue after
WHI-1623/WHI-1624 merge, with a frozen schedule committed before the first report-split solve.

## 9. Gates

- **Fixtures (probe, passing):** F1–F2 endpoint zeroing in both orders; F3 dead non-target terminal;
  F4 target fund with zero consumer (identity); F5 partial-target refusal; F6 cap 0–3 identity and
  ledger == seam; F7 exact retention under truncation (199 / 342 / 461); F8 incumbent and endpoints
  evaluated distinctly; F9 Brent penalty without numeric warnings; F10 reverse-edge union rejected;
  F11 former cycle scenario valid end to end; F12 fund-prefix collision; F13 real CL zero-output
  consumer refused with the true gross; F14 PF t = 0 identity (2/3 consumers, non-divisible inputs);
  F15 work target never exceeded; F16 time reason preserved at 133 cut points.
- **Implementation gates (WHI-1623/1624):** the above ported to `tests/routing/`; analytic two-CPMM
  optimum; exhaustive small-integer oracle with a narrow feasible island and a flat plateau (both
  solvers); LB variable-fee bin crossing; CL incomplete coverage; A < consumer count; all-zero plan;
  invalid-best / valid-second-best admission; quote-cap sweeps for both E2 modes; reverse / shuffle /
  rerun determinism; truncation never `no_route`; tuning reproduction of the probe outputs (or every
  difference explained).

## 10. Dispositions (R021 vocabulary)

- `reject`: a gate fails on the report split — invalid returned plan; never-worse violation against the
  identical base record; reconstruction or budget-0 identity failure; ledger ≠ seam count; a control
  not call-matched or exceeding its work target; a refusal returning a gross ≠ the base record.
- `inconclusive`: gates pass but a question cannot be evaluated (> 10 % of a base's scheduled cases
  truncated or `unsupported_topology`, or a base strategy unavailable).
- `keep_experimental`: otherwise. No win is required; Q2's tuning expectation is reported, not hidden.
  Nothing is adopted as a default.

## 11. Not in 0.2.3

Deferred static follow-ups: zero-loss incumbent-vs-bound early exit (Fast Mode analogue), exact LB
bin-math / per-tick caching (MC v7.0.5 analogue; prior L03/L04 work), charged portfolio
(meta-aggregation; the historical "best known" is a free oracle envelope, not a portfolio). Out of
scope: RTSE, predictive execution, JIT on-chain re-split, Beam/MEV, RFQ/Prop AMM, dynamic
intermediates, merge-by-token reparameterisation, cycle unlock by deactivation, non-gross objectives.

## 12. Review dispositions

Every finding was accepted and the final round agreed. Full reports: [`reviews/`](reviews/)
(`round-N.md` = report of round N). Counts: round 1 (verdict materially-flawed): 19 (R023-01..19);
round 2 (DISAGREE): 6 + 6 minor (R2-01..06, R2-m1..m6); round 3 (DISAGREE): 3 + 5 minor (R3-01..03,
R3-m1..m5); round 4 (**AGREE**): 4 minor (R4-01..04). Total 43, none open.

| ID | Finding | Disposition (where) |
| --- | --- | --- |
| R023-01 | zero endpoints produce invalid plans | canonicalisation (§3, §4.5) |
| R023-02 | dead non-target terminal | scoring-time feasibility check (§4.2) |
| R023-03 | FW gap invalid (post-state rate ≠ aggregate marginal) | removed; no certificate claimed (§5.1) |
| R023-04 | global token-DAG admission | admission rule (§5.1) |
| R023-05 | endpoints missing; search is local | exact endpoints always evaluated; "local heuristic" (§4.4) |
| R023-06 | fixed wiring ≠ SCO | fixed funding topology domain (§3) |
| R023-07 | seeding / attribution | explicit seed + positive-first-hop rule (§5.1) |
| R023-08 | quote accounting | one attempt ledger (§4.6) |
| R023-09 | unequal budgets / confounded control | separate matched controls (§5.3) |
| R023-10 | degenerate sign test | descriptive only, no p-values (§8) |
| R023-11 | prior art / controls | PRIME-Flow, L07, repair; M4/S4/REP arms (§2, §8) |
| R023-12 | objective scope | gross-only (§6) |
| R023-13 | percentile convention | nearest rank (§7) |
| R023-14 | share exactness | exact `Fraction` shares (§4.1) |
| R023-15 | source corrections | corrected source table (§1, [`sources.md`](sources.md)) |
| R023-16 | artifact self-containment | pinned probe ([`probe/`](probe/)) |
| R023-17 | exclusions overstated | deferred static follow-ups (§11) |
| R023-18 | r̄ is not a marginal bound | fresh δ quotes on post-states, no pruning (§5.1) |
| R023-19 | exposure / provenance | `previously_exposed`, tuning-informed nominees (§8) |
| R2-01 | cycle after polish | all-steps DAG invariant + topology rebuild (§5.1, §4.5) |
| R2-02 | budget identity / retention | incumbent holder, guarded phases, no free materialisation (§4.5, §4.6) |
| R2-03 | joint ledger / unmatched control | separate ledgers, exact call matching (§5.3) |
| R2-04 | target residual topologies | canonicalise + `unsupported_topology` refusal (§3) |
| R2-05 | zero / endpoint aliasing on the grid | exact rational cache keys, grid index 0 = t = 0 (§4.4) |
| R2-06 | inference and decision rules | inference withdrawn; dispositions defined (§7, §8, §10) |
| R2-m1 | source pins | single pinned probe and hashes ([`probe/SHA256SUMS`](probe/SHA256SUMS)) |
| R2-m2 | recorded metadata | every probe row stores its canonical plan, full plan hash and `shared_sequential` reuse label ([`probe/results/`](probe/results/)) |
| R2-m3 | Brent savings / overhead | paired overhead ratios (§7) |
| R2-m4 | attribution of "100x" | attributed to U1 only (§1) |
| R2-m5 | Brent sentinel warnings | finite relative penalty +1.0 (§4.4, F9) |
| R2-m6 | implicit definitions | collision-checked fund prefixes, PF grouped scaling (§5.1, F12) |
| R3-01 | refusal reports wrong gross | true gross Σ(produced − consumed) over target funds (`ledger_gross`) (§3, F13) |
| R3-02 | work control not work-matched; incomplete control matrix | hard `work_target`, own wall allowance, both modes and truncated rows (§5.3, F15) |
| R3-03 | PF scales a perturbed seed | proportional seed, original vector, last-positive remainder (§5.1, §4.2, F14) |
| R3-m1 | time stop mislabelled as quotes | caught reason preserved (§5.1, F16) |
| R3-m2 | "90 %" vs "76 %" | ≈ 76 % wording (§7) |
| R3-m3 | moving reference list / stale analysis | frozen 23-run reference list (§7) |
| R3-m4 | weak fixture gate | fixtures exit non-zero; exact retention values (§9 F7) |
| R3-m5 | status vocabulary | status mapping (§6) |
| R4-01 | "E1 unchanged" wording | qualitative conclusions unchanged, tables rerun, v4 authoritative (§7) |
| R4-02 | capped call control | disclosed; invocations started vs completed calls (§5.3, §7) |
| R4-03 | stale probe comments / PF docstring | this contract is normative (`w_orig·(1−t) ⊕ t`, §5.1); the pinned probe stays byte-identical to its reviewed hash ([`probe/README.md`](probe/README.md)) |
| R4-04 | E1-truncated rows | "activation/control not reached: E1 truncated" (§5.3) |

## 13. Residual risks

Single block; tuning-selected nominees; M4 / S4 / REP bases not probed (no local records); heuristic
label oracle (one label per (layer, token); top-3 is not the global top-3 admissible); local line
search; E2-full tail cost; probe timing not authoritative; the cooperative deadline does not replace
the worker's hard limit.
