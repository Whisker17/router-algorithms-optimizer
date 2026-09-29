# 0.2.1 source register and evidence inventory (WHI-1547)

Companion of [`contract.md`](contract.md) (`R021-C/1`). It pins every source the contract
relies on, lists what is missing, records where the external proposal's assumptions differ
from the actual repository, and gives the disposition of the report's documentation
findings D1–D5. Reconstructions R1–R10 are **new WHI-1547 evidence**, recomputed by
`tests/docs/test_research_021_contract.py`; they are not an execution of the external
author's bundle.

## 1. Repository pins

| Item | Pin | Evidence |
| --- | --- | --- |
| Release 0.2.1 baseline B | `81559ab16376cf46416a69727c3ca0b45e72771a` (`dev`) | orchestration document; `git rev-parse origin/dev` at WHI-1547 start |
| External handoff baseline | `358bbe0c0a86965607c950d12ee0ce0e84915b2d` | handoff header |
| B versus handoff baseline | only `docs/handoff/algorithm-exploration-handoff.md` (+377 lines, PR #55, commit B itself) | `git diff --stat 358bbe0 81559ab` |
| Corpus | `mantle-5src-101082044-091b0759`, `bundle_hash` `717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143` | `docs/references/v1-acceptance/bundles.json` |
| Splits | `bundle_tuning` `ee7afa7e…279b` (96), `bundle_report` `85202b20…71c0` (302), `sor_cohort_tuning` `b900b866…7ade` (96), `sor_cohort_report` `8213b7b0…e640` (302) | same file; directories present in the primary clone's gitignored `data/corpus/mantle-5src-101082044/` |

So no source, configuration or test differs between the handoff's baseline and B: every
repository fact below holds at both.

## 2. Supplied external files

| File (as named by the report §8) | Status | Pin / location |
| --- | --- | --- |
| `research-report.md` | present, owner-supplied, untracked in the primary clone's `docs/external/` | SHA-256 `0588e83ab19a07bc0da4c6f7df42c60b7bd5e572d6903ab86d62318c388ba1d9`, 33,416 bytes; archived byte-identically as [`sources/research-report.md`](sources/research-report.md) with [`sources/SHA256SUMS`](sources/SHA256SUMS) |
| `verify_research.py` | **absent** | — |
| `results.json`, `results-repeat.json` | **absent** | — |
| `run.log` | **absent** | — |
| `prepare_splits_scan.py` | **absent** (the report itself says it was never run against this repository) | — |
| `provenance.json`, `README.md` | **absent** | — |

Search performed: the primary clone's `docs/external/` holds only the report; a bounded
filename search (`~/Downloads`, `~/Desktop`, `~/Documents`, `~/Work`, `/tmp`, depth 6) found
no `verify_research.py`, `prepare_splits_scan.py` or research `provenance.json`. No other
channel to obtain the bundle is documented. The report's links to these files therefore
dangle in the archive, and its §2.6 counts (200 graphs, 400 order checks, 1,426 / 228,984
quotes, "0 mismatches", byte-identical reruns) are the author's claims only.

Attribution: the report is an external research agent's Chinese-language analysis dated
2026-09-29, supplied by the repository owner; its author is not named in the file. It is
archived for internal reference by later worktrees (private internal research, no
redistribution) and is not edited: corrections live here and in the contract.

References cited by the report and their status here: [H] is the repository handoff (read
at B). [U] `UniswapV2Library.getAmountOut` agrees with the repository's migrated formula
(`pools/constant_product.py`, which also pins Merchant Moe Classic `MoePair` at `460bf55`).
[B] Boost RCSP, [C1]/[C2] Angeris et al. / Diamandis et al. and [L] LFJ fees were not
fetched by WHI-1547; [C2] is pinned by WHI-1557 and [L] belongs to WHI-1560.

## 3. Actual repository behavior versus the report's assumptions

| # | Report assumption | Repository at B | Consequence (contract section) |
| --- | --- | --- | --- |
| 1 | Source state is "as the handoff says"; dev HEAD not confirmed | B = handoff baseline + the handoff document only | all handoff navigation facts hold |
| 2 | CPMM fee factor 997/1000 | generic `fee_bps`; the only admitted CPMM source (Moe Classic v1) requires `fee_bps == 30`, the identical floored formula | report CPMM arithmetic transfers to admitted CPMM pools; synthetic fixtures may use other fees (X4: 5 bps) |
| 3 | `q(x) = 0` is a feasible zero output | a positive input whose output floors to 0 is `insufficient_output_amount`; the evaluator makes that plan `invalid_plan`; only a zero-*input* step is a no-call zero (R2) | `zero_output_leg: infeasible` (§3.1); the report's split domain can be larger than the repository's |
| 4 | 20-point grid `floor(A·k/20)`, `A−x` | `direct_split`: floors for every leg but the last in bundle insertion order, `ALL_REMAINING` last, zero non-final legs dropped | equal only when the floored pool comes first; admitted order changes the optimum (R6: 58 vs 59) — `pool_order` is part of the domain |
| 5 | repeated-token walks might be admissible | the evaluator's static check rejects any plan token-graph cycle (R1: the 23,708 walk is `invalid_plan`) | `token_reuse: simple_path`, `dag_admission: plan_token_dag` |
| 6 | static sum / sequential / merged accounting may coincide | evaluator replays steps sequentially (165); `incremental_graph` merges one step per pool (166); static sum is 180 (R4) | `pool_reuse` vocabulary (§3.5) |
| 7 | one budget meaning | `max_candidates` means pools, paths, finalists, per-chunk paths, per-chunk relaxations or a route-count threshold depending on the solver; `metis_inspired` searches `graph.label_hops` while its fallback uses `search.max_hops` | governing-limit table (§3.3); caps are separate options |
| 8 | in-solve validation may be free | the evaluator calls the metered `pools.quote.quote_exact_in`, so in-solve evaluation already consumes the worker's quote meter | one attempt ledger (§5.3); bypassing the meter is forbidden |
| 9 | X4/X4b classes unknown | X4 loses a *simple* 4-hop path (R7); X4b loses a legal continuation to prefix-dependent admission (R8) | D3 below |
| 10 | split directory names to verify | `bundle_tuning`, `bundle_report`, `sor_cohort_tuning`, `sor_cohort_report` exist | §6.1 |
| 11 | E/L/S arms are new labels | existing arms: A0 ≈ `E3`, M3 = `L3`, M4 = `L4`, M4-off = `E4` (tuning, WHI-1449) | §6.2; M4-off never ran on the report split |
| 12 | `--strategies base` for a `max_splits` scan | runs the six base algorithms; for `incremental_graph` the scan changes only its embedded fallback | §6.2 `max_splits_scan` labeling |

## 4. Documentation findings D1–D5

| ID | Report claim | Evidence checked | Finding | Disposition |
| --- | --- | --- | --- | --- |
| D1 | handoff §6.6 calls a historical p95 coverage gap "可测的上限" | R9: `v1-acceptance/report-full/paired_gross.csv`, run F1, `path_split` vs `uni_sor_port` coverage row: p95 +31.31 bps, mean +1598.63 bps; since max ≥ mean, some cases lie far above the p95. The gap compares two strategies with different protocol coverage; it is not the causal gain of adding LB to a fixed strategy | confirmed | handoff §6.6 reworded; the number and `v1-acceptance.md` unchanged |
| D2 | "所有 L08 决策比较均 inconclusive" conflicts with the clean H1→H2 `opt_in_only` | R10: `latency-results/final-dbac5a10.json`: all 7 `decision`-role comparisons are `inconclusive` (host load only); of 5 `informational` comparisons exactly one, `L07-sampling-ablation` H1→H2, is `opt_in_only` | the sentence is correct for the decision role; the report's tension is wording, not contradictory frozen evidence | handoff §5.2 sentence scoped to the seven decision comparisons and names the informational one |
| D3 | "cannot repeat a token" may mean a cyclic optimum or a discarded legal prefix | R7: X4's lost optimum S–A–X–Y–D repeats no token; the dominant 2-hop label at X (S–Y–X) had already visited Y. R8: X4b's lost S–B–V–X–D is legal; only the dominant label's continuation closes the committed cycle X→A→V→X. R1: the report's walk A–B–C–B–T is genuinely cyclic and outside the evaluator's domain | confirmed: both repository fixtures are legal-prefix losses | handoff §5.1 reworded; §6.1 notes that amount-only top-k is not a general repair (report §2.2; not reconstructed here, WHI-1549 owns it); `routing-algorithms.md` and the frozen results unchanged |
| D4 | every optimality statement needs its complete domain | DESIGN §2.6/§2.11 and the solver docstrings | adopted | contract §3 domain record and governing-limit table |
| D5 | future campaigns need fixed identity fields | DESIGN §2.10–§2.11, L01 §2 | adopted | contract §4.1 (`source`), §6.1 (`holdout_exposure`), §6.3 (freeze list), §9.3 (settings hashes) |

## 5. New reconstructions (R1–R10)

All in [`fixtures/reconstructions.json`](fixtures/reconstructions.json); expected values from
an independent hand integer formula or committed artifacts.

| ID | Source | Recomputed | Repository check |
| --- | --- | --- | --- |
| R1 | report §2.1 | single-label 9,938; best simple path A–D–C–B–T 19,560; walk A–B–C–B–T 23,708; at C the via-B label (11,926) dominates via-D (9,840) | evaluator: simple path `ok` 19,560; walk `invalid_plan` (token cycle) |
| R2 | report §2.3 | `q(0..6)` on (1000, 1000) = 0, 0, 1, 2, 3, 4, 5 (not discretely concave) | evaluator: 1-unit input with zero output is `invalid_plan` |
| R3 | report §2.3 | G(0) = G(1) = G(2) = 70, G(3) = 72, G(15) = 76 = raw max | — |
| R4 | report §2.4 | 180 / 165 / 166 | evaluator: sequential 165, merged 166 |
| R5 | report §2.5 | stepwise 99 vs continuous ∈ (198.40, 198.41) | — |
| R6 | new | grid optimum 58 (order p1, p2) vs 59 (p2, p1); raw optimum 59 at x = 10; exact-rational tangent bound 59; continuous optimum ∈ (59.36, 59.37) | `direct_split` returns 58 and 59 for the two orders |
| R7 | X4 fixture | 19,979,965,070 / 9,989,982,535 / 1,995,991,039 / 998,998,253; 4994.98 bps | evaluator accepts the simple 4-hop plan |
| R8 | X4b fixture | — | evaluator accepts the M4-off union, rejects the cyclic union |
| R9 | `paired_gross.csv` | p95 31.31 < mean 1598.63 | — |
| R10 | `final-dbac5a10.json` | 7 decision comparisons `inconclusive`; 1 of 5 informational `opt_in_only` | — |

Not reconstructed or verified here: the report's §2.2 top-k beam family (its construction is
not specified), its §2.6 random 200-case checks and work counts, the N1 proof boundary,
the N2 two-pool certificate beyond R6's example, and the contents of [B]/[C1]/[C2]/[L].
Those remain external claims; WHI-1549/1551/1557 own the corresponding proofs.
