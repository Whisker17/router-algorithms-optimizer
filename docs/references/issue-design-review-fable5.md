# Interactive Issue-Design Review — fable5

Date: 2026-09-24. Status: finalized after two discussion rounds and the advisor's final re-read. All material design findings were resolved; there are no remaining design-review blockers.

## Scope and participants

The owner requested an interactive fable5 subagent discussion before finalizing the issue design. The parent authored the documents and made all edits; the advisor had a fresh, read-only context and used the native supervisor interview channel. This is design review, not a code-review merge gate or evidence that implementation/tests exist.

- Advisor model: `claude/claude-fable-5` (confirmed by run status).
- Workflow: `7401d1b1-fc39-47eb-bc5e-9eed353c9ed7`.
- Retained child: `abda71b3-070a-4f78-b966-99cc2427ff42`.
- Repository base during review: `main`, commit `70632ce02e80f9bf1b615b030bbca4c81671583e`, with owner-requested local design changes.
- Reviewed files: `docs/DESIGN.md`, `docs/ISSUE_PLAN.md`; standards: `docs/agents/issue-template.md` and repository governance.
- User constraints retained: one real snapshot, Python runtime, five sources, Exact Input, shared-pool evaluation, CLI/HTML, quality/latency trade-offs, daily ten-minute target; mandatory Uni SOR and optional Jupiter challenge.

## Discussion record

### Round 1: independent findings and parent response

The advisor judged the scoped upstream-pinned SOR Python port mechanically achievable and honestly labeled. It accepted matched V2/V3 cohorts, LB capability exclusions, a validation-only upstream harness, and the separation between Metis research and implementation. It raised six findings:

| Finding | Advisor challenge | Parent resolution |
| --- | --- | --- |
| F1: mandatory SOR licensing contingency | An incompatible GPL-derived port could deadlock the mandatory gate without an owner decision path | Add explicit compatible-licensing/distribution or formal-scope-amendment choices; port stays blocked, no silent SOR-inspired replacement |
| F2: I17 atomicity/milestone | An M0 license/scope task bundled a golden harness depending on M1 serialization | Keep I17 as the M0 contract; add I22 for actual upstream harness/goldens at M1; port depends on I22 |
| F3: missing price dependency | I19 consumed prices owned by I11 without a dependency | Add I11 → I19 and explicitly assign schema ownership to I11 |
| F4: 600-second ambiguity | Text both treated timing as a target and required an unconditional hard checkbox | Preserve the target with an explicit acceptance disjunction: meet it or obtain a recorded owner-approved amendment; pending misses keep I21 incomplete |
| F5: cross-source block coordination | Individual admission snapshots could be mistaken for the final common-block corpus | Label admission blocks provisional; I11 freshly runs every collector at the final verified common block |
| F6: early cost interface ownership | Algorithms precede empirical cost fitting but need an objective seam | I02 supplies gross-only and visibly synthetic fixed-cost modes through one ObjectiveContext interface |

The parent additionally identified and fixed two hidden coupling risks:

1. I02 owns a small `prepare` dispatcher with a synthetic source. Each source collector can register itself without requiring Agni's CLI work. Shared CL collector, CLI and registry files are serialized mutation lanes even where logical issue dependencies allow parallel scheduling.
2. The corpus is immutable and can support gross-only runs before cost calibration. A final experiment manifest references corpus/price and separate cost-model hashes; attaching a model never mutates an existing bundle.

### Round 2: challenge reconciliation

The advisor accepted the resolutions and explicitly withdrew the F4 disagreement once the acceptance criterion used a decidable disjunction. The parent and advisor agreed that simply documenting a timing miss is not permission to close the issue or quietly shrink coverage.

For F1, the advisor requested separate treatment of the harness/imported dependencies, generated fixture data, and translated port. The parent incorporated this distinction without making blanket legal claims: output does not automatically inherit a program's license, embedded protected material must still be considered, and a folder/process boundary alone does not eliminate derived-code obligations. Any actual licensing/distribution decision belongs to the future source-contract work and owner decision path.

The advisor accepted stable I22 numbering instead of renumbering existing issues, and asked for corresponding map/reverse-edge changes, which were applied.

## Revision submitted for final review

SHA-256 before final status-only edits:

- `docs/DESIGN.md`: `ef0a2f1d0baded0b9a0e90731ceaa90c17282829c5ad77cc5beec17d7cc4b08d`
- `docs/ISSUE_PLAN.md`: `fe4e3623e67f65276e2b5e42a854cf17e07167de35ca087f647380a3efb09196`

The parent asked the advisor to read these actual files, not approve a change summary, and flag any new blockers.

## Structural validation

A local read-only document check confirmed:

- 25 drafts: one governance prerequisite, 22 core issues, two challenge issues.
- Required template sections, English version/component titles and acceptance checklists.
- Every dependency identifier exists; `Blocked By` and `Blocks` are reciprocal.
- Delivery-map dependencies and milestones match issue bodies.
- No dependency cycle or core milestone depending on a later core milestone.
- Challenge issues do not gate core acceptance.
- `git diff --check` passes.

No product tests were run or claimed: this task changes design documents only. No Linear issues, Release bindings, branches or PRs were created by this document-authoring task.

## Remaining execution prerequisites

The docs can be complete while implementation readiness remains conditional. Deliberate repository bootstrap (`origin/dev`), real Linear Release bindings, protocol/RPC admission evidence and SOR license compatibility still need their planned work. C02 remains an explicit conditional shell until C01 supplies a concrete approved hypothesis and go verdict.

## Later owner-authorized publication

This record describes the earlier fable5 design review. After it concluded, the owner clarified the need for source-traceable Solidity behavior and explicitly requested Opus 5.5 publication/version assignment. Those later changes and their separate parent acceptance are recorded in [linear-publication-audit.md](linear-publication-audit.md) and [RELEASE_PLAN.md](../RELEASE_PLAN.md). The issues and real Releases now exist; the review-era statements about unpublished drafts are historical, not current tracker status. No later edits are attributed to the earlier reviewed hashes.

## Final advisor verdict

The advisor independently re-read the revised files and verified the SHA-256 values recorded above. Its final verdict was:

> The revised issue design is approved at the design level: honest about the SOR port boundary and its licensing risk, correctly atomized after the I17/I22 split, dependency- and milestone-consistent, bootstrap-aware without manufacturing refs, and with all comparison-fairness, shared-state, provenance and challenge-separation policies intact.

It confirmed that all six findings and both parent additions were present in the actual revised documents. F1 and F4 were explicitly discussed and reconciled, rather than simply accepted without explanation. No material point remained unresolved at the review limit.

The 22 core issue drafts plus governance prerequisite are design-ready; publication/implementation still require the documented bootstrap and real Release bindings. I18 remains gated by licensing resolution and actual upstream goldens. C02 remains a conditional implementation shell until C01 provides its specific mechanism and go verdict. These are planned execution gates, not unfinished issue-design work.

The complete advisor output was produced at the workflow's bound output `review/fable5-issue-design.md` (retention-managed subagent artifact). This repository-local record preserves the substantive findings, discussion, verified hashes and final verdict independently of artifact retention. After the verdict, the parent changed only the status paragraphs in DESIGN/ISSUE_PLAN and the project status mirror; the reviewed requirements and issue bodies were unchanged.
