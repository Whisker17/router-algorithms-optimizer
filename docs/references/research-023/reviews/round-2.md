# Round 2 independent review — 0.2.3 research draft

## Verdict

**DISAGREE.** V2 fixes much of the source attribution, domain framing, integer initialization and reporting, and the reported tuning numbers are reproducible. I reran **all 96 IG Brent R2 cases**: every result row matches `ig_b2.json` exactly, and rerunning `analyze2.py` reproduces `analysis.txt` byte-for-byte. Nevertheless, a full synthetic activation run accepts a plan that final replay rejects for an economic token cycle; budget handling and the allegedly matched activation control also remain incorrect. These are mechanism and evidence problems that must change before this becomes the research contract, rather than minor implementation details.

Reviewed: `/tmp/research-023-draft/draft-v2.md`, all of `probe_v2/polish2.py` and `analyze2.py`, all seven result JSONs, and the relevant unchanged repository semantics at `95d0744db23d19d14f65eafd8512b05d84ec9858`. Source corrections were checked against the public pages retained from round 1; no further web fetch was needed. All review writes are under `/tmp/research-023-review/`. The authoritative report path is this file; the previous report is preserved at [r2/review-r1.md](/tmp/research-023-review/r2/review-r1.md).

## 1. Disposition of R023-01 through R023-19

“Closed” means the specific original defect is resolved; it does not imply that every broader claim about the replacement mechanism holds. Code locations below refer to `/tmp/research-023-draft/probe_v2/polish2.py` unless another file is named.

| Original finding | Status | Evidence and remaining work |
|---|---|---|
| **R023-01 — drained zero references** | **closed** | Lines 130–137 remove zero references and steps. Independent fixtures for both endpoint orders now evaluate `ok`, producing 90 and 9 respectively. The original drained-fund bug is fixed. The later activation-cycle problem is different, R2-01 below. |
| **R023-02 — non-target terminal counted as output** | **closed** | Lines 120–126 reject positive unconsumed non-target funds. The round-1 dead-intermediate fixture now returns `None` during scoring. A newly identified valid target-residual case still breaks the broader “any evaluator-valid plan” claim; see R2-04. |
| **R023-03 — invalid FW diagnostic** | **closed** | Draft response row 03 and §6 remove the FW gap, its stop rule and its gate. No FW certificate remains in the probe. The distinction between sequential post-state quoting and aggregate derivatives is acknowledged. |
| **R023-04 — global DAG admission** | **partially closed** | Lines 340–343 check a seeded canonical plan and proceed to another candidate on rejection. But lines 344–356 do not revalidate the polished plan, and dormant steps remain in the optimization topology. A complete synthetic `run_case` accepts an X→Y→X cycle, R2-01. |
| **R023-05 — endpoints/local search** | **partially closed** | Lines 160–191 evaluate endpoint keys and use local golden/bounded Brent search; exact integer outputs select the winner. However, a sub-1e-9 share makes the endpoint key coincide with `k=0`, so the supposedly guaranteed incumbent evaluation is missing, R2-05. Brent's 1e300 sentinel also causes reproducible overflow/invalid warnings; handling is not yet specified beyond acknowledging them. |
| **R023-06 — support versus fixed wiring / execution domain** | **partially closed** | Draft §3 correctly limits E1 to fixed funding topology and names E2's sequential reuse. The response matrix nevertheless says `pool_reuse` is recorded per case: no JSON row contains that field. The interior transfer grid `D=10^9` is also absent from the purported normative mechanism, R2-05/R2-m2. The SCO overstatement is fixed. |
| **R023-07 — seeding and attribution** | **partially closed** | Lines 285–291 transfer an exact rational share from the largest REQUEST consumer, ties by lowest index; no A+1 normalization remains. Line 347 checks positive branch flow, and logs include novelty/flow for accepted proposals. But acceptance does not verify the final replay, logs disappear on truncation, and the extra-polish control is not actually matched. Positive flow alone does not isolate causation, R2-01/R2-03. |
| **R023-08 — quote counting / budget** | **open** | Search, post-state construction and seed evaluation now use `Meter.quote`, including `evaluate(..., quote=meter.quote)`. Initialization at lines 301–310 is outside `try`; cap 0/1 raises uncaught `Budget`. Line 361 reconstructs the final plan using a fresh unlimited meter. Mid-polish budget exhaustion discards already completed improvements. No wall limit or publication callback exists in the probe. See R2-02. |
| **R023-09 — fairness / extra-round confound** | **open** | C100/C200 are correctly relabeled finer-granularity controls. But control and treatment use the same meter, and the control runs once per outer iteration while treatment may polish zero to three proposals. **22/89** complete rows have unequal call counts, contrary to draft response row 09. See R2-03. |
| **R023-10 — hypotheses/statistics** | **partially closed** | Draft §5.2 removes the degenerate own-base sign test, names C100, and provides a tuning-informed Brent nomination rule. However, §6.3 says differing trajectories make a sign test “valid,” which is insufficient: binomial sign inference also requires justified independent/exchangeable signs. Directed-pair families, zero denominators, truncated outcomes and multiplicity are not fully specified. See R2-06. |
| **R023-11 — missing prior work/controls** | **closed** | Draft §2 adds PRIME-Flow and L07; §5.3 adds repair, M4±polish and S4±polish; §6.2 adds the restricted current-flow/new-path ablation. CFMM polish is appropriately optional and CP+CL-scoped. The precise restricted ablation parameterization should still be written into the contract. |
| **R023-12 — objective scope** | **closed** | Draft response row 12 and §7 explicitly make new IDs gross-only and non-gross objectives `unsupported (objective)`. Optional cost replay is descriptive with applicability flags. The probe consistently uses `gross_only()`. |
| **R023-13 — percentile convention** | **closed** | `analyze2.py:14–19` uses nearest rank. Its full output reproduces exactly, including corrected historical IG p95 gaps **3.684 / 4.003 / 7.756 bps**. |
| **R023-14 — fixed SCALE reconstruction** | **closed** | Lines 75–84 use exact `Fraction(amount, produced)` without a finite initialization scale. All seven JSONs have 96/96 `v0 == recorded`; the fresh Brent run confirms it. The different target-residual bug is R2-04, not the original large-integer rounding problem. |
| **R023-15 — source corrections** | **closed** | Draft §1 corrects the JIT date and same-pool denominator, Iris→Metis wording, v7.0.5 entries, Xi/Moallemi authorship, FVO/G-FVO staleness values and PRIME's assumptions. These agree with round-1 fetched sources. A new chronology implication in lines 48–49 should be removed, R2-m4. |
| **R023-16 — self-contained artifacts/diagnostics** | **partially closed** | Inputs/imports no longer depend on `/tmp/sco_probe`; metadata records argv/env/bundle/script hash and `nod` now retains its size stratum. Two result files name script hashes whose source bytes are absent, and all seven truncated activation rows omit the control and activation log. See R2-03/R2-m1. |
| **R023-17 — overstated exclusions** | **closed** | Draft §8 correctly calls zero-loss early exit, exact caching and charged portfolios deferred static follow-ups. Dynamic execution and absent venues remain excluded. |
| **R023-18 — output bound versus marginal bound** | **closed** | Draft response row 18 and §6.1 use fresh post-state δ quotes without bound pruning. No unsafe r̄-only aggregate-marginal prune remains. |
| **R023-19 — exposure/provenance** | **partially closed** | Draft §5.6 declares `previously_exposed`, one frozen comparison, hashes, all statuses and single-block limits. Tuning-informed nomination is disclosed. Final contract still needs explicit failure/truncation analysis, retry/disposition rules and retrievable source/settings pins; present truncated-row omissions and mixed script pins prevent full closure. |

## 2. New or unresolved substantive issues

### R2-01 — blocker: seed admission does not protect the polished activation plan

**Location:** `polish2.py:335–356`; draft §6.1, response rows 04/07/08.

The evaluator is called on the **seed** plan. `polish` subsequently reallocates over the full `Topo`, including zeroed branches omitted by `canonical_plan`. Re-enabling such a branch can introduce a token cycle. Neither `simulate` nor final candidate acceptance checks that cycle. The final `main()` assertion eventually detects it, but the previous valid incumbent has already been replaced in `run_case`.

**Reproduction:** [cycle2.py](/tmp/research-023-review/r2/cycle2.py), [cycle2.log](/tmp/research-023-review/r2/cycle2.log). A synthetic original DAG has S→X→Y→T and S→Y→T, with the former dormant. The admitted new path is S→Y→X→T. All pools are fee-zero CPMMs; input is 10,000, and reserves are:

| Pool/direction | Reserves |
|---|---|
| S→X | 100,000 / 100,000 |
| X→Y | 1,000,000 / 10,000 |
| Y→T | 1,000 / 100,000 |
| S→Y | 1,000 / 1,000,000 |
| X→T | 10,000 / 1,000,000 |

The complete probe run prints:

```text
polished base = 99890
seed gross = 173878, evaluator status = ok
polished candidate = 1089676
new REQUEST flow = 5721; reactivated old REQUEST flow = 3562
act_log = accepted novel_pools=1 flow=5721
final replay_status = invalid_plan
error = economic token cycle: X -> Y -> X
```

This also disproves the draft's statement that cycle-unlock is out of scope under the implemented mechanism: zero edges disappear during admission but remain available for later optimization.

**Required fix:** establish a DAG invariant over all steps the optimizer can reactivate, or rebuild/freeze the topology from a validated canonical plan and explicitly prohibit reactivation of removed steps. Validate each accepted polished plan before publishing/replacing the incumbent; preserve the previous valid plan on a failed replay. Add this complete activation fixture, not only invalid-seed/next-candidate tests. No supplied 96-case JSON is claimed to contain an invalid plan; this is a demonstrated general-domain failure.

### R2-02 — major: budget-zero identity, retained-incumbent behavior and quote totals are false

**Location:** `polish2.py:295–316`, `194–229`, `359–364`; draft §5.1 and response row 08.

Three separate defects remain:

1. **Initialization escapes truncation handling.** The original replay and initial simulation execute before `try`. A one-step valid plan with cap **0 or 1** raises uncaught `Budget`; no identity result or truncated row is returned.
2. **Completed improvements are lost.** `best` updates only after an entire `polish()` returns. A later budget exception unwinds the function and loses prior accepted exchanges. A three-pool fixture has base **199**; completed pair searches find **342**, then **461**. With cap **140**, the returned final is still **199**, `truncated=True`.
3. **Final materialization receives free quotes.** Line 361 uses `Meter(None)`. In a one-step fixture with cap 3, the reported `q_total` is **3** but the actual quote seam sees **5** calls. One is the independent final evaluator, which may legitimately be outside solve accounting; the other reconstructs the submitted plan and belongs inside the attempt unless the complete incumbent/allocations were already saved.

See [corners2.log](/tmp/research-023-review/r2/corners2.log). The code's comment “last validated incumbent (published before risky phases)” is not implemented: there is no publication callback, and `best` may hold a simulated candidate rather than an independently validated plan. The probe also has no 900-second enforcement; only a quote cap is implemented.

**Required fix:** start with the supplied valid base plan available without re-quoting; place every budgeted phase inside the guard; retain/materialize a complete validated incumbent at the defined acceptance points; persist it through exceptions. Charge all solver work to the same meter and distinguish the external final evaluator. Demonstrate cap 0, initialization cuts, mid-pair/mid-round cuts and final-materialization cuts. If prototype timing remains unenforced, say so explicitly instead of presenting its run as a 300k/900-second attempt.

### R2-03 — major: the matched control is neither independently budgeted nor call-matched

**Location:** `polish2.py:320–358`; `analyze2.py:66–80`; draft §4.3, §6.2 and response row 09.

The control calls `polish` on the treatment's meter at line 324. Its work reduces the treatment's remaining budget; `q_total` includes both algorithms. Consequently **7/96 truncations and the reported E2 quote totals are properties of a joint control+treatment procedure**, not E2 alone.

The call matching claim is also false. There is one control polish per outer iteration, but up to three admitted candidate polishes. The logged complete rows prove **22/89 mismatches**. For example:

```text
emp-779ded-c96de2-large-2:
control calls = 2
candidate polish calls = 4
iteration 0: one accepted candidate
iteration 1: three no_gain_or_zero_flow candidates
```

Other cases perform control polish while all three candidates fail admission, so matching fails in both directions. Disclosing unequal pair-set size does not cover unequal call counts or shared budgets.

On `Budget`, `out.update(... control, act_log ...)` is skipped. **All seven truncated rows omit `control`, `act_log` and `activated`**, including six whose final output already exceeds initial polish. Thus the response matrix's 41 admission rejections “over 96 cases” actually counts only the **89 complete rows**. The +.274 bps arithmetic is correct on those rows but is not an isolated, budget-fair activation effect.

**Required fix:** give each arm a separate attempt ledger with identical declared initial budgets and base charging. Specify whether the control matches candidate polish invocations, actual work, or both through separate controls; implement that definition exactly. Keep logs and last control/treatment values on truncation, and preregister how incomplete pairs enter quality/coverage analyses. Recompute §4.3 after the change, or label the current table as the joint exploratory procedure and withdraw its fairness/standalone-cost claims.

### R2-04 — major: “any evaluator-valid plan” still has a reconstruction counterexample

**Location:** `polish2.py:75–84`, `98–106`, `120–126`; draft response row 02 and §3.

An output-token fund may remain a terminal balance even if a later step references it for **zero** input. The evaluator sums its **remaining balance**, not just funds absent from the consumer map (`routing/evaluator.py:517–524`).

Construct a valid plan: S→T consumes 100, produces 90 in `out`; a subsequent T→X step references `out` with amount 0. Evaluator result: **ok, gross 90**. Fraction reconstruction assigns `out` share `[0]`, but `simulate` gives its sole consumer the full remainder **90**, so it returns **infeasible**, not the original plan. The exact output is in `corners2.log`.

This is not a Fraction precision problem; it is an incorrect assumption that every referenced fund is fully consumed. The original non-target terminal rejection is fixed, but the expanded domain claim is still false.

**Required fix:** preserve target-token residual balances in the parameterization, or narrow the supported domain explicitly and validate the restriction before optimization. Canonicalizing the input before reconstructing its topology addresses this zero-reference example; state how any other partially consumed target fund is handled. Preserve the original incumbent for unsupported topology rather than crashing an exploratory run.

### R2-05 — major: the search's zero point and exact endpoints can alias on its hidden grid

**Location:** `polish2.py:26`, `206–219`; draft §5.1 and response row 05.

Interior transfers use a **1e-9 share grid**, despite the draft describing rational/continuous transfers without registering this grid. Bounds are `lo=-floor(s_j*D)`, `hi=floor(s_i*D)`. The endpoint special case is checked before the meaning of zero transfer:

```python
t = si if k == hi else (-sj if k == lo else Fraction(k, D))
```

For `s_i=1/10^12`, `hi=0`, so `F(0)` removes the first consumer rather than evaluating the incumbent. The real-quote fixture shows:

```text
initial shares = [1/1000000000000, 999999999999/1000000000000]
first F(0)     = [0, 1]
incumbent_evaluated_in_search = False
```

If both small shares produce `lo==hi==0`, the two endpoints and the incumbent all share one cache key. Only one can be evaluated. The outer incumbent check still prevents a lower simulator score from replacing it; this is a mechanism/coverage defect, not evidence that the supplied rows regress.

**Required fix:** represent exact endpoint transfers and zero distinctly, for example with rational transfer cache keys or tagged evaluations. Register any interior quantization, endpoint mapping, tie rule (`max((value,k))` currently favors larger k) and termination behavior. Add sub-grid-share fixtures with large raw fund amounts so “small share” does not get mistaken for “zero money.”

### R2-06 — major: inferential and campaign decision rules remain incomplete

**Location:** draft §5.2, §5.6, §6.3; `analyze2.py:22–26`, `50`, `67`.

Replacing the own-base sign test with C100 removes the structural wins-only problem. It does **not** make per-case signs independent: requests share directed token pairs and the same frozen state. “Trajectories differ, so a two-sided sign test ... is valid” in §6.3 is not a sufficient justification. Furthermore, `analyze2.py` calls **emp/nod** a family; the repository's R021 family definition is the **directed token pair**. The six origin/size cells do not supply that breakdown.

Before the contract is fixed, it must specify zero-output denominators, exclusions and interpretation of capped pairs, directed-pair families, and whether the several sign tests are descriptive or decision-making with a multiplicity policy. It also needs an explicit disposition mapping for an unevaluable/refuted quality hypothesis versus a valid experimental mechanism; merely saying “per R021 vocabulary” does not fix the decision rule. This can be resolved without inventing a new statistical study: descriptive finite-corpus results are acceptable if the significance-based claims are withdrawn or properly qualified.

## 3. Additional minor corrections

| ID | Issue | Evidence / correction |
|---|---|---|
| **R2-m1** | Two source pins cannot be resolved from the supplied artifact root | Current `polish2.py` SHA is `78f7a509e53f8b8792f9aa2721af3e5ea3dc67e5e11317731aeea77fe2488aae`. `ig_g1.json` names `c25c37a6…`; `ps_g1.json` names `54a09149…`. Neither corresponding source copy is present. Preserve those versions and explain the changes, or demonstrate replays at the current pin. The critical ig_g2/ig_b2 nominee comparison uses the same current pin. |
| **R2-m2** | Response matrix overstates recorded metadata | No row records `pool_reuse`, despite response row 06. Outputs store a 16-hex repr-based plan hash but not the plan. Truncated rows lose logs as above. Record canonical plans/full hashes, reuse mode, current active support, stage work and stop reason. |
| **R2-m3** | §4.2 overstates Brent's p50 savings and understates some overhead | Ratios of reported p50 quotes give **12.45%** saving for IG R1, **13.92%** for IG R2, **7.41%** for PS R1—not “12–22%” across these rows. Paired extra/base p50 is **7.04%** for IG golden R2, outside the stated ~2–6%. For the Brent R2 nominee it is **5.44%**, but paired p95 overhead is **125.72%**, mean **24.20%**. Keep the useful median result and show its tail. |
| **R2-m4** | New attribution/interpretation overreach | Draft lines 48–49 attach the Oct-2025 Ultra “100x” claim to other changes “(MC v7.0.5).” The sources do not establish that later v7 changes were bundled into that measurement. Cite U1's contemporaneous changes instead. Also, the remaining ~5 bps PS gap is consistent with support limitations but does not prove them: local polishing is not a within-support oracle. |
| **R2-m5** | Brent sentinel handling remains an observed numerical warning, not a resolved rule | Fresh ig_b2 emits overflow/invalid warnings, and a synthetic `line_search` emits 9 such warnings. In that fixture the exact cached winner is still correct. Specify a bounded finite penalty or explicit safe fallback, how SciPy status/maxiter/nonfinite behavior is recorded, and pin SciPy/NumPy versions. Do not assert the warnings corrupted existing rows; they did not prevent exact reproduction. |
| **R2-m6** | Several small but consequential contract definitions remain implicit | Define collision-free new fund IDs (`ACT{it}` can collide with an arbitrary valid input plan), exact grouped REQUEST scaling for the PRIME-Flow-style ablation, and whether canonicalization permanently removes a variable or only changes emitted plans. Distinguish the scalar 1-D iteration limit from whole-polish quote/wall limits. |

## 4. Probe correctness checklist

This covers the full implementation, including code paths not exercised by the empirical rerun.

| Component | Result |
|---|---|
| Trace/topology construction, lines 49–84 | Deterministic fund/consumer order; exact Fraction initialization for fully consumed funds; the valid target-residual exception is R2-04. Initial full allocation identity is not independently checked beyond gross equality. |
| Simulation, lines 87–127 | Sequential physical-pool state, merged input amounts, exact quote checks and positive non-target terminal rejection are correct for the declared fully allocated DAG subset. It does not itself validate DAGs, and its consumer-map terminal rule is too broad. Clamping share allocations should become an asserted invariant or a declared normalization rule. |
| Canonicalization, lines 130–137 | Original zero-reference/zero-step bug is fixed; multiple positive inputs remain merged. Zero-output descendants drop when their resolved inputs are zero. Dormant topology remains available in `Topo`, producing the E2 admission failure. |
| Golden search, lines 147–175 | Endpoint-key evaluation, cache reuse, local bracketing and final exact integer comparison work at ordinary shares. No global optimality is claimed. Endpoint/zero key collisions remain. Ties use larger integer k for the final cached winner. |
| Brent wrapper, lines 176–191 | Correct use of SciPy `method='bounded'` minimization; relative objective avoids converting an entire large raw output directly into the optimization signal. The selected result is the best exact cached output, not blindly SciPy's returned point. Sentinel warnings/status handling and grid semantics remain unresolved. |
| Meter, lines 34–43 | Counts each call before dispatch, including failed quotes; cap is checked before an excess quote. Caller scope, final free simulation and control mixing invalidate the claimed attempt accounting. |
| Pairwise acceptance, lines 194–229 | Pairwise weights conserve the fund and strict simulator-output improvement is required. Rational shares are copied rather than mutated in place on acceptance. A budget exception loses the local accepted state, and E2 candidate validation is insufficient. |
| Candidate generation, lines 235–268 | Deterministic sorted edges/layers, token-simple path search, no traversal through the target, finite hop bound, and exact fresh post-state quotes. One-label pruning is properly acknowledged as heuristic. “Top-3” means the retained search's top target paths, not the globally best three admissible paths. |
| Seed, lines 271–292 | Fixed A+1 bug: exact share transfer, deterministic donor/tie selection and sum-one assertion are correct. The **raw integer** seed still follows floors/remainder, which is fine if described as a rational share seed. Fund IDs need collision avoidance. |
| Admission and positive-flow attribution, lines 330–357 | Seed evaluator correctly uses `quote=meter.quote`; next-candidate behavior exists. Positive first-hop allocation is checked. But no post-polish evaluator call precedes acceptance, and positive flow plus an unmatched control does not isolate activation's contribution. |
| Final replay, lines 361–390 | Independent final evaluator and main's assertions successfully validate all supplied corpus outputs. They cannot repair an invalid accepted incumbent or justify free solver materialization. The synthetic cycle makes main's final assertion fail rather than silently writing a good row. |
| Analysis/provenance | Nearest-rank quantiles and empirical/nod size strata are fixed. The best-known scan currently reproduces the right historical envelope, but it relies on a date-prefix glob and does not explicitly filter `gross_only`, completeness or a frozen run list. Use a manifest of the intended 23 runs and assert objective/cohort. Preserve unconditional statuses and zero-output rows separately. |

## 5. Reproduction and numerical confirmation

The repository remained read-only. Bytecode writing and uv synchronization were disabled. The new probe accepts an explicit output path, so no output redirection wrapper was needed.

```bash
PYTHONDONTWRITEBYTECODE=1 python \
  /tmp/research-023-draft/probe_v2/analyze2.py "$PWD" \
  > /tmp/research-023-review/r2/analysis.txt

cmp /tmp/research-023-review/r2/analysis.txt \
  /tmp/research-023-draft/probe_v2/analysis.txt

TOL_BPS=1 ROUNDS=2 K=2 CAP_RESIDUAL=300000 \
PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache PYTHONPATH=. \
uv run python /tmp/research-023-draft/probe_v2/polish2.py \
  data/corpus/mantle-5src-101082044/bundle_tuning \
  data/results/20260925T153506524692Z-09f63aea \
  incremental_graph polish brent /tmp/research-023-review/r2/ig_b2.json

PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache PYTHONPATH=. \
uv run python /tmp/research-023-review/r2/corners2.py

PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache PYTHONPATH=. \
uv run python /tmp/research-023-review/r2/cycle2.py

PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 \
UV_CACHE_DIR=/tmp/research-023-review/uv-cache PYTHONPATH=. \
uv run python /tmp/research-023-review/r2/verify2.py
```

Environment: **Python 3.13.13, SciPy 1.18.1**. `K=2` in the fresh metadata differs from the supplied polish-only run's unset K, but K is unused in polish mode; all result rows are identical. The final output path is the other expected metadata difference.

### Draft §4.1 — confirmed arithmetic

| Arm | Gain mean / p50 / p95 / max, bps | Improved | Reported extra quotes p50 / p95 / max | Mean gap after |
|---|---|---:|---|---:|
| ig_g1 | .443 / .086 / 1.975 / 7.836 | 73/96 | 972 / 26,185 / 42,051 | .374 |
| ig_b1 | .422 / .086 / 1.976 / 6.530 | 72/96 | 851 / 20,724 / 32,667 | .395 |
| ig_g2 | .479 / .086 / 2.081 / 7.836 | 73/96 | 1,896 / 52,340 / 83,837 | .339 |
| **ig_b2 — fresh rerun** | **.478 / .086 / 2.081 / 7.836** | **72/96** | **1,632 / 41,140 / 63,850** | **.339** |
| ps_g1 | .311 / .016 / 1.306 / 10.026 | 55/96 | 108 / 1,521 / 1,671 | 5.040 |
| ps_b1 | .296 / .016 / 1.306 / 8.246 | 58/96 | 100 / 1,217 / 1,375 | 5.055 |

All §4.1 gap quantiles and the six Brent-R2 origin/size mean gains also match the analysis. Fresh ig_b2: **96/96 exact row equality**, including plan hashes and quote counts; all original reconstruction and final replay assertions passed. All seven supplied artifacts have **96/96** initial gross equality, valid/equal final replay and non-regression. That is **672 recorded rows checked**, with 96 freshly rerun.

### Draft §4.3 — numbers reproduce, interpretation does not

- Complete/truncated: **89 / 7**.
- Control over initial polish: mean **+.010 bps**, p95 **+.039**.
- Activated over control on the 89 complete rows: mean **+.274**, p50 **+.125**, p95 **+.978**, max **+1.437**; **69/89** improved.
- Accepted-event histogram on those 89 rows: **{0:20, 1:6, 2:63}**.
- Recorded complete-row outcomes: **132 accepted, 41 admission rejections, 31 no-gain/zero-flow**.
- All 96 final retained outputs: mean gain **.766 bps**, mean historical gap **.051 bps**, **50** above historical best.
- Joint-procedure reported extra quotes: p50 **11,567**, p95 **265,066**, max **272,007**; max base+extra **300,000**.

These match the saved analysis exactly. They must be labeled with R2-02/R2-03's accounting and selection limitations. Two accepted events report **zero novel pools**; that is allowed for sequential branch reuse, but they must not be presented as new-pool discovery.

Evidence files:

- [analysis.txt](/tmp/research-023-review/r2/analysis.txt): exact analysis reproduction.
- [ig_b2.json](/tmp/research-023-review/r2/ig_b2.json): fresh 96-case result.
- [verification.txt](/tmp/research-023-review/r2/verification.txt): row equality, source pins, control mismatches and overhead calculations.
- [control-call-mismatches.json](/tmp/research-023-review/r2/control-call-mismatches.json): all 22 complete-case mismatches.
- [corners2.log](/tmp/research-023-review/r2/corners2.log): endpoint fixes, target-residual failure, budgets, dropped improvements and zero-key collision.
- [cycle2.log](/tmp/research-023-review/r2/cycle2.log): accepted-invalid full activation run.
- [SHA256SUMS](/tmp/research-023-review/r2/SHA256SUMS): reviewed draft/source/results and fresh-result pins.

## 6. Agreement statement and required changes

**DISAGREE.** The exact blocking/major items that must change are:

1. **R2-01:** maintain DAG validity through polishing and validate before accepting/publishing an activation.
2. **R2-02:** implement budget-zero identity, retain completed valid improvements on truncation, and remove uncharged solver materialization; accurately distinguish the quote-only prototype from the proposed wall-limited implementation.
3. **R2-03:** separate control/treatment ledgers, implement the declared matching rule, preserve truncated diagnostics, and correct or rerun the affected activation table.
4. **R2-04:** support or explicitly exclude evaluator-valid target-residual topologies with a defined incumbent-preserving refusal behavior.
5. **R2-05:** distinguish zero and exact endpoint transfers and register the actual search grid/tie semantics.
6. **R2-06:** finalize the inferential assumptions, true family breakdown, capped/zero-outcome analysis and disposition rules—or keep the conclusions explicitly descriptive.

The source corrections and the core E1 empirical signal are sufficient motivation to continue. They do not justify closing the six items above. Residual risks after this review are the single tuning block, no full fresh activation rerun, missing historical source bytes for two minor arms, and untested M4/S4/repair polishing. No repository file was edited or staged; the same pre-existing `.pi/` and `docs/external/` untracked entries remain.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "Reviewed all 19 prior findings, the complete revised probe and analysis, reproduced 96 Brent R2 cases and the exact analysis output, and reported six concrete blocking/major items plus minor corrections and residual risks."
    }
  ],
  "changedFiles": [
    "/tmp/research-023-review/review.md",
    "/tmp/research-023-review/r2/review-r1.md",
    "/tmp/research-023-review/r2/corners2.py",
    "/tmp/research-023-review/r2/cycle2.py",
    "/tmp/research-023-review/r2/verify2.py",
    "/tmp/research-023-review/r2/ig_b2.json",
    "/tmp/research-023-review/r2/analysis.txt",
    "/tmp/research-023-review/r2/verification.txt",
    "/tmp/research-023-review/r2/control-call-mismatches.json",
    "/tmp/research-023-review/r2/SHA256SUMS"
  ],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "python /tmp/research-023-draft/probe_v2/analyze2.py <repo>; cmp with supplied analysis.txt",
      "result": "passed",
      "summary": "Byte-identical analysis output; every numeric table entry reproduced."
    },
    {
      "command": "TOL_BPS=1 ROUNDS=2 CAP_RESIDUAL=300000 PYTHONPATH=. uv run python polish2.py <bundle_tuning> <IG-c50-run> incremental_graph polish brent /tmp/research-023-review/r2/ig_b2.json",
      "result": "passed",
      "summary": "All 96 result rows exactly match the supplied ig_b2 artifact, including plan hashes, gross and quotes."
    },
    {
      "command": "PYTHONPATH=. uv run python /tmp/research-023-review/r2/corners2.py",
      "result": "passed",
      "summary": "Confirmed original endpoint/dead-terminal fixes and demonstrated new target-residual, budget, incumbent-retention and zero-transfer aliasing defects."
    },
    {
      "command": "PYTHONPATH=. uv run python /tmp/research-023-review/r2/cycle2.py",
      "result": "passed",
      "summary": "Full synthetic activation run accepted gross 1089676 but final evaluator rejected economic cycle X -> Y -> X."
    },
    {
      "command": "PYTHONPATH=. uv run python /tmp/research-023-review/r2/verify2.py",
      "result": "passed",
      "summary": "Checked 672 recorded rows, 22/89 control-call mismatches, missing diagnostics on all seven truncated activation rows, and two unresolved historical script hashes."
    },
    {
      "command": "git rev-parse HEAD; git status --short; git diff --cached --name-only",
      "result": "passed",
      "summary": "Repository stayed at 95d0744 with unchanged pre-existing untracked entries and no staged files."
    }
  ],
  "validationOutput": [
    "96/96 fresh Brent R2 rows identical; analysis.txt byte-identical.",
    "Original zero-endpoint fixtures now valid in both orders; dead non-target output rejected during scoring.",
    "Cap 0/1 raises uncaught Budget; cap 140 returns base 199 despite completed pair improvements to 342 and 461.",
    "22 of 89 complete activation rows have unequal control/candidate polish-call counts.",
    "Seed-valid activation can become an accepted invalid token-cycle plan after polishing."
  ],
  "residualRisks": [
    "No complete fresh activation corpus run; its artifact was re-analyzed and a full synthetic activation failure was reproduced.",
    "Two minor arm artifacts refer to historical script bytes not present in the supplied directory.",
    "Evidence remains one previously explored tuning block; M4/S4/repair polishing and report execution remain untested.",
    "Brent sentinel warnings reproduce even though the supplied corpus output reproduces exactly."
  ],
  "noStagedFiles": true,
  "diffSummary": "Round-two review and scratch evidence only under /tmp/research-023-review; repository unchanged.",
  "reviewFindings": [
    "blocker R2-01: seed-only admission allows later accepted economic token cycles.",
    "major R2-02: budget identity, incumbent retention and quote accounting remain incorrect.",
    "major R2-03: shared control/treatment budget and unequal polish calls invalidate matched-control interpretation.",
    "major R2-04: valid target-residual plans fail reconstruction.",
    "major R2-05: zero and endpoint transfers alias on an undeclared interior grid.",
    "major R2-06: statistical assumptions and campaign outcome/disposition rules need completion."
  ],
  "manualNotes": "DISAGREE with contract readiness, not with the reproduced corpus arithmetic. The original round-one report was preserved in r2/review-r1.md before the authoritative review.md was replaced. No reviewed draft or probe was edited; all runtime instrumentation lives in scratch scripts."
}
```
