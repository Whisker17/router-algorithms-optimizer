# Round 4 independent review — 0.2.3 research contract

## Verdict

**AGREE: the research and design are accurate and complete enough to be written as the 0.2.3 research contract (remaining items are minor and listed).** All three round-3 major findings are closed. I reproduced the **27/27 fixtures**, reran the complete **96-case full activation arm, including its controls**, reproduced both analyses byte-for-byte, and independently replayed all **960 saved plans**. Additional adversarial checks found no new correctness blocker: 1,000 randomized split/merge allocations, both activation modes at every quote-budget cut in small fixtures, exact PF endpoints, work-target limits, independent control deadlines and zero-output baselines all passed.

This agreement covers research-contract readiness, not implementation acceptance, default adoption, global optimality or latency. The three full-mode quote-cap truncations are real and reproduced; PF did not truncate on these tuning cases. The data support the revised framing: E1 adds modest value but loses to C100 on case counts, while activation adds further value at a declared work cost.

Reviewed repository: `95d0744db23d19d14f65eafd8512b05d84ec9858`. Reviewed draft: `/tmp/research-023-draft/draft-v4.md`, with its explicitly inherited v3 sections. Reviewed probe SHA: `b508bcc48d520ba56840e8e9bc4274d548c6d0d962fe2607adfc66c7fc9111b3`. The prior review is preserved at [r4/review-r3.md](/tmp/research-023-review/r4/review-r3.md). No repository or submitted-draft/probe file was modified.

## 1. Closure status

Code locations below refer to `/tmp/research-023-draft/probe_v4/polish4.py` unless otherwise stated.

| Prior finding | Status | Evidence |
|---|---|---|
| **R3-01 — wrong gross on refusal** | **closed** | `ledger_gross:466–475` subtracts all consumed amounts from produced target-token funds. `run_case:483–488` uses that value before domain refusal. F13 reproduces the actual CPMM→CL dust case: original **89**, returned **89**, independent replay **89**, unchanged plan and **0 quotes**. The unsupported-topology path no longer reports zero for a positive incumbent. |
| **R3-02 — controls** | **closed** | `Ledger:47–55` refuses quotes at the separate work target before exceeding it. `controls:513–516` constructs independent deadlines and ledgers for each control; `main:571–575` uses the allowance remaining after base+E1 and runs controls for both modes, including treatment truncations after E1. All **288** saved activation rows have controls; **288/288** invocation counts match; **0** work-target overruns. The fresh full-mode run reproduces its three truncated treatments and all their controls. |
| **R3-03 — perturbed PF group / bad zero endpoint** | **closed** | Proportional seeding at lines 369–370 preserves the old vector. Line 406 stores the original `pf_base`; lines 445–450 scale that vector by `1−t`. The last-positive-share remainder at lines 128–137 makes zero-weight consumers receive exactly zero. F14 passes for two/three consumers and non-divisible inputs. My test intercepts the actual `pf_split` closure: the zero-new-share endpoint reproduces the original canonical plan exactly, and the all-new-share endpoint assigns all 100,003 input units to the new branch. |
| **R3-m1 — time stop mislabeled as quotes** | **closed** | Lines 422–434 preserve and re-raise the caught `Budget` reason. F16 exercises **133** injected time-cut points, including activation validation; no cut is mislabeled `max_quotes`, and every incumbent remains valid and never worse. |
| **R3-m2 — 90% versus 76%** | **closed** | Exact recomputation gives PF/full activation-stage mean-gain ratio **0.7575734821**, consistent with “≈76%.” The total-gain comparison including E1 is separately described as ≈91%. The quoted p50 total-probe costs **2,013 vs 7,451** reproduce. |
| **R3-m3 — moving reference list / stale analysis** | **closed** | `analyze4.py` reads `reference-runs.frozen.json`, asserts 23 entries, verifies bundle/completion/objective and does not regenerate the list. Both saved analysis files now reproduce **byte-for-byte**, including C100 sections. |
| **R3-m4 — weak fixture gate** | **closed** | `fixtures4.py` now exits nonzero if any check fails. F7 asserts the exact retained values: **199** at cap 50, **342** at caps 80/100, **461** at caps 140/180. The actual run reports **27/27 passed**, exit 0. |
| **R3-m5 — status vocabulary** | **closed for the contract** | Draft §0.2/§5.6 maps topology refusal to `ok` with an explicit scope diagnostic, cooperative stops to `ok` plus truncation diagnostics, hard limits to existing `timeout`, and passes through base statuses. The standalone probe still uses its local `unsupported_topology` status; the proposed repository mapping is now explicit. |

Earlier closed items did not regress:

- **R2-01 DAG invariant:** union admission still checks every potentially active step, `Holder.offer` checks structure, and accepted activations receive an independent metered replay. F10 rejects the reverse-edge cycle; F11 returns valid gross **1,089,662** in the former failing scenario.
- **R2-02 retained incumbent:** accepted completed exchanges still update the Holder before subsequent risky work. Exact cap-cut retention checks pass; no free final materialization was reintroduced.
- **R2-05 exact keys:** the remainder change does not alter rational transfer keys, explicit endpoints or the tie rule. Tiny-share fixtures still evaluate the incumbent and endpoints distinctly.
- Source attribution, gross-only scope, fixed-funding-topology limitations, removal of FW claims, descriptive statistics, nearest-rank percentiles, exposure labels and prior-art distinctions remain intact.

## 2. Audit of changed code

### Gross and refusal

`ledger_gross` uses the recorded trace's resolved integer inputs, including zero references, so it agrees with the evaluator's target-token remaining-balance rule. Initial zero-step canonicalization does not change those balances. The base plan and gross are available before any guarded quote, preserving cap-zero behavior and truthful refusal. `shares_or_refusal` continues to enforce the declared fully consumed-fund domain; it does not silently normalize a partially consumed target fund.

### Remainders and canonicalization

For each fund, the share vector sums exactly to one and is nonnegative under the internal construction and pairwise transfers. Therefore at least one share is positive. `simulate` floors every other allocation and assigns the remaining integer balance to the **last positive share**. This conserves the entire fund, gives zero-weight consumers zero input, and leaves the remainder on an explicit valid allocation.

Canonicalization removes zero references and steps before evaluator submission. Positive merged inputs remain merged, and repeated physical pools still replay sequentially. Rebuilding from a completed canonical plan remains sound within the fully consumed domain. My 1,000-case randomized DAG test included zero shares in different positions, two incoming funds, split/merge steps, repeated use of a physical pool and non-divisible amounts: every simulated result matched independent evaluator replay and survived rebuilding without changing gross.

### PF and donor seeding

Full mode retains its declared largest-consumer donor seed. PF now uses proportional seeding and the original canonical REQUEST vector, so varying the new branch's total share no longer perturbs the original group's relative proportions. At the zero-new-share endpoint the new branch gets **zero raw units**, and the old canonical plan is reproduced. At the other endpoint all old source allocations become zero and the new branch receives the full request. Intermediate-fund variables remain fixed in PF, as intended for this ablation.

The all-steps DAG check still occurs before either seed mode is evaluated. Positive-flow attribution remains based on actual integer input to the new branch, not merely on a positive rational parameter.

### Ledgers, controls and deadlines

The work target is checked before executing a quote, independently of the global quote cap and cooperative deadline. A target of zero cannot buy a free positive quote. Each control owns its ledger, while its shared E1 quote spend is pre-charged. The treatment's `q_total` no longer includes either control.

Controls now receive separate deadlines with the same declared remaining wall allowance. Instrumentation observed fresh allowances of approximately **7 seconds for each control**, rather than a shared absolute deadline. The main driver also subtracts the recorded base time and measured E1 time before forming that allowance. This is still a cooperative prototype clock; it does not substantiate production latency or replace the future worker's hard wall enforcement.

The work control may stop before its target when another polish call finds no improvement. That is the registered local stopping rule, not a global-optimality certificate. The call control matches **optimizer invocations**; a final invocation can itself be cut by the global budget, which must remain visible in the report.

### Activation `finally` and retention

An unfinished scalar search does not replace the candidate Holder; prior completed accepted exchanges survive. Before a candidate replaces the global Holder, activation performs `evaluate(..., quote=ledger.quote)` and checks exact gross equality. If validation cannot fit the remaining budget, the previous global incumbent remains. The caught reason is preserved, including `time`.

The new remainder rule did not weaken these invariants. My additional sweeps covered every quote-cap cut in two small complete activation runs:

- full mode: **153/153** cut points valid, never worse, ledger=actual quote seam;
- PF mode: **108/108** cut points valid, never worse, ledger=actual quote seam.

Where E1 completed, I also ran the controls at each cut and verified their work targets. No new blocker was found.

## 3. Remaining minor items

These do not prevent writing the contract, but should be corrected or clarified in the consolidated document.

| ID | Severity | Finding and evidence | Recommended wording/action |
|---|---|---|---|
| **R4-01** | minor | “E1 unchanged from v3” and “E1 results are unchanged” are not literally true. The A0 Brent R2 mean changes from .474 to .473, and its `emp-09bc4e-201eba-large-3` output changes by −20 raw units. IG Brent R1 and golden R2 change one row each; C100+Brent R2 changes two rows. These are differences versus v3, **not regressions against the original base**. | Say **“E1's qualitative conclusions are unchanged; all tables were rerun under the new remainder rule.”** Keep v4 values/pins authoritative. |
| **R4-02** | minor | One **call-count control** in `c100_actpf.json`, `emp-78c1b0-deadde-medium-4`, stops at `max_quotes`. Its invocation count still matches **2/2**, but the second invocation is incomplete. The PF treatment itself does not truncate. Analysis records this correctly; draft §4 emphasizes the treatment and work-control stop counts. | Add a short disclosure of this capped call control, and consistently distinguish **invocations started** from **completed optimizer calls**. The primary work-target control remains separately reported. |
| **R4-03** | minor | Several script headers still say “v3”/`fixtures3.py`; the `pf_split` docstring retains the old normalization formula even though lines 445–450 implement the corrected original-vector scaling. | Update comments and command examples to v4 and `w_orig*(1−t) ⊕ t`; no algorithm change is required. |
| **R4-04** | minor | Controls are available only once an E1 snapshot exists. If the budget ends during initial reconstruction/E1, `e1 is None` and the driver cannot instantiate an activation-stage control. Current corpus E2 rows all finish E1, so all **288** have controls. | In the contract, label these future cases **“activation/control not reached: E1 truncated”**, preserve their denominators, and do not treat absent controls as completed matched comparisons. This is a reporting clarification, not a defect in the current data. |

For publication, consolidate the inherited v3 sections and v4 changes into one contract rather than requiring readers to resolve several draft versions. Planned implementation gates for LB behavior, incomplete CL coverage, the real strategy wrappers and M4/S4/REP compositions remain future work; this review does not claim they have run.

## 4. Reproduction and numerical confirmation

All work stayed outside the repository. The fresh invocation used the original v4 script with an explicit review output path; no behavioral wrapper or probe edit was used.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-draft/probe_v4/fixtures4.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
CAP_RESIDUAL=300000 TOL_BPS=1 TIME_LIMIT=900 ROUNDS=2 K=2 \
uv run python /tmp/research-023-draft/probe_v4/polish4.py \
  data/corpus/mantle-5src-101082044/bundle_tuning \
  data/results/20260925T153506524692Z-09f63aea \
  incremental_graph activate brent /tmp/research-023-review/r4/ig_act.json

PYTHONDONTWRITEBYTECODE=1 python \
  /tmp/research-023-draft/probe_v4/analyze4.py "$PWD"
PYTHONDONTWRITEBYTECODE=1 python \
  /tmp/research-023-draft/probe_v4/c100_summary.py "$PWD"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-review/r4/corners4.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache \
uv run python /tmp/research-023-review/r4/replay4.py

PYTHONDONTWRITEBYTECODE=1 python /tmp/research-023-review/r4/verify4.py
```

**Fresh full activation:** all **96 result rows match exactly after excluding only `probe_seconds` and `e1_seconds`**. This includes plans/hashes, gross, counters, activation logs, three quote-cap truncations, and both control results. Expected command/output-path metadata differ. No timing verdict is drawn from this run.

The reproduced treatment truncations are:

- `emp-78c1b0-201eba-low-3`
- `emp-78c1b0-201eba-medium-1`
- `emp-78c1b0-deadde-low-2`

All report `max_quotes`, keep validated incumbents and have both controls.

**Artifact-wide verification:** all ten arms use the current script hash; all **960 saved plans** independently replay with exact matching gross and full plan hashes. There are **288 activation/control rows**, **288/288 matched invocation counts**, **zero work-target overruns**, and no base+attempt quote total above 300,000. Both `analysis.txt` and `c100_summary.txt` reproduce byte-for-byte from the frozen 23-run reference list.

### Draft §4 confirmed figures

| Arm | Mean gain over own base, bps | Improved | Comparator H/E/L | Paired total-work p50 |
|---|---:|---:|---|---:|
| A0 + golden R1 | .417 | 73/96 | C100: 29/20/47 | .928 |
| A0 + Brent R1 | .424 | 72/96 | C100: 30/19/47 | .921 |
| A0 + golden R2 | .466 | 73/96 | C100: 29/20/47 | .985 |
| **A0 + Brent R2** | **.473** | **72/96** | **C100: 30/19/47** | **.952** |
| C100 + Brent R2 | .321 | 75/96 | C200: 37/18/41 | .932 |
| PS + Brent R2 | .300 | 58/96 | — | — |
| PS + golden R2 | .315 | 55/96 | — | — |
| E2-full on A0 | .740 | 85/96 | C100: 68/11/17 | 1.263 |
| E2-PF on A0 | .675 | 85/96 | C100: 68/11/17 | 1.018 |
| **E2-PF on C100** | **.476** | **85/96** | **C200: 68/11/17** | **.972** |

The tables' percentile, overhead and directed-family summaries also reproduce. In particular, E2-PF on C100 has **25 family net wins / 4 net losses** versus C200. This supports it as the efficient tuning nominee, not as a generalization result or default-adoption decision.

| Activation arm | Mean gain over call-count control | Improved / worse | Mean gain over work-target control | Improved / worse | Work-control stops |
|---|---:|---:|---:|---:|---|
| Full on A0 | .248 bps | 74 / 1 | .247 bps | 74 / 3 | converged 83, work_target 13 |
| PF on A0 | .184 bps | 71 / 4 | .199 bps | 74 / 3 | converged 43, work_target 53 |
| PF on C100 | .145 bps | 73 / 1 | .152 bps | 73 / 2 | converged 39, work_target 57 |

Exact activation-stage means are **.2665853775 bps** for full and **.2019580127 bps** for PF on A0, ratio **.7575734821**. The revised ≈76% statement is correct. Full-mode outcomes **133 accepted / 33 DAG rejections / 78 no-gain-or-zero-flow / 35 stops / 2 budget-before-validation** reproduce.

### Additional adversarial checks

- **1,000 randomized DAG allocations:** evaluator equality, exact fund conservation, zero-share input=0 and rebuild identity; zero failures.
- **Actual PF closure endpoints:** original canonical plan at new share zero, new branch receives all input at share one; both replay validly.
- **Work targets 0–30:** no overrun; summed new control quote counts equal the actual metered seam count.
- **Independent deadlines:** each control receives a fresh approximately 7-second test allowance.
- **Full/PF quote-cap sweeps:** **153 + 108** cut points, all valid/never worse with exact meter accounting; controls also respect targets wherever E1 completed.
- **Zero-output CL baseline:** all three modes at cap 0, cap 1 and uncapped—**9/9** valid, gross zero retained, meter counts exact.

Evidence is preserved in [fixtures4.log](/tmp/research-023-review/r4/fixtures4.log), [ig_act.json](/tmp/research-023-review/r4/ig_act.json), [analysis.txt](/tmp/research-023-review/r4/analysis.txt), [c100_summary.txt](/tmp/research-023-review/r4/c100_summary.txt), [corners4.log](/tmp/research-023-review/r4/corners4.log), [zero-output.log](/tmp/research-023-review/r4/zero-output.log), [replay4.log](/tmp/research-023-review/r4/replay4.log), [verification.txt](/tmp/research-023-review/r4/verification.txt) and [SHA256SUMS](/tmp/research-023-review/r4/SHA256SUMS).

## 5. Agreement statement

**AGREE: the research and design are accurate and complete enough to be written as the 0.2.3 research contract (remaining items are minor and listed).** Incorporate R4-01–R4-04 while consolidating the contract; none requires changing the accepted experimental direction or reopening the earlier major findings.

Residual limits remain: one adaptively explored tuning block; descriptive rather than inferential conclusions; heuristic local optimization and label search; unprobed M4/S4/REP compositions; and no production-latency or Jupiter-equivalence claim. The repository stayed at the reviewed SHA, with no edits or staged files and only the pre-existing `.pi/` and `docs/external/` untracked entries.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Closed all round-three findings with code and execution evidence; reproduced fixtures, the complete 96-case full activation arm and controls, both analyses and 960 saved-plan replays; additional adversarial checks found no new blocker."
    }
  ],
  "changedFiles": [
    "/tmp/research-023-review/review.md",
    "/tmp/research-023-review/r4/review-r3.md",
    "/tmp/research-023-review/r4/corners4.py",
    "/tmp/research-023-review/r4/replay4.py",
    "/tmp/research-023-review/r4/verify4.py",
    "/tmp/research-023-review/r4/ig_act.json",
    "/tmp/research-023-review/r4/analysis.txt",
    "/tmp/research-023-review/r4/c100_summary.txt",
    "/tmp/research-023-review/r4/verification.txt",
    "/tmp/research-023-review/r4/SHA256SUMS"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-draft/probe_v4/fixtures4.py", "result": "passed", "summary": "27/27 passed, exit 0; strengthened retention assertions and failure exit verified."},
    {"command": "CAP_RESIDUAL=300000 TOL_BPS=1 TIME_LIMIT=900 ROUNDS=2 K=2 PYTHONPATH=. uv run python polish4.py <tuning> <IG-c50-run> incremental_graph activate brent <review-output>", "result": "passed", "summary": "All 96 non-timing rows match exactly, including three quote-cap truncations and both controls."},
    {"command": "python analyze4.py <repo>; python c100_summary.py <repo>; cmp with supplied outputs", "result": "passed", "summary": "Both analyses reproduce byte-for-byte from the frozen reference list."},
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-review/r4/replay4.py", "result": "passed", "summary": "960 saved plans independently replay with matching gross and full hashes."},
    {"command": "PYTHONPATH=. uv run python /tmp/research-023-review/r4/corners4.py", "result": "passed", "summary": "1000 randomized DAG cases, PF endpoints, 31 work targets, separate deadlines and 261 full/PF quote-budget cuts all passed."},
    {"command": "Read-only uv Python CL zero-output fixture", "result": "passed", "summary": "9/9 mode/budget combinations retained valid zero-output incumbents with exact accounting."},
    {"command": "python /tmp/research-023-review/r4/verify4.py", "result": "passed", "summary": "288 control rows, 288 invocation matches, zero work-target overruns; all quote totals within the global cap."},
    {"command": "git rev-parse HEAD; git status --short; git diff --cached --name-only", "result": "passed", "summary": "Repository unchanged at 95d0744; no staged files."}
  ],
  "validationOutput": [
    "27/27 supplied checks; full 96-case activation reproduction identical except elapsed-time fields.",
    "960/960 independent saved-plan replays and hashes verified.",
    "288/288 control invocation matches and zero work-target overruns.",
    "Prior refusal counterexample now returns true gross 89; PF zero endpoint reproduces the original plan.",
    "1000 randomized split/merge allocations and 261 activation quote-cap cuts passed.",
    "Three full-mode quote-cap truncations reproduce and retain valid incumbents."
  ],
  "residualRisks": [
    "Evidence is a single adaptively explored tuning block and supports descriptive conclusions only.",
    "M4/S4/REP compositions and the final report campaign remain unprobed.",
    "One C100 PF call-count control hits max_quotes; its matched count is invocations started, not completed calls.",
    "E1 has small raw differences from v3 despite unchanged qualitative conclusions; wording should reflect that.",
    "No production-latency, global-optimality or Jupiter-equivalence evidence is claimed."
  ],
  "noStagedFiles": true,
  "diffSummary": "Review and scratch evidence only under /tmp/research-023-review; no repository or submitted-probe modifications.",
  "reviewFindings": [
    "No unresolved blocker or major finding; AGREE with research-contract readiness.",
    "minor R4-01: replace literal E1-unchanged wording with unchanged qualitative conclusions.",
    "minor R4-02: disclose the one capped C100 PF call-count control and distinguish started from completed invocations.",
    "minor R4-03: update stale v3 headers and PF docstring.",
    "minor R4-04: label activation controls not reached when E1 itself truncates."
  ],
  "manualNotes": "AGREE: the research and design are accurate and complete enough to be written as the 0.2.3 research contract; remaining items are minor and listed. Fresh full activation included both controls and matched every non-timing field. The previous report was archived before replacing authoritative review.md."
}
```
