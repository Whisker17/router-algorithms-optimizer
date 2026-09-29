# Mantle Router Algorithm Optimizer — Design / PRD

Status: product direction approved through the design interview; design and issue decomposition finalized after interactive fable5 review on 2026-09-24 ([review record](references/issue-design-review-fable5.md)). Subsequent owner-directed updates made Solidity-source migration explicit and published the versioned issue set; [independent publication acceptance](references/linear-publication-audit.md) passed. Implementation is not started. Uniswap SOR is a mandatory comparator; Jupiter/Metis is a challenge track. Real planned Releases **0.1.0** and **0.2.0** now exist in the owner's pipeline.

The spec of record is this file. The source issue design and published-ID cross-reference are in [ISSUE_PLAN.md](ISSUE_PLAN.md). Existing repository code is a Python template, not any of the modules described below.

## 1. Background & Goals

### 1.1 Vision

Build an offline, reproducible experimental tool for choosing and improving Mantle swap-routing algorithms. Freeze a real liquidity state, run several algorithms against identical requests, independently evaluate their plans, and explain the trade-off between output quality and computation. The deliverable is evidence that can guide a production Router, not a production Router itself.

The Linear project is [Mantle Router Algorithm Optimizer](https://linear.app/whisker-personal/project/mantle-router-algorithm-optimizer-3abba3f613f7), project UUID `29829418-7ca6-43c9-bf03-a49f05c76b1a`, team `Whisker-Personal` (`WHI`). Its description calls for a pluggable benchmark, static liquidity snapshots, and historical fee calibration using Dune.

The colleague's `plan-and-compilation.html`, attached to the Linear project description linked above (not archived in this repository; fetch the current attachment URL from Linear), explains compilation and execution after route selection. Its conservation, rounding and ordered-state lessons are useful; its IR, opcodes, JIT policies, registry and ABI are not requirements here. Release 0.2.0 adds it as an optional comparator; its boundary is §2.13. The local [pre-research](references/pre-research-from-gpt-6-pro.md) supplies algorithm leads, not a validated implementation specification. Its unresolved citation markers and illustrative numeric parameters must not be treated as verified evidence.

### 1.2 v1 Scope

- Python 3.12+, algorithms called through one Python interface.
- One real Mantle block snapshot; synthetic fixtures additionally verify mathematical and flow invariants. No rolling snapshots or historical state replay during a benchmark.
- Single-token input/output, Exact Input, integer base units, no borrowed capital.
- Five liquidity sources: Agni v3, Merchant Moe Liquidity Book v2.2, FusionX v3, Uniswap v3 and Merchant Moe Classic v1. Source names are discovery labels; actual deployments and semantics must be verified.
- Exact protocol math and deterministic execution of ordered plans, including splitting, merging and shared physical pools.
- Four explanatory baselines, one incremental graph heuristic and one mandatory **Uniswap SOR routing-core Python port** with upstream parity evidence.
- Dune-derived request sampling and execution-cost calibration; fixed-block contract quote/simulation checks for supported pool math.
- CLI, machine-readable results and an offline HTML report. Report measured daily/full-run duration on a recorded reference machine; there is **no total-runtime acceptance threshold**. The owner withdrew the earlier ten-minute target. Per-case resource limits still prevent runaway work and are reported explicitly.
- Jupiter/Metis is an explicitly separate future challenge. The owner has now limited the current execution batch to **0.1.0 only**: neither its research nor its implementation is required or authorized in this batch; the existing 0.2.0 issues remain future work.

### 1.3 Non-goals

Production quote APIs, wallet integration, signing/broadcast, Router contracts, calldata compilation, JIT/RFQ, cross-chain swaps, Exact Output, multi-input/output orders, flash loans, cyclic arbitrage and state changes between benchmark requests. Fee-on-transfer, rebasing and other unsupported token semantics are excluded explicitly, not silently approximated. Native MNT is normalized to WMNT for the initial routing universe; native wrap/unwrap entrypoints are not modeled as free operations or counted as supported user orders.

No claim of global optimality, complete Mantle liquidity coverage, production latency, complete transaction executability, or reproduction of Jupiter's proprietary production engine follows from this benchmark.

### 1.4 Success criteria

1. A versioned data bundle contains a block hash, validated pool states, fixed requests, price/cost context and provenance; its hashes permit independent replay.
2. Every admitted source has its own deployment/semantic validation evidence. All five sources appear in the full-source corpus, or core v1 remains incomplete with an explicit blocker. Partial reports are still allowed and labeled.
3. On declared fixtures, exact-in pool math agrees in integer output with the authoritative fixed-block quote or simulated swap. Unexplained differences fail admission; there is no generic bps tolerance that hides a protocol-math bug.
4. The evaluator rejects invalid funding, inconsistent states and incomplete plans; valid shared-pool examples reproduce an independent expected result.
5. The six mandatory algorithms emit plans through the same interface. The SOR port passes the scoped upstream parity contract in §2.7, not just a generic route-quality test.
6. Results preserve every scheduled case, including unsupported, no-route, timeout and model-error outcomes. Like-for-like comparisons use declared common cohorts.
7. Raw and estimated net outputs, measured latency and fee sensitivity can be inspected in HTML and exported. Replays reproduce deterministic routes/outputs; wall-clock timings are expected to vary.
8. Record daily/full-profile wall time on the documented machine. Runtime is an observed metric, not a pass/fail criterion or an owner-approval trigger. Completeness, correctness, reproducibility and coverage remain required; failures, sources, algorithms and cases cannot be silently dropped to improve timing.

## 2. Requirements / Specification

### 2.1 Domain vocabulary

| Term | Meaning |
| --- | --- |
| Snapshot | Immutable protocol state at one chain ID, block number, hash and timestamp |
| Universe | Explicit admitted set of pools and tokens, with selection and exclusion reasons |
| Case | Exact-input request: token addresses, raw input amount and snapshot ID |
| Plan | Ordered swaps with explicit funding references and terminal funds |
| Physical pool | One mutable state identity, regardless of how many plan edges reference it |
| Quote | Pure result of simulating a swap from a supplied state, including its next state |
| Evaluation | Fresh replay of a plan from the original snapshot, independent of solver claims |
| Gross output | Target-token output after protocol fees and price impact, before execution cost |
| Estimated net output | Gross output minus execution cost converted to target-token units |
| Cohort | A declared common case/pool/capability set for a comparison |
| Best known | Best valid result found under a recorded budget; not a mathematical upper bound |
| Certified bound | An upper bound proven for a declared candidate domain and objective ([research-021 contract](references/research-021/contract.md) §4); numerical estimates and best-known values are not bounds |
| Challenge | Optional extension with a documented feasibility/stop decision, outside the core gate |

### 2.2 Discovery and immutable snapshot

Preparation first verifies factory/deployment addresses, relevant contract versions/code hashes, token metadata, fees and any protocol-specific behavior. Do not classify a pool solely by its Dune project name or ABI resemblance. Dune `project_contract_address` is an emitting address and can identify a router instead of a pool.

Discover candidate pools through verified deployment/factory information and historical activity. Record both the selection method and omitted sources/pools. Activity coverage, trade-volume coverage and liquidity-depth coverage are different quantities. The first universe is an explicit list rather than a hardcoded claim of “100 pools.” Include admitted inactive pools when the documented discovery policy identifies useful depth; do not confuse zero recent volume with zero liquidity.

Owner-selected access policy (2026-09-24): use the default public Mantle mainnet RPC `https://rpc.mantle.xyz`; a read-only `eth_chainId` probe returned `0x1388` (5000). This establishes current connectivity/chain identity only, not historical-state or archive guarantees. Select an accessible sufficiently finalized block and preflight fixed-block reads and all required state before freezing it. Use bounded retries/backoff and cached reads; never change to `latest`, mix blocks or introduce a paid/private RPC silently. If the public endpoint cannot supply necessary state, record the exact missing capability while continuing independent work.

Dune access uses the owner's existing Enterprise subscription. Ordinary scoped discovery, export and fee-calibration queries are authorized without per-query approval or an invented numeric credit cap. Apply chain/date/partition filters, begin with bounded samples, reuse cached exports/execution results, and choose modest compute unless evidence justifies more. Avoid unbounded historical scans, duplicate full exports, excessive concurrency and repeated costly failed queries; log query IDs and usage/cost metadata when available. Enterprise access is not an instruction to maximize spend. Preparation may access the network; measured solver runs remain offline.

Choose one sufficiently finalized, RPC-readable block within the available indexed history. Freeze its number, hash, timestamp and chain ID (Mantle mainnet `5000`). Every state read uses that block and verifies the hash before publication. RPC failure must never fall back to `latest` or mix blocks. Reorg/hash mismatch invalidates the pending bundle.

State includes reserves for Classic pools; price, active liquidity, tick spacing, initialized tick data and fee configuration for CL pools; bins, active bin, static/variable fee parameters and timestamp-dependent accumulators for LB. The timestamp is frozen, but an LB swap may update its dynamic-fee state within a plan. Model these transitions exactly.

Collect sufficient initialized ticks/bins to evaluate the declared amount envelope in both directions. A state-range exhaustion returns `incomplete_snapshot`; it is not equivalent to exhausted real liquidity. Final publication requires all reference cases to fit the collected state or be explicitly excluded before algorithms are evaluated. Save completeness bounds and source-read evidence. Historical RPC support and full state recovery are M0 feasibility checks, not presumed available.

Bundle layout is a directory containing `manifest.json`, protocol state files, `cases.jsonl` and price context, all covered by SHA-256. JSON amount fields are decimal strings; use Python integers after validation. Writes go to a temporary directory and become visible only after checksums and schema validation pass. Full bundles live under gitignored `data/`; small regression fixtures are checked in. No database is needed.

A corpus can be frozen before empirical cost calibration and is then usable for explicitly gross-only experiments. Cost-model artifacts are separately immutable. The final experiment manifest binds the unchanged corpus hash, objective, price-context hash and cost-model hash; adding a model creates a new experiment identity, never an in-place rewrite of an earlier bundle. Per-source admission fixtures may use provisional blocks. Final corpus preparation chooses and verifies one common block and freshly runs all five collectors there; incompatible or incomplete source state at that block blocks publication instead of silently removing a source.

### 2.3 Pool simulation and admission

The common seam is `quote_exact_in(pool_state, token_in, amount_in_raw, context) -> SwapResult`, with output amount, new state, consumed amount and execution features. It must not mutate the input snapshot. Results distinguish insufficient real liquidity, unsupported semantics and missing snapshot state. Partial fills do not silently satisfy Exact Input.

The simulation is a source-traceable migration of the **deployed Solidity swap/quote math and relevant state transitions into Python**, not merely an implementation of a generic AMM formula. For every admitted source, pin the chain deployment/code hash, matching source commit or verified source, relevant Solidity libraries/functions/storage fields, and their Python counterparts. Document every omitted branch and why it is outside the admitted Exact Input domain. Preserve Solidity integer widths, signedness, overflow/revert behavior and rounding where observable. A deployment whose source/behavior cannot be matched stays unadmitted. This is not a migration of complete Router/token/governance contracts, and benchmark solves do not execute Solidity on an EVM; fixed-block contract/fork runs are the independent differential reference.

Use protocol integer arithmetic, rounding direction and overflow/revert behavior. Continuous optimization may use approximations internally, but final evaluation always uses exact math. A shared CL implementation is allowed only after Agni/FusionX/Uniswap differences are checked; each source retains its own admission record and tests.

Admission tests cover both swap directions, small/large input, tick/bin boundaries, multiple crossed intervals, dynamic-fee transitions, zero/insufficient liquidity and integer dust. Use a protocol quoter at the fixed block where authoritative; otherwise a fork simulation of the pool operation. Store request, block identity, contract/code version and raw expected output. Deterministic tests consume captured evidence offline; explicit online preparation commands refresh it. A full Router executor is not required.

Quoted output alone is insufficient for stateful reuse. Test next-state correctness against relevant contract state after a simulated operation, including two sequential swaps through the same pool. Do not generate all expected values with the same Python math under test.

### 2.4 Cases and selection bias

Use historical activity to select token pairs, then stratify raw input amounts across low, medium and large trades. The historical window must end no later than the snapshot; record absolute boundaries and export hashes, not a relative `now - N days` recipe alone. Historical swap legs are not user orders: either reconstruct reliable endpoints or explicitly label the pair/amount corpus as leg-derived. Deduplicate transaction identity when deriving transaction-level statistics.

Freeze case IDs, both token addresses, raw amounts, selection rule, seed, stratum and price provenance before comparing algorithms. Symbol collisions must not merge tokens. Dune USD fields can be missing; unknown prices are not zero.

The corpus includes pairs without a direct pool and cases near liquidity/rounding boundaries. Synthetic cases are a separate correctness suite, not mixed into empirical performance averages. Historical-frequency weighting is optional output alongside unweighted/per-stratum results, never the sole score that hides large-order behavior. Parameter tuning and final reporting use disjoint declared cases or a clearly labeled exploratory report; one snapshot does not establish temporal generalization.

### 2.5 Plan semantics and evaluator

`RoutePlan` contains an ordered list of `SwapStep` values. A step has a verified pool ID, input/output token, a list of input-fund references with explicit integer amount or `ALL_REMAINING`, and a new output-fund ID. One source fund begins with the request amount. Multiple consumers split a fund; several same-token funds entering one swap merge their selected balances without an invented external call. This representation is experimental data, not an EVM instruction format.

The evaluator initializes a fresh state map and fund ledger for each plan. It validates identifiers and types, positive order input, supported directions, producer-before-consumer order, distinct output identities, available balances and token conservation. A zero-input step has a deterministic zero output and no pool call; unproduced funds are never interpreted as zero. Each physical pool maps to one transaction-local state; later uses see prior updates. The algorithm's chosen order is retained.

All input must be allocated, and intermediate funds must be consumed for a plan to count as a fully filled success. Target-token terminal balances are summed. Residual input/intermediate balances are reported and make the plan `invalid_plan` under the v1 full-fill policy; no implicit dust donation or extra capital is allowed. Integer remainder belongs to an explicit final allocation. Economic token cycles and external funding are rejected in the v1 feasible set.

Evaluation returns gross output, a per-step trace, next-state identifiers, residuals, route features, estimated cost/net output when available, and a typed status. Solver-reported output is retained only as a diagnostic. Reordering/merging a route during normalization requires re-evaluation and cannot silently replace the submitted route.

### 2.6 Mandatory local algorithms

All algorithms implement §4.3 and use the same admitted simulator/cost context.

| ID | Behavior | Capability |
| --- | --- | --- |
| `direct` | Evaluate every admitted direct pool | Single pool |
| `single_path` | Enumerate cycle-free paths up to the configured hop bound and choose a full-input route | Multi-hop, no split |
| `direct_split` | Discrete allocation across direct pools | Direct split |
| `path_split` | Discrete allocation across candidate multi-hop paths with physical-pool conflicts excluded | Multi-hop disjoint split |
| `incremental_graph` | Allocate chunks while maintaining consistent tentative shared-pool state and funding; re-evaluate/normalize complete plans | Shared-pool graph heuristic |
| `uni_sor_port` | Scoped, upstream-pinned Uniswap SOR routing-core port, as specified next | SOR V2/V3 compatible subset |

**Base and optimized strategy groups (owner decision, WHI-1528).** The six algorithms above are the **base strategies** and remain the references. The ordinary CLI also compares two named **optimized strategies**: `uni_sor_adaptive` (the frozen L08 v1 arm H3, near-full candidates plus adaptive percentage sampling) and `uni_sor_optimized` (arm H4, shortlist plus sampling with the L02–L04 exact quote controls and no L05). Both are experimental heuristics over `uni_sor_port`'s routing core, with its V2/V3 capabilities. They form a separate group: they are not defaults, are not adopted, and imply no loss tolerance. `main.py run|quote` runs both groups by default: base first, sequentially, under the same objective, budget and search constraints. `--strategies base|optimized|profile` narrows the selection, with `profile` meaning the profile's exact selection. Reports keep the groups apart. Details are in [strategy-groups.md](references/strategy-groups.md).

**Metis-inspired in the default comparison (owner decision, WHI-1540).** The default `--strategies all` also runs the experimental `metis_inspired` (§2.8, WHI-1449; NOT Jupiter Metis) once, after the optimized strategies, as a separately labeled `custom` comparator: not a seventh base reference, not an SOR optimization, not a default router. It shares the profile's objective, budget, measurement, worker and `search.*` values. A `graph.label_hops` / `label_pruning` / `chunks` the profile does not declare comes from the registered `config/metis_challenge/m4.yaml` (sha256-pinned); declared values win, and a `label_hops` below `search.max_hops` is refused, not re-tuned. Its hop domain can therefore differ from the shared `search.max_hops`, and reports print its recorded settings. It is a new profile-derived comparison, not a rerun of the frozen M4 arm. `--strategies profile` and saved effective profiles, including earlier eight-algorithm ones, replay literally.

**0.2.1 independent strategies (contract `R021-C/1`, WHI-1547).** Five further experimental `custom` identities are frozen by name: `metis_history`, `direct_split_certified`, `incremental_graph_repair`, `uni_sor_cycle_safe` and `cfmm_dual`. Each is registered only when its own implementation lands and is then appended once to `--strategies all` after `metis_inspired`, in that order (at most 14 strategies). Inclusion is not adoption, and saved profiles still replay literally. Each returns an ordinary `RoutePlan` for the unchanged evaluator; none relaxes exact math, full fill or the cycle/capital rules. Their comparison domains, governing limits, certificate/bound metadata, work units, tuning/holdout exposure, bounded presets versus stress profiles and disposition vocabulary are defined in [research-021/contract.md](references/research-021/contract.md), with sources and corrections in [research-021/sources.md](references/research-021/sources.md).

Keep the best valid simpler route as a candidate so optimization cannot accidentally discard it. Candidate discovery samples multiple input sizes, since a pool bad at 100% can be useful for a small split. Report candidate truncation and quote budgets. Do not label greedy/BFS/finite-grid results optimal. Incremental graph search must make its final execution structure agree with its state accounting: `f(x+delta)-f(x)` is not automatically equivalent to two sequential on-chain fee-bearing swaps.

### 2.7 Mandatory Uniswap SOR introduction

“Introduce Uni SOR” means a source-traceable implementation with behavioral parity evidence, not renaming `path_split` or copying only the general idea. The benchmark runtime remains Python. A pinned Node/TypeScript harness is permitted **only as an offline validation tool**, not as a second timed algorithm runtime or live quote service.

Initial inspected upstream: `Uniswap/smart-order-router`, commit `04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647`, package version `4.31.10`. Relevant source files are `src/routers/alpha-router/functions/compute-all-routes.ts` and `best-swap-route.ts`. Refreshing this pin requires an explicit provenance update and re-running parity. The inspected LICENSE is GPL v3 text and package metadata says GPL; determine file/dependency obligations before translating or redistributing code, retain attribution and a license inventory. This design does not silently relicense the repository.

The owner selected **private internal research** as the current use and wants autonomous upstream code reuse. Agents may independently reuse, adapt and translate suitable upstream code, retaining the pinned source/version and any required license/notice files. Routine permitted reuse does not require a separate owner approval or a standalone legal-signoff milestone. This does not change repository visibility, authorize public/external distribution or erase upstream terms; private visibility is not a license exemption.

Record a concise source inventory for the harness/dependencies, generated fixtures and Python port. If a concrete upstream restriction prevents the intended internal use, prefer a permitted implementation/source while preserving the required behavior. If mandatory behavior cannot be delivered from a permitted source, report that specific technical/source constraint rather than silently substituting a generic SOR-inspired heuristic. Do not block ordinary GPL-compatible private work on a hypothetical future distribution decision.

The parity boundary is the exact-input V2/V3 routing core supplied with **identical ordered candidate pools, percentage grids, route-quote tables and abstract gas-score inputs**. Preserve bounded path enumeration, percentage sorting, BFS seeds/expansion, pool-conflict exclusions, split limits, pruning, ordering/ties as observed at the pin, and final input-remainder assignment. Capture source-to-port mappings and every deliberate exclusion. Candidate pool providers, online subgraphs, production caches, chain-specific gas providers, V4/hooks, calldata and Exact Output are outside this boundary.

Generate frozen golden fixtures using the actual pinned upstream functions, not a separately rewritten JavaScript algorithm. Include no-route, competing percentages, pool overlap, tie ordering, split bounds and nondivisible integer inputs. Exact selected plan/allocation agreement is required for supported deterministic fixtures; any upstream nondeterminism must be demonstrated, canonicalized explicitly and scoped in the evidence. The benchmark independently re-quotes the final plan after remainder assignment; differences from upstream cached reported quotes are documented separately from selection parity.

Provide a **matched V2/V3 cohort** for all algorithms when comparing against SOR. On the full five-source universe, SOR's missing LB support is visible; LB-only cases are `unsupported`, not `no_route`. Other algorithms may exploit LB in a separately labeled full-coverage comparison. The same matched candidate universe prevents attributing liquidity-coverage differences to search quality. Replacing online candidates/cost providers is an explicit adaptation, not a claim that the entire production SOR was reproduced.

Completion requires the port, reproducible upstream fixtures, parity tests, license/provenance records and benchmark/report inclusion. A feasibility note or a generic SOR-inspired solver alone does not satisfy core v1.

### 2.8 Jupiter/Metis challenge and research roadmap

Study Jupiter's public description of incremental route building, interleaved route generation/quoting, split/merge and reuse of liquidity. Maintain a source register distinguishing historical technical descriptions from current API behavior. Do not assume production source is available or infer the complete optimizer from an API.

The challenge feasibility deliverable must state: accessible primary sources/code and licenses; what can be reproduced; how Mantle's pool/fee/transaction semantics differ from Solana; the distinct hypothesis beyond `incremental_graph`; and a go/no-go verdict. A justified no-go is a completed **research** outcome, not a completed implementation.

If feasible, add `metis_inspired` as a clearly labeled Python experimental variant with explicit limits, ablation against `incremental_graph`, the common evaluator and the same budget/corpus. Live Jupiter quotes are neither an oracle for Mantle nor timed benchmark input. Full production Metis equivalence is not claimed. The implementation issue stays blocked when the research gate says no-go; it is never marked Done on a research-only result.

Further candidates from the pre-research are recorded without making them core requirements: Balancer-style continuous marginal allocation, Sushi Tines cumulative-flow routing, CFMM/dual optimization for scoped high-budget references, and piecewise-linear optimization. Each future entry needs a source/license pin, supported invariant family, executable-plan recovery and an identical-objective comparison. Closed products such as Pathfinder/Odos are capability references, not presumed reusable source.

### 2.9 Execution-cost and price model

Pool swap fees and price impact belong to the simulator. Execution cost is a separate empirical model. Dune's `dex.trades`, `mantle.transactions` and `gas.fees` schemas were discovered during design; schema availability is not proof of complete rows or correct fee semantics.

Export transaction-level samples, deduplicate hashes, isolate known swap-related shapes, and retain source/protocol/router mix, success, hop count and available complexity features. Whole-transaction fees must not be counted once per swap leg. Verify Mantle fee-component interpretation against receipts/official rules for the selected period; do not blindly add an L1 field to an already-total fee or assume `gas_used * gas_price` is always the total.

Start with the smallest interpretable model justified by data (per-shape summaries or a simple regression). Record training/holdout split, feature definitions, model version, residual/error distribution and applicability domain. Do not attribute historical router overhead to intrinsic pool costs without evidence. Features unavailable for historical samples, such as reliable tick crossings, are not invented. Extrapolated shapes, especially new shared graphs, carry an unsupported/low-confidence cost flag.

Use a frozen price context to convert native execution fees into the output asset. Store source, timestamp, unit and missingness; do not assume stablecoins are perfectly pegged. Unknown price/cost still permits gross-output comparison but produces no reliable net score.

The report exposes nominal estimated net output plus low/high cost scenarios derived from holdout errors or explicitly configured stress assumptions. These scenarios are not statistical confidence intervals unless that interpretation is justified. Show rank reversals. A gross-only run is a labeled development mode; core v1 requires a validated model for its declared applicability cohort, while uncovered cases remain visibly unranked on net output.

Algorithms receive the same immutable objective/fee model for selection. The first synthetic slice owns `ObjectiveContext` and a common complete-plan cost interface, with gross-only and explicitly synthetic fixed-cost modes for tests. These modes let algorithm work proceed before empirical calibration; they cannot substantiate real net-output claims. The corpus ticket owns the frozen price schema; the empirical cost ticket consumes that schema rather than redefining it.

Declare whether a run maximizes gross output or estimated net output; do not optimize all algorithms for gross and later claim a comparison of net-optimal algorithms. Non-additive complete-plan costs must be rechecked by every algorithm's final selection policy.

### 2.10 Runner, budgets and reproducibility

`prepare(snapshot, config)` performs explicitly charged algorithm-specific preprocessing; `solve(case, context, budget)` produces a plan or typed result. Load/preparation time, per-case solve time and external final-evaluation time are reported separately. Also record total batch time and amortized preparation cost. The evaluator is excluded from solver latency except when the solver calls it during search, which is charged normally.

The runner calls Python plugins in isolated worker processes to enforce timeouts and state reset; this is not a cross-language plugin protocol. Run measured algorithms sequentially on the same recorded machine. Use fixed input order/seeds, declared warmup, repeats and fresh per-case scratch state. No hidden persistent route cache across measured cases; immutable prepared indexes are permitted and their cost is reported. Shuffle/reverse order checks detect accidental state leakage.

Use a monotonic clock. Return attempted/counted quotes, explored/truncated candidates, declared limits, latency samples and status for each case. Measure memory in a separate pass when instrumentation would distort latency. CPU/model, OS, Python/dependency versions and worker settings are recorded. Wall-clock timing is measurement, not a bitwise reproducibility guarantee.

Statuses include `ok`, `unsupported`, `no_route`, `timeout`, `invalid_plan`, `incomplete_snapshot`, `model_error`, `algorithm_error` and `cancelled`. Capability declarations determine `unsupported`; a timed-out search is not evidence of no feasible route. A timeout may preserve a separately labeled last valid candidate, but cannot be counted as a normal completed solve. Failure to evaluate one solver does not erase the case or contaminate another.

Interrupted runs preserve atomically completed case records and a manifest marked incomplete. Re-running with the same bundle/config cannot silently overwrite an existing run; resume, if implemented, requires matching hashes and versions. Resume is not required for v1: explicit new run IDs are sufficient.

### 2.11 Reports and comparison rules

Emit `run.json`, per-case JSONL, CSV summaries and a self-contained `report.html` without a server/CDN. Include routes/traces for selected examples, all error details, source coverage, raw/estimated-net quality, latency distributions, quote counts, memory, complexity and fee sensitivity.

Compare paired common-success cases and separately show unconditional coverage/status counts over the full scheduled corpus. Unsupported/no-route/timeouts cannot disappear from denominators. When no direct baseline exists, its ratio is N/A; alternative baselines can still be compared. Aggregate relative improvements per case/stratum rather than averaging raw amounts of different assets. Report sample counts with percentile statistics and mark underpowered samples.

Show matched protocol/capability comparisons separately from capability/coverage gains. High-budget outputs share the same snapshot, objective and constraints and are “best known.” Gross-output quality is not compared directly with a net-output reference. The chart of quality versus measured time is a Pareto view, not a single weighted score with arbitrary coefficients.

Display exact replay command, bundle/config hashes, git revision plus dirty patch hash where relevant, algorithm/source pins and all deviations. Escape token symbols, error strings and other external text in HTML. Reports must remain useful with JavaScript disabled; interactive filtering can be progressive enhancement.

### 2.12 Parameters and provenance

Non-secret parameters live in validated YAML under `config/`; the public default RPC URL is non-secret configuration. Credentials and any credential-bearing private RPC overrides belong in `.env` and are never serialized into artifacts. Reject unknown keys, invalid bounds and conflicting objectives. Secret validation applies only to commands needing those services; offline run/report and public-RPC-only preparation must not require a private RPC credential.

| Parameter | Initial rule | Evidence/status |
| --- | --- | --- |
| Snapshot count | 1 real block | Owner decision |
| Core sources | Five named sources in §1.2 | Owner decision after exploratory Dune census |
| Order type/runtime | Exact Input / Python | Owner decision |
| Daily total wall time | Measured and reported; no acceptance ceiling | Owner removed the earlier 600-second target |
| `search.max_hops` | Initial trial 3 | Pre-research suggestion, unvalidated |
| `search.max_splits` | Initial trial 4 | Pre-research suggestion, unvalidated |
| `search.percent_step` | Initial trial 5; positive divisor of 100 | Pre-research suggestion, unvalidated |
| `graph.chunks` | Explicit per profile; sweep alongside percentage granularity | Unvalidated until calibration |
| `corpus.window_start/end`, pairs, amount strata, seed | Explicit frozen values selected by preparation evidence | Not silently inferred at run time |
| Quote/time/candidate caps; warmup/repeats | Explicit profile values calibrated on the reference machine | No invented universal defaults |
| Cost sensitivity | Held-out error-derived scenarios or labeled stress inputs | Must cite model/data version |
| Named optimized strategy settings (`strategies.<name>`) | Exactly the sha256-pinned L08 v1 arm (H3 / H4); a differing value or changed file is refused | WHI-1510 registered values; not tuned for WHI-1528 |
| `metis_inspired` graph settings under `--strategies all` (`graph.label_hops`, `graph.label_pruning`, `graph.chunks`) | A declared profile value; otherwise the sha256-pinned `config/metis_challenge/m4.yaml` value (4, true, 50); `label_hops` below `search.max_hops` is refused | WHI-1449 registered values; not tuned for WHI-1540 |
| Per-strategy `algorithm_options.<id>` (0.2.1) | Validated by that identity's factory; bounded comparison values from its sha256-pinned preset; shared `search`/`graph`/budget keys are reserved and refused | Contract `R021-C/1` §9; not implemented before WHI-1548 |
| Protocol math tolerance | Exact raw output on the admitted contract domain | Correctness requirement; scoped exception requires spec change and evidence |

All profile values appear in output even if inherited from defaults. Profile calibration fixes representative daily corpus/budgets before the final comparison. Report runtime, but do not fail acceptance or request an owner waiver solely because of duration. Retain explicit per-case time/quote limits, honest timeout status and full corpus/algorithm coverage. Python integers govern money; floating point is permitted for timing/statistics and approximate internal optimization only.

### 2.13 Optional colleague comparator boundary (Release 0.2.0)

The owner added the colleague's design to Release 0.2.0 as an **optional comparator**. It does not replace `RoutePlan`, the evaluator, the six base strategies or the optimized strategies. [colleague-routing-contract.md](references/colleague-routing-contract.md) (WHI-1536) is the source register and contract. It pins the attachment by SHA-256 and keeps route search, fixed-plan replay, policy resolution and encoding separate:

- **Fixed-plan replay.** The source's funding-graph rules (per-layer bps on frozen bases, final remainder, pure split/merge, merge-then-split, zero inputs, selected call order) map onto ordinary ordered `RoutePlan` steps with explicit `FundInput` amounts. Recovery is charged to solve, holds for one snapshot and request only, and adds no swap, same-token step, compiler or second plan standard. Evaluator checks are not weakened.
- **Policy resolution.** Baseline/JIT candidate policies are `unsupported` until their unresolved rules are decided. Real JIT/RFQ providers are outside the offline domain. An AMM-only local-candidate policy may exist only as a labeled adaptation.
- **Route search.** The attachment starts after topology and shares were chosen. No accessible source defines the search, and none may be inferred from encoding examples. A colleague solver needs a source-backed contract, or an owner-authored `colleague_inspired` contract with explicit deviations. A research no-go leaves the solver unimplemented, never Done.
- **Encoding.** Encoding, calldata, Router/ABI and byte-size results are not benchmark requirements or performance evidence.

Comparisons keep a known plan's replay time apart from routing solve time and use matched cohorts (§2.11).

## 3. Cross-cutting Policies

- No signing keys, trading or broadcast capability is needed. Preparation tools are read-only on-chain.
- Keep an immutable snapshot and fresh request-local mutable state. Boundary validation and explicit errors are required.
- Do not compare different liquidity universes or objectives as if they differed only by algorithm.
- Source identity, algorithm provenance and licensing travel with every imported method. Do not call a method upstream-equivalent without the declared parity evidence.
- Synthetic correctness, fixed-block admission, empirical benchmark and execution-cost model validation are distinct evidence sets.
- Data omissions and unsupported behaviors are visible. No price, liquidity or fee missingness becomes zero.
- Research claims from pre-research require primary-source verification. No unsupported performance improvement promises.
- Follow the repository's issue/worktree/PR workflow when implementation starts. This document does not grant merge or branch-policy exceptions.

## 4. System Architecture

### 4.1 Tech stack

Python 3.12+ with uv, pytest, Ruff and mypy. Keep standard-library dataclasses, integers, JSON, hashing, multiprocessing and timing where sufficient. Add an EVM client/ABI library and YAML validation dependency only when the first collector/config slice needs them; lock versions. A numerical library is optional until a selected algorithm needs it. No production database, API framework or frontend build system.

The optional Node toolchain under `tools/upstream/uni_sor/` exists solely to regenerate pinned reference fixtures. It is not a timed solver, runtime fallback or required dependency for an ordinary offline benchmark replay.

### 4.2 Module layout

These are planned paths, not modules currently implemented:

- `snapshot/` — deployment discovery, fixed-block collection, bundle/case validation and provenance; exposes immutable `SnapshotBundle` and `Case` data.
- `pools/` — verified protocol adapters and pure state transitions; exposes `quote_exact_in` and typed simulation failures.
- `routing/` — plan types, fund/state evaluator, simple registry and algorithm implementations; depends on immutable snapshots and the pool interface.
- `benchmark/` — objective/cost context, worker lifecycle, budgets, measurements and result records; depends on solver/evaluator interfaces.
- `report/` — aggregate machine records into CSV/HTML; depends only on the versioned result schema, not live chain data.
- `main.py` — thin CLI for `prepare`, `validate`, `run`, `report` and protocol verification subcommands.
- `config/` — checked, non-secret profiles; `tests/` — unit/behavior fixtures and separately marked preparation integration tests; `tools/upstream/` — source-parity tools only.

### 4.3 Load-bearing interfaces

```python
quote_exact_in(state, token_in, amount_in_raw: int, context) -> SwapResult
prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedAlgorithm
solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult
evaluate(bundle: SnapshotBundle, case: Case, plan: RoutePlan,
         objective: ObjectiveContext) -> Evaluation
run_experiment(bundle, profile, algorithms) -> RunManifest
render_report(run_manifest) -> ReportPaths
```

Types are typed dataclasses/enums with explicit serialization at artifact boundaries; concrete fields are defined by §2. Do not build dynamic plugin discovery: an explicit name-to-factory mapping is sufficient. Capability declarations identify supported pool families and topologies, independent of a case's eventual `no_route` result.

The highest useful test seam is **frozen bundle + case + plan → evaluated result**, and **frozen bundle + profile → run records → report**. Pool/foreign-source differential checks are additional necessary seams because a common math bug can fool every solver.

### 4.4 Core flows

1. **Prepare:** validate deployment catalog → choose block → collect exact states → capture admission evidence → construct frozen cases/prices/cost context → validate → publish bundle.
2. **Run:** validate bundle/profile → start solver worker → charge preparation → solve each case under limits → independently evaluate complete plans → append atomic result records → finalize manifest.
3. **Report:** validate run schema → form explicit cohorts → compute paired quality/coverage/sensitivity summaries → write offline HTML/CSV with replay instructions.
4. **Add algorithm:** declare scope and source → implement Python interface → run conservation and source-parity tests → register profile → compare on matched and expanded cohorts.

### 4.5 State and recovery

Only versioned files persist. Input bundles never change in place; new data means a new hash/ID. Results are append-only per run with a manifest completion flag. Failed preparation stays unpublished; failed solver workers are replaced without reusing their pool state. Ordinary offline commands require no RPC/Dune access. Small fixtures are tracked; large datasets/results are local or stored in a separately documented artifact location with checksums.

## 5. Data & Observability

Per-case records contain case/algorithm/config/bundle IDs, status, plan, evaluated output, residuals, timing samples, counters, estimated cost and sensitivity/applicability, error category and trace reference. Run metadata includes corpus denominators, source admission coverage, environment, code/dependency/source pins and preprocessing costs.

Log preparation failures with protocol/pool/block identifiers, not credentials or signed URLs. Reports expose unsupported/unknown results and scope exclusions prominently. No alerting service or telemetry backend is required for this CLI tool.

### 5.1 Design-stage evidence

Private exploratory Dune query [8825927](https://dune.com/queries/8825927), execution `01M393RAJ3DKM23PRGW2MB4Y06`, completed during the 2026-09-24 design session. A rolling seven-day query over `dex.trades` returned the following **priced swap-leg volume**, not TVL or unique user-order volume:

| Source/version | Emitting contract count | Priced leg volume USD | Share of priced total |
| --- | ---: | ---: | ---: |
| Agni 3 | 56 | 11,499,730.23 | 90.73% |
| Merchant Moe 2.2 | 33 | 1,025,399.21 | 8.09% |
| FusionX 3 | 27 | 57,320.13 | 0.45% |
| Uniswap 3 | 16 | 55,874.77 | 0.44% |
| Merchant Moe 1 | 46 | 35,725.46 | 0.28% |

There were unpriced rows for Moe 2.2 (54), Uniswap 3 (1), and Moe 1 (2). Counts are not verified unique pool counts. Indexed observations extended roughly from 2026-09-17 07:06 UTC to 2026-09-24 06:02 UTC. The maximum observed block was 101049729; this is **not** the selected benchmark block. Future reproducible exports must use fixed boundaries and store rows/SQL. These preliminary results motivated protocol investigation; the owner explicitly selected all five sources despite the concentration in Agni/Moe LB.

## 6. Milestones

| Milestone | Exit condition |
| --- | --- |
| M0 — Evidence and readiness | Verified source/deployment/admission plan, workflow prerequisites, pinned SOR source/parity contract and data/RPC feasibility established |
| M1 — First replayable comparison | Synthetic end-to-end experiment and first real verified source run through evaluator, baseline and minimal report |
| M2 — Five-source corpus | All five sources admitted at one block, corpus/prices frozen and shared-pool evaluator validated |
| M3 — Mandatory algorithm comparison | Four baselines, graph heuristic and parity-tested SOR port run in their declared cohorts |
| M4 — Reproducible decision report | Calibrated cost model, honest sensitivity/cohorts, validated daily profile and acceptance evidence |
| C1 — Jupiter challenge | Feasibility memo, then an optional distinct Metis-inspired experiment if go; not a core-v1 blocker |

The concrete version plan is in [RELEASE_PLAN.md](RELEASE_PLAN.md). Release `0.1.0` delivers the complete approved core across M0–M4 (including mandatory Uni SOR); Release `0.2.0` contains the conditional Jupiter challenge. These are planned releases in the owner's `router-algorithms-optimizer` pipeline, not declarations of deployment. Issue details/dependencies are in `docs/ISSUE_PLAN.md`. Milestones describe capability stages and never substitute for Linear Release metadata. Publication binds real Release IDs; it does not create git integration branches.

For this publication, the owner requested **every current issue** to receive a release assignment. G00 is therefore associated with 0.1.0 for delivery tracking, as a bounded exception to the usual governance-Release omission. It retains its unversioned governance title, carve-out scope and governance base-resolution rules; the association does not turn it into version-scoped work or waive any merge gate.

WHI-1425 completed repository bootstrap through PR #1 (`7c913cd132eae8987d8b2ddd47d304ac4211536d`), and `origin/dev` exists. With no production tag, the documented bootstrap base remains `origin/dev`. This documentation handoff is tracked by WHI-1469, with the separate governance mirror in WHI-1470; future worktrees must branch from the base carrying this full spec. Tracker publication, public-RPC selection and Enterprise Dune access do not change git routing or release merge gates. The current execution batch is limited to 0.1.0. The latest owner decisions are recorded in [the execution decision record](references/0.1.0-execution-decisions.md).

## 7. Rejected Alternatives

- **Build a production Router first:** conflicts with the approved static benchmark goal.
- **Make the colleague's execution format mandatory:** imports downstream concerns unrelated to the experiment; conserve funds/order with a smaller plan type.
- **Query live RPC while timing algorithms:** destroys fixed-state comparability and mixes network/solver costs.
- **Treat historical observed output as the counterfactual optimum:** historical state/order/coverage may differ; use the frozen evaluator.
- **Approximate every protocol as constant product:** misses the dominant concentrated-liquidity/bin models and dynamic fees.
- **Fit pool fees and execution fees together:** obscures deterministic protocol math and risks double counting.
- **Independent quotes for overlapping paths:** double counts liquidity; use one evolving physical-pool state.
- **Trust solver output without replay:** permits invalid money/state plans to win.
- **Cross-language solver protocol in v1:** owner chose one Python runtime; the upstream test harness is a validation-only exception.
- **Rename a generic percentage search “Uni SOR”:** mandatory SOR inclusion requires a pin, scoped port and upstream fixture parity.
- **Use live Jupiter API as a Mantle comparator:** chain, liquidity, timing and execution model differ; study methods as a separately named challenge.
- **Claim high-budget heuristic output is an optimum:** no certificate; label best known.
- **Start with a dashboard service, compiler, database or exhaustive solver suite:** not needed for the approved CLI/report workflow. Future algorithms follow evidence and provenance gates.

## 8. Known Risks & Open Questions

| Risk/question | Resolution owner/deliverable | Blocking effect |
| --- | --- | --- |
| Exact deployment/math differences among five sources | Protocol admission catalog and real replay tickets | A source cannot be admitted without evidence |
| Archive RPC and full tick/bin retrieval | Snapshot preflight and protocol collectors | No publishable real bundle without consistent state |
| Dune history may be incomplete or leg-based | Corpus/cost export provenance and coverage | Limits claims; never silently implies full Mantle coverage |
| Dynamic LB fees and repeated-pool state | LB differential/stateful fixtures | Blocks LB admission and valid graph results |
| Upstream source restrictions and supported SOR parity boundary | Source inventory/contract; autonomous permitted private reuse | Keep required notices and parity; only concrete incompatibility with current internal use is a blocker, not speculative future distribution |
| SOR candidate/cost adaptation and rounding behavior | Golden harness plus port parity report | Scope/deviations must be explicit in reports |
| Shared graph cost model is an extrapolation | Cost validation/applicability report | Unreliable net rankings withheld, gross results retained |
| No full transaction executor | Explicit v1 boundary | No production-executability claim |
| Single snapshot overfitting | Held-out cases and scope labels | Temporal generalization remains unknown |
| Runtime versus source/algorithm scope | Profile measurements and explicit per-case limits | No total-runtime acceptance gate; retain honest timeouts and complete coverage |
| Metis public evidence may be insufficient | Challenge feasibility memo | No-go leaves challenge implementation unstarted, core unaffected |
| Shared spec on implementation base | WHI-1469 documentation handoff and WHI-1470 governance mirror | Future implementation must use the committed full spec on the resolved base, not an old template or stale local governance |

## 9. Primary References

- Project: https://linear.app/whisker-personal/project/mantle-router-algorithm-optimizer-3abba3f613f7
- Local pre-research: `docs/references/pre-research-from-gpt-6-pro.md` (unresolved citations; verify claims before reuse).
- SOR pinned tree: https://github.com/Uniswap/smart-order-router/tree/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647
- SOR path enumeration: https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/routers/alpha-router/functions/compute-all-routes.ts
- SOR BFS/allocation: https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/routers/alpha-router/functions/best-swap-route.ts
- SOR license: https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/LICENSE
- Jupiter archived Metis description: https://discuss.jup.ag/t/archived-jupiter-v3-the-metis-routing-algo/21712
- Jupiter current fast-mode distinction: https://developers.jup.ag/docs/swap/advanced/reduce-latency
- Agni concentrated liquidity: https://agni.finance/
- Merchant Moe LB: https://docs.merchantmoe.com/liquidity-book/introduction-to-liquidity-book
- Research candidates (not yet pinned/adopted): https://github.com/balancer/balancer-sor ; https://github.com/sushiswap/sdk ; https://github.com/KyberNetwork/kyberswap-dex-lib ; https://github.com/bcc-research/CFMMRouter.jl
