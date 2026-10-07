# Contract review round 1 — `R024-C/1` at `0d0bae25abf9025fc455370052f035a717ef4097`

Verbatim stdout of the round-1 dispatch (`REVIEWER`, `mantle/gpt-6-astra`, native thinking `medium`). Dispositions: contract §13.

---

Reviewed `dbc9432620fad36cabe7cdf0f4024b819326e2eb...0d0bae25abf9025fc455370052f035a717ef4097`. **The contract is not yet mechanical enough to approve.** Its selection arithmetic, profile pins, provenance invariants and timing-attempt design are substantially complete, but the campaign gates contain contradictions and one reporting choice remains unspecified.

I checked the issue of record, the final downstream issue text, D1/D2 findings, the changed files, relevant solver/analysis code, committed ledgers and saved R023 records. Verification performed:

- Contract test: **28 passed**.
- Ruff, with caching disabled: **passed**.
- Mypy, with cache directed to `/dev/null`: **passed, 182 source files**.
- In-memory mutation of ε from `1/100` to `1/10`: **rejected by the contract test**.
- All **80 files** listed in R023’s tuning SHA256SUMS matched their committed hashes.
- Recomputed the quoted tuning invocation durations, **11.9423/9.9770** report solve-hours, and the identical-work example: **37,015 quotes**, **11.814945708 s** versus **10.190749167 s**.

No benchmark or campaign was run. I did not run the full suite or the remaining `tests/docs` tests. The checkout was clean before and after; HEAD remained unchanged.

Reviewer provenance: fresh independent `REVIEWER` context; requested model `mantle/gpt-6-astra`, with runtime `PI_MODEL=gpt-6-astra`. Native effort was not independently exposed in the environment inspected. D1-F7 records prior R023 contract participation by the same configured reviewer model; this fresh context does not establish absence of that prior participation.

**C1-F1 — blocking — G4 treats legitimate non-success records as defects**

Location: [contract.md:220](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r1/docs/references/research-024/contract.md:220), §5.4; propagated by C12 and §9.1.

**Evidence:** G4 requires `search.<id>.base_gross` to equal the base gross **on every case**. Both solvers return immediately when the base is non-`ok`, before adding `base_gross`:

- `routing/algorithms/split_polish.py:683`
- `routing/algorithms/marginal_activation.py:585`

This occurs in the pinned R023 report records. In both `R-E1b-A0` and `R-E2pf-A0`, case `bnd-1bdd88-78c1b0-dust` has status `no_route`, a matching `search.base`, and **no `base_gross` field**. The ordinary pass-through is correct, but C12 applies G4 to it and §9.1 consequently requires `reject`. The existing R023 auditor handles this distinction explicitly.

Hard-killed records also need a defined branch because they can lack `search.base` entirely. Successful `not_reached` returns can omit post-processing fields used by G5/G7.

**Concrete edit:** Add an explicit applicability table for G4–G7 covering normal success, base-status pass-through, pre-polish `not_reached`, and runner termination. Require fields on the branches that produce them; specify the fallback/status checks and disposition for the other branches. Add a contract example using the saved `no_route` shape so an implementer cannot silently invent missing-field defaults.

**C1-F2 — blocking — C12 requires a work pass that the campaign forbids**

Location: [contract.md:443](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r1/docs/references/research-024/contract.md:443), §7.4 C12; conflicts with §7.2 and C14.

**Evidence:** C12 applies **G3–G7 to every record of the new identities and their controls**. G6 requires an ordinary/work-pass pair and equality of executed and counted quotes. However:

- §7.2 explicitly excludes `E2-wm` and `E2-cm` from `WP-*`.
- §12 repeats that exclusion in `campaign.no_work_pass`.
- C14 correctly separates charged control work from embedded uncharged treatment work.
- `marginal_activation.py:626` executes the embedded activation under a separate meter.

Therefore those controls cannot satisfy the literal G6 requirement. Interpreting “their controls” to include `BASE-*` also applies post-processor-specific fields to ordinary base rows.

**Concrete edit:** Replace C12 with an arm-to-gate applicability matrix. Apply G6 only to arms with a registered `WP-*` twin, post-processor-specific checks only to post-processor records, and the C11/C14 attribution audits to E2 controls. State that each base comparison uses the matching cohort and split. Test that both excluded controls have a complete, noncontradictory audit path.

**C1-F3 — blocking — “Best rankable other row” has no selection rule**

Location: [contract.md:436](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r1/docs/references/research-024/contract.md:436), §7.4 C5.

**Evidence:** C5 requires a per-family table against “the best rankable other row,” but never defines *best*. The contract does not specify its aggregation objective, failure treatment, comparison cohort, or tie-break. C9(b) separately defines a **per-case oracle envelope**, which is not necessarily one strategy row.

WHI-1633 would have to choose between a single aggregate winner, a different winner per family, or the oracle after observing the campaign.

**Concrete edit:** Remove the discretionary choice. The simplest edit is to require per-family tables against **every rankable other row**, consistent with C9(a). Alternatively, explicitly use C9(b)’s oracle envelope and call it an envelope, or register a complete rule for choosing one row.

**C1-F4 — blocking — The measured R022 launch-wait figure is wrong**

Location: [contract.md:624](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r1/docs/references/research-024/contract.md:624), §10.3.

**Evidence:** The table says “9 failed gates (≈45 min).” The committed timing ledger records:

- First launch sample: `1791093592.828542`.
- Successful tenth gate: `1791097493.163882`.
- Elapsed launch window: **3900.335340 seconds = 65.01 minutes**.

The nine 300-second retry sleeps account for 45 minutes; the ten sampling windows add approximately 20 minutes.

**Concrete edit:** Change the statement to “9 failed gates; approximately 65 minutes including sampling, of which 45 minutes were retry sleeps.” This is a small correction, but factual errors are blocking under this review’s stated severity rule.

**C1-F5 — blocking — The review acceptance record is still incomplete**

Location: [contract.md:893](/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-wt/review-whi-1630-r1/docs/references/research-024/contract.md:893), §13; `reviews/README.md:3`.

**Evidence:** §13 contains only a future-recording statement and an empty table. Neither it nor the README states the required R023 participation disclosure or explicitly preserves the final release’s **three-review/two-fix-batch** budget. The review test passes with no reports and no dispositions because both finding sets are empty.

This is expected first-round bookkeeping, but the acceptance criterion is not met at the reviewed SHA.

**Concrete edit:** Preserve this report, record its head, actual dispatch model/native effort and fresh-context provenance, disclose the known R023 model participation, and give every finding a disposition. Explicitly state that design and contract reviews neither replace nor consume the final release review budget. Make the final acceptance check require a nonempty review record and those metadata fields.

The **per-unit timing-attempt boundary is acceptable**. Units are fixed before timing; `UP` retains the paired baseline and candidate measurements within one attempt; every started or aborted attempt counts toward its unit’s cap; the earliest valid attempt wins; and cross-unit measurements are labelled `cross_window`. That does not combine favorable fragments into a synthetic paired result. The exhaustive triggers, no-launch terminal outcome, record keys, explicit departure from R022, and separation of “attempted correctly” from “usable latency” carry D2’s intended safeguards.

| Acceptance criterion | Assessment |
|---|---|
| Implementation 1–6 fixed mechanically and self-contained | **Not met** — §§5.4, 7.4 and 10.3 need C1-F1–F4. P\*, grids, exact selection arithmetic, reuse conditions, roster/provenance and timing boundaries otherwise check out. |
| Prior 0.2.3 influence disclosed; report split excluded from selection | **Met** — §§3, 5.3, 5.9 and 5.12 disclose exposure and prohibit report-bundle selection inputs. |
| RELEASE_PLAN 0.2.4 gates match the shared standard; 0.2.3 lists six shipped issues | **Met** — both release sections contain the required changes. |
| Independent review recorded with dispositions, provenance, R023 disclosure and separate final-review budget | **Not met** — §13 is pending; C1-F5. |
| Ruff, mypy and `tests/docs` pass | **Not fully established** — Ruff, mypy and all 28 new contract tests passed; the remaining `tests/docs` tests were not run in this review. |
| Contract check rejects a pinned-value mutation | **Met** — ε mutation rejected; existing worked-example checks also exercise limit and tie-break changes. |

Verdict: DISAGREE (blocking: C1-F1, C1-F2, C1-F3, C1-F4, C1-F5)
