# Round 3 independent review — 0.2.3 research contract

## Verdict

**DISAGREE: three major items remain—R3-01 (refusal returns the wrong gross), R3-02 (control matching and budgets), and R3-03 (PF does not preserve the original flow at its zero-new-branch endpoint).** The earlier activation-cycle blocker is fixed, as are the original zero-budget exception, completed-exchange retention, transfer-key collision and Brent sentinel warnings. I reproduced all 21 supplied fixtures, reran all 96 IG Brent R2 cases with identical non-timing results, and independently replayed all **960 saved plans** with matching gross and hashes. The revised conclusion that E1 loses to C100 on case counts at roughly equal paired work is supported; the remaining defects are in boundary behavior, experimental controls and the PF ablation's interpretation, not corruption of those corpus outputs.

Reviewed repository: `95d0744db23d19d14f65eafd8512b05d84ec9858`. Reviewed draft: `/tmp/research-023-draft/draft-v3.md`. Reviewed probe SHA: `e211106ad4b132612129ad24d39725f8f9d086cfe8c39c17239a0d964ef2401e`. The previous report is preserved at [r3/review-r2.md](/tmp/research-023-review/r3/review-r2.md). This report is written to the authoritative `/tmp/research-023-review/review.md`, overriding the alternative round-specific filename.

## 1. Closure matrix

Code line references in this report are to `/tmp/research-023-draft/probe_v3/polish3.py` unless otherwise stated.

| Prior item | Status | Evidence |
|---|---|---|
| **R2-01 — activation cycle** | **closed** | `union_with_branch:358–360` checks the graph of every potentially active step. Initial canonicalization removes dormant steps; successful calls rebuild the topology at lines 300/416/449. `Holder.offer:178–185` additionally checks structure, and activation acceptance independently replays at lines 408–409. Fixtures F10/F11 reproduce: reverse-edge union refused; previous end-to-end cycle case now returns **ok, 1,089,662 ≥ 99,890**. All 960 saved plans replay validly. |
| **R2-02 — budget identity/retention/accounting** | **partially** | Initial Holder construction needs no quotes; reconstruction is inside the budget guard; stored plans eliminate free final materialization. F6 passes cap 0–3 with ledger=seam. F7 outputs **342 at cap 80**, **461 at cap 140**. My sweep of **100 activation quote-cap cut points** found zero invalid plans, regressions, quote-count mismatches or cap overruns. Remaining wall-budget/control issues and incorrect time-stop labeling are R3-02/R3-m1. |
| **R2-03 — matched controls** | **partially** | Separate quote ledgers now prevent controls from consuming treatment quotes; actual call counts match on **96/96** full-E2 rows. But the work control overshoots the treatment's E2 spend on **17/96**, and wall budgets are not independently matched. PF and truncated treatments receive no controls. See R3-02. |
| **R2-04 — target residual topology** | **partially** | Zero-reference target consumers are canonicalized correctly; the supported-domain restriction and `partially_consumed_fund` refusal are explicit. The refusal's end-to-end result still computes gross incorrectly: actual incumbent **89**, reported **0**, R3-01. F5 only tests the helper's refusal reason, so it misses this. |
| **R2-05 — transfer-key aliasing/grid** | **closed** | Exact rational keys separately evaluate zero and both endpoints at lines 225–227; interior grid and deterministic tie rule are declared. My both-small-shares check evaluates `0`, `1/10^12`, `−2/10^12` separately and chooses zero on a tie. The ordinary small-share fixture also passes. This fixes key aliasing; it does not make a zero-weight remainder leg necessarily receive zero raw units, relevant to R3-03. |
| **R2-06 — inference/decisions** | **closed** | Draft §5 withdraws significance claims, makes questions descriptive, records directed-pair family results, and supplies reject/inconclusive/keep rules. All current tuning baselines are positive, with no missing or truncated result. Implementation-ready status mapping still needs the minor clarification R3-m5. |
| **R2-m1 — source pins** | **closed** | All ten v3 arm JSONs name the same present script hash and record Python/SciPy/NumPy versions. V2 pins are explicitly superseded. Saved source/result identities are in [r3/SHA256SUMS](/tmp/research-023-review/r3/SHA256SUMS). |
| **R2-m2 — plan/reuse metadata** | **closed** | Rows now contain the complete canonical plan, full hash, source, repeated-pool count, statistics and activation log. Independent reconstruction/replay verified every saved plan and hash. Missing controls for truncated/PF treatments are tracked under R3-02 rather than treated as successful coverage. |
| **R2-m3 — overhead statistics** | **closed** | §4.1 uses paired overhead/work ratios and shows heavy tails. C100 comparison **30/19/47**, family counts **13/14**, work p50 **.952** is reproduced for the nominee. The new 90% arithmetic error is separate, R3-m2. |
| **R2-m4 — source/PS interpretation** | **closed** | The link from Ultra's October-2025 100x statement to later v7.0.5 changes is removed. PS's remaining gap is described as consistent with support limits, not proof. No new unsupported Jupiter mechanism is asserted. |
| **R2-m5 — Brent penalty** | **closed** | The finite relative penalty replaces 1e300; exact cached outputs still select the result. F9 produces zero numerical warnings and finds the expected vicinity; SciPy status/nfev are recorded. The fresh corpus rerun matches. This remains local heuristic optimization, appropriately labeled. |
| **R2-m6 — IDs/canonicalization/PF definition** | **partially** | Collision-checked prefixes, permanent removal after completed calls, grid and tie rules are now specified. The intended current-flow/new-path PF split still uses donor-perturbed old weights, and its zero endpoint need not reproduce the current plan; R3-03. |

Still-relevant round-1 items remain resolved: non-target terminal outputs are rejected; zero references are removed; FW-gap claims are absent; source dates/quotes and prior-art distinctions are corrected; non-gross objectives are excluded by the proposed contract; percentiles use nearest rank; previously exposed report data and tuning-informed nominations remain disclosed. E1/E2 still have no global-optimality or production-latency claim.

## 2. Remaining major findings

### R3-01 — major: unsupported-topology refusal does not return the incumbent's value

**Location:** `run_case:455–466`, especially `gross0` at lines 461–462; draft response R2-04 and §3.

The domain refusal is reasonable, but the refused result computes gross by summing only target funds absent from the consumer map. That is precisely the assumption that does not hold for the refused topology.

I constructed an **evaluator-valid** mixed CPMM/CL plan:

1. S→T consumes 100 and produces 90 in fund `out`.
2. A Uniswap-v3-style CL pool consumes **1** from `out`, returning **0** because it is fee-only dust.
3. The remaining **89 T** is legitimate terminal output.

The CL state uses price 1, liquidity 10^9, fee 3000, tick spacing 60 and admitted `uniswap_v3` semantics. This needs no mock quote or fabricated trace.

[Independent counterexample output](/tmp/research-023-review/r3/corners3.log):

```text
original = ok 89; trace outputs = [90, 0]
returned status = unsupported_topology
reason = partially_consumed_fund:out
returned final = 0; q_total = 0
returned plan unchanged = True
independent replay = ok 89
```

Thus the plan is retained, but its claimed gross is wrong. The main driver's unconditional replay-equality assertion at line 545 aborts on this valid input. F5's helper-only check does not test the promised refusal behavior.

**Required change:** derive the base gross from actual target-fund residual balances, or supply the already evaluated baseline gross explicitly. Keep the plan and value together on all refusal/cap-zero paths. Add this real CL zero-output-consumer fixture end to end through `run_case` and final replay. The requested narrowing of the domain can stay; the refusal must be truthful.

### R3-02 — major: the work control is not work-matched, and the control matrix is incomplete

**Location:** `controls:485–509`, `main:546–549`; draft response R2-03 and §5.2–5.3.

**Quote overshoot.** The work control checks `led.n - q_e1 < treat_q` only before a complete polish call. Its ledger cap remains the global residual cap rather than `min(cap, q_e1 + treat_q)`, so the final call can spend arbitrarily more than its target.

This occurs in the supplied data, not only a fixture:

```text
17 of 96 full-E2 cases overshoot the treatment E2 quote spend.
emp-78c1b0-09bc4e-large-1:
  treatment E2 quotes = 234
  work-control quotes = 24,254
  ratio = 103.6495726496
  converged = False
```

A small independent fixture with target **1 quote** spends **213**, improving gross 199→461. See [work-control-overshoots.json](/tmp/research-023-review/r3/work-control-overshoots.json) and [corners3.log](/tmp/research-023-review/r3/corners3.log).

This gives the control extra work, so it does not manufacture an activation win; however, it invalidates the “work-matched” label and interpretation of the paired losses/trade-offs. The call-matched control is now correctly call-matched on the current 96 complete full-E2 cases.

**Wall budgets.** The treatment gets `900 − recorded base solve time`. Controls instead get a fresh 900-second deadline, without charging base/E1 time. Both controls receive the same absolute deadline, so the work control also loses whatever wall time the call control consumed before it. Their wall allowances are neither independently equal nor matched to treatment.

**Missing arms/outcomes.** `main:546` runs controls only for untruncated `mode == 'activate'`. Neither **ig_actpf** nor **c100_actpf** has controls, and a truncated full treatment gets none. Yet Q3 names both PF and full against controls, and the disposition rule requires control matching. The report schedule must say exactly which base/mode gets which control and how capped calls are classified.

**Required change:** enforce the treatment's extra-work target inside the control's quote ledger, with a visible `work_target` stop distinct from global budget exhaustion; retain the best valid control result at that cut. Alternatively, explicitly rename and register the present whole-call overshooting control, report its overage and remove equal-work claims. Give each control its own correctly charged wall allowance, define PF/full/base control pairing and preserve outcomes for truncated treatments. Recompute the affected control statistics or retain them only under the corrected exploratory label.

### R3-03 — major: PF scales a perturbed seed flow, not the original current flow

**Location:** `union_with_branch:362–367`, `pf_split:430–448`; draft response m6, §4.4 and §5.1.

`union_with_branch` subtracts the seed from **one largest old REQUEST consumer**. PF then uses those already changed old weights as `rest` and multiplies them by `(1−t)/(1−s0)`. Consequently it preserves the relative proportions of the **donor-perturbed seed**, not of the current plan whose flow is supposed to be the fixed side of this ablation.

For a valid 100,000-unit plan split 50,000/50,000:

```text
original shares       = [1/2, 1/2]
PF at new share t = 0  = [4999/9999, 5000/9999, 0]
actual raw inputs     = [49,994, 50,005, 1]
original gross        = 99,994
PF zero endpoint      = infeasible (new CPMM branch receives dust input 1)
```

The last structural consumer receives the integer remainder, even when its nominal share is zero. In this fixture that makes the supposedly zero-new-branch endpoint quote a dust leg and fail, rather than reconstructing the valid original plan. The outer Holder still protects the original incumbent, so this is **not** a demonstrated returned-plan regression. It is a concrete mismatch in the “current flow versus new branch, no other variable moves” ablation and its endpoint semantics.

**Required change:** retain the original REQUEST weight vector for PF and scale that vector by `1−t`, or seed all original consumers proportionally so the same vector is recovered. Materialize the zero-new-branch endpoint as the exact original canonical plan, with a declared integer-remainder rule. If donor perturbation is intentional, name and register it as an additional change and control it; do not describe that arm as holding the original flow fixed. Add endpoint identity checks for several original REQUEST consumers and non-divisible input sizes. The full-polish mode may retain its separately declared donor-seed rule.

## 3. Minor corrections and verification gaps

| ID | Finding | Evidence / requested correction |
|---|---|---|
| **R3-m1** | A wall stop during activation validation becomes `max_quotes` | `activate:410–422` catches any `Budget`, then raises `Budget('max_quotes')`. With no quote cap and a deadline expired exactly at validation, my fixture returns `truncated_by: max_quotes`, q=53, while the actual cause is time. The valid base remains intact. Preserve the caught reason in both log and final status. |
| **R3-m2** | PF captures ~76% of E2-stage gain, not 90% | Exact recomputation: full stage mean **.2667654019 bps**, PF **.2024863539**, ratio **.759042786**. About 91% applies instead to the **total** gain including shared E1 (.676/.740). Paired PF/full total probe quote ratio p50 is **.264629**, so “roughly a quarter” is reasonable when labeled as total probe work, not the activation-stage quote sum. |
| **R3-m3** | Saved analysis and reference manifest are not fully frozen inputs | Rerun `analyze3.py` adds two C100 sections absent from saved `analysis.txt`; all eight pre-existing sections match exactly, and `c100_summary.txt` is byte-identical. The script regenerates `reference-runs.json` by scanning all complete gross runs on this bundle rather than consuming the pinned list, so future runs could change the supposed 0.1.0 envelope. Read and verify the frozen 23-run manifest. Refresh the complete analysis artifact before publication. |
| **R3-m4** | Fixture process exit and retention assertions are too weak | `fixtures3.py` prints failed checks but does not exit nonzero when any `check()` is false. F7 verifies only `final >= base`, so returning 199 after losing improvements could pass the named retention test. Assert the intended retained result at the relevant cut points and fail the process if any check fails. The actual run was 21/21; this is a future regression-detection weakness. |
| **R3-m5** | Research outcome names need mapping to repository statuses | Draft §5.4 lists `unsupported_topology` and `truncated` alongside solver statuses, but `routing/algorithms/base.py:41–62` has neither as a `SolveStatus`. Keep them as explicit scope/stop diagnostics or define their mapping to `unsupported`, cooperative `ok` with truncation, and hard `timeout`. Preserve zero/no-route/unsupported denominators when moving from this successful-base-only probe to the full campaign. |

## 4. Line-by-line audit conclusions

- **Topology and canonicalization:** initial trace canonicalization removes zero references before variables are created. For the supported fully consumed domain, exact rational shares reconstruct all explicit inputs, and the driver checks allocation identity, not only gross. `rebuild` derives incoming totals from the complete stored plan's consumed amounts; this is sound once the supported-domain conservation invariant holds. The refusal's separate gross calculation is the exception R3-01.
- **All-steps DAG invariant:** a valid canonical input is acyclic. Adding a branch is refused unless the union of all potentially active edges is acyclic. A canonical candidate is a subgraph of that union, so zeroing/reactivating variables during a single call cannot create the previous cycle. Rebuilding after the call permits a later union to use only the surviving topology. This addresses the round-2 blocker without changing evaluator semantics.
- **Simulation and Holder:** every positive swap uses sequential physical-pool state and exact quotes; all inputs to a step are merged. Shares conserve each fully consumed fund, positive non-target terminals fail, and canonicalization removes zero references. `Holder.offer` checks structure and retains a complete plan immediately after an accepted **completed exchange**. It relies on the simulator for numerical replay, with the external final evaluator providing an independent check; no free re-quoting is needed to materialize the submitted plan.
- **Budget cuts and `finally`:** the current pair's unfinished search need not be accepted; previous completed exchanges survive. In activation, only independently replayed candidates replace the global holder. If the cap cannot fund that validation, the earlier global plan remains, and the event is logged. Across **100** cap cut points, all returned plans remained valid and never worse, and actual quote calls equaled the ledger. Of these cuts, 93 logged `budget_before_validation`; no invalid partial candidate was promoted. The remaining failure is loss of the *time* reason, R3-m1.
- **Deadline coverage:** a deadline is checked before each exact quote. This is a cooperative guard, not a hard interruption during a long quote or quote-free work; the future worker's hard wall limit still matters. Historical base timing remains descriptive. Control wall-budget charging needs R3-02's correction.
- **Line search:** exact Fraction cache keys fix both ordinary and degenerate endpoint-index aliasing. The zero endpoint wins equal-output ties, followed by smaller absolute transfer and then smaller signed transfer. Both scalar methods are bounded, use the registered interior grid and keep exact cached outputs as the final authority. MAXITER's loop/evaluation meaning differs naturally between golden and SciPy bounded Brent; report the respective counters, not a falsely identical number of evaluations.
- **Brent penalty:** with nonnegative gross and positive reference, feasible objectives are at most +1; +1 is a finite worst-output penalty. Exact feasible-point filtering distinguishes infeasibility from a legitimate zero-output tie. The near-optimum warning fixture passes without overflow warnings. Nothing makes this a global optimizer on integer/non-unimodal objectives.
- **Activation:** token-simple label search remains a declared heuristic. Three retained terminal paths are not necessarily the world's three best admissible paths. Fresh fund prefixes avoid collisions, raw branch flow is checked, and repeated pools are explicitly sequential. Final plans and reuse metadata now permit independent auditing.
- **Controls and PF:** separate quote ledgers and full-mode invocation matching are genuine improvements. Equal-work limits, wall charging and nominal PF zero-endpoint identity are not yet correct, as demonstrated above.

## 5. Reproduction log and draft §4 numbers

All commands ran with the repository read-only. `PYTHONDONTWRITEBYTECODE=1` and `UV_NO_SYNC=1` avoided bytecode or environment synchronization writes. `analyze3.py` writes a reference manifest beside itself, so I copied the supplied scripts/data into `/tmp/research-023-review/r3/analysis-input/` and ran the analysis there; no submitted artifact was changed.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-draft/probe_v3/fixtures3.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
CAP_RESIDUAL=300000 TOL_BPS=1 TIME_LIMIT=900 ROUNDS=2 K=2 \
uv run python /tmp/research-023-draft/probe_v3/polish3.py \
  data/corpus/mantle-5src-101082044/bundle_tuning \
  data/results/20260925T153506524692Z-09f63aea \
  incremental_graph polish brent /tmp/research-023-review/r3/ig_b2.json

PYTHONDONTWRITEBYTECODE=1 python \
  /tmp/research-023-review/r3/analysis-input/analyze3.py "$PWD"
PYTHONDONTWRITEBYTECODE=1 python \
  /tmp/research-023-review/r3/analysis-input/c100_summary.py "$PWD"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-review/r3/corners3.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-review/r3/replay3.py

PYTHONDONTWRITEBYTECODE=1 python /tmp/research-023-review/r3/verify3.py
```

Results:

- **Fixtures:** 21/21 printed PASS, including the prior cycle example and retained improvements.
- **Fresh real arm:** 96/96 rows match after excluding only `probe_seconds`; plans, hashes, gross, counters, source, status and stopping fields are identical. Command path/output metadata naturally differ.
- **Independent saved-plan replay:** **960/960** plans across ten arms evaluate `ok`, reproduce the recorded final gross and match the saved SHA-256.
- **Analysis:** all eight sections in the supplied `analysis.txt` match exactly. The rerun additionally includes the two C100 files now present. `c100_summary.txt` and regenerated reference-run list match byte-for-byte.
- **Metadata:** every arm names the same current `polish3.py` hash; all ten have 96 cases, zero recorded truncations, zero unsupported topology and valid/non-regressing final outputs.

### Confirmed headline figures

| Arm | Mean gain over own base, bps | Improved | Comparator H/E/L | Paired total-work ratio p50 |
|---|---:|---:|---|---:|
| IG golden R1 | .417 | 73/96 | C100: 29/20/47 | .928 |
| IG Brent R1 | .424 | 72/96 | C100: 30/19/47 | .921 |
| IG golden R2 | .466 | 73/96 | C100: 29/20/47 | .985 |
| **IG Brent R2** | **.474** | **72/96** | **C100: 30/19/47** | **.952** |
| C100 + Brent R2 | .321 | 75/96 | C200: 37/18/41 | .932 |
| C100 + PF activation | .477 | 85/96 | C200: 68/11/17 | .972 |
| PS Brent R2 | .300 | 58/96 | — | — |
| PS golden R2 | .315 | 55/96 | — | — |
| IG full activation | .740 | 85/96 | C100: 69/11/16 | 1.263 |
| IG PF activation | .676 | 85/96 | C100: 68/11/17 | 1.018 |

The nominee's overhead **p50 5.7%, p95 124%, mean 24%**, historical gap **.817→.344**, and family net-win/net-loss **13/14** versus C100 reproduce. The C100-based comparisons, their family counts and work p95s **2.221 / 2.262** also reproduce. This supports the deliberate withdrawal of the earlier broad cheap-precision narrative.

Full-E2 control arithmetic also reproduces: call-matched control gain **.018**, activation over it **.248**; work-control gain **.019**, activation over it **.247**; **96/96** invocation matches, **84** work controls ending without another improving call, **145** accepted activation events and **33** DAG rejections. The work-control label needs correction because of its measured overshoot. PF-stage gain is **.202**, full-stage gain **.267**; their ratio is **75.9%**.

Evidence:

- [fixtures3.log](/tmp/research-023-review/r3/fixtures3.log)
- [ig_b2.json](/tmp/research-023-review/r3/ig_b2.json)
- [analysis.txt](/tmp/research-023-review/r3/analysis.txt)
- [c100_summary.txt](/tmp/research-023-review/r3/c100_summary.txt)
- [verification.txt](/tmp/research-023-review/r3/verification.txt)
- [replay3.log](/tmp/research-023-review/r3/replay3.log)
- [corners3.log](/tmp/research-023-review/r3/corners3.log)

## 6. Agreement statement

**DISAGREE: R3-01, R3-02 and R3-03 must change before the design is accurate and complete enough to serve as the 0.2.3 research contract.** Fix the refused incumbent's value; make control budgets/matching and the PF/truncated control schedule truthful; and either preserve the original current-flow group in PF or explicitly register and control the donor perturbation and its endpoint behavior. Correct the 90% sentence and the other minor items alongside those changes.

The E1/E2 experimental direction remains supported, and no new invalid plan was found among the 960 saved corpus results. Remaining empirical limits are one adaptively explored tuning block, no full fresh activation campaign in this review, and unprobed M4/S4/REP compositions. The repository remains at the reviewed SHA with no edits or staged files; the same pre-existing untracked `.pi/` and `docs/external/` entries remain.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Completed the round-three closure matrix, full source audit, supplied fixtures, fresh 96-case Brent arm, both analyses, 960 independent saved-plan replays, and new boundary counterexamples with three major findings and explicit residual risks."
    }
  ],
  "changedFiles": [
    "/tmp/research-023-review/review.md",
    "/tmp/research-023-review/r3/review-r2.md",
    "/tmp/research-023-review/r3/corners3.py",
    "/tmp/research-023-review/r3/replay3.py",
    "/tmp/research-023-review/r3/verify3.py",
    "/tmp/research-023-review/r3/ig_b2.json",
    "/tmp/research-023-review/r3/analysis.txt",
    "/tmp/research-023-review/r3/c100_summary.txt",
    "/tmp/research-023-review/r3/verification.txt",
    "/tmp/research-023-review/r3/work-control-overshoots.json",
    "/tmp/research-023-review/r3/SHA256SUMS"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-draft/probe_v3/fixtures3.py", "result": "passed", "summary": "21/21 checks printed PASS; process exit alone is not a sufficient gate because the script does not fail on a false check."},
    {"command": "CAP_RESIDUAL=300000 TOL_BPS=1 TIME_LIMIT=900 ROUNDS=2 PYTHONPATH=. uv run python polish3.py <tuning> <IG-c50-run> incremental_graph polish brent <review-output>", "result": "passed", "summary": "All 96 result rows match after excluding elapsed time."},
    {"command": "python analyze3.py <repo>; python c100_summary.py <repo> using scratch copies", "result": "passed", "summary": "All previously saved analysis sections reproduce; two additional C100 sections appear; C100 summary and reference-run list match byte-for-byte."},
    {"command": "cmp rerun-analysis.txt supplied-analysis.txt", "result": "failed", "summary": "Supplied analysis predates the two C100 JSON sections; there is no difference in its existing eight sections."},
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-review/r3/replay3.py", "result": "passed", "summary": "960 saved plans independently replay with matching gross and full hashes; zero problems."},
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-review/r3/corners3.py", "result": "passed", "summary": "Demonstrated wrong refusal gross, PF endpoint drift/dust, control overshoot and mislabeled time stop; 100 activation quote-budget cut points passed validity, retention and accounting checks."},
    {"command": "python /tmp/research-023-review/r3/verify3.py", "result": "passed", "summary": "Verified pins, deterministic reproduction, 17 work-control overshoots, 96 call matches and the corrected 75.9% stage-gain ratio."},
    {"command": "git rev-parse HEAD; git status --short; git diff --cached --name-only", "result": "passed", "summary": "Reviewed SHA unchanged; no staged files or repository edits."}
  ],
  "validationOutput": [
    "21/21 supplied fixtures; 96/96 fresh deterministic rows; 960/960 saved-plan independent replays.",
    "Partial target residual: valid incumbent gross 89, unsupported-topology result falsely reports 0.",
    "Work control overshoots in 17/96 cases; maximum 24254 quotes versus treatment E2 spend 234 (103.65x).",
    "PF zero-new-share endpoint changes original 50000/50000 allocation to 49994/50005/1 and can be infeasible.",
    "100 activation quote-cap cuts produced no invalid plan, regression, ledger mismatch or quote overrun.",
    "PF/full activation-stage gain ratio is 0.7590, not approximately 0.90."
  ],
  "residualRisks": [
    "No full fresh activation corpus run; all saved activation plans were independently replayed and synthetic activation budget behavior was exercised.",
    "One adaptively explored tuning block; no new-block generalization or production latency evidence.",
    "M4/S4/REP polishing and report-split compositions remain unprobed.",
    "Control budget and PF ablation interpretation remain unresolved despite valid corpus outputs."
  ],
  "noStagedFiles": true,
  "diffSummary": "Round-three review and scratch reproduction artifacts only under /tmp/research-023-review; no repository modifications.",
  "reviewFindings": [
    "major R3-01: unsupported-topology result does not preserve the incumbent gross.",
    "major R3-02: work target overshoot, unmatched wall allowances and incomplete control schedule.",
    "major R3-03: PF scales donor-perturbed weights and does not reproduce the original current flow at zero new share.",
    "Previous activation-cycle blocker closed; current corpus arithmetic and plan validity verified."
  ],
  "manualNotes": "DISAGREE on the three named major items, not on the empirical result reproduction. Analysis ran from scratch copies because analyze3.py writes reference-runs.json beside itself. The prior report was archived before replacing authoritative review.md; reviewed material and repository files were not edited."
}
```
