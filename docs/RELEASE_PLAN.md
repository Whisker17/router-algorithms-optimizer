# Release Plan — Mantle Router Algorithm Optimizer

Status: publication independently accepted on 2026-09-24: 25 issues, six milestones and 42 blocking edges. [Acceptance and ID map](references/linear-publication-audit.md). Since publication, WHI-1425 completed via PR #1; `origin/dev` now exists. The owner has limited the current execution batch to **0.1.0**. Both releases remain Planned; 0.2.0 is future work.

Pipeline: [router-algorithms-optimizer](https://linear.app/whisker-personal/pipeline/router-algorithms-optimizer/releases), ID `3ae52bda-d2f7-4a18-9cd7-15dff9f8a8ed`.
Project: [Mantle Router Algorithm Optimizer](https://linear.app/whisker-personal/project/mantle-router-algorithm-optimizer-3abba3f613f7), UUID `29829418-7ca6-43c9-bf03-a49f05c76b1a`.
Team: Whisker-Personal (`WHI`), UUID `37abcce9-0070-470b-a57b-d8213047c418`.

## Published releases

| Version | Linear Release | ID | Original product-plan issues |
| --- | --- | --- | ---: |
| 0.1.0 | [Reproducible Mantle routing benchmark](https://linear.app/whisker-personal/pipeline/router-algorithms-optimizer/release/010-reproducible-mantle-routing-benchmark-68441970a261) | `c257e99c-df35-41d6-8576-a072601dcb5b` | 23 |
| 0.2.0 | [Jupiter Metis challenge](https://linear.app/whisker-personal/pipeline/router-algorithms-optimizer/release/020-jupiter-metis-challenge-14714fc6ff72) | `a65958a5-f0b4-42e7-9ace-1d376e363bca` | 2 |
| 0.2.1 | 0.2.1 — Independent routing strategies and CFMM dual routing (see [below](#021--independent-routing-strategies-and-cfmm-dual-routing)) | `ed16e106-fa3e-4b8a-b022-e7208eb8ef41` | 16 |

The counts above describe the original product/bootstrap plan, not a live tracker total.
Documentation/workflow follow-ups are tracked separately; current membership is visible
in each linked Linear Release. Linear limits Release descriptions to 255 characters.
Stored summaries point here for the full scope and exit gates; issue bodies were not shortened.

## Current execution decision — 0.1.0 only

- Work only on the remaining 0.1.0 issues. WHI-1448/WHI-1449 (0.2.0) remain future work; the batch does not need a Jupiter go/no-go decision and must not automatically start them after core completion.
- Use the default public Mantle RPC `https://rpc.mantle.xyz`. Its chain ID was verified as 5000; historical-state completeness still needs preflight. Do not silently buy a private/archive RPC, mix block states or replace missing data with approximations.
- Use existing Enterprise Dune access for reasonable scoped queries. Apply chain/time/partition filters, bounded initial samples, modest compute and cache reuse; avoid full-history scans, redundant exports and runaway retries/concurrency. No numeric credit budget was specified, and routine queries do not need per-query approval.
- Code reuse is authorized for private internal research: agents independently select and adapt permitted upstream implementations while retaining required notices and source pins. Ordinary reuse does not need owner approval; repository visibility and public distribution do not change.
- The owner removed the total-runtime acceptance threshold. Record daily/full timing as a metric, retain per-case safety budgets and correctness/coverage requirements, and do not create a 600-second approval gate.
- The full spec/plan handoff is WHI-1469; the separate governance mirror is WHI-1470. Use the committed documents from the resolved base. See [the execution decision record](references/0.1.0-execution-decisions.md).

## Why two versions

The approved first deliverable is a complete static benchmark, including mandatory Uni SOR. Splitting M0–M4 into releases would turn prerequisite-only or incomplete comparisons into apparent product deliveries. Keep these as milestones within the first release. The Jupiter challenge has a different uncertainty and acceptance boundary, so give it the next release without making the core dependent on it.

All releases start in the pipeline's **Planned** stage. The pipeline's `isProduction` flag does not mean anything has shipped; do not mark a release Released, set commitSha, fabricate dates, or create tags/branches during issue publication.

## 0.1.0 — Reproducible Mantle routing benchmark

**Original product plan: 23 assigned issues** — G00 plus I01–I22. Subsequent documentation/workflow handoffs include WHI-1469 and WHI-1472; their real Release associations are in Linear. WHI-1470/WHI-1471 are separate unversioned governance follow-ups. All feature/research titles carry `[0.1.0]`; G00 retains its governance title/routing as described below.

Deliver:

1. Verified deployments, one immutable real snapshot, and real protocol behavior for Agni v3, FusionX v3, Uniswap v3, Merchant Moe Classic v1 and Merchant Moe LB v2.2.
2. Source-traceable Solidity-to-Python migration of quote/swap math and relevant state transitions, checked against independent fixed-block quotes and sequential pool-swap state.
3. A correct split/merge/shared-pool evaluator and an isolated, reproducible experiment runner.
4. Six mandatory algorithms: direct pool, best bounded path, direct split, disjoint path split, incremental graph and the parity-tested upstream-pinned Uni SOR Python port.
5. A stratified real corpus, frozen prices, validated historical execution-cost model, uncertainty/sensitivity and honest matched/full-source cohorts.
6. CLI, offline HTML/CSV/JSON results, source and environment provenance, replay instructions and the calibrated daily profile.

Exit gates:

- Every required source has independent integer-output/state evidence; no generic AMM approximation substitutes for deployed behavior.
- Uni SOR includes source/provenance scope, required upstream notices, actual upstream-generated fixtures and exact supported selection/allocation parity. Permitted private internal reuse is agent-autonomous. Its LB exclusion is reported, not mistaken for a search failure.
- Every scheduled case remains represented, including errors/unsupported/timeouts; deterministic replays and cohort denominators are checked.
- Daily/full timing is measured and reported, with no total-runtime pass/fail threshold. Correctness, reproducibility and coverage remain mandatory; per-case limits still guard runaway work.
- I21 produces the convergence report; M0–M4 are capability milestones, not replacements for real Release bindings.
- Before shipping, complete the independent [whole-release review gate](GIT_WORKFLOW.md#release-review-gate) over the full integrated candidate and verify blocker fixes plus end-to-end evidence. Issue-level validation does not substitute for this release gate. No fixed review count is imposed on individual issues; existing human promotion/integration gates remain.

## 0.2.0 — Jupiter/Metis challenge

**Assigned issues: 2** — C01 and C02, both titled `[0.2.0]` and bound to this real Release.

Deliver a source-backed assessment of Metis methods and, if feasible, a distinct Python experiment with a reproducible comparison against the accepted 0.1.0 baseline.

- C01 is specified research and may be `ready-for-agent`, blocked on the implemented incremental graph baseline. It must identify evidence, accessible code/licensing, Mantle/Solana differences, one distinct hypothesis, test/ablation and a go/no-go decision.
- C02 is a conditional implementation issue. It is published in Todo with `needs-info`, not `ready-for-agent`; its mechanism must be filled from C01 before work begins. It depends on C01 and the complete core acceptance report I21.
- If go: implement the Metis-inspired variant, run matched-budget ablations and deliver measured results, including negative results.
- If no-go: the research may be complete, but C02 is not Done and the release does not automatically pass. An explicit owner disposition must remove/defer/cancel implementation or amend the release scope; the evidence and reason must remain visible.
- Do not call live Jupiter/Solana quotes a Mantle benchmark or claim reproduction of the full production Metis engine.

## 0.2.1 — Independent routing strategies and CFMM dual routing

**Planned issues: 16** — WHI-1547 to WHI-1562, titled `[0.2.1]` and bound to Release `ed16e106-fa3e-4b8a-b022-e7208eb8ef41`. Release state, dependencies and review rounds live in the Linear document [Release 0.2.1 — orchestration](https://linear.app/whisker-personal/document/release-021-orchestration-dcfa31f5326c); this section only fixes scope and gates.

- Five independent experimental strategies — `metis_history`, `direct_split_certified`, `incremental_graph_repair`, `uni_sor_cycle_safe` and `cfmm_dual` (CPMM, then validated concentrated liquidity) — each behind its own research contract, plus the shared per-strategy options/diagnostics seam (WHI-1548), an LB scoping study (WHI-1560), worked-example documentation (WHI-1561) and the paired batch/single-request comparison (WHI-1562).
- The shared comparison contract is [`research-021/contract.md`](references/research-021/contract.md) (`R021-C/1`, WHI-1547): identities, domains, certificates, work units, holdout exposure, presets and the `keep_experimental` / `reject` / `inconclusive` / `not_implemented` vocabulary.
- Research precedes implementation: an implementation issue keeps `needs-info` until its research contract is committed and implementable. A research `no_go` or `blocked` outcome is a completed research result, never a completed implementation.
- Exit gates: every implemented strategy is selectable in batch and single-request mode and appears once in the ordinary comparison; old profiles replay literally; every scheduled row stays visible; the frozen dispositions are backed by pre-registered raw evidence; the complete release candidate passes the [whole-release review gate](GIT_WORKFLOW.md#release-review-gate). No default-router adoption, loss tolerance, runtime ceiling or performance gain is implied.

## Governance assignment exception for this publication

The owner requested every current issue receive a version assignment in the new pipeline. G00 is therefore associated with **0.1.0 for delivery tracking only**. It remains governance-only, uses an unversioned title, and resolves its git base by the governance carve-out rules. This is a bounded metadata exception for G00, not a change to global version routing or any human merge gate.

G00 / WHI-1425 is Done after PR #1 established `origin/dev` and project governance. Its delivery association remains 0.1.0. The approved specification is delivered through WHI-1469 with a separate AGENTS mirror in WHI-1470; subsequent issue worktrees must use the resolved base carrying these documents. Product implementation and production release remain separate work.

## Publication metadata (original 25-issue publication)

The rules below describe the original 25 published issues. Later follow-ups are WHI-1469 (0.1.0, `chore`) and WHI-1470 (unversioned governance, no Release, `chore`); current workflow state is authoritative in Linear.

- Every issue is in the specified project/team and has exactly one Release in this pipeline.
- All 25 issues start Todo, unassigned, without fabricated due dates. Status means queued, not implemented or unblocked.
- G00: `ready-for-human` + `chore`.
- I01 and I17: `ready-for-agent` + `research`.
- Other core tickets: `ready-for-agent` + existing canonical `Feature` label.
- C01: `ready-for-agent` + `research`; C02: `needs-info` + `Feature`.
- Core/governance priority High; challenges Medium.
- Six project milestones: M0 Evidence and readiness; M1 First replayable comparison; M2 Five-source corpus; M3 Mandatory algorithm comparison; M4 Reproducible decision report; C1 Jupiter challenge.
- Every dependency has native Linear relations and real-identifier mirrors in the issue body. Draft keys remain only as an immutable provenance marker, not dangling dependencies.
- Before a possibly duplicated create, reconcile by project, exact title and provenance marker. An uncertain mutation response is never blindly retried.

## Historical acceptance of the original publication

The following checks describe the original 25-issue publication snapshot, before WHI-1469/WHI-1470 and the later owner amendments. The audit is a dated record, not a claim that current issue counts or body hashes never change.

The parent independently reads all issues back with `includeRelations` and `includeReleases` and verifies:

1. Exactly 25 mapped issues exist, with no missing/duplicate draft key.
2. Full template bodies and all acceptance criteria survived publication, including Solidity migration requirements.
3. Pipeline, Release, title prefix (except governance G00), project, team, milestone, priority, labels, unassigned state and Todo status match the plan.
4. All dependency edges and body mirrors match the approved graph, with no invented or missing blockers.
5. 23 issues are associated with 0.1.0 and 2 with 0.2.0; Jupiter cannot block the core exit.
6. Releases remain Planned, no completion/commitSha/deployment was claimed, and C02 remains gated.

The final publication map and independent verification result are stored in `docs/references/linear-publication-audit.json` and summarized in `docs/references/linear-publication-audit.md`. Acceptance passed: no missing/duplicate issue, no body-content mismatch, no wrong Release binding and no missing/extra blocker.
