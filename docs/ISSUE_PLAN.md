# Linear Issue Design — Mantle Router Algorithm Optimizer

Status: published to Linear on 2026-09-24 by the requested Opus 5.5 subagent and independently accepted by the parent. All **25 issues (WHI-1425–WHI-1449)**, **42 native blocking edges**, **six milestones**, and **two Planned releases** were verified. See the [published ID map and audit](references/linear-publication-audit.md). The design was finalized through [interactive fable5 review](references/issue-design-review-fable5.md), followed by owner-directed Solidity-source and release-assignment clarifications. Implementation has not started; Linear is the workflow-state authority.

Spec: [DESIGN.md](DESIGN.md). Each issue body follows [the canonical template](agents/issue-template.md), in English. Paths and verification commands below are implementation targets unless already present; tests/commands are not claimed to exist today.

## Current execution amendment — owner decision, 2026-09-24

Only 0.1.0 is in the current execution batch; C01/C02 remain future work. Use the default public Mantle RPC `https://rpc.mantle.xyz` and existing Enterprise Dune access. Routine bounded queries are authorized without a new numeric credit cap: filter by chain/date/partition, sample before broad exports, cache results and avoid excessive concurrency/retries. Public RPC access must still pass fixed-block/state-completeness checks. Agents may autonomously reuse suitable upstream code for private internal research while preserving required notices/provenance. The owner removed the total-runtime acceptance ceiling: measured timing stays in reports but is not a completion gate. See DESIGN §§2.2, 2.7, 2.12 and [the decision record](references/0.1.0-execution-decisions.md).

G00 / WHI-1425 has completed and `origin/dev` exists. WHI-1469 delivers the full approved product documents and WHI-1470 separately synchronizes AGENTS.md. These are follow-up delivery issues outside the original 25-key map below. WHI-1469 blocks WHI-1426/WHI-1427 until the spec lands; WHI-1470 depends on WHI-1469. Published owner amendments supersede the dated original audit.

## Publication and execution contract

- Project: **Mantle Router Algorithm Optimizer**, UUID `29829418-7ca6-43c9-bf03-a49f05c76b1a`; team **Whisker-Personal / WHI**.
- The owner has authorized publishing all 25 issues in the `router-algorithms-optimizer` pipeline. Core titles use `[0.1.0]`; challenge titles use `[0.2.0]`. Bind their real Release IDs on publication and verify each title prefix against the actual Release field; a milestone is never a substitute. See [RELEASE_PLAN.md](RELEASE_PLAN.md).
- `G00` remains governance-only with an unversioned title and governance git routing. For this issue set only, the owner's all-issues-assigned request associates G00 with Release 0.1.0 as a delivery prerequisite; this is a bounded exception to the normal governance-Release omission, not a change to branch or merge rules. `I01`–`I22` are core; `C01`–`C02` are the nonblocking challenge. I22 was added during review and appears beside I17 in execution order; keys are stable, not a sequence mandate. The core exit does not depend on either challenge ticket.
- All published issues are Todo and unassigned. The bodies below retain stable design keys to preserve design-review history; the publication map resolves each to its real WHI identifier. Linear bodies use actual WHI references, and all dependency edges are native `blocks` / `blocked-by`. Original “Proposed metadata” lines below describe authoring intent; exact published metadata, including canonical label casing and real Release IDs, is recorded in the independently verified audit.
- Fully specified product issues remain Todo with `ready-for-agent` and native prerequisites. G00 is Done; C02 is future `needs-info` work. WHI-1469/WHI-1470 track this documentation handoff. Routine permitted private code reuse has no owner-approval gate; source parity, concrete source restrictions and protocol/data evidence still apply. Linear remains authoritative for current status.
- No production tag exists. WHI-1425 established `origin/dev`; WHI-1469 delivers the complete spec. Every implementation must verify its actual Release binding and the bootstrap base before starting. Do not create a release integration branch as a side effect of publication or choosing a ticket, and do not implement from `main` as a fallback.
- Keep one writer per module/worktree. `Blocked By` is a logical prerequisite graph; shared-file conflicts additionally require serialized execution even where the graph permits parallelism. In particular `snapshot/collectors/concentrated.py`, source collectors, `main.py`, registries and shared config are serialized mutation lanes. The first CL collector ticket to land creates the shared collection code; later tickets reuse it. Logical parallel eligibility for I04/I05/I06 is not permission for concurrent edits.
- Test commands are offline unless explicitly marked as preparation/integration. A pytest suite created by a ticket must fail when that ticket's core behavior is removed; template smoke success is not acceptance evidence.
- Parameters inherit DESIGN §2.12. Trial values are unvalidated; no ticket silently turns them into proven production defaults.

## Delivery map

| Key | Deliverable | Blocked by | Milestone |
| --- | --- | --- | --- |
| G00 | Deliberate workflow bootstrap and project binding | None | M0 |
| I01 | Verified five-source admission catalog | G00, WHI-1469 | M0 |
| I02 | Synthetic exact-input experiment from CLI to saved result | G00, WHI-1469 | M1 |
| I03 | Integer CL model with independent fixed-state evidence | I01, I02 | M1 |
| I04 | Agni fixed-block direct-route replay | I03 | M1 |
| I05 | FusionX fixed-block direct-route replay | I03 | M2 |
| I06 | Uniswap v3 fixed-block direct-route replay | I03 | M2 |
| I07 | Moe Classic fixed-block direct-route replay | I01, I02 | M2 |
| I08 | LB stateful quote model with independent evidence | I01, I02 | M2 |
| I09 | Moe LB fixed-block direct-route replay | I08 | M2 |
| I10 | Split/merge/shared-pool plan evaluation | I02 | M2 |
| I11 | Frozen five-source corpus at one block | I04, I05, I06, I07, I09 | M2 |
| I12 | Isolated measured experiments with complete outcomes | I02 | M2 |
| I13 | Best bounded single-path baseline | I10 | M3 |
| I14 | Direct-pool allocation baseline | I10 | M3 |
| I15 | Disjoint multi-hop allocation baseline | I13, I14 | M3 |
| I16 | Shared-pool incremental graph heuristic | I15 | M3 |
| I17 | SOR source/parity/provenance contract | I01 | M0 |
| I22 | Actual pinned upstream SOR harness and goldens | I02, I17 | M1 |
| I18 | Required Python SOR routing-core port | I03, I10, I22 | M3 |
| I19 | Historical transaction-cost context with validation | I01, I10, I11 | M4 |
| I20 | Offline report with honest cohorts and sensitivity | I12, I19 | M4 |
| I21 | Reproducible six-algorithm acceptance experiment | I11, I16, I18, I20 | M4 |
| C01 | Metis challenge feasibility and distinct hypothesis | I16 | C1 |
| C02 | Conditional Metis-inspired comparison | C01, I21 | C1 |

The graph is not a mandate to start I17 late: source/parity/provenance work should run early after I01, with I22's reference harness following I02 and I17. Baselines can develop on small checked-in fixtures while real-source collection proceeds. I21 is the only core convergence gate.

---

# G00 — [Infra] Establish the repository bootstrap and Linear project binding

Publication metadata: Release 0.1.0 (delivery association by explicit owner request; governance title/routing unchanged); milestone M0; priority High; labels `ready-for-human`, `chore`; state Todo. This is a publication-time exception for G00, not a general policy change.

## Objective
Establish the documented git/tracker prerequisites so subsequent implementation issues can resolve a valid base and project identity without guessing.

## Context
At original publication, DESIGN §6 recorded only `origin/main` and no production tag. G00 is now Done and `origin/dev` exists; see the current DESIGN §6. Governance must be isolated from version-scoped documentation/product changes. The owner requested every current issue be assigned in the new pipeline: G00 is associated with 0.1.0 solely as a delivery prerequisite, while its unversioned title, governance-only carve-out scope and `dev` routing remain unchanged. No merge/human-review exception is waived.

## Blocked By
None (entry point). The initial `dev` creation is a deliberate owner/bootstrap operation, not ordinary issue implementation.

## Blocks
- I01 — needs valid issue/worktree routing.
- I02 — needs valid issue/worktree routing.

## Implementation
1. Deliberately establish `dev` from the reviewed bootstrap state using the owner-controlled setup path; do not fabricate tracking refs or waive push protection implicitly. Record the resulting remote ref before worktrees are allocated.
2. Customize only governance carve-out paths: `AGENTS.md`, `docs/agents/issue-tracker.md`, `README.md` and relevant `config/agent-roles.conf` entries. Bind project/team to the values above; preserve the human merge exceptions and mandatory worktree policy.
3. Verify the real Linear release bindings and milestones created by this publication. Keep core/challenge Release versions distinct from milestone names; do not duplicate releases or use pipeline production status as evidence of deployment.
4. Verify role/runtime access and per-clone/worktree hooks. Publish a governance-only PR once `origin/dev` exists. Do not include DESIGN or product modules in that PR.

## Out of scope
Product implementation, silent high-risk-path waivers, creating `release/v*`, production tags or deployment.

## Acceptance criteria
- [ ] `origin/dev` exists and the bootstrap action/ref are documented.
- [ ] Project/team placeholders are resolved in tracker guidance; unresolved setup choices remain explicit rather than fabricated.
- [ ] A sample core ticket's title prefix matches its actual Linear Release field.
- [ ] New worktree merge-base verification and push hooks succeed; governance changes remain separate from feature work.

## Testing / Verification
Run `git fetch origin --prune`, inspect refs, and verify the documented worktree merge-base equality against the resolved bootstrap base. Read the actual Release field through Linear, not projectMilestone.

## References
DESIGN §6; `docs/GIT_WORKFLOW.md`; `docs/agents/issue-tracker.md`.

---

# I01 — [0.1.0] [Snapshot] Publish a verified five-source admission catalog

Proposed metadata: Release 0.1.0 (proposed); milestone M0; priority High; labels `ready-for-agent`, `research`.

## Objective
Produce an evidence-backed catalog and executable preflight that identifies which exact deployments and states each source adapter must support.

## Context
DESIGN §§2.2–2.3 and §5.1. The exploratory Dune counts are emitting addresses, not verified pool counts.

## Blocked By
- G00 — workflow and tracker initialization.
- WHI-1469 — committed full specification handoff (follow-up outside the original key map).

## Blocks
- I03 — verified CL semantics and reference pools.
- I07 — verified Classic deployment/fees.
- I08 — verified LB version/fee semantics.
- I17 — provenance and license investigation conventions.
- I19 — verified transaction/source identities.

## Implementation
1. Add `config/protocols.yaml` and `snapshot/preflight.py` with verified chain/factory/quoter addresses, code/version provenance, pool identity rules, token restrictions and source-specific math differences.
2. Record findings in `docs/references/protocol-admission.md`, including Agni v3, FusionX v3, Uniswap v3, Moe Classic v1 and Moe LB v2.2. For each deployed Solidity code hash, identify its matching verified source/commit, quote/swap functions, math libraries, state fields and the planned Python migration map; audit applicable source licenses before translation.
3. Probe fixed-block RPC reads and available Dune schemas/history without secrets in output. Identify a common candidate block and the method for complete tick/bin recovery.
4. Define exact checks and independent quote/state evidence required for each source. Record unsupported or missing evidence as a blocker; do not silently shrink the five-source scope.

## Out of scope
Full dataset capture, algorithm implementation, claiming admission from an ABI match alone.

## Acceptance criteria
- [ ] All five sources have verifiable deployment/code-hash-to-Solidity-source mapping, relevant function/storage migration scope and license provenance; no generic formula substitutes for an unmatched deployment.
- [ ] Preflight rejects wrong chain, block mismatch and unavailable required state.
- [ ] Dune-emitter-to-pool validation and state completeness strategies are documented.
- [ ] Missing RPC/data capabilities are actionable blockers with no fabricated fallback.

## Testing / Verification
`uv run pytest tests/snapshot/test_preflight.py`; run the explicit online preflight once and retain redacted evidence plus block identity.

## References
DESIGN §§2.2, 2.3, 5.1, 8; `docs/references/pre-research-from-gpt-6-pro.md`.

---

# I02 — [0.1.0] [Benchmark] Run a synthetic direct swap from CLI to a replayable result

Proposed metadata: Release 0.1.0 (proposed); milestone M1; priority High; labels `ready-for-agent`, `feature`.

## Objective
Deliver the first vertical experiment: a frozen synthetic constant-product bundle can be validated, solved, evaluated and saved through the CLI without network access.

## Context
DESIGN §§2.2, 2.3, 2.5, 4.3. This is a real narrow slice, not empty interfaces for every future module.

## Blocked By
- G00 — workflow and tracker initialization.
- WHI-1469 — committed full specification handoff (follow-up outside the original key map).

## Blocks
- I03, I07, I08 — working simulator/bundle/result contracts.
- I10 — initial plan/evaluator to extend.
- I12 — working experiment to instrument.
- I22 — common fixture/result serialization for upstream goldens.

## Implementation
1. Implement minimal typed data in `snapshot/models.py`, JSON validation/hashing in `snapshot/bundle.py`, integer CPMM math in `pools/constant_product.py` and the single-step path in `routing/evaluator.py`/`routing/plan.py`.
2. Add `routing/algorithms/direct.py`, an explicit registry, `benchmark/runner.py` and `benchmark/results.py`; choose the best admitted direct pool and independently evaluate it. Introduce `benchmark/objective.py` with the shared `ObjectiveContext`/complete-plan cost seam, gross-only mode and an explicitly synthetic fixed-cost mode for algorithm tests. Synthetic costs must never support empirical net-output claims.
3. Replace the template entrypoint with `main.py prepare`, `validate` and `run`; the first prepare source imports a synthetic fixture through the common collector dispatcher. Later source tickets register collectors without depending on Agni's CLI work. Validate `config/` YAML, require credentials only for online commands, and serialize large integers as strings.
4. Check in a tiny synthetic bundle and save a versioned result with hashes, raw output/status and replay instructions. Keep the runner initially simple; I12 owns hard timeout/measurement isolation.

## Out of scope
Real collectors, multi-hop/shared plans, production gas modeling, rich HTML.

## Acceptance criteria
- [ ] A documented offline command produces the expected integer output and identical deterministic replay.
- [ ] Bad checksum, negative amount, unsupported schema and unknown config keys fail clearly.
- [ ] `direct` compares all admitted direct pools and returns typed no-route when none exist.
- [ ] No credentials or network are needed for synthetic prepare/validate/run; gross-only and synthetic-cost objectives share one interface and are visibly labeled.

## Testing / Verification
`uv run pytest tests/test_synthetic_run.py tests/pools/test_constant_product.py`; run `uv run python main.py run --bundle tests/fixtures/synthetic --profile config/smoke.yaml` with networking disabled.

## References
DESIGN §§2.2, 2.3, 2.5, 2.10, 2.12, 4.3.

---

# I03 — [0.1.0] [Pools] Verify integer concentrated-liquidity state transitions

Proposed metadata: Release 0.1.0 (proposed); milestone M1; priority High; labels `ready-for-agent`, `feature`.

## Objective
Make exact-input CL quotes and subsequent state transitions verifiable against independent contract evidence before any live source is admitted.

## Blocked By
- I01 — deployment/semantic and evidence catalog.
- I02 — pool interface and offline replay contracts.

## Blocks
- I04, I05, I06 — source-specific real replay.
- I18 — compatible V3 states for SOR integration.

## Implementation
1. Add `pools/concentrated.py` by migrating the matching deployed Solidity swap-math/state-transition behavior: integer price movement, liquidity changes, fee/rounding rules and initialized-tick traversal with explicit source configuration. Record each relevant Solidity library/function and its Python counterpart, including integer widths/signedness/revert branches and scoped omissions.
2. Capture independently generated fixed-state fixtures under `tests/fixtures/concentrated/` with contract/code/block provenance and both directions; provide fixture regeneration instructions.
3. Return next state and crossed-tick features, enforce data-range bounds, and distinguish incomplete snapshot from real liquidity exhaustion.

## Out of scope
Treating all V3-like deployments as equivalent, full-pool crawling, floating-point final scoring.

## Acceptance criteria
- [ ] Raw outputs match independent evidence on small, boundary-crossing, multi-tick and insufficient-liquidity cases.
- [ ] Two sequential swaps agree on output and relevant next state with contract simulation.
- [ ] Missing ticks fail as incomplete state; the input state remains unchanged.
- [ ] Protocol differences from I01 are implemented or block that source explicitly; the Solidity-to-Python map covers every admitted execution branch and independently generated contract evidence verifies the migration.

## Testing / Verification
`uv run pytest tests/pools/test_concentrated.py`; replay captured quote and state-transition evidence offline.

## References
DESIGN §§2.2–2.3; `docs/references/protocol-admission.md`.

---

# I04 — [0.1.0] [Snapshot] Replay an Agni swap from a fixed-block snapshot

Proposed metadata: Release 0.1.0 (proposed); milestone M1; priority High; labels `ready-for-agent`, `feature`.

## Objective
Capture verified Agni pools at a chosen block and run a direct-route comparison against that frozen state.

## Blocked By
- I03 — verified CL simulator and admission rules.

## Blocks
- I11 — Agni contribution to the common five-source corpus.

## Implementation
1. Implement block-pinned CL collection in `snapshot/collectors/concentrated.py` and Agni wiring in `config/protocols.yaml`.
2. Extend `main.py prepare` to capture state and completeness bounds, verify the block hash, and atomically publish a bundle only after admission checks.
3. Save Agni-specific integer quote/next-state evidence and run the existing `direct` baseline on the captured bundle.

## Out of scope
Automatic admission of sibling CL sources, collection at latest on failure, dynamic monitoring.

## Acceptance criteria
- [ ] The direct result agrees with fixed-block Agni evidence in both directions.
- [ ] Every state component records the same block identity; changing the hash aborts publication.
- [ ] Traversing beyond collected tick bounds fails explicitly.
- [ ] The saved bundle replays without RPC access.

## Testing / Verification
`uv run pytest tests/snapshot/test_agni.py`; explicit online `prepare --source agni --block <verified-number>` followed by offline replay.

## References
DESIGN §§2.2–2.3, 4.4; protocol-admission catalog.

---

# I05 — [0.1.0] [Snapshot] Admit FusionX v3 through fixed-block replay

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Demonstrate that FusionX v3 is represented by correct source-specific state and math in the same offline comparison pipeline.

## Blocked By
- I03 — CL interface/math and evidence.

## Blocks
- I11 — FusionX contribution to the final corpus.

## Implementation
1. Add FusionX discovery/config and collection to `snapshot/collectors/concentrated.py` and `config/protocols.yaml`, reusing any collector that has landed.
2. Apply verified deviations from I01; add source-specific fixtures and provenance rather than assuming Agni/Uniswap equivalence.
3. Exercise `prepare` and the direct baseline against a frozen FusionX bundle.

## Out of scope
Parallel mutation of the CL collector/registry with I04 or I06; unverified fallback fees.

## Acceptance criteria
- [ ] Verified factory/pool/code identity and completeness metadata accompany every admitted state.
- [ ] Independent fixed-block output/state checks pass for both directions and crossed ticks.
- [ ] An offline direct-route replay succeeds; an incompatible deployment is rejected.

## Testing / Verification
`uv run pytest tests/snapshot/test_fusionx.py`; capture and replay FusionX evidence using an explicit block.

## References
DESIGN §§2.2–2.3; protocol-admission catalog.

---

# I06 — [0.1.0] [Snapshot] Admit Uniswap v3 through fixed-block replay

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Add verified Mantle Uniswap v3 pools to the offline universe and the SOR-compatible coverage cohort.

## Blocked By
- I03 — CL interface/math and evidence.

## Blocks
- I11 — Uniswap contribution to the final corpus.

## Implementation
1. Verify Mantle deployment/factory/quoter identities and wire `uniswap_v3` in `config/protocols.yaml` and `snapshot/collectors/concentrated.py`.
2. Capture complete bounded tick state and independent raw-output/next-state fixtures for the declared case envelope.
3. Run `prepare` and `direct` through the same bundle/evaluator seam as other sources.

## Out of scope
Uniswap v4/hooks or treating Uniswap SOR itself as a pool adapter.

## Acceptance criteria
- [ ] Verified Mantle source metadata and fixed block identity are present.
- [ ] Quote and state-transition fixtures pass, including tick crossing and malformed state.
- [ ] Offline replay uses only the published bundle; source capability is exposed to cohort selection.

## Testing / Verification
`uv run pytest tests/snapshot/test_uniswap_v3.py`; serialize shared collector edits with I04/I05.

## References
DESIGN §§2.2–2.3, 2.7.

---

# I07 — [0.1.0] [Snapshot] Replay Merchant Moe Classic swaps from verified reserves

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Admit Moe Classic v1 using real reserves, verified fee semantics and the existing exact-input experiment pipeline.

## Blocked By
- I01 — Classic deployment and fee facts.
- I02 — CPMM simulator and bundle/result seam.

## Blocks
- I11 — Classic contribution to the final corpus.

## Implementation
1. Implement `snapshot/collectors/classic.py` and source metadata in `config/protocols.yaml`.
2. Migrate token/reserve ordering, fee/rounding and reserve-update behavior from the actual deployed Solidity source into `pools/constant_product.py`; document the relevant source functions/code hash and Python mapping, including observable overflow/revert behavior.
3. Capture fixed-block reserves and independent two-swap evidence, then run `direct` offline.

## Out of scope
Merchant Moe LB, assuming all CPMMs have the same fee, fee-on-transfer tokens.

## Acceptance criteria
- [ ] Both directions and sequential reserve updates agree with independent evidence.
- [ ] Wrong source, token order and unsupported token behavior are rejected.
- [ ] Captured Classic pools are available in full and SOR-compatible cohorts where verified.

## Testing / Verification
`uv run pytest tests/snapshot/test_moe_classic.py`; explicit fixed-block capture plus offline replay.

## References
DESIGN §§2.2–2.3, 2.7.

---

# I08 — [0.1.0] [Pools] Verify Liquidity Book quotes with evolving fee state

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Model LB bin traversal and dynamic-fee transitions correctly enough to evaluate repeated use of a physical pool at a frozen timestamp.

## Blocked By
- I01 — exact LB version/state/evidence definition.
- I02 — pool simulation and offline fixture seam.

## Blocks
- I09 — real LB collector/admission.

## Implementation
1. Implement `pools/liquidity_book.py` by migrating the verified Solidity bin-math, fee and state-transition functions, with a source-to-Python mapping. Include integer arithmetic, fixed/variable fees, time-dependent accumulator inputs, revert behavior and transaction-local next state.
2. Add independently captured quote and post-swap state fixtures in `tests/fixtures/liquidity_book/`, with documented contract source/code and regeneration method.
3. Include both directions, multi-bin swaps, volatility-fee changes, integer rounding and repeated use. Freeze block time, not the fee state itself.

## Out of scope
Substituting CL/constant-product math, historical mean pool fees, graph optimization.

## Acceptance criteria
- [ ] Raw outputs and relevant next-state fields match fixed-state contract evidence; every relevant Solidity function/storage field maps to its Python implementation or a justified out-of-domain exclusion.
- [ ] Repeated swaps at the same timestamp observe prior state changes.
- [ ] Missing bins/fee state are explicit errors distinct from real liquidity exhaustion.
- [ ] The input snapshot is never mutated.

## Testing / Verification
`uv run pytest tests/pools/test_liquidity_book.py`; expected values must originate outside the Python implementation under test.

## References
DESIGN §§2.2–2.3; Merchant Moe LB documentation in DESIGN §9.

---

# I09 — [0.1.0] [Snapshot] Replay Merchant Moe LB swaps from a complete bounded snapshot

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Collect the real LB state needed by the declared input envelope and make it replayable in the same benchmark as CL and Classic pools.

## Blocked By
- I08 — verified LB math and state fields.

## Blocks
- I11 — LB contribution to the final corpus.

## Implementation
1. Add `snapshot/collectors/liquidity_book.py` and Moe v2.2 source wiring.
2. Collect active bin, initialized bins, fee configuration/accumulators and timestamp at one block, documenting enumeration and completeness bounds.
3. Gate publication on quote and sequential-state checks, then exercise the direct baseline on the offline bundle.

## Out of scope
Inventing empty bins, falling back to current state or silently excluding hard-to-read LB pools.

## Acceptance criteria
- [ ] State and provenance cover both directions for the declared amount envelope.
- [ ] Missing data aborts or records an explicit predeclared exclusion before comparison.
- [ ] Offline direct and sequential-pool replays match independent fixed-block evidence.

## Testing / Verification
`uv run pytest tests/snapshot/test_moe_lb.py`; run explicit fixed-block preparation and offline replay.

## References
DESIGN §§2.2–2.3, 8.

---

# I10 — [0.1.0] [Routing] Evaluate split and merged funding with shared pool state

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Accept valid ordered split/merge plans and reject plans that obtain output by double-spending funds or double-counting pool liquidity.

## Blocked By
- I02 — working single-step ledger/evaluator.

## Blocks
- I13, I14 — multi-step/split algorithm evaluation.
- I18 — normalized SOR allocations and residual handling.
- I19 — complete-plan cost features.

## Implementation
1. Extend `routing/plan.py`/`routing/evaluator.py` with multiple funding references, integer amounts/ALL_REMAINING, explicit output funds and ordered physical-pool state.
2. Validate conservation, producer/consumer ordering, directions, full fill, terminal asset and cycle restrictions from DESIGN §2.5.
3. Add step traces, residual diagnostics and features for later cost models; preserve solver order and independently recompute output.

## Out of scope
Compiler/opcodes, arbitrary Router ABI, automatic route repair or funding with external balances.

## Acceptance criteria
- [ ] Shared-prefix/suffix, split-after-merge and repeated-pool fixtures match independent expected values.
- [ ] Double spending, unready funds, wrong token, cyclic funding, duplicate output IDs and hidden residuals fail.
- [ ] Integer remainder and zero-input behavior are deterministic.
- [ ] Evaluation order across cases cannot mutate the source bundle or another plan's state.

## Testing / Verification
`uv run pytest tests/routing/test_plan_evaluation.py`; include a fixture where independent shared-pool quotes visibly overestimate output.

## References
DESIGN §§2.5, 4.3; pre-research §4.

---

# I11 — [0.1.0] [Snapshot] Freeze a five-source corpus with stratified requests

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Publish one immutable real data bundle that fixes the liquidity universe, request distribution and price context before algorithms are compared.

## Blocked By
- I04, I05, I06, I07, I09 — all five collectors/admission paths.

## Blocks
- I19 — authoritative frozen price context and final corpus identity.
- I21 — real acceptance corpus.

## Implementation
1. Implement `snapshot/corpus.py` and `snapshot/prices.py`; save the exact Dune SQL/export, fixed historical window ending at the snapshot, selection seed and missing-price handling.
2. Treat I04–I09 replay bundles as admission evidence at provisional blocks. Choose and verify one final common block, then freshly run every source collector there; an incompatible/incomplete source blocks publication. Build pair/amount strata including no-direct-pool cases and declare tuning/report splits.
3. Extend bundle validation for state envelope, source exclusions, price units/timestamps and checksum completeness. Publish full-source and SOR-compatible cohort descriptors. I11 owns `snapshot/prices.py` and its frozen schema; later cost work consumes it. This immutable corpus is usable in gross-only mode before I19; a later experiment manifest references its hash and a separate cost-model hash.
4. Save a corpus manifest and reproducible preparation recipe in `docs/references/corpus.md`; keep large state/export files outside git.

## Out of scope
Multiple real snapshots, treating swap legs as reconstructed user orders without evidence, temporal generalization claims.

## Acceptance criteria
- [ ] All admitted pools share the same block identity; all five source families are represented.
- [ ] Case selection is deterministic and distinguishes leg-derived versus reconstructed-order data.
- [ ] Amount bounds are covered by captured state or explicitly excluded before evaluation.
- [ ] Tampered/mixed-block bundles fail; all offline replays require no secrets/network.

## Testing / Verification
`uv run pytest tests/snapshot/test_corpus.py`; verify bundle hashes and replay its declared common cohort offline.

## References
DESIGN §§2.2, 2.4, 2.7, 2.12.

---

# I12 — [0.1.0] [Benchmark] Measure isolated solves without losing failed cases

Proposed metadata: Release 0.1.0 (proposed); milestone M2; priority High; labels `ready-for-agent`, `feature`.

## Objective
Make repeated solver measurements fair and robust to timeouts, crashes and mutable state while preserving every scheduled result.

## Blocked By
- I02 — runnable experiment/result schema.

## Blocks
- I20 — complete measured records for reporting.

## Implementation
1. Extend `benchmark/runner.py`, add `benchmark/worker.py` and typed `Budget`/status records; call ordinary Python solver factories inside isolated workers.
2. Measure algorithm-specific preparation, solve, evaluation and total batch time separately using a monotonic clock. Record quote/candidate counters and declared truncation.
3. Implement explicit warmup/repeats/seeds, timeout termination, fresh case state and worker replacement; save completed records atomically with an incomplete manifest on interruption.
4. Add a separate memory measurement mode and environment provenance. Preserve unsupported/no-route/error/timeout distinctions and prevent accidental run overwrite.

## Out of scope
Cross-language plugin services, distributed scheduling, hidden shared route caches, mandatory resume support.

## Acceptance criteria
- [ ] Hanging/crashing test solvers cannot stall or contaminate the next case.
- [ ] Every scheduled case has a terminal or explicit cancelled/incomplete outcome.
- [ ] Reversed case order preserves deterministic outputs; prepare cost is never hidden.
- [ ] Memory instrumentation is not mixed into normal timing samples.

## Testing / Verification
`uv run pytest tests/benchmark/test_runner.py`; exercise fake slow/crashing/stateful solvers and interruption recovery records.

## References
DESIGN §§2.10–2.12, 4.5.

---

# I13 — [0.1.0] [Routing] Compare the best bounded single path against direct pools

Proposed metadata: Release 0.1.0 (proposed); milestone M3; priority High; labels `ready-for-agent`, `feature`.

## Objective
Measure the isolated benefit of multi-hop routing without split allocation.

## Blocked By
- I10 — ordered multi-step evaluation.

## Blocks
- I15 — multi-hop candidate generation for path allocation.

## Implementation
1. Add `routing/algorithms/single_path.py` and reusable bounded path traversal in `routing/search.py`.
2. Enumerate cycle-free paths within explicit hop/candidate/quote limits, retain direct candidates and score complete exact-input plans under the declared objective.
3. Register capability/config metadata and trace candidate truncation; no static spot-rate shortest-path approximation may replace real quotes.

## Out of scope
Splits, a global optimality claim beyond the fully enumerated bounded feasible set.

## Acceptance criteria
- [ ] Fixtures select a better indirect route when appropriate and retain a better direct route otherwise.
- [ ] Unreachable pairs, cycles, hop caps and budget exhaustion have distinct correct behavior.
- [ ] Evaluated output agrees with the returned path and integer intermediate amounts.

## Testing / Verification
`uv run pytest tests/routing/test_single_path.py`; compare tiny graphs against exhaustive bounded enumeration.

## References
DESIGN §§2.5–2.6, 2.12.

---

# I14 — [0.1.0] [Routing] Compare discrete allocation across direct pools

Proposed metadata: Release 0.1.0 (proposed); milestone M3; priority High; labels `ready-for-agent`, `feature`.

## Objective
Measure how much same-pair liquidity aggregation improves a fixed-input swap.

## Blocked By
- I10 — split funding and exact final evaluation.

## Blocks
- I15 — allocation primitives and remainder handling.

## Implementation
1. Add `routing/algorithms/direct_split.py` with percentage/quantity sampling and a bounded allocation search.
2. Preserve the full-input single-pool candidate, maximum split count, explicit remainder assignment and common objective/cost context.
3. Emit fund-referenced plans, candidate/quote counts and allocation metadata; re-evaluate the final integer allocations.

## Out of scope
TVL-proportional allocation as an assumed optimum, multi-hop and shared-pool conflicts.

## Acceptance criteria
- [ ] A known two-pool fixture beats the best single pool while a small trade can correctly remain unsplit.
- [ ] Tiny/nondivisible amounts fully allocate without overspending or unexplained residuals.
- [ ] Results match exhaustive discrete enumeration for small fixtures and the declared grid.

## Testing / Verification
`uv run pytest tests/routing/test_direct_split.py`; include unequal fees/prices, insufficient liquidity and net-cost reversal cases.

## References
DESIGN §§2.5–2.6, 2.9, 2.12; pre-research §§3.2–3.3.

---

# I15 — [0.1.0] [Routing] Compare multi-hop allocation over disjoint paths

Proposed metadata: Release 0.1.0 (proposed); milestone M3; priority High; labels `ready-for-agent`, `feature`.

## Objective
Combine multi-hop and splitting while keeping path liquidity independent, yielding an interpretable baseline for shared-graph methods.

## Blocked By
- I13 — bounded multi-hop candidates.
- I14 — discrete allocation/remainder primitives.

## Blocks
- I16 — established nonsharing comparison baseline.

## Implementation
1. Add `routing/algorithms/path_split.py` and physical-pool conflict checks.
2. Generate/rank candidate paths at multiple amounts and search admissible allocations within declared budgets.
3. Retain direct and single-path fallbacks, compare complete plans under the common objective and expose pruned candidates.

## Out of scope
Calling this implementation Uni SOR, shared-pool plans or unqualified optimality claims.

## Acceptance criteria
- [ ] A multi-hop split fixture improves on the preceding baselines.
- [ ] Different paths referencing the same physical pool cannot be selected together.
- [ ] A route useful only at a small allocation is not removed solely because its full-input quote is poor.
- [ ] Small discrete fixtures agree with exhaustive feasible combinations.

## Testing / Verification
`uv run pytest tests/routing/test_path_split.py`; verify overlap, grid remainder and budget cases.

## References
DESIGN §§2.6–2.7, 2.12.

---

# I16 — [0.1.0] [Routing] Compare incremental allocation with shared pool state

Proposed metadata: Release 0.1.0 (proposed); milestone M3; priority High; labels `ready-for-agent`, `feature`.

## Objective
Test whether an incremental graph heuristic finds useful shared-liquidity plans beyond the disjoint-path baseline.

## Blocked By
- I15 — nonsharing baseline and reusable search/allocation seams.

## Blocks
- I21 — mandatory graph comparator.
- C01 — concrete baseline for a distinct Metis hypothesis.

## Implementation
1. Add `routing/algorithms/incremental_graph.py` with explicit chunk/budget controls and tentative per-physical-pool state.
2. Couple allocation and route exploration, normalize a complete ordered funding plan, and re-evaluate after any split/merge restructuring.
3. Preserve best simpler candidates and record how incremental estimates differ from actual final ordered swap semantics.
4. Keep heuristics and unsupported topology rules explicit; avoid importing Metis branding or the colleague's compiler.

## Out of scope
Global optimality, flash loans, assuming marginal aggregate output equals repeated fee-bearing swaps.

## Acceptance criteria
- [ ] Shared-suffix and shared-prefix cases produce valid, independently replayed plans.
- [ ] A fixture detects duplicated initial liquidity and inconsistent allocation/execution state.
- [ ] A nondivisible input fully allocates; budget exhaustion is visible.
- [ ] Capability-matched comparisons and expanded-topology results can be distinguished.

## Testing / Verification
`uv run pytest tests/routing/test_incremental_graph.py`; compare small plans with explicit sequential simulation and merged-call alternatives.

## References
DESIGN §§2.5–2.6; pre-research §§3.4, 4.

---

# I17 — [0.1.0] [Routing] Define the Uniswap SOR source and parity contract

Proposed metadata: Release 0.1.0 (proposed); milestone M0; priority High; labels `ready-for-agent`, `research`.

## Objective
Define the pinned upstream source scope, behavioral parity boundary and provenance needed to implement the mandatory SOR port and reference harness under the private internal research policy.

## Context
Initial inspected upstream commit: `04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647` (package 4.31.10). The upstream LICENSE contains GPL v3 text; attribution alone does not resolve obligations for translated code.

## Blocked By
- I01 — provenance/admission conventions and source facts.

## Blocks
- I22 — pinned source, parity and provenance contract for the harness.

## Implementation
1. Write `docs/references/uni-sor-port-contract.md` with exact included/excluded functions, source-to-behavior mapping, initial pin, dependency/license inventory and intended attribution/distribution.
2. Record source pins and required notices for the harness/imported dependencies, generated fixture data and translated Python port. Handle embedded source/data rights according to the actual artifacts; the repo remains private and internal.
3. Autonomously reuse and translate upstream code suitable for the intended private research use, preserving required notices and source mappings. Routine reuse needs no separate owner licensing decision or speculative distribution review. Do not change repository visibility, strip required notices or silently replace mandatory SOR behavior; record only a concrete source restriction that prevents the current use.
4. Define the actual-upstream goldens required from I22, including ordering/ties, BFS, overlap, remainder and provider substitutions. Do not generate the harness/corpus in this ticket.

## Out of scope
Node harness implementation, golden generation, Python port, implicit repository relicensing or a legal conclusion based solely on process isolation.

## Acceptance criteria
- [ ] Exact source pin, included behavior and excluded providers/V4/Exact Output scope are documented.
- [ ] Harness, fixture-data and translated-port obligations are assessed separately with sources and distribution assumptions.
- [ ] Required notices and source/version mappings accompany reused artifacts; permitted private internal reuse can proceed without a separate owner approval gate. Any concrete restriction preventing the current use is identified precisely, not inferred from hypothetical future distribution.
- [ ] The golden-fixture contract specifies verifiable selection/allocation behavior, not only quote quality.

## Testing / Verification
Cross-check the contract against the pinned source and concise artifact inventory. Verify the upstream behavior mapping and fixture categories are specific enough for I22/I18; retain notices without inventing a separate legal-approval milestone.

## References
DESIGN §2.7 and pinned URLs in §9; pre-research §3.2.

---

# I22 — [0.1.0] [Routing] Generate goldens from the actual pinned Uniswap SOR functions

Proposed metadata: Release 0.1.0 (proposed); milestone M1; priority High; labels `ready-for-agent`, `feature`.

## Objective
Produce reproducible upstream behavioral fixtures that can validate the Python port without a live quote provider.

## Blocked By
- I02 — common fixture/result serialization.
- I17 — pinned source/parity contract and required provenance/notices.

## Blocks
- I18 — actual upstream expected selections and allocations.

## Implementation
1. Add `tools/upstream/uni_sor/` with a locked validation-only Node harness calling the pinned upstream path enumeration and route-combination functions on frozen pools, percent grids, quote tables and gas-score inputs.
2. Produce `tests/fixtures/uni_sor/` goldens for BFS seeds/pruning, ordering/ties, pool conflicts, split limits, no-route and integer remainder behavior. Expected behavior must come from actual upstream functions, not rewritten JavaScript.
3. Save generation commands, dependency lock hashes, input/output hashes and both upstream selected allocation and cached-quote/remainder details. Implement `tests/routing/test_sor_fixture_schema.py` for offline ingestion.
4. Apply I17's artifact-specific notices/distribution controls. No Node/network dependency enters the timed Python algorithm runtime.

## Out of scope
The Python port, full AlphaRouter online service, corpus prices or empirical gas fitting.

## Acceptance criteria
- [ ] Every golden is traceable to an actual pinned upstream function call and frozen inputs.
- [ ] Two generation runs produce identical artifacts after dependencies are installed, or demonstrated upstream nondeterminism is explicitly scoped and canonicalized in the approved contract.
- [ ] All I17 parity categories are represented, including integer remainder and overlapping pools.
- [ ] Python can validate/consume the fixtures offline without Node installed.

## Testing / Verification
Run the documented locked harness command twice and compare hashes; inspect upstream-call provenance. Run `uv run pytest tests/routing/test_sor_fixture_schema.py` offline.

## References
DESIGN §2.7; `docs/references/uni-sor-port-contract.md`.

---

# I18 — [0.1.0] [Routing] Introduce the mandatory parity-tested Uniswap SOR Python port

Proposed metadata: Release 0.1.0 (proposed); milestone M3; priority High; labels `ready-for-agent`, `feature`.

## Objective
Make `uni_sor_port` a real benchmark participant whose declared routing-core behavior matches the pinned upstream implementation.

## Blocked By
- I03 — V3-compatible state/math interface.
- I10 — final allocation normalization and independent replay.
- I22 — actual upstream goldens under the resolved source/license contract.

## Blocks
- I21 — required SOR participant in the core acceptance run.

## Implementation
1. Implement `routing/algorithms/uni_sor_port.py` from the I17 contract, preserving scoped path enumeration, percent sorting, BFS, overlap exclusions, split/pruning/order behavior and remainder assignment.
2. Add `tests/routing/test_uni_sor_parity.py`; compare selected plans and exact allocations to upstream goldens, separating selection parity from re-quoted final output.
3. Declare V2/V3 capability and normalized fixture/provider deviations. Register matched-cohort and full-universe coverage modes with explicit LB exclusions.
4. Apply recorded notices/provenance and expose upstream pin/deviation metadata in results. The port uses the benchmark's pool/cost context for experiments; provider substitution stays visible.

## Out of scope
LB parity with upstream, V4/hooks, live Node fallback, copying the entire production SOR stack.

## Acceptance criteria
- [ ] Every supported deterministic golden passes exact selection/allocation parity.
- [ ] Final integer plans pass conservation and independent evaluator checks, including rounding remainder.
- [ ] Reports can identify the source pin, port scope and adapted providers.
- [ ] Matched V2/V3 comparisons share candidates; unsupported LB-only cases are not mislabeled no-route.
- [ ] A generic SOR-inspired implementation cannot satisfy this issue without parity evidence.

## Testing / Verification
`uv run pytest tests/routing/test_uni_sor_parity.py`; run an offline matched-cohort experiment with `uni_sor_port` and local baselines.

## References
DESIGN §§2.7, 2.11; `docs/references/uni-sor-port-contract.md`.

---

# I19 — [0.1.0] [Benchmark] Calibrate execution cost with held-out transaction evidence

Proposed metadata: Release 0.1.0 (proposed); milestone M4; priority High; labels `ready-for-agent`, `feature`.

## Objective
Provide an interpretable shared cost/price context with measured errors, enabling honest estimated-net comparisons without double-counting pool fees.

## Blocked By
- I01 — verified source/transaction identities and data access.
- I10 — complete-plan execution features.
- I11 — frozen price-context schema/data and corpus identity.

## Blocks
- I20 — validated sensitivity/applicability fields.

## Implementation
1. Add `benchmark/costs.py` and preparation/export helpers under `snapshot/`; preserve exact SQL, fixed dates, rows/hashes and transaction deduplication.
2. Verify Mantle fee components against receipts; distinguish whole-transaction overhead from pool fees and avoid repeated per-leg totals.
3. Fit the simplest supported shape model, save deterministic train/holdout assignment, residuals, feature coverage and applicability rules in a versioned artifact.
4. Consume I11's immutable price context without redefining `snapshot/prices.py`; convert native cost and expose nominal/low/high scenarios and unknown/extrapolated flags. Store the model as a separate immutable artifact and document it in `docs/references/cost-model.md`.

## Out of scope
Pretending historical router costs exactly predict arbitrary new execution graphs, inventing missing features or prices, training on the holdout set.

## Acceptance criteria
- [ ] Multi-hop transaction fixtures cannot multiply a total fee by leg count or add already-included fee components twice.
- [ ] Model artifacts reproduce predictions and holdout error statistics.
- [ ] Missing price/cost preserves gross results but withholds reliable net scores.
- [ ] Unsupported graph shapes remain flagged; at least one supported cohort has independently validated cost evidence.

## Testing / Verification
`uv run pytest tests/benchmark/test_costs.py`; reproduce the redacted export/model validation report and a fee-driven ranking reversal.

## References
DESIGN §§2.9, 2.12, 5.

---

# I20 — [0.1.0] [Report] Render offline quality-versus-cost comparisons with honest cohorts

Proposed metadata: Release 0.1.0 (proposed); milestone M4; priority High; labels `ready-for-agent`, `feature`.

## Objective
Turn saved run records into an offline report that explains algorithm trade-offs without hiding failures, coverage differences or fee uncertainty.

## Blocked By
- I12 — measured complete-status records.
- I19 — cost applicability and sensitivity contract.

## Blocks
- I21 — inspectable acceptance report.

## Implementation
1. Implement `report/aggregate.py`, `report/html.py` and `main.py report`; emit self-contained HTML and CSV from result files only.
2. Separate matched V2/V3 and full-source cohorts, gross/net objectives, common-success comparisons and all-scheduled-case status denominators.
3. Show quality/time trade-offs, per-amount/pair summaries, rank reversals, sample counts, representative plan traces, upstream source pins and replay commands.
4. Escape externally sourced text and provide useful static tables without JavaScript/CDN dependencies.

## Out of scope
Web server, live data requests, combining raw amounts of different assets into a meaningless average.

## Acceptance criteria
- [ ] Missing direct baselines show N/A; unsupported/no-route/timeouts stay visible in totals.
- [ ] Synthetic run records exercise paired comparisons, differing coverage and fee uncertainty.
- [ ] SOR scope/pin and Metis-inspired labels cannot be confused with full upstream products.
- [ ] Report generation succeeds offline and escaped token/error text cannot inject HTML.

## Testing / Verification
`uv run pytest tests/report/test_report.py`; open the generated artifact offline and check tables, links and sample traces.

## References
DESIGN §§2.7–2.11, 5.

---

# I21 — [0.1.0] [Benchmark] Deliver the reproducible six-algorithm acceptance report

Proposed metadata: Release 0.1.0 (proposed); milestone M4; priority High; labels `ready-for-agent`, `feature`.

## Objective
Deliver the first complete real comparison and a calibrated daily profile that satisfies the core-v1 correctness, coverage and usability contract.

## Blocked By
- I11 — frozen real five-source corpus.
- I16 — four baselines plus graph heuristic.
- I18 — mandatory parity-tested SOR port.
- I20 — measured, cost-aware report pipeline.

## Blocks
- C02 — stable core comparison for the optional challenge.

## Implementation
1. Calibrate `config/daily.yaml` and `config/full.yaml` on a recorded reference machine; fix corpus/strata, budgets, warmup/repeats and objective before final measurement. Keep parameter provenance from DESIGN §2.12.
2. Run all six mandatory algorithms on declared matched/full cohorts; rerun deterministic outputs and check case-order independence.
3. Compose an immutable final experiment manifest referencing the unchanged I11 corpus/price hashes and I19 model hash; never mutate the old corpus to attach cost context. Save evidence in `docs/references/v1-acceptance.md`, including source admission, SOR parity, cost holdout results, all failures and uncertainty.
4. Record measured daily/full wall time and profile limitations. Duration is not an acceptance threshold and needs no owner waiver; keep per-case limits, honest timeout records and all declared sources/algorithms/cases.

## Out of scope
Production latency or global-optimality claims, Jupiter implementation as a core release gate, silent scope reduction to pass timing.

## Acceptance criteria
- [ ] All five sources and all six mandatory algorithms are represented with explicit capability cohorts.
- [ ] Protocol admission, shared-pool correctness, SOR parity and cost-model validation evidence are linked.
- [ ] Exact deterministic replay succeeds; every scheduled outcome remains in the report.
- [ ] Daily/full wall time and environment are recorded. No total-runtime threshold blocks acceptance; correctness, reproducibility, coverage and honest timeout reporting remain mandatory.
- [ ] Offline HTML/CSV and exact reproduction commands are delivered, with held-out/exploratory labeling.

## Testing / Verification
Run `uv run pytest`, `uv run ruff check .`, `uv run mypy`, then the documented daily/full commands against the frozen bundle. Save stdout/manifest hashes and measured timings; do not claim template smoke tests validate the benchmark.

## References
DESIGN §§1.4, 2.10–2.12, 6, 8.

---

# C01 — [0.2.0] [Routing] Define a source-backed Jupiter Metis challenge

Publication metadata: Release 0.2.0 (planned challenge); milestone C1; priority Medium; labels `ready-for-agent`, `research`; state Todo. It is blocked by I16 and never attached to the core completion gate.

## Objective
Determine whether a reproducible Metis-inspired experiment can test a distinct routing hypothesis beyond the core incremental graph heuristic.

## Blocked By
- I16 — concrete graph baseline and behavior to improve or challenge.

## Blocks
- C02 — explicit go decision and bounded hypothesis.

## Implementation
1. Write `docs/references/jupiter-metis-challenge.md` using primary sources, including the archived Metis description and current API behavior distinctions.
2. Inventory publicly available code/licenses, separate verified facts from inferred methods, and map Solana assumptions to the Mantle frozen-state domain.
3. Define one distinct hypothesis, mechanism, expected differentiating fixtures, ablation, computational budget and go/no-go conditions. Explicitly compare it with `incremental_graph` rather than relabeling that algorithm.
4. Return a reasoned feasibility verdict. Insufficient source/reproducibility is a legitimate research outcome and leaves C02 blocked or canceled by an explicit scheduling decision.

## Out of scope
Claiming production Metis equivalence, live Jupiter quotes as Mantle ground truth, implementing a variant without a distinct experiment.

## Acceptance criteria
- [ ] Source/license availability and inference boundaries are explicit.
- [ ] The hypothesis differs observably from I16 and has a reproducible proposed test, or the memo documents why no credible experiment is available.
- [ ] The memo contains a clear go/no-go verdict and its consequences for C02.
- [ ] Core-v1 completion remains independent of this challenge.

## Testing / Verification
Review every algorithm claim against its cited primary passage; inspect the proposed ablation and record falsifiable success/failure criteria in the memo.

## References
DESIGN §2.8 and §9; `docs/references/pre-research-from-gpt-6-pro.md` §§3.4, 5.

---

# C02 — [0.2.0] [Routing] Evaluate the approved Metis-inspired challenge

Publication metadata: Release 0.2.0 (planned, conditional); milestone C1; priority Medium; labels `needs-info`, `Feature`; state Todo, blocked by C01 and I21. Release membership plans the work; it does not make the shell AFK-ready. Assign `ready-for-agent` only after C01 yields go and its concrete mechanism is inserted.

## Objective
Implement and measure the source-backed hypothesis selected by C01 on the same Mantle benchmark contracts, with transparent attribution and limits.

## Context
This is a conditional issue shell, not AFK-ready today. C01 must fill in the algorithm mechanism and quantitative experiment/budget before publication as executable work. Research-only completion cannot satisfy this implementation issue.

## Blocked By
- C01 — go verdict and explicit distinct algorithm/ablation contract.
- I21 — stable validated core corpus, runner and comparison report.

## Blocks
None.

## Implementation
1. After the go gate, add `routing/algorithms/metis_inspired.py` implementing the approved mechanism through the existing Python interface; reference the exact C01 decision section.
2. Add `tests/routing/test_metis_inspired.py` with distinguishing fixtures, conservation/state checks and a disabled-mechanism ablation.
3. Register the optional profile and compare against `incremental_graph` plus SOR/local baselines on matched objective, corpus, capabilities and budgets.
4. Publish `docs/references/metis-challenge-results.md` with source scope, measured trade-offs, failures and a keep/drop recommendation. Report negative results honestly.

## Out of scope
Production Jupiter engine claims, new live services, changing the corpus to favor the challenge, satisfying implementation with a literature summary.

## Acceptance criteria
- [ ] The C01 go contract has been inserted; no unresolved mechanism remains at implementation start.
- [ ] An executable Python variant passes common correctness checks and the hypothesis-specific tests.
- [ ] A same-budget ablation and paired baseline report are reproducible.
- [ ] Branding says Metis-inspired, source gaps remain visible and no core algorithm is silently replaced.

## Testing / Verification
`uv run pytest tests/routing/test_metis_inspired.py`; run the C01-defined challenge profile and archive manifest/report hashes. A no-go means this ticket remains unimplemented, not Done.

## References
DESIGN §2.8; `docs/references/jupiter-metis-challenge.md` (produced by C01).
