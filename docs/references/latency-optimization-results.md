# L08 — final latency results and adoption decisions (WHI-1510)

Status: **pre-registered; final collection pending** (this section is replaced by the
measured results and dispositions once the single pre-registered session has run, or by
the precise blocker if it cannot run). Research key L08
([latency-optimization-research.md](latency-optimization-research.md) §§4, 6). Base
`911f819` (0.1.2 `dev`, WHI-1504…1509 merged, every experiment default-off / opt-in).

Nothing in this issue activates an optimization, changes a default profile, a reference
algorithm, a source pin, the frozen corpus or a result schema. An exact control judged
adopt-eligible stays explicit and default-off until a separate reviewed change; a heuristic
stays opt-in, and any quality loss needs the owner's explicit acceptance (no default loss
tolerance exists: `heuristic_default_loss_tolerance: null`).

## 1. Evidence inventory before the final collection

| Evidence | Source | What it supports | What it cannot support |
| --- | --- | --- | --- |
| L01 experiment `d5061563` | `bbda6e2` | semantic reference of the six algorithms on the L01 matrix (statuses, 685 plans, one budget-bound record) | any timing (max 1-min load 161) |
| L02 pair 1 `08a7dfe8` / `e47f54fe` + SB | `299b88a` / `8c7337a` | exactness of L02 in every L01 stage; direction of the effect | an adopt verdict (loads 62 / 24; not covered by the owner exception) |
| L02 pair 2 `01e21065` | `299b88a` | nothing beyond retained raw data | interrupted during the baseline |
| L02 pair 3 `4e49878d` (owner-accepted load, max 5.174) | `299b88a` reference path | a disclosed partial reference observation: fixed-order full-source matrix 144 + sentinel 6, matched matrix 132/144 | a pair or a baseline: no reverse order (no A/A noise), no cold/memory/CLI, no candidate, no SB; its code-path ids differ from `911f819`, so it measures another source |
| L02–L05 `work` differentials (300 records; L05 50 records × 2 orders) | each PR's source | exactness and executed-work reduction of each control, instrumented | latency |
| L06 tuning/held-out (`9cfb76a` / `05a640a`), L07 tuning/held-out (`cd27439` / `6c934c8`) | pre-fix sources | quality/regret, fallbacks, quote counts of the registered settings | timing (single-shot, loaded), and L07's seed/validation metadata of the corrected source `d8ea906` (merged in `911f819`) |

Missing before this issue: any uncontaminated complete back-to-back pair; any
spawned-worker (warm, cold, memory, charged) measurement of an explicit control; any
`uni_sor_fast` timing with repeats, cold charge or memory; L07's corrected-source counters;
any heuristic + exact composition.

## 2. Pre-registered final protocol (frozen before collection)

Registration: [`config/latency/l08.yaml`](../../config/latency/l08.yaml) (`L08` v1),
committed before any final collection; the measured source is that clean commit. Plan
approved by the orchestrator at the first checkpoint (arms, order, comparisons, same-arm
SB, launch gate, continue-on-load, blocked outcome).

**Unchanged from L01.** [`l01.yaml`](../../config/latency/l01.yaml) v1 (sha256
`961fb522…7f1b`) and [`l01-sufficient-budget.yaml`](../../config/latency/l01-sufficient-budget.yaml)
v1 (`4059506d…b43b`) are used as they are: frozen parent bundle and pinned
`daily_gross.yaml`, the 24-case matrix with its tuning/held-out split plus the sentinel
(USDC → USDT 1000, block 101082044), both cohorts, warmup 1 + 5 measured attempts in fixed
and reverse order, cold fresh-worker stage + separate tracemalloc pass on full source,
the 0.5 × logical-CPU load rule (5.0 on the 10-core host) and every acceptance rule and
threshold (held-out decisions, 0.02 s timing floor, 10 % and 2 × A/A noise, wall and CPU,
cold charged gate, sufficient-budget exactness, verdict order). Derived bundle hashes are
therefore the L01 ones.

**What an arm adds (prospective differences, stated before collection).**

1. *Controls in the worker.* `benchmark.latency run --arms … --arm NAME` wraps each
   algorithm's `solve` so the child worker installs the arm's L02–L04 quote controls
   around that solve only (L05 binds `graph_reuse=True` into `incremental_graph` only).
   Instances are fresh per solve, so construction and filling are charged to the timed
   solve; the reference kernels are restored before returning; the parent's independent
   final evaluation runs in another process on the default path. The measured lifetime is
   `per_solve` only; per-worker/process/bundle lifetimes are not measured and would need a
   new decision. Each record's `search.l08_controls` carries the per-solve memo/prefix
   counters; the run manifest and `uni_sor_fast`'s `quote_path` name the effective
   controls instead of the solver's static "controls off" label.
2. *Complete response.* `main.py quote` cannot select controls and is not changed, so the
   quote-CLI stage (the measured six-algorithm complete response) belongs to the default
   arm R only. A controlled, subset or overlay arm's complete-response boundary is its cold
   per-algorithm charge (start-up incl. prepare + solve + transport + independent
   evaluation); no six-algorithm CLI total is claimed for it. Each arm registers its
   complete stage set; fewer stages are refused, never "partial".
3. *Arm identity.* Experiments and SB runs record the arms file (sha256) and arm;
   `report.latency compare` accepts only the registered comparisons between arms of the
   same arms file and source, the exact lane refuses different algorithm scope or
   heuristic settings, and SB evidence counts only for its own arm and source.

**Arms** (settings are the predecessors' declared settings, not tuned here):

| Arm | Algorithms | Controls / overlay |
| --- | --- | --- |
| R | six references | none (default path) + quote CLI |
| E1 | six | L02 `skip_empty_spans=True` |
| E2 | six | L02 + L03 `TickMathReuse(16384)`, `BinMathReuse(4096)` per solve |
| E3 | six | E2 + L04 `CLPrefixReuse(4096, 262144)` per solve |
| E4 | six | E3 + L05 `graph_reuse=True` (`incremental_graph` only; the other five equal E3: a between-experiment A/A check) |
| S0 | `uni_sor_port` | none (heuristic reference; L01 never drops a baseline algorithm) |
| H1 | `uni_sor_fast` | L06 nomination `p5.100-k8-d0` |
| H2 | `uni_sor_fast` | H1 + L07 combined nomination `c25-r1-snone` |
| H3 | `uni_sor_fast` | L07 adaptive-only nomination (probes 25/50/75/100, 10⁶ routes per probe, `c25-r1-snone`) |
| H4 | `uni_sor_fast` | H2 + L02–L04 (composition) |

Not timed: the aggressive settings (`p100-k2-d0`, `c50-r1-s250`) and the other sweep
points; their dispositions rest on their recorded quality losses (up to 35.7 bps).

**Order.** One session, strictly sequential: R, E1, E2, E3, E4, S0, H1, H2, H3, H4; every
arm that schedules `incremental_graph` gets its own same-source, same-arm L01-SB run right
after its experiment (`benchmark.latency session`).

**Comparisons** (verdict = L01's, verbatim): decisions L02 (R→E1), L03 (E1→E2), L04
(E2→E3), L05 (E3→E4) in the exact lane; L06 (S0→H1), L07-combined (S0→H2),
L07-adaptive-only (S0→H3) in the heuristic lane (`uni_sor_fast=uni_sor_port`).
Informational: cumulative R→E3 and R→E4, H2→H4 (exact controls under the heuristic, exact
lane), H1→H2 (sampling ablation), S0→H4 (composition).

**Dispositions.** exact `adopt_eligible` → adopt-eligible, not activated; `reject` →
rejected; `inconclusive` → not adopted (inconclusive, with its reason) — never described
as slow. Heuristic `opt_in_only` → opt-in only; `reject` → rejected; `inconclusive` → not
adopted (inconclusive).

**Load, noise and stops.** L01's 5.0 rule is the validity criterion; contaminated
experiments are retained and labelled and make their comparisons inconclusive. The owner's
5.174 exception covers only experiment `4e49878d`. Launch gate: one short readiness check
(5 samples of the 1-minute load, 30 s apart; no known heavy competitor); ≤ 3.0 is launch
headroom only, not a validity rule. No waiting loop, no retries: after contamination alone
the single session continues to completion; it stops only for a coverage, determinism or
provenance failure or a broken runner. If the host is busy, the campaign is not launched
and this document reports the blocker.

**Held-out visibility, disclosed.** L02's structure was chosen after inspecting a held-out
case, and the L06/L07 held-out results were visible before later work; the held-out split
is not untouched evidence for any of these experiments.

## 3. Results

Pending the session.

## 4. Reproduction

```bash
uv run python -m benchmark.latency session --arms config/latency/l08.yaml \
  --bundle data/corpus/mantle-5src-101082044/bundle --out data/latency-l08
uv run python -m report.latency final data/latency-l08/<session> \
  --json final.json --markdown final.md --records records.jsonl
# one arm / one comparison by hand:
uv run python -m benchmark.latency run --protocol config/latency/l01.yaml \
  --arms config/latency/l08.yaml --arm E1 --bundle <parent bundle> --out <dir>
uv run python -m benchmark.latency sufficient --sufficient config/latency/l01-sufficient-budget.yaml \
  --arms config/latency/l08.yaml --arm E1 --bundle <parent bundle> --out <dir>
uv run python -m report.latency compare <R> <E1> --lane exact --sufficient <R SB> <E1 SB>
```
