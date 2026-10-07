# Contract review round 3 — `R024-C/1` at `92adee0154d4c3feac6696d897d861bf91d2183e`

Verbatim stdout of the round-3 dispatch (`REVIEWER`, `mantle/gpt-6-astra`, native thinking `medium`), the last of the three allowed rounds. Dispositions: contract §13.

---

**Contract review round 3 — `R024-C/1`**

Reviewed `dbc9432620fad36cabe7cdf0f4024b819326e2eb...92adee0154d4c3feac6696d897d861bf91d2183e`. **The round-2 findings are resolved, but the revision introduces a blocking inconsistency in sensitivity-arm validation.**

I checked the issue of record, downstream requirements, D1/D2 findings, previous contract reviews, the complete contract and revisions, documentation tests, relevant solver/runner/work-pass code, and saved R023 records.

Verification:

- Prescribed `pytest tests/docs -q -p no:cacheprovider`: **196 passed**.
- `ruff check --no-cache .`: **passed**.
- `mypy --cache-dir /dev/null`: **passed, 182 source files**.
- `git diff --check`: **passed**.
- The three saved tuning files used for the sensitivity reproduction match their committed SHA256SUMS.

No benchmark, campaign execution or full suite was run. No files or tracker records were changed. `git status --short` was empty before and after; HEAD remained unchanged.

Reviewer provenance: fresh independent **REVIEWER** context; runtime `PI_MODEL=gpt-6-astra`, corresponding to the requested `mantle/gpt-6-astra`. Native effort is recorded as **medium** in §13 and `config/agent-roles.conf`; this context did not independently expose the active effort. The same configured model participated in `R023-C/1` and the R024 design reviews, as disclosed in §13. This final contract round does not consume the separate release-review budget of **3 reviews / 2 fix batches**.

**Resolution of previous findings**

| Finding | Resolution | Evidence |
|---|---|---|
| **C2-F1 — conflicting work-pass termination requirements** | **Resolved** | §5.4.1 defines P1–P5 for every paired arm, including ordinary base rows. §7.4 C13 explicitly applies that rule; P2–P4 are reported outcomes, P5 is a defect and `check` problem. §9.1 incorporates missing-work labels into the inconclusive threshold. §12 supplies every pair class; tests at `tests/docs/test_research_024_contract.py:430`, `:447` and `:454` execute and check them. |
| **C2-F2 — undefined totals when both twins or reference work are unavailable** | **Resolved** | Rule M preserves missing values and propagates them through sums. §5.6 makes missing candidate totals ineligible and missing A0 totals `no_selection` for both identities; §5.8 defines outcome precedence and reference defects. §12 now includes the runner’s null-count B0 shape, P3/P4, and WE7/WE8. The branch, pair and worked-example tests pass. |
| **C2-F3 — controls without attribution fields have no audit branch** | **Resolved** | §7.5 explicitly classifies termination, `not_reached`, non-`ok`, refusal, missing control blocks and reached controls. C11/C12 reference that audit; the paragraph after C15 distinguishes labels from `check` problems. The saved `no_route` control records follow K2, without invented activation/control values. §12 and the test at `:481` cover K0–K5. Audit arithmetic itself is not exercised by that classifier test; see suggestion C3-F2. |
| **C1-F1 — legitimate non-success records become defects** | **Resolved** | §5.4 and §12 distinguish B0–B3 and restrict G3–G7 to the fields each branch produces. The new Rule M/P closes the termination gaps left in round 2. Tests at `:395`, `:406` and `:422` check applicability and record skeletons; solver early returns and the runner’s timeout shape agree. |
| **C1-F2 — controls require a forbidden work pass** | **Resolved** | §7.4 C12 excludes G6 for `E2-wm`/`E2-cm`, consistent with §7.2 and §12 `campaign.no_work_pass`. Base rows receive completeness/C1/C13 rather than post-processor gates. §7.5 completes the control-audit branches missing in round 2. |

These are operative edits, not merely dispositions asserted in §13. The earlier C1-F3–F5 fixes also remain present: exhaustive family comparisons, the corrected approximately 65-minute launch wait, and the populated review/provenance record.

**C3-F1 — blocking — Sensitivity arms inherit gates requiring the wrong base reference and an unscheduled work pass**

Location: [docs/references/research-024/contract.md:351](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r3/docs/references/research-024/contract.md:351), §5.10; interacting with §5.4 lines 228–236 and §5.8 line 310.

The revision adds:

> “The §5.4 gates apply to them except G6 (no work pass); a `defect` there is handled as in §5.8.”

Two inherited requirements conflict with the sensitivity schedule:

1. **G1 still requires both an ordinary run and a work pass.** Section 5.10 schedules ordinary runs only and exempts only G6.
2. **G4 uses the original P* base reference.** The newly added definition at line 228 binds “base reference record” to §5.3’s reference arms, which run under P* with chunks 50. Section 5.10 runs chunks 100/200 but never overrides that binding.

The second problem is reproducible from pinned R023 tuning records:

| Record for `emp-09bc4e-201eba-large-3` | Counted/base quotes |
|---|---:|
| `T-E1b-C100`: embedded `search.base.quotes` | 37,827 |
| `T-C100`: reference `quotes.counted` | 37,827 |
| `T-A0`: P* reference `quotes.counted` | 38,098 |

The embedded base object equals the C100 reference object on **all G4 fields**. It correctly differs from A0. Literal application of the revised G4 therefore produces a false `defect`; revised §5.8 then changes the identity to `blocked_defect` and requires a fix and whole-grid rerun.

WHI-1631 would have to reinterpret the gates after observation to avoid this outcome. The current tests do not exercise sensitivity-arm gate application.

**Concrete edit:** Give §5.10 an explicit applicability rule:

- G1 checks the scheduled ordinary records only; G6 is inapplicable.
- For the post-processor sensitivity arm, G2–G4 use its base arm at the **same chunk setting**.
- The standalone sensitivity base receives completeness checks, without post-processor-field requirements.
- Keep the original P* common reference solely for the separately reported `Q`.
- Add a contract example proving that a correct c100 embedded base may differ from A0 without a defect, and that an ordinary-only sensitivity run satisfies completeness.

**C3-F2 — suggestion — Control tests classify K5 but do not exercise its audits**

Location: [tests/docs/test_research_024_contract.py:481](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r3/tests/docs/test_research_024_contract.py:481), with contract §7.5.

`test_control_examples_classify` checks only K0–K5 classification. It does not execute the target/spend checks or the `treatment_unavailable` outcome. Consequently, these newly registered semantics lack an executable check comparable to Rule P and the selection examples.

**Concrete edit:** Add small examples covering a work-target overrun, a call mismatch with and without `stop`, an embedded-treatment mismatch, and an absent treatment activation block. Assert their defect/unmatched outcomes. The normative text already defines these outcomes, so this is nonblocking verification improvement.

**WHI-1630 acceptance**

| Criterion | Assessment |
|---|---|
| Self-contained, mechanical specification of Implementation 1–6 | **Not met** — C3-F1 makes §5.10’s validation contradictory. P*, grids, objective, missing-work handling, no-winner precedence, roster/provenance, campaign accounting, timing and scheduling are otherwise specified. |
| Prior 0.2.3 influence disclosed; report split excluded from selection | **Met** — §§3, 5.3, 5.9 and 5.12. |
| Release-plan 0.2.4 gates match the shared standard; 0.2.3 lists six shipped issues | **Met** — `docs/RELEASE_PLAN.md` §§0.2.3–0.2.4 and passing contract checks. |
| Independent review recorded with dispositions, role/model/native effort, fresh context, R023 participation and separate release-review budget | **Met for recorded rounds 1–2** — §13, retained reports and the nonempty review-record test. This round’s report and dispositions must subsequently be retained by the implementer. |
| Ruff, mypy and `tests/docs` pass | **Met** — all passed on the reviewed HEAD; 196 documentation tests and 182 mypy source files. |

Verdict: DISAGREE (blocking: C3-F1)
