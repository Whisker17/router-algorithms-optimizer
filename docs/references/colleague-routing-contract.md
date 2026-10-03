# Colleague routing design: source register and comparison contract (WHI-1536)

Research deliverable for Release 0.2.0 (actual Release `a65958a5-f0b4-42e7-9ace-1d376e363bca`).
Written 2026-09-28 from product commit `ec42cef7468335a8290d23aa27213fdbaa2e7fb3`. This is
research only. It implements nothing, and it is not a solver.

## 0. Verdicts

| Concern | Verdict | Consequence |
| --- | --- | --- |
| **Fixed-plan replay (execution)** | **GO, bounded.** The SWAP-only M3 funding graph maps exactly onto the existing `RoutePlan`/`FundInput`/evaluator (§5), with one representation gap (G-1, §5.5) that needs a decision | WHI-1537 can be made ready once its Implementation section adopts §5 and the parent decides G-1 and §6 |
| **Policy resolution (JIT / local candidates)** | **NOT GO as a faithful feature.** The source fixes baseline failure, candidate ties and single-source execution. It leaves threshold equality/arithmetic, zero baseline and candidate failure open, and there are no frozen JIT/RFQ providers (§6) | Excluded and visibly `unsupported`, unless the owner decides D-P1…D-P4 for a labeled AMM-only adaptation |
| **Route search** | **BLOCKED.** No accessible source defines the search (§4) | WHI-1538 stays blocked and `needs-info`. No faithful or "inspired" solver may be implemented from this document |
| **Encoding (Packed/ABI)** | **Out of scope.** The benchmark does not need it (DESIGN §1.3, §7) | Nothing downstream |

A research Done state for this issue does not mark any implementation done or ready (§11).

## 1. Source identity

Everything in this document traces to one source: the colleague's
`plan-and-compilation.html`. It is the only upload in the Linear project description.

- Title: `计划、编译与编码（M3）· 技术评审讲解`. The page states its source as
  `docs/modules/plan-and-compilation.md`, version 2026-09-23.
- **196262 bytes, SHA-256 `5c259cc638b66f06cebe81f73ee1139bdd5d573dfb76e2197717080ebb0fcc63`**.
  A fresh copy fetched through Linear on 2026-09-28 was verified against these values.
- The file is a single self-contained HTML page with no external links. The page itself marks
  every amount, address, Adapter ID and quote as demo data (P3).

[`colleague-design/provenance.json`](colleague-design/provenance.json) records the retrieval,
the exact passages quoted here (`P1`…`P22`, Chinese original plus English translation), the
searches for other sources and the missing documents. Signed URLs are not recorded. Per DESIGN
§1.1 the attachment is not archived in the repository. The passages are short quotations kept
for private internal research. The page carries no license notice. No colleague was contacted.

**Missing documents.** Searches covered the Linear project, its documents and attachments,
issue and document search, GitHub code search and the local workspace. None of the following
is accessible:

- the routing-and-solving module named in §11 of the page (P19);
- the GraphQuoter specification or code (P4);
- the Router execution/settlement module;
- the protocol adaptation module (Adapter/Codec/Quoter, JIT/RFQ providers);
- the quote/transaction preparation module;
- the markdown original of the page.

## 2. Four separate concerns

The page describes a pipeline that starts **after** the solver has chosen topology, shares,
policies and order (P1, P8, P19). This contract keeps four concerns apart:

| Concern | Input → output | What the source provides | Benchmark treatment |
| --- | --- | --- | --- |
| **Route search** | request + frozen state → chosen funding graph | Only assertions: a routing solver exists, it uses "four template classes" and "at most one split group" (P15), and GraphQuoter valued the plan in call order (P4). No definitions | Blocked (§4) |
| **Fixed-plan replay** | chosen graph + ratios + order → actual per-step amounts and states | Full integer and ordering rules (§§3–6, 9.2, 10) | Maps onto `RoutePlan` + `routing.evaluator.evaluate` (§5) |
| **Policy resolution** | per-hop baseline + candidates → one executed source | Partial rules (P4, P7, P11) | Excluded pending decisions (§6) |
| **Encoding** | execution IR → route bytes → calldata | Complete byte formats (§§7–8) | Not reproduced (§7) |

An encoding result (for example 7 semantic nodes → 5 instructions → 182-byte route → 452-byte
calldata) is not evidence of routing quality or gas savings. All five real swaps remain (the
page's own §5.3).

## 3. Feature map

Legend: **S** = directly specified by the source; **E** = existing benchmark equivalent;
**A** = adaptation required; **U** = unsupported here; **Q** = unresolved.

| Feature | Source | Status | Benchmark counterpart / note |
| --- | --- | --- | --- |
| Root split by bps of a frozen base, one final REMAINDER | P9, §7.4 | S, E | Explicit integer `FundInput` + last consumer `ALL_REMAINING`. The same floors as `direct_split.leg_amounts` and SOR D-1 at the root (§9) |
| Per-layer bps on each layer's own frozen base | P9, P18 | S, A | Explicit integers computed from the actual parent balance during recovery (§5.3) |
| Pure SPLIT/MERGE as ownership only, no extra call | §1, §5.2 | S, E | `RoutePlan` has no split/merge steps. A multi-input step is a merge |
| MERGE_INTO ALL (single-consumer merge) | §5.2 | S, E | One step with `ALL_REMAINING` on every member |
| MERGE_INTO BPS then SINGLE REMAINDER (merge then split) | §10.4 | S, A | Members decomposed greedily (§5.3). No same-token swap is invented |
| Zero input: no quote, ready zero output, relation kept | P13, P16, P17 | S, E / A | Evaluator zero-input step. A zero reference to a drained fund is gap G-1 (§5.5) |
| Unready slot is never a zero balance | P13 | S, E | The evaluator rejects external/unproduced funds |
| Selected external call order preserved; later calls see earlier state | P4 | S, E | The evaluator keeps submitted order and one state per physical pool |
| Whole-graph valuation in order (GraphQuoter) | P4 | S (claim only), Q | The evaluator's ordered replay is the benchmark's analogue. GraphQuoter's own rules are unseen |
| Single producer, fresh outputs, exclusive merge members, no cycles, swap tokens differ | §9.2, §7.4 | S, E | `_check_plan` + ledger drain rule |
| Terminal list, merged slot never terminal | §3.1, §7.4 | S, A | Every target-token balance is summed; other leftovers are `invalid_plan` residuals |
| Merge member limit 4 (configurable), 1 bps threshold | P12, P4 | S (demo config) | Not adopted as benchmark defaults |
| JIT_SWAP baseline + candidates, threshold vs baseline once, first-wins tie, baseline failure reverts | P4, P7, P11 | S (partial), Q | Excluded (§6) |
| JIT/RFQ providers (Fermi, Kipseli), inventory state | P4 | U | No frozen provider model exists |
| OrderSpec `minAmountOut`, `deadline`, `recipient`, fee binding, native wrap | §3.1 | U | The benchmark reports gross/net output. Settlement constraints are not modeled |
| Four solver templates, one split group, candidate generation, allocation, scoring, ties, budgets, stop rules | P15, P19 | Q | No source (§4) |
| Compiler errors → upper layer re-solves another graph | P14 | Q | Belongs to the missing search contract |
| Encoding, planHash/routeHash, Router decode, ABI | §§7–9 | U | Out of scope (§7) |

## 4. Route search: blocked

**What the source says.** The routing solver had already chosen the three-way allocation
(P1). The call order X→Y→C→V→Z is fixed, and GraphQuoter valued the whole plan in that order,
including shared-pool and inventory state (P4). Merge-then-split encoding extends only what
can be encoded, while the solver's "four template classes" and "at most one split group" stay
unchanged (P15). The routing-and-solving module delivers a RoutingPlan whose topology,
ratios, policies and call order are already fixed (P19).

**What cannot be inferred.** The page never names or defines the four templates. The main
example is one instance with one split group. The appendix graphs (§10.3: three split groups;
§10.4: two) exceed "at most one split group", and the page explicitly says the solver does
not search them. Encoding examples therefore say nothing about the search grammar, and this
contract derives none from them.

**Missing inputs.** WHI-1538 needs every item below before it can be dispatched:

| # | Missing input |
| --- | --- |
| S1 | Definitions of the four template classes (shapes, hop limits, where merges and splits may occur) |
| S2 | What "one split group" means: does a merge count, is the shared suffix part of the group, how are nested groups treated |
| S3 | Candidate pool/path generation: universe, per-pair limits, hop bound, pool identity and deduplication |
| S4 | The share/allocation search: bps grid or continuous method, minimum shares, remainder assignment |
| S5 | The objective: gross, net or gas-adjusted, and whether JIT candidates enter valuation |
| S6 | GraphQuoter valuation rules: ordering of branches, how shared state and inventory are charged, how quote failures are handled |
| S7 | How the external call order is chosen among valid topological orders |
| S8 | Ties, determinism and tie-break ordering |
| S9 | Budgets, truncation and stop rules |
| S10 | Fallback to simpler routes, and handling of ErrUnencodableSemantics/ErrFormatLimit re-solves (P14) |
| S11 | Solve-time selection of JIT candidates and their parameters |

**Pseudocode.** None can be written without inventing S1–S11. The acceptance criterion's
blocked branch applies. A differentiating *search* example cannot be defined either. The
execution-level differentiator is in §10.

**Paths forward.** Both need the owner and parent; neither is started here.

- (a) Obtain the routing-and-solving and GraphQuoter sources, then redo §4 and target a
  faithful identity.
- (b) The owner authors an explicitly named `colleague_inspired` grammar. That contract must
  state S1–S11 itself, list every difference from the colleague design, bring independently
  derived fixtures, and claim no parity.

## 5. Fixed-plan replay contract (execution GO)

### 5.1 Integer rules

All amounts are integer raw units.

- `BPS_OF_PARENT(b) = floor(base × b / 10000)`, where `base` is the parent's frozen produced
  amount. Every sibling uses the same base (P9: the second way is still based on
  1 000 000 001).
- `REMAINDER = base − consumed` and must be the group's last executed consumer.
- `ALL` takes the whole amount of a slot that has exactly one consumer.
- A layer never multiplies ratios into a flattened share. For example
  `floor(floor(3 × 50%) × 80%) = 0`, while the flattened `floor(3 × 40%) = 1` is invalid.
- `MERGE_INTO`: consume and close the members, freeze their sum as a new base, then take the
  first branch (`ALL` or `BPS`). Later branches use `SINGLE` on the merged base and end with
  `REMAINDER`.
- A zero input makes no quote or swap, produces a ready zero output and keeps its consumption
  relation. A merge completes even when its first branch is zero (P13, P16, P17).

### 5.2 Order and shared state

The source keeps the selected external call order and never picks another topological order
(P4). The evaluator already replays steps in the submitted order, with one transaction-local
state per physical pool. A recovered plan therefore emits steps in the source instruction
order.

It must **not** be normalized with `incremental_graph.merged_plan`. That function re-sorts by
token topology and merges all uses of one pool. Each `M3 SWAP` becomes exactly one
`SwapStep`, so repeated use of a pool stays sequential and fee-bearing, as the source orders
it.

### 5.3 Canonical recovery to `RoutePlan`

The adapter pre-evaluates on the frozen snapshot, and that work is charged to solve. The
recovered plan is a concrete fixed-snapshot plan, not an amount-independent policy.

1. Slot 0 → `REQUEST`. Every swap output slot `k` → fund `s<k>`. A merged slot has **no**
   fund: references to it resolve to its members.
2. `SINGLE(k, BPS)` → `FundInput(s<k>, resolved int)`.
   `SINGLE(k, ALL|REMAINDER)` → `FundInput(s<k>, ALL_REMAINING)`.
3. `MERGE_INTO(m, ALL, members)` → one `FundInput(member, ALL_REMAINING)` per member.
4. A BPS draw from a merged slot takes its resolved amount greedily from members in ascending
   slot order, and emits only the positive takes. A zero draw emits `(lowest member, 0)`.
   Every decomposition gives the swap the same total, so the greedy rule only makes recovery
   deterministic.
5. A REMAINDER on a merged slot → `ALL_REMAINING` on every member that still holds a balance.
6. Step order = instruction order. `token_in`/`token_out` come from the slot tokens and the
   pool. No same-token step is ever added.
7. Comparison: source and recovered traces must agree per instruction on `amount_in`,
   `amount_out` and zero-input status, and the plan must evaluate `ok` with no residual.

`route_features` depend on this representation. In fixture F3, for example, the recovered plan
has `split_funds = 2` and `merge_steps = 1`, while the source has one merge and two split
groups. These counts are not M3 instruction counts. The current empirical cost model only uses
split/merge as a chain-versus-parallel bit (`benchmark/costs.py`), so the cohort is unchanged.

### 5.4 Shapes outside the admitted replay domain

- **JIT_SWAP hops.** See §6. They are `unsupported`, except for a separately labeled
  `policy_off` replay that uses the baseline pool as an ordinary SWAP. That replay is an
  ablation, not the source's behavior.
- **Same-token split of a split child with no swap in between.** The semantic rule is
  well-defined (F5), but the source shows split fusion only when "each child is fully consumed
  by its branch's first hop" (§5.2 of the page). Whether such a graph can be encoded is
  unknown, so it is admitted only as an integer rule and a recovered-plan check, never as a
  source golden.
- **Anything the evaluator rejects**: token cycles, external funding, unknown pools, reused
  outputs, residual balances. These stay `invalid_plan`, with no evaluator weakening.
- **Merge member limit.** The limit of 4 is page configuration. Adopting a limit is a profile
  decision.

### 5.5 Gap G-1: zero reference to a drained fund

The evaluator drains a fund as soon as its balance reaches zero after a consumer. Any later
reference fails with "already fully consumed", even at amount zero. M3 allows a zero final
REMAINDER, for example `BPS 10000` followed by `REMAINDER` (P17 and the §7.4 bps rules), and
lets every child of a zero-valued parent resolve to zero. Such a plan has no representable
reference. Fixture F6 proves this against the unchanged evaluator.

There are two options; the parent decides for WHI-1537:

- **Elide (recommended).** Drop a reference whose fund is already drained and whose amount is
  0, drop a step whose references are all dropped, and record each elision as a diagnostic.
  The result is exact: the source makes no pool call for a zero input either, and outputs,
  states and pool calls are unchanged. F6's elided candidate evaluates `ok`.
- **Common-contract change.** Allow zero references to drained funds in the evaluator. This is
  outside WHI-1537's scope and needs its own decision.

### 5.6 Hand-derived fixtures

[`colleague-design/fixtures.json`](colleague-design/fixtures.json) holds the fixtures below.
Expected values were derived by hand, and each derivation is written beside its value. Pool
outputs are either printed by the source (`origin: source`) or chosen for replay
(`origin: fixture`). None comes from pool math.

| Fixture | Mechanism | Key expected values |
| --- | --- | --- |
| IR-1 / F1 | root remainder, shared suffix, main case | 1000000001 → **500000000 / 350000000 / 150000001**; merged Z input 0.150+0.104+0.044 = **0.298 WETH**; nominal-1500 flattening leaves **1** unit → `invalid_plan` |
| IR-2 / F5 | nested rounding | layered **0** vs invalid flattened **1**; allocations X1/X2/Y = 0/1/2 |
| IR-3 | per-layer bases (page §10.3) | 600/401, 75/226, 401/602 |
| IR-4 / F3 | merge then split (page §10.4) | **101 + 199 = 300 → 180 / 120**; recovered BC1 inputs `s1:101, s2:79`, BC2 `s2:ALL_REMAINING`; re-summing members → `invalid_plan` |
| F2 | shared suffix (page §10.2) | 1001 → 600/401, merged ALL |
| F4 | zero allocation | root 1 → 0/1; merged base 0+1 = 1 → 0/1; 2 zero-input steps, 2 pool calls; a consumed member is not reusable |
| F6 | gap G-1 | zero REMAINDER → `invalid_plan` "already fully consumed"; elided plan `ok` |
| F7 | display-order discrepancy | §5.7 |

`uv run python docs/references/colleague-design/check_fixtures.py [--attachment FILE]` checks
all of them. It recomputes the integer rules, runs its own small M3 slot ledger (written from
the page's rules, not from the evaluator), recovers each plan by §5.3, and replays it through
the unchanged `routing.evaluator.evaluate` using a scripted pure quote seam. It exits non-zero
on any mismatch; a mutated expectation was confirmed to fail.

### 5.7 Display discrepancy

The page's single data model, `SEM_NODES` (P20), orders the nodes N0…N6 with N5 MERGE before
N6. But its displayed `RoutingPlan.nodes` instance (P21) is generated as N0, then every SWAP
node (N1, N2, N3, N4, **N6**), then a hard-coded N5 line. That puts N6, which consumes F140,
before N5, which produces it. The type comment calls `nodes` the "selected order" (P6).

F7 shows the `SEM_NODES` order passes producer-before-consumer and the displayed order fails
at N6. This does not change the five-swap call order X→Y→C→V→Z, because MERGE is pure
ownership and lowers to #4's input phase.

**Disposition:** goldens use the `SEM_NODES`/`INSTRS` order. The displayed list is
page-rendering output and is not copied. It says nothing about unseen production code, and no
production bug is alleged.

## 6. Policy resolution (JIT): excluded pending decisions

| Rule | Source | Status |
| --- | --- | --- |
| At the hop, the Router compares the selected candidates on the **actual input** and **executes one source** | P4 | Specified. Only the chosen source's state changes, which is the selected-only commitment |
| Baseline and candidates quote the same `amountIn` | page §5.2 | Specified |
| Baseline quote failure reverts the whole transaction | P11 | Specified → in replay the whole plan fails (`invalid_plan`), never falls back |
| Threshold judged once, relative to the baseline | P11 | Specified in words. Its exact arithmetic is **not** specified |
| Candidate tie → keep the first-appearing | P11 | Specified. Whether the baseline counts as "appearing" when the threshold is 0 is ambiguous |
| Threshold equality (`≥` or `>`), the formula (for example `cand × 10000 ≥ base × (10000 + bps)`) and its rounding | none | **D-P1, unresolved** |
| Baseline succeeds with zero output | none | **D-P2, unresolved** |
| A candidate quote fails or partially fills (skip or revert) | none | **D-P3, unresolved** |
| Candidate probes are side-effect-free quotes | implied by P4 ("executes one") | Adaptation assumption |
| Where candidates come from (JIT/RFQ inventory, signed quotes) | P4 names only demo providers | **U**: no frozen provider model; S11 unresolved |
| Zero input at a JIT hop | P13 (no quote) | Consistent: no policy runs and the output is zero |
| 1 bps threshold | P4 | Demo configuration, not a benchmark default |

**Disposition.** A faithful JIT policy is not implementable. An AMM-only local-candidate
experiment is possible only as a labeled adaptation. It would use other admitted frozen pools
on the same pair as candidates, quote them on the current transaction-local state, commit only
the chosen pool and emit that pool into the plan. It needs owner decisions on:

- D-P1…D-P3;
- **D-P4**: who selects candidates and the threshold, and whether probes count against
  `Budget.max_quotes`. They should, because every probe is a quote.

Until then, WHI-1537 treats a non-empty candidate set as `unsupported`. The `policy_off`
ablation (§5.4) is labeled as such.

## 7. Encoding: out of scope

Packed Encoder bytes, TokenTable, slot descriptors, opcodes, Candidate/Adapter codecs,
planHash/routeHash, Router decode validation, ABI layout and calldata sizes are not benchmark
requirements (DESIGN §1.3, §7). They were not reproduced, and their numbers are not
performance evidence. The encoding rules that matter semantically (exclusive members, fresh
slots, REMAINDER last, zero-input handling) are restated in §5.1 and already enforced by the
evaluator.

## 8. Domains

- **Protocols.** The frozen, verified pools of the benchmark bundle. For a matched comparison
  with SOR variants, use the V2/V3 cohort; the full five-source universe is labeled separately
  (DESIGN §2.7, §2.11). No live RPC during solve.
- **Topology.** Any acyclic funding graph expressible by §5.3, including shared suffixes,
  merges, merge-then-split and repeated physical pools in order. The search's own topology
  domain is unknown (S1, S2).
- **Providers.** AMM pools only. JIT/RFQ/inventory providers are unsupported.
- **Objective.** The shared `ObjectiveContext` (gross or estimated net). Unknown costs stay
  unknown. The source's settlement constraints (`minAmountOut`, `deadline`) are not modeled.
- **Ordered state.** One evolving state per physical pool, in submitted order (§5.2).

## 9. Comparison with existing strategies

| | M3 replay semantics | `path_split` | `incremental_graph` | `uni_sor_port` and `uni_sor_adaptive` / `uni_sor_optimized` |
| --- | --- | --- | --- | --- |
| Allocation arithmetic | independent floors on each layer's frozen base, REMAINDER last | independent floors of the root on a grid, last leg `ALL_REMAINING` | cumulative chunks `floor(A·k/K) − floor(A·(k−1)/K)`, summed per path | independent `quotient(A·p/100)` per route, last route `ALL_REMAINING` (D-1) |
| Shared suffix / pool | yes, ordered | no (pool-disjoint) | yes, one merged step per pool | no (pool-conflict exclusion) |
| Merge then split | yes | no | yes, with absolute amounts from chunk accounting | no |
| Order | selected external call order | step order of its legs | re-sorted token-topologically | B-F1 order |
| Search | unknown (§4) | branch-and-bound on a grid | greedy marginal chunks | upstream BFS over percents |

Rounding check, verified with the repository helpers for shares 50/35/remainder (grid units
10/7/3 of 20):

| Input | M3 layered floors | `path_split` / SOR root floors | `incremental_graph` chunks |
| --- | --- | --- | --- |
| 1000000001 | 500000000 / 350000000 / 150000001 | 500000000 / 350000000 / 150000001 | 500000000 / 350000000 / 150000001 |
| 19 | **9 / 6 / 4** | 9 / 6 / 4 | **9 / 7 / 3** |

The root arithmetic of the grid solvers matches M3. Only per-layer bases on *realized
intermediate* balances, and shared structure, distinguish M3. `incremental_graph` expresses
merges but not bps-of-realized-parent layering.

## 10. Differentiating fixture and ablations (for WHI-1539)

- **Execution differentiator.** F1's shape has three branches (one of them two-hop) merging
  into one shared final pool, and F3's has a merge then a split on the realized merged balance.
  Neither `path_split` nor any SOR variant can emit these shapes. `incremental_graph` can emit
  their topology, but it re-orders steps and derives absolute amounts from its own search. In
  the same snapshot, a fixed ratio plan and its recovered explicit-amount plan are equivalent
  by construction (§5.3), so fixed-plan replay measures semantic equivalence and adaptation
  overhead only, never routing quality.
- **Ablations**, once their inputs exist:
  - (i) the same fixed plan: the source-rule adapter against the plan's direct `RoutePlan`
    form (equivalence and overhead only);
  - (ii) `policy_off` against an approved AMM-only policy on the same plan (only after D-P1…D-P4);
  - (iii) the search against `incremental_graph`/`path_split` on matched cohorts (only after
    S1–S11).

## 11. Downstream consequences and proposed amendments

These are proposals for the parent orchestrator. They were not applied, and no tracker state
was changed.

**WHI-1537 (plan semantics): execution GO, bounded.**

- Replace Implementation step 1 with "implement contract §5.3 exactly".
- Add G-1 handling per the parent's choice (§5.5; elision recommended).
- State that JIT/candidate hops are `unsupported` until D-P1…D-P4 are decided, and allow only
  a labeled `policy_off` ablation.
- Change the policy acceptance criterion ("Threshold-boundary/tie/baseline-failure tests match
  the approved policy contract") to "baseline failure → `invalid_plan`; ties first-wins where
  a policy is approved; otherwise candidate hops are visibly `unsupported`".
- Cite `colleague-design/fixtures.json` F1–F7 and `check_fixtures.py` as the source goldens,
  and require a new test to reproduce them through the adapter.
- Keep `needs-info` until the parent decides G-1 and whether policy stays excluded. Then the
  issue can become `ready-for-agent`.

**WHI-1538 (solver): search BLOCKED.**

- Keep `needs-info` and the blocking relation.
- Add the S1–S11 list as the explicit missing inputs.
- Add "neither a faithful nor a `colleague_inspired` identity may be implemented until either
  the sources are provided or an owner-authored grammar contract exists (§4 paths a/b)".
- Research Done on WHI-1536 does not unblock it.

**WHI-1539 (benchmark).**

- Keep it blocked behind WHI-1538 for full-search comparison.
- Note that only question (a), fixed-plan equivalence and overhead, becomes possible after
  WHI-1537, and that it must never be ranked as routing latency or quality.

**DESIGN.md** gains a short boundary statement (§2.13) pointing here.

## 12. Verification performed (2026-09-28)

- Attachment freshly fetched via Linear GraphQL. Byte length 196262 and SHA-256 matched the
  issue's values (`check_fixtures.py --attachment` → ok).
- All passages were read from the fetched file. The `SEM_NODES` array and the
  `RoutingPlan.nodes` generator were inspected in the embedded script.
- Referenced-source searches are recorded in `provenance.json`; none found a source.
- Every fixture was derived by hand and checked by `check_fixtures.py` against the unchanged
  evaluator: all checks passed, and mutating three expectations produced three failures.
- The rounding comparison in §9 was computed with `direct_split.leg_amounts` and the
  `incremental_graph` chunk formula.
- The contract was compared with `routing/plan.py`, `routing/evaluator.py`,
  `routing/algorithms/base.py`, `incremental_graph.py`, `path_split.py` and `uni_sor_port.py`
  at `ec42cef`. No performance campaign was run, and no historical report or profile was
  changed.
