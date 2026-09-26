# L04 — exact CL traversal-prefix reuse across independent amounts (WHI-1506)

Status: **integrated default-off, for correctness, under the owner's development-first
amendment of 2026-09-26.**

- The reuse is `pools.concentrated.CLPrefixReuse(max_keys, max_checkpoints)`. It is
  selected only by passing an instance explicitly: `concentrated.swap(..., prefix_reuse=...)`.
- Every ordinary caller runs the unchanged reference loop with no prefix, no L02 and no
  L03. That covers `quote_exact_in`, the `pools.quote` dispatcher, the evaluator and all
  six solvers. No profile, schema, search policy or default changed.
- The **performance adopt/reject decision is deferred to WHI-1510**. This note makes no
  adoption claim and no speedup claim. Every timing here is an in-process diagnostic on
  a heavily loaded shared host. None of it is an L01 run.

Research key L04 ([latency-optimization-research.md](latency-optimization-research.md)
§3.2). Base `9f4a39e` (0.1.2 `dev`, WHI-1505 merged). Protocol L01 v1 is unchanged.

## 1. Disposition

| Piece | Disposition | Evidence |
| --- | --- | --- |
| Exact reuse of completed CL steps across independent amount queries on one original state | **Implemented, explicit and off by default.** Exact on unit/fixture/golden evidence and on all 300 L01 records. Performance pending WHI-1510 | §§3–5 |
| An amount-only search seam (output without a state transition) | **Not needed, not built.** Every prefixed call returns the complete `SwapOutcome`, full new state included, from the unchanged epilogue | §3 |
| Reusing prefixes of a state *across* swaps (sequential amounts) | **Out of scope by design.** Different amounts are independent original-state queries. A returned `new_state` is a different key | §3 |

## 2. Remaining repeated work after L02 + L03

Counts come from the explicitly selected post-L03 control (`l02_l03`: L02 on, fresh
tick/bin memos per solve) over all 300 L01 records. The default reference is not the
control. The counters are instrumented function wrappers.

- 268,707 CL swaps make **35,994,954 logical loop iterations**. L02 leaves
  **6,260,016 executed `computeSwapStep` calls**.
- Sentinel `uni_sor_port` (full source): 3,289 CL swaps, 908,823 logical steps,
  430,951 executed. These are 20 percentage sizes per route on the same 53 original pool
  states. The exact-amount `QuoteCache` cannot share traversal between different
  amounts.

## 3. The change and its exactness contract

**Lemma.** For `fee < 1e6`, `computeSwapStep` takes its full branch
(`mulDiv(R, 1e6 − fee, 1e6) >= amountIn`) iff `R >= amountIn + mulDivRoundingUp(amountIn,
fee, 1e6 − fee)`. Proof: the left side holds iff `R >= ceil(amountIn·1e6 / (1e6 − fee))`,
and that is exactly the right side. The full branch's outputs `(target, amountIn,
amountOut, feeAmount)` do not depend on `R`. A test checks this on the formula and on the
migrated step (`test_prefix_full_step_threshold_is_the_gross_step_cost`).

**Record.** For each key, the instance keeps a checkpoint after every completed *full*
step: all loop variables, with the cumulative **gross** input `C` (`amountIn + feeAmount`,
taken before the protocol-fee split) in place of the amount-dependent remainder. Ticks
crossed are kept in crossing order. A pass is recorded only when it reached its target
and did not overdraw the remainder. A partial step either stops short of the target or,
if rounding lands it on the target, costs more than the remainder, so it is never
recorded.

**Resume.** By the lemma, a query of amount `A` executes step `i` iff `A > C[i]` and
reuses it as recorded iff `A >= C[i+1]`. Let `j` be `bisect_left(C, A)`:

- `C[j] == A`: return checkpoint `j`. The input is exhausted exactly, and the reference
  stops there, never walking later **zero-cost** steps (an edge crossing followed by
  empty words, or a zero-movement crossing).
- otherwise: resume at `j − 1` and let the **reference code** run the partial step.
- beyond the frontier: resume at the frontier, run the reference loop and extend the
  record lazily.

**Not cached.** Validation (`AS`, `SPL`, int256, source/hook admission) always runs
first. Errors (`MissingState`, reverts) and partial steps are never cached. Missing
state is re-raised by the reference read on every query. `fee >= 1e6` never uses the
reuse.

**Output.** The epilogue materializes the full new state and is unchanged. Outputs share
only immutable `TickInfo` objects with the record.

**Identity.** The key is `(id(state), zero_for_one, sqrt_price_limit_x96,
skip_empty_spans)`.

- The record holds the state strongly, so its id cannot be reused.
- Every lookup also compares a fingerprint of **every** `ConcentratedPoolState` field:
  pool/source/tokens, fee and protocol fee, price, tick, liquidity, fee growth, protocol
  fees, word range, LM pool, and the two frozen mappings (identity first, then content).
- A state changed in place with `object.__setattr__` (a frozen-contract violation) is
  therefore re-recorded, not answered stale.
- A returned or derived state, or an equal copy, is a separate key (a separate valid
  identity).
- The premise is the snapshot ownership contract: `_freeze_mapping` wraps private copies.
  Pool id, direction, amount or a caller-owned mapping's identity alone are never trusted.
- There is no time dependence: the CL swap reads no timestamp, and the LM hook is a
  counted no-op.
- Including the direction is redundant given the limit, since `SPL` forbids one limit
  for both directions (see §4).

**Composition.** Explicit L02 and L03 compose. L02 passes are checkpointed at pass ends,
so their skipped iterations are zero-cost and inside one pass. Logical `steps`, the
crossed ticks and LM-hook counts are carried in the checkpoints, so protocol features
are unchanged. `stats()` reports physical saving separately as `reused_steps`.

**Bounds.** There are at most `max_keys` records and `max_checkpoints` checkpoints in
total, and each record's initial point counts toward the total.

- The least recently used record is evicted first.
- A record that cannot grow stops extending, and the reference loop continues.
- Both bounds must be positive plain ints and have no default (0, −1, `None`, 1.5,
  `True` and `"8"` are refused).
- The instance is caller-owned mutable state: one per solve, not thread-safe, and no
  module-level instance exists (a test checks this).

## 4. Correctness evidence

**Tests.** 42 new tests, all comparing with the reference loop: complete outcome or
identical exception.

| Test | Covers |
| --- | --- |
| `test_prefix_full_step_threshold_is_the_gross_step_cost` | The lemma: 4,000 formula cases (fees 0, 1, …, 1e6−1) and 1,500 real `computeSwapStep` cases, including zero liquidity. `R = cost − 1` is never recordable |
| `test_prefix_reuse_generated_shuffled_amounts_match_reference` | 90 generated pools. All sources, fees and protocol fees, LM hooks, truncated windows, random limits, both directions, L02 on/off and with/without L03. Random amounts plus recorded boundaries `c−1, c, c+1` in shuffled order, through a primed, an unprimed and a shared tiny-budget (3 keys / 64 checkpoints) instance. 3,243 queries per instance: 2,962 OK and 281 errors; 601 exact-boundary amounts, 75 at zero-cost ties; the tiny instance evicted 172 times |
| `…_exact_exhaustion_never_advances_through_zero_cost_steps` (×4) | `A = C_edge` with tied checkpoints: answered with **0** executed steps, stops at the edge, fewer logical steps than the whole walk. `C−1` is partial, `C+1` walks to the bound. `quote_exact_in` gives `OK` vs `INSUFFICIENT_LIQUIDITY`. Both directions × L02 |
| `…_zero_cost_first_step_at_an_initialized_tick` (×2) | Price exactly on an initialized tick: a zero-movement, zero-cost crossing at `C[1] == C[0]`, so every positive amount runs it |
| `…_fee_rounding_and_protocol_fees` (×15) | Fees 0, 1, 3000, 999,999 and 1e6 × Uniswap/Agni/FusionX protocol-fee rules and hooks, amounts descending then ascending plus every recorded boundary. At 1e6 the reuse is never consulted |
| `…_preserves_incomplete_state_and_liquidity_failures` (×2) | The error happens after recording up to an unknown word and is never cached. The exactly exhausting amount does not read that word, and one more unit does. Missing initialized-tick data. Real exhaustion stays `INSUFFICIENT_LIQUIDITY` |
| `…_keys_on_the_complete_state_never_on_derived_or_changed_states` | A returned state, an equal copy, the other direction and another limit are separate keys. Eight in-place field changes (liquidity, price, fee, protocol fee, fee growth, ticks, bitmap, word range) are all invalidated, and each result differs from the stale one. No output aliasing |
| `…_is_bounded_and_accounts_reused_work` | The checkpoint budget stops growth at exactly 40. Repeat queries execute `steps − 39` physical steps with identical logical steps. A query inside the prefix executes only its partial step. LRU key eviction, `clear()` |
| `…_bounds_must_be_positive_ints` (×6), `…_replays_contract_evidence` (×6), `test_ordinary_callers_never_reuse_prefixes` | Constructor refusals. Every deployed-bytecode fixture swap (Uniswap v3 / Agni / FusionX, real and controlled), L02 off/on, plus independent amounts. Default callers execute every step on every call |
| `tests/routing/test_uni_sor_parity.py::test_cl_prefix_reuse_rebuilds_the_golden_rows_and_selection` (×2) | Pinned upstream SOR goldens c01/c02 with prefix alone and with L02+L03+prefix. Every frozen `raw_quote` row, the golden selection, and the independent default-path evaluation equal to the solver's own. 86 resumed queries on c02 |

**Bounded mutant probes.** Ten single-line mutants were each run once against the
prefix tests, and the file was restored and verified with `cmp` afterwards. This was not
a mutation campaign.

- **Killed (9):** `bisect_right` for ties; resume at `j` always; recording partial steps;
  no fingerprint; key without the limit; no eviction; an off-by-one remainder; allowing
  fee ≥ 1e6; not recording crossings.
- **Survived (1):** the key without the direction. This is an equivalent mutant: for one
  state, a valid limit implies the direction.

**Complete-solve differential, all 300 L01 records.** Setup: 4 derived bundles × 6
algorithms × (24 cases + sentinel), `work` at `310c6b9`. Raw data is in
[work-summary-310c6b9.json](latency-l04/work-summary-310c6b9.json).

- **Per-call equality:** every CL swap the solver made, in call order, has the same
  arguments and the same outcome or exception in `l02_l03` and `l02_l03_l04`
  (`all_cl_calls_equal` true). So every quote-table entry and candidate score the solvers
  saw is identical, not only their answers.
- **Record equality:** `all_semantic_equal` / `all_work_equal` against the control and
  `all_baseline_semantic_equal` / `all_baseline_work_equal` against the L01 default
  reference records of `bbda6e2` are all **true**. That includes every `uni_sor_port`
  record (selection) and the `incremental_graph` record truncated at 50,000 quotes.
- **Independent evaluation:** the final evaluation of each record ran on the ordinary
  default path after the variant was removed.

**Order independence on real queries.** Tool `shuffle` at `02b474e`, raw data in
[shuffle-02b474e.json](latency-l04/shuffle-02b474e.json). Every CL call of an ordinary
solve was replayed in two shuffled orders, with L02 off and on, through a fresh instance,
and each complete outcome (full new state) was compared with a fresh reference call.

- Targets: sentinel `uni_sor_port` (3,289 calls), `incremental_graph` (1,180),
  `path_split` (810) and matrix `uni_sor_port` `bnd-09bc4e-201eba-liq_at` (3,562).
- Result: **0 mismatches**.
- Limit: every captured solver call was on one of the 53 original states. Derived and
  changed states are exercised only by the unit tests.

## 5. Accounting (diagnostic; the performance decision is WHI-1510's)

**Work** (300 records, cold instance per solve, post-L03 control → +L04):

| Algorithm | CL swaps | Executed `computeSwapStep` | Resumed queries | Instrumented CPU s (sum, single-shot) |
| --- | ---: | ---: | ---: | ---: |
| direct | 226 | 5,364 → 5,364 | 0 | 0.05 → 0.05 |
| single_path | 4,708 | 251,935 → 125,270 | 2,165 | 2.21 → 1.71 |
| direct_split | 9,813 | 150,591 → 13,457 | 5,107 | 1.34 → 0.65 |
| path_split | 40,716 | 1,255,004 → 158,627 | 20,734 | 11.47 → 6.87 |
| incremental_graph | 127,832 | 1,412,314 → 239,522 | 31,072 | 21.37 → 16.68 |
| uni_sor_port | 85,412 | 3,184,808 → 190,530 | 54,760 | 24.30 → 11.47 |
| **total** | 268,707 | **6,260,016 → 732,770** | 113,838 | |

Logical steps (35,994,954) are identical. The difference is physical computation.

- **Budgets:** there were no evictions or invalidations at the declared evidence bounds
  (4,096 keys / 262,144 checkpoints). The largest single solve held 55 keys and 15,370
  checkpoints.
- **`direct`:** almost no amount repeats, so recording is pure overhead there.

**In-process paired timing.** Tool `paired` at `310c6b9`, raw data in
[paired-310c6b9.json](latency-l04/paired-310c6b9.json). Setup: sentinel full source,
3 rotated pairs, a cold instance per solve, solve-window medians.

| Target | l02_l03 s | +L04 s | ratio | max 1-min load |
| --- | ---: | ---: | ---: | ---: |
| `uni_sor_port` | 2.009 | 0.481 | 0.24 | 56.5 |
| `incremental_graph` | 0.687 | 0.456 | 0.66 | 35.5 |
| `single_path` | 0.1431 | 0.0898 | 0.63 | 29.9 |
| `direct` | 0.00351 | 0.00370 | 1.06 | 28.0 |

Every sample exceeds the 5.0 load bound on this 10-core host. These are contaminated
diagnostics from one case, not a distribution and not a verdict. `direct` shows that the
cold recording cost is not hidden.

**Memory and setup** (tracemalloc pass, instrumented):

- **Sentinel `uni_sor_port`:** the records hold 10,035 checkpoints / 51 keys and release
  **6.36 MB** on `clear()`, about **630 B per checkpoint** including crossings and
  fingerprints. The solve peak was 251.4 MB vs 314.8 MB for the control: fewer transient
  step objects.
- **Other targets:** `single_path` peak +1.7 MB (records 5.7 MB); `direct` +0.28 MB.
- **Worst case:** by that per-checkpoint size, a full 262,144-checkpoint budget would be
  on the order of 165 MB. Records also keep their keyed states alive, which matters for
  derived states. This is an estimate, not a measurement, and the bounds are experiment
  settings, not defaults.

## 6. Effects on WHI-1507 / WHI-1508 and what WHI-1510 must measure

- **WHI-1507 (L05, graph marginal reuse):** L04 already removes most of
  `incremental_graph`'s CL step execution (1.41 M → 0.24 M). L05's remaining share must
  be measured against an explicitly selected L02+L03+L04 (or whatever WHI-1510 adopts),
  not the reference. States derived by allocations are new L04 keys: exact, but cold.
- **WHI-1508 (L06, SOR-fast shortlist):** in this sample, exact L04 removes about 94 % of
  `uni_sor_port`'s executed CL steps. A heuristic shortlist's latency gain and its
  quality loss should be weighed against the post-L04 exact cost, not the reference cost.
  `uni_sor_port` and its goldens stay intact.
- **WHI-1510:** compare L02+L03 against L02+L03+L04, both explicitly selected, back to
  back on a quiet host with L01-SB. Measuring the default-off integration is measuring
  the reference. It must also decide:
  - the lifetime (per solve, as measured here, or longer);
  - the bounds, with no default;
  - whether the `direct`-like no-reuse overhead is acceptable.

## 7. Reproduction

Read-only inputs: the derived bundles and baseline records of L01 experiment
`data/latency-012/whi-1503/20260926T090434484129Z-d5061563` in the primary clone. Raw
outputs are in the gitignored `data/latency-l04/` of the WHI-1506 worktree.

```bash
uv run pytest tests/pools/test_concentrated.py tests/routing/test_uni_sor_parity.py -q -k prefix
uv run python tools/latency/l04_prefix_reuse.py work --experiment <L01 dir> --out data/latency-l04/work.json
uv run python tools/latency/l04_prefix_reuse.py shuffle --experiment <L01 dir> --seeds 2 \
  --target full_source/sentinel:uni_sor_port:quote-b5feb74821d5 --out data/latency-l04/shuffle.json
uv run python tools/latency/l04_prefix_reuse.py paired --experiment <L01 dir> --pairs 3 \
  --target full_source/sentinel:uni_sor_port:quote-b5feb74821d5 --out data/latency-l04/paired.json
```

`02b474e` only added the `shuffle` command. The `work` and `paired` code paths are
identical to `310c6b9`.
