# Contract review round 2 — `R024-C/1` at `1d043a19e8d807743f5c45a41c36934375aa528b`

Verbatim stdout of the round-2 dispatch (`REVIEWER`, `mantle/gpt-6-astra`, native thinking `medium`). Dispositions: contract §13.

---

**Contract review round 2 — `R024-C/1`**

Reviewed `dbc9432620fad36cabe7cdf0f4024b819326e2eb...1d043a19e8d807743f5c45a41c36934375aa528b`. The revision fixes the ordinary `no_route` gate, the forbidden control work-pass requirement, the family-comparison choice, the launch-wait figure and the review record. **Three failure-handling gaps still prevent mechanical downstream implementation.**

I checked the issue of record, downstream issue requirements, D1/D2 findings, the complete contract, round-1 report and revision, contract tests, solver/runner/work-pass code, and relevant saved R023 records.

Verification:

- `pytest tests/docs -q -p no:cacheprovider`, with the prescribed environment: **187 passed**.
- `ruff check --no-cache .`: **passed**.
- `mypy --cache-dir /dev/null`: **passed, 182 source files**.
- Recomputed R022 launch waiting: **65.005589 minutes**, including **9 failed gates**.
- Confirmed the saved `no_route` and E1-truncated control shapes against §12.
- Confirmed the runner’s hard-timeout shape using its record classes, without executing a solver.

No benchmark, campaign or full suite was run. No files or tracker records were changed. The checkout was clean before and after; HEAD remained unchanged.

Reviewer provenance: fresh independent `REVIEWER` context; requested model `mantle/gpt-6-astra`, runtime environment `PI_MODEL=gpt-6-astra`. Native effort is **`medium` per the recorded dispatcher/configuration mapping**; this context did not independently expose the active effort. The same configured model participated in R023-C/1 and the R024 design reviews, as disclosed in §13. This contract review does not consume the final release’s **3 reviews / 2 fix batches**.

**Round-1 resolutions**

| Finding | Resolution | Evidence |
|---|---|---|
| **C1-F1 — non-success records treated as defects** | **Partly resolved** | §5.4 and §12 now distinguish B0–B3. G4 no longer requires `base_gross` for B1, and G5 permits B2’s charged replay without an own-quotes field. Tests at `tests/docs/test_research_024_contract.py:391` and `:402` check the table, classification and ledger skeletons. Saved R023 `no_route` and E1-truncated records agree. Termination handling remains incomplete across selection and campaign rules: **C2-F1/F2**. |
| **C1-F2 — forbidden work pass required for controls** | **Partly resolved** | §7.4 C12 explicitly excludes G6 for `E2-wm`/`E2-cm`, restricts post-processor gates to the appropriate arms, and names the matching cohort/split. This resolves the original G6 contradiction. However, the promised complete audit path still lacks C11 applicability for legitimate controls without attribution fields: **C2-F3**. The new tests do not exercise this arm-specific audit path. |
| **C1-F3 — undefined “best rankable other row”** | **Resolved** | §7.4 C5 now requires every rankable other row, plus the separately labelled C9(b) envelope. There is no remaining strategy-selection decision. This is an actual normative edit, although no dedicated C5 regression assertion was added. |
| **C1-F4 — incorrect launch-wait figure** | **Resolved** | §10.3 now states approximately 65 minutes including sampling, with 45 minutes of retry sleeps. The committed timing ledger independently reproduces the correction. No dedicated arithmetic assertion was added. |
| **C1-F5 — incomplete review acceptance record** | **Resolved** | §13 records the round-1 SHA, report, five dispositions, role/model/native-effort mapping, fresh context, prior R023 participation and separate release-review budget. The test at `tests/docs/test_research_024_contract.py:540` now requires a nonempty report set, matching disposition IDs, round metadata and the disclosure phrases. |

**C2-F1 — blocking — C13 contradicts the new work-pass termination exception**

Location: [contract.md:462](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r2/docs/references/research-024/contract.md:462), §7.4 C13; conflicts with §5.4 at line 239.

**Evidence:** §5.4 explicitly allows an ordinary/work-pass pair with exactly one B0 termination: G6 is not evaluated, the outcome is `work_pass_terminated`, and work is missing rather than zero. C13 still requires **every** `WP-*` record to equal its ordinary twin on status, score and evaluation, with executed quotes equal to counted quotes.

For an ordinary `ok` result whose instrumented twin times out, these requirements cannot both hold. The work-pass wrapper adds `r022_work` only after the solver returns (`tools/research_022/pruning_work.py:126`); a killed worker supplies no such block. C12 also assigns C13 to ordinary base rows, for which the post-processor B0–B3 classification is not applicable.

**Concrete edit:** Make C13 explicitly share the termination/missing-work policy, including ordinary base rows. Define which pairs are reconciled, which receive missing-work labels, and how those labels affect §9.1 and `check`. Add a paired-record example where ordinary succeeds and its work pass terminates; require one unambiguous campaign outcome.

**C2-F2 — blocking — Both-twins termination leaves selection work totals undefined**

Location: [contract.md:239](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r2/docs/references/research-024/contract.md:239), §§5.4–5.8; §12 `selection.record_branches`.

**Evidence:** The new missing-work rule covers B0 in **exactly one** twin. It does not cover B0 in both twins or missing work in a reference arm.

A runner hard timeout produces:

```json
{
  "status": "timeout",
  "quotes": {"attempted": null, "counted": null},
  "search": {},
  "evaluation": null,
  "score": null
}
```

This follows `benchmark/worker.py:354`, `benchmark/runner.py:539` and `benchmark/results.py:124`. The work block is absent.

If a candidate, its work-pass twin and its base reference all time out on a case, complete manifests can satisfy G1 and matching statuses satisfy G2. G3–G7 are inapplicable to B0. Nevertheless, §5.6 requires sums over **every status**, including the missing quote/CL/LB values. If A0’s work is missing, the denominator is also undefined. §5.8 treats a reference as unavailable only through G1 failure, which a complete manifest containing timeout records does not imply.

The §12 timeout example supplies `counted: 300000`; its classification test therefore does not cover the null-count shape or attempt the work calculation.

**Concrete edit:** Define missing-work handling for either or both twins and for every reference arm before calculating totals. A minimal rule is candidate ineligibility whenever required work is unavailable, and an explicit no-selection outcome when required reference work is unavailable. Preserve missing values rather than substituting zero. Add null-count, both-terminated and missing-reference examples with expected outcomes.

**C2-F3 — blocking — C11 has no audit branch for legitimate controls without attribution fields**

Location: [contract.md:460](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r2/docs/references/research-024/contract.md:460), §7.4 C11–C12 and §9.1.

**Evidence:** C12 unconditionally adds C11 audits to E2 controls. C11 exempts `not_reached` rows, but does not specify applicability for base pass-through, termination or topology refusal.

Both saved `R-E2pf-A0-wm` and `R-E2pf-A0-cm` contain this case:

```text
case: bnd-1bdd88-78c1b0-dust
status: no_route
not_reached: null
scope: null
activation field: absent
control field: absent
counted quotes: 4
```

This is the legitimate early return at `marginal_activation.py:585`. There are no target, control-call or embedded-activation values on which to evaluate C11. Topology refusal also bypasses control execution. The existing R023 auditor explicitly separates non-`ok`, refused and `not_reached` rows before reading those fields (`tools/research_023/r023_analysis.py:239`).

Without that applicability rule, WHI-1633 must decide whether absent fields are unmatched, defective or defaulted. §9.1’s rejection rule for a control “not call-matched” makes that choice consequential.

**Concrete edit:** Register C11’s branch conditions explicitly: retain and label non-`ok`, refused and pre-control `not_reached` rows as unmatched; apply target/spend audits only where control execution occurred; distinguish legitimate absence from missing fields on a reached control. Reuse the existing R023 audit distinctions and add the saved `no_route` control example to the contract checks.

**WHI-1630 acceptance**

| Criterion | Assessment |
|---|---|
| Self-contained, mechanical specification of Implementation 1–6 | **Not met** — C2-F1–F3 leave work reconciliation, selection totals and control-audit outcomes undecided. P\*, grids, exact ranking arithmetic, provenance rules and the timing protocol otherwise remain specified. |
| Prior 0.2.3 influence disclosed; report split excluded from selection | **Met** — §§3, 5.3, 5.9 and 5.12. |
| Release-plan 0.2.4 gates match the shared standard; 0.2.3 lists six shipped issues | **Met** — `docs/RELEASE_PLAN.md` §§0.2.3–0.2.4 and the passing release-plan contract check. |
| Independent review recorded with dispositions, provenance, R023 disclosure and separate final-review budget | **Met for the recorded round** — §13 and `reviews/round-1.md`. This round’s report and dispositions must subsequently be retained by the implementer. |
| Ruff, mypy and `tests/docs` pass | **Met** — all passed on the reviewed HEAD; 187 documentation tests and 182 mypy source files. |

Verdict: DISAGREE (blocking: C2-F1, C2-F2, C2-F3)
