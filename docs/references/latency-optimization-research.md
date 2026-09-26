# Routing latency research and the 0.1.2 experiment plan

Research date: 2026-09-26. Product source: `b2a680578f65ac65653a8160f04a3d97a5c5e71e`.
Scope: **research and issue planning only**. No solver, pool math, evaluator, runner,
profile or dependency was changed. Scratch diagnostics called the existing implementations;
count-only wrappers forwarded to the original functions unchanged.

## 1. What the reported nine seconds means

The existing local artifact `data/quotes/20260926T051300040217Z-f064e5b6/quote.json`
identifies an exploratory **USDC → USDT, 1000 USDC** request at Mantle block 101082044,
using `config/daily_gross.yaml` (two hops, four splits, 5% grid, 200 graph chunks).
It records `uni_sor_port` solve time **9.144412292 s**, not an invented estimate.

`benchmark/worker.py:_serve_attempt` times the actual `factory.solve` call. Worker
startup/preparation, input loading, independent final evaluation and report rendering
are outside that timer; candidate-sink IPC invoked inside solve is inside it. The CLI
runs six algorithms sequentially. Its total wall time is not a single SOR solve or a
hosted API's response time. The 0.1.1 `quote` contract remains one solve per algorithm,
no warmups/repeats/memory pass; research replication used separate invocations.

| Algorithm | Original solve, seconds | Three new solves, seconds (range) | Counted quotes | Profiled CL steps |
| --- | ---: | ---: | ---: | ---: |
| direct | 0.018547 | 0.085755–0.340535 | 12 | 4,232 |
| single_path | 0.443572 | 1.588736–2.658438 | 265 | 49,034 |
| direct_split | 0.324516 | 1.202443–2.155492 | 240 | 78,888 |
| path_split | 2.267848 | 4.567211–7.019686 | 1,289 | 184,454 |
| incremental_graph | 2.966060 | 6.317326–10.813779 | 1,918 | 210,748 |
| uni_sor_port | 9.144412 | 18.419108–38.433692 | 3,734 | 908,823 |

The new runs used a shared Apple M2 Pro / 10-core workstation, CPython 3.13.13. Start
load averages were **61.208 / 33.192 / 18.765**. Six-algorithm CLI wall times were
72.725, 46.990 and 41.943 seconds. This is visibly unsuitable for establishing a clean
latency distribution. Three observations are not p95, a production SLA, or a speedup
measurement. All evaluations and search statistics were deterministic on this case;
all six statuses were `ok`. The original direct/single-path timings also show why it
would be misleading to say all six have a nine-second problem.

Evidence: [compact machine-readable record](latency-research/evidence.json), including
original artifact hash, request/bundle/profile identities, each run, environment and
profile counters. Existing v1 timing contamination is separately documented in
[v1-acceptance.md §7](v1-acceptance.md#7-wall-time-and-environment).

## 2. Is Uniswap API <1 second a like-for-like baseline?

**Not established.** Treat the owner's observation as motivation, not as a measured
ninefold algorithm gap or an already-approved universal subsecond acceptance threshold.

Official documentation distinguishes quote-response preference from settlement:
“FASTEST controls quote response speed, not settlement complexity” [U1]. It describes
FASTEST/BEST_PRICE and AMM/UniswapX behavior, but the inspected pages provide no numeric
subsecond SLA. The inspected supported-chain table does not list Mantle/5000 [U2]; this
is an observation about that table, not proof that every possible service endpoint lacks
support. No authenticated API quote or matched chain/amount/block capture was made.

The endpoint, chain, routing preference, protocol/quote type, amount, response output,
server/cache condition and timing boundary of the reported API observation are unknown.
The public SOR repository also does not prove the hosted API's current production
commit/configuration or cache hit rate. Do not assert those internals as facts.

What the pinned open source **does** establish:

- Candidate selection applies direct/endpoint/second-hop/base-token/TVL budgets before
  path enumeration (`getV3CandidatePools`, `getV2CandidatePools`, [U3]). Limits include
  `topN`, `topNDirectSwaps`, `topNTokenInOut`, `topNSecondHop`, `topNWithEachBaseToken`
  and `topNWithBaseToken`; no unfetched per-chain numeric defaults are assumed.
- `alpha-router.ts` has route-cache paths that **re-quote** cached routes. One cached
  route uses 100% input only; several cached routes use percentage sampling [U4]. A
  cached topology is not the same thing as a cached final price, and a cached-winner
  shortlist is not equivalent to the complete candidate universe.
- V3/mixed quotes use chunked on-chain quoter calls with asynchronous batch execution;
  V2 quotes use local `getOutputAmount` calls [U5/U6]. Our measured solve is entirely
  local integer simulation. Adding RPC concurrency or choosing a faster RPC therefore
  does not address its measured CPU work.

There is documentation ambiguity about the detailed FASTEST/UniswapX/protocol-filter
interaction between current official pages. Do not build correctness or timing claims on
that ambiguity without an actual schema/request check. Missing API access or a nonmatching
chain must not block local optimization experiments.

## 3. Measured attribution, not guessed hotspots

Six separate diagnostics loaded the same derived request bundle and effective profile,
prepared the same factory, used the same seed and quote meter, then wrapped exactly
`solve` in `cProfile(timer=time.process_time)`. Preparation/loading were excluded. These
calls omitted worker IPC/candidate streaming and the hard worker clock; profiling adds
substantial overhead. **Their CPU/wall durations are diagnostic, not benchmark latency.**
Their complete evaluation and search statistics matched the isolated-run results for
all six algorithms.

### 3.1 Quoting dominates; SOR's BFS does not

For SOR, cumulative profiled CPU was:

| Function / activity | Instrumented cumulative CPU seconds | Interpretation |
| --- | ---: | --- |
| `uni_sor_port.solve` | 26.851 | Whole profiled solver |
| `build_route_quotes` | 26.753 | About 99.6% of solver cumulative CPU |
| `pools.quote.quote_exact_in` | 26.545 | About 98.9%, nested within quote-table construction |
| `get_best_swap_route_by` | 0.071 | About 0.26%; not the first optimization target |
| `compute_all_routes` (three family calls) | 0.014 | About 0.05% on this small graph |
| `_freeze_mapping` | 0.098 | About 0.37%; whole-state copy is not the dominant measured cause here |

These are **nested cumulative figures, not additive buckets**. Profiler overhead can
change proportions; no direct uninstrumented speedup follows from these percentages.
The result nevertheless rejects a blanket “rewrite BFS/DP first” explanation.

The case enumerated 210 SOR routes (163 V3, 4 V2, 43 mixed), then 20 percentage sizes:
4,200 entries, 2,727 valid and 1,473 null. It made 3,734 actual pool quotes with 3,500
memo hits. A per-solve `QuoteCache` already exists; simply proposing another identical
memo is not an optimization. `single_path` already has prefix reuse/dead-prefix pruning,
and `path_split` already performs multi-size pruning and branch-and-bound.

### 3.2 Concentrated-liquidity traversal has avoidable work to investigate

A separate **count-only** SOR run wrapped existing functions without changing their
returns. Full evaluation and search statistics still matched the baseline.

- 3,289 CL quotes executed **908,823 swap steps**.
- 479,410 steps had zero liquidity; **478,425 had both zero liquidity and an
  uninitialized target**, about 52.6% of all CL steps.
- The loop called tick-to-sqrt math 908,823 times for only **13,131 distinct tick inputs**.
- The most traversed pool (`0xd145db1dfc3fcd2e999b47f3a02c85bd7750ed09`) had 226,720
  steps across 180 quotes, all with positive liquidity. Another pool had 216,040
  zero-liquidity/uninitialized steps. Both empty-span traversal and repeated real
  liquidity traversal matter; optimizing only empty spans cannot remove all work.

`pools/concentrated.py:swap` deliberately follows Solidity's word-by-word behavior.
An optimization must preserve the meaning of that behavior:

- **Never skip positive-liquidity word boundaries casually.** Even an uninitialized
  boundary can affect per-step integer rounding and fees.
- Zero-liquidity skipping must stop at initialized ticks, price limits, MIN/MAX ticks and
  collected-state boundaries, preserving the exact failure classification and logical
  protocol features. Unknown bitmap state is never empty state.
- Reusing completed traversal prefixes across different amounts is a separate, harder
  exact experiment. Equal cumulative-input boundaries require care: when input is fully
  consumed, the original loop does not continue through later zero-cost steps.
- Current query-state semantics, checked widths, fee growth, protocol fees, admitted
  hooks and new-state immutability are still required. Search-only output acceleration
  cannot silently replace a full state transition in the final evaluator.

### 3.3 Incremental graph has a second algorithmic hotspot

With 340 bounded paths and 200 chunks, `incremental_graph` called both `marginal` and
`creates_cycle` **68,000 times**. Their cumulative CPU was 2.157 and 1.053 seconds within
a 9.232-second instrumented solve; the former also includes quote work. The embedded
`path_split` took 5.889 cumulative seconds. Reuse of unaffected marginal scores and
incremental reachability/cycle checks is a credible next experiment **after** shared quote
improvements. Existing per-pool quote memoization does not remove all path rescanning.

A cache key must account for pool aggregate-state versions, direction and actual chunk
size/carry. A path's score remaining valid does not imply its cycle admissibility remains
valid after new graph edges. Stable ties, candidate-order/budget semantics, dust handling
and final merged-plan evaluation must remain unchanged in an exact variant.

## 4. Prioritized experiment directions

These are **unimplemented hypotheses**, not measured gains. Detailed execution scopes,
acceptance criteria and native dependencies are in the [0.1.2 Linear Release](https://linear.app/whisker-personal/pipeline/router-algorithms-optimizer/release/012-routing-latency-optimization-experiments-14487d7f7d22).
All eight experiments were published as Todo / ready-for-agent. Complete descriptions,
Release bindings and 17 native blocking edges were read back and verified; the dated
[issue snapshot](latency-research/issue-plan.json) preserves that evidence. L-keys are
stable research identifiers, mapped to actual issues below.

| Issue / key | Direction | Class | Dependency |
| --- | --- | --- | --- |
| [WHI-1503](https://linear.app/whisker-personal/issue/WHI-1503) / L01 | Reproducible latency/quality measurement matrix and experiment harness | Measurement foundation | First; depends on WHI-1502 |
| [WHI-1504](https://linear.app/whisker-personal/issue/WHI-1504) / L02 | Skip proven zero-liquidity empty CL spans while preserving exact behavior | Exact quote-kernel experiment | After L01 |
| [WHI-1505](https://linear.app/whisker-personal/issue/WHI-1505) / L03 | Bounded exact tick/bin math and immutable traversal-index reuse | Exact shared-work experiment | After L01/L02; measure incremental benefit |
| [WHI-1506](https://linear.app/whisker-personal/issue/WHI-1506) / L04 | Reuse exact completed CL traversal prefixes across amount queries | Exact multi-amount quote experiment | After L02/L03; higher correctness risk |
| [WHI-1507](https://linear.app/whisker-personal/issue/WHI-1507) / L05 | Reuse unaffected graph marginal scores and maintain cycle reachability incrementally | Exact graph-search experiment | After L01/L04 |
| [WHI-1508](https://linear.app/whisker-personal/issue/WHI-1508) / L06 | Uniswap-inspired candidate-pool/route shortlist in a separately named SOR-fast variant | Heuristic, opt-in | After L01/L04 |
| [WHI-1509](https://linear.app/whisker-personal/issue/WHI-1509) / L07 | Coarse-to-fine percentage evaluation and anytime refinement in that experimental variant | Heuristic, opt-in | After L06 |
| [WHI-1510](https://linear.app/whisker-personal/issue/WHI-1510) / L08 | Paired end-to-end/quality validation and explicit adoption report for the six references and experimental variants | Convergence | After all experiment dispositions |

L02–L04 benefit the shared quote layer used by **all six** algorithms. Direct is already
fast in the original observation, so it gets coverage/regression checks rather than a
contrived standalone optimization. L05 targets graph-specific repeated work. L06/L07
address the SOR route×amount workload with explicit quality trade-offs.

Every experiment ends in a measured **adopt or reject** disposition. An adopted change
needs its correctness/performance evidence; a rejected proposal is recorded as rejected,
not described as an implemented speedup. No optimization ticket is started by this
research request. No candidate or profile becomes the default merely because it is faster.

### Exact lane

Keep the six named reference behaviors, parameter/candidate domains and SOR source-parity
contract. Compare on sufficient budgets for semantic equivalence; separately report gains
in completion under fixed resource budgets. Preserve integer outputs, valid-plan funding,
state transitions, statuses/failures and SOR selection/order/fill. Logical protocol execution
features must not be confused with fewer Python operations. Keep an independent reference
path/recorded contract fixtures so a shared optimized bug cannot validate itself.

Pure math caches are bounded and keyed by all function inputs. Derived state indexes
cannot weaken loader validation or trust a caller-owned mutable mapping. Cold preparation,
cache population, hit/miss accounting, memory and warm reuse must be reported. Prefix reuse
must fall back to the exact reference when outside its demonstrated domain, not hide
unsupported or incomplete state.

### Heuristic lane

Top-N/base-token/end-point selection in upstream SOR [U3] is a useful lead, but it is not
part of this port's frozen full-cohort A-1 contract. Changed candidates, grids or queue
scheduling therefore live in an explicit experimental identity (e.g. `uni_sor_fast`), while
`uni_sor_port` and its goldens remain intact. Differential tests with restricted injected
inputs do not prove parity with the original full candidate set.

Build latency–quality curves across pairs/amounts and adversarial cases, retaining all
failures and reporting eligible universe versus actually searched pools/routes. Do not
select solely by TVL or the full-input quote: low-TVL pools and small split fractions can
matter. Coarse-to-fine sampling can miss a narrow optimum; exact final replay does not
make the search globally optimal. Define regret relative to the same-scope best-known
reference, not an inaccessible mathematical optimum.

No default quality-loss tolerance or universal subsecond SLA was supplied by the owner.
Pre-register experimental parameter sweeps and acceptance/noise rules in L01; treat
subsecond latency as a hypothesis to evaluate on declared workloads. An approximate
variant remains opt-in until its trade-off is explicitly accepted.

## 5. What is deliberately not prioritized

- **BFS, DP or branch-and-bound micro-optimization first:** SOR selection was ~0.26% and
  path-split branch-and-bound ~0.016 CPU seconds on the profiled case. Re-profile after
  removing quote work before investing here. Do not assume a theorem about all workloads.
- **A new generic cache layer:** identical per-solve quote caching already exists. State
  copying/freezing was minor in this sample; do not advertise it as the measured root cause.
- **RPC batching to fix local solve time:** no RPC occurs in this timer. Upstream network
  batching [U5] is not local CPU parallelism. Adding nested process pools also conflicts
  with the current daemon-worker design and can add IPC/startup cost.
- **Unbounded cross-request winner caches:** API-like warm caches require explicit state,
  amount/objective invalidation and cold/warm accounting. A prior winner is a heuristic
  shortlist, not the full current search. No quote service is being built in 0.1.2.
- **Replacing DP with continuous optimization immediately:** Balancer's allocator equalizes
  post-swap prices using derivatives and bounds [B1], but it has iteration/local-minimum
  handling and is not proof for our integer CL/LB/shared-pool domain. This is a later lead
  if measured residual allocation cost justifies it, not a prerequisite for this release.
- **A Tines/Metis port:** useful search leads were not backed by retrievable primary files
  in this research run; do not claim their production internals or make them a dependency.
  The existing Jupiter challenge stays separate.
- **Rust/C++ rewrite or dropping validation:** no evidence yet requires a language change.
  Preserve the Python and exact-protocol contract; reduce proven unnecessary work first.

## 6. Measurement and quality contract for L01/L08

1. Freeze source revision, snapshot hash/block, token/amount pairs, objective, pool/cohort
   eligibility, route/granularity parameters and budget. Use the user case as a sentinel,
   then add representative pairs, small/large amounts, CL/LB/CPMM, dense/sparse liquidity,
   tick/bin/rounding boundaries, shared pools and failures. Keep tuning separate from a
   held-out comparison set.
2. Run baseline and candidate sequentially on the same declared machine, with recorded
   load/noise conditions. Predeclare warmup/repeats and order for the experiment harness;
   **do not change `quote`'s one-solve-per-algorithm contract**. Do not invent p95 from a
   handful of samples or present profile time as ordinary latency.
3. Report solve wall and CPU separately from preparation/startup, transport/IPC, final
   evaluation and complete single-request response. A six-algorithm CLI total is another
   metric. Account for work moved into preparation or caches; report cold and declared
   warm scenarios separately, plus quote/route/step work and peak memory.
4. Keep exact numeric/state/parity checks, deterministic replay and snapshot immutability.
   Compare non-timing outputs against baseline/reference fixtures and preserve every
   scheduled failure/timeout/unsupported/incomplete outcome. More completed work under a
   time budget is reported explicitly rather than treated as identical search scope.
5. For heuristic experiments report paired raw-output regret, success/coverage changes,
   tail losses and latency–quality curves in matched cohorts. Gross and net objectives
   cannot be mixed; current SOR zero-gas selection and uncalibrated shared/split costs
   remain explicit. Unknown net scores stay N/A.
6. Make API comparison optional evidence, not a dependency on credentials or Mantle
   support. If obtained, redact secrets and identify endpoint/chain/type/amount/output,
   routing preference, timing and cache conditions. Without matching inputs it is a
   separate product observation, not a correctness or speed oracle.

## 7. Reproduction and evidence boundaries

The unchanged canonical command is:

```bash
uv run python main.py quote \
  --bundle data/corpus/mantle-5src-101082044/bundle \
  --profile config/daily_gross.yaml \
  --token-in USDC --token-out USDT --amount 1000 --details \
  --quotes-dir /tmp/router-latency-research/replay
```

Existing corpus preparation/provenance is in [corpus.md](corpus.md). No new chain/Dune
collection was performed for this research. Complete raw runs and pstats were left in
`/tmp/router-latency-research/`; the compact record and exact script transcripts are
checked in here:

- [evidence.json](latency-research/evidence.json): authoritative recorded inputs, results,
  diagnostic attribution and count-only evidence for this note.
- [baseline.py.txt](latency-research/baseline.py.txt): exact three-invocation research wrapper.
- [profile_one.py.txt](latency-research/profile_one.py.txt): exact per-solver diagnostic.
- [count_sor_steps.py.txt](latency-research/count_sor_steps.py.txt): exact forwarding counter.

These `.py.txt` files are documentary transcripts, not production modules or an optimization.
To reproduce, copy to a scratch `.py`, adjust the recorded checkout/scratch paths to your
machine, regenerate the baseline first, then execute diagnostics with the checkout's
Python environment. Run diagnostics one at a time, with an external process timeout;
never overlap them with the unprofiled baseline. They intentionally expose the difference
between the ordinary isolated worker measurement and a direct profiling call. The exact
measured CPU values and load-contaminated wall times are not portable performance targets.

Scope limits: one request was profiled, on one snapshot and shared machine. The counters
are strong evidence of work on that case; they do not establish universal bottlenecks,
actual optimization gains or a subsecond service promise. All proposed optimizations
remain unimplemented. No business source/profile/fixture was modified.

## 8. Primary-source register

Inspected on 2026-09-26. U3–U6 are pinned to the existing SOR commit; official web docs
and B1 are mutable, so recheck/pin them before implementation relies on changing details.

- **U1:** [Uniswap AMM vs UniswapX Routing](https://developers.uniswap.org/docs/trading/swapping-api/amm-vs-uniswapx-routing) — FASTEST/BEST_PRICE and quote/settlement distinction. No numeric subsecond SLA established.
- **U2:** [Uniswap Supported Chains & Tokens](https://developers.uniswap.org/docs/trading/swapping-api/supported-chains) — Mantle/5000 absent from the inspected table; no matched API availability claim.
- **U3:** [SOR candidate selection](https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/routers/alpha-router/functions/get-candidate-pools.ts) — top-N, direct/endpoints/second hops/base tokens.
- **U4:** [SOR AlphaRouter](https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/routers/alpha-router/alpha-router.ts) — amount distribution and cached-route re-quoting.
- **U5:** [SOR on-chain quote provider](https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/providers/on-chain-quote-provider.ts) — RPC batching/concurrency and its different cost model.
- **U6:** [SOR V2 quote provider](https://github.com/Uniswap/smart-order-router/blob/04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647/src/providers/v2/quote-provider.ts) — local route×amount×hop quoting.
- **B1:** [Balancer allocator](https://github.com/balancer/balancer-sor/blob/master/src/router/sorClass.ts) — marginal-price/derivative allocation lead, iteration/local-minimum handling; mutable and not adopted here.
- Local contract: [Uni SOR port](uni-sor-port-contract.md), `routing/algorithms/*.py`,
  `routing/search.py`, `benchmark/worker.py`, `pools/concentrated.py`, `pools/cl_math.py`,
  `pools/liquidity_book.py`, and `snapshot/models.py` at the recorded product commit.

Source retrieval gaps: attempted Tines `Graph.ts` and historical Metis primary pages
returned 404 in the research lane; they are not evidence for an implementation claim.
Automated source-check semantics were unavailable, so the external brief used manually
inspected original passages. Absence of a public SLA in these pages does not prove that
no private contractual SLA exists. Preserve required source notices/pins for any later
reuse under the project's existing private internal research policy.
