# L08 — final latency results and adoption decisions (WHI-1510)

Status: **the single pre-registered L08 session ran to completion; no experiment is
adopted.** Session `20260926T200553953133Z-dbac5a10` (2026-09-26 20:05–22:17 UTC, measured
source `2fda208`, clean) measured all ten arms with complete coverage, clean determinism
gates and exact semantics, but 8 of the 10 arm experiments exceeded L01's unchanged host-load
rule (1-minute load > 5.0 on the shared 10-core host). Every decision comparison is therefore
`inconclusive` → **not adopted (inconclusive)**: that is a measurement-environment blocker,
not evidence that any experiment is slow (§§3–5). Research key L08
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

## 3. Results of session `dbac5a10`

Evidence (committed, compact): [`latency-results/`](latency-results/) —
[`final-dbac5a10.md`](latency-results/final-dbac5a10.md) / [`.json`](latency-results/final-dbac5a10.json)
(`report.latency final`, generated at `e9f3ebd`, clean; every comparison's verdict, reasons,
timing, charged costs, regret and status transitions), per-case records
[`records-dbac5a10.jsonl.gz`](latency-results/records-dbac5a10.jsonl.gz) (1,750 rows; uncompressed
sha256 `c1b133b7…c7da`), the session manifest, the launch record, the R-vs-L01 identity check
and `SHA256SUMS-dbac5a10.txt` over all 291 raw non-bundle files. The raw session (836 MB,
incl. derived bundles) stays under the gitignored `data/latency-l08/` of the WHI-1510 worktree.
Tables below are derived from the records by [`tables.py.txt`](latency-results/tables.py.txt).
Measured source and later commits: every experiment ran at `2fda208`; the later commits of this
issue change only the report generator's per-case row shape (`e9f3ebd`), one docstring of
`benchmark/latency.py` (a repository guard requires runtime modules naming `uni_sor_*` to name
the pinned `uni_sor_port`) and documentation/evidence, so no measured code path changed.

### 3.1 Session, coverage and load

Launch: the orchestrator waived only the optional 3.0 launch headroom, prospectively (readiness
19:58–20:00 UTC: 3.85, 4.24, 3.84, 4.29, 4.07; instantaneous check 20:05:40 UTC 4.63, no heavy
competitor). During the session unrelated work started (a `cargo test` of another project from
~21:00 UTC, browser and system daemons); per the pre-registered rule the single session
continued, and nothing was retried or discarded.

| Arm | experiment | UTC | load samples | max 1-min load | samples > 5.0 | contaminated | own SB |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| R | `…ce605e14-R` | 20:05–20:52 | 780 | 12.25 | 135 (17.3 %) | yes | ok, not bound |
| E1 | `…db26dbac-E1` | 20:53–21:11 | 770 | 11.46 | 602 (78.2 %) | yes | ok, not bound |
| E2 | `…63367a9a-E2` | 21:11–21:29 | 770 | 34.99 | 762 (99.0 %) | yes | ok, not bound |
| E3 | `…19c85727-E3` | 21:29–21:37 | 770 | 5.62 | 215 (27.9 %) | yes | ok, not bound |
| E4 | `…add6ede2-E4` | 21:37–21:45 | 770 | 5.62 | 58 (7.5 %) | yes | ok, not bound |
| S0 | `…070ced63-S0` | 21:45–22:05 | 145 | 10.78 | 82 (56.6 %) | yes | — |
| H1 | `…bf485880-H1` | 22:05–22:08 | 145 | 4.88 | 0 | **no** | — |
| H2 | `…ab5c9349-H2` | 22:08–22:10 | 145 | 4.37 | 0 | **no** | — |
| H3 | `…c88e4a25-H3` | 22:10–22:16 | 145 | 5.73 | 63 (43.4 %) | yes | — |
| H4 | `…536bd1f3-H4` | 22:16–22:17 | 145 | 6.16 | 145 (100 %) | yes | — |

Every arm: 0 coverage problems (every required run complete, every scheduled record once,
declared samples and cold charge evidence present) and 0 order/cold/repeat inconsistencies;
the session gate never stopped. R's five `main.py quote` invocations exited 0 with exactly one
solve per algorithm (CLI wall 7.77–7.86 s, median 7.77 s; sum of the six solves 6.16 s). The
owner's 5.174 exception (experiment `4e49878d`) is not applied to any of these experiments, and
that retained partial `299b88a` run enters no verdict here.

### 3.2 Exactness, defaults and state (load-independent)

- **The default path is unchanged.** R (`2fda208`) uses the L01 derived bundle hashes, and all
  900 of its records (both orders, both cohorts, cold, sentinel) have semantic fields identical to
  the L01 reference `d5061563` (`bbda6e2`): [`r-vs-l01-d5061563.json`](latency-results/r-vs-l01-d5061563.json).
- **Every control is exact through the spawned-worker path.** In all seven exact comparisons
  (L02, L03, L04, L05, both cumulative, H2→H4): 0 semantic mismatches in every stage, order,
  cohort and bundle; 0 status regressions (750 records per six-algorithm comparison: 685
  ok→ok, 65 no_route→no_route); the budget-bound `incremental_graph` /
  `bnd-78c1b0-201eba-round_at` record is established 1/1 by each arm's own same-source
  L01-SB (unbounded, identical). Work counters differ only for L05: 125 = every
  `incremental_graph` record of the ten runs (its declared `graph_reuse` counters).
- **The controls ran inside the workers and match their predecessors' accounting** (per-solve
  counters over all four bundles): E2 tick memo 5,936,056 hits / 323,960 misses and bin memo
  239,272 / 13,789 — exactly WHI-1505's figures; E3 at most 55 prefix keys and 15,370
  checkpoints in one solve, 0 evictions or invalidations — WHI-1506's figures; with L04 the tick memo sees
  only 408,810 hits because resumed prefixes skip those steps.
- **State isolation:** fixed ≡ reverse order and cold ≡ warm process in every arm; every
  attempt consistent (warmup included); instances are fresh per solve.
- **Heuristic scope under exact controls:** H2→H4 is exact (0 mismatches, identical quotes),
  so L02–L04 compose with the L06/L07 search without changing it.

### 3.3 Verdicts (L01 rules, verbatim)

| Comparison | lane | role | baseline → candidate | verdict | disposition | reason |
| --- | --- | --- | --- | --- | --- | --- |
| L02 | exact | decision | R → E1 | inconclusive | not adopted (inconclusive) | host load (R 12.25, E1 11.46) |
| L03 | exact | decision | E1 → E2 | inconclusive | not adopted (inconclusive) | host load (E1 11.46, E2 34.99) |
| L04 | exact | decision | E2 → E3 | inconclusive | not adopted (inconclusive) | host load (E2 34.99, E3 5.62) |
| L05 | exact | decision | E3 → E4 | inconclusive | not adopted (inconclusive) | host load (E3 5.62, E4 5.62) |
| L02-L04-cumulative | exact | info | R → E3 | inconclusive | — | host load |
| L02-L05-cumulative | exact | info | R → E4 | inconclusive | — | host load |
| H4-exact-controls | exact | info | H2 → H4 | inconclusive | — | host load (H4 6.16) |
| L06 | heuristic | decision | S0 → H1 | inconclusive | not adopted (inconclusive) | host load (S0 10.78) |
| L07-combined | heuristic | decision | S0 → H2 | inconclusive | not adopted (inconclusive) | host load (S0 10.78) |
| L07-adaptive-only | heuristic | decision | S0 → H3 | inconclusive | not adopted (inconclusive) | host load (S0 10.78, H3 5.73) |
| L07-sampling-ablation | heuristic | info | H1 → H2 | **opt_in_only** | opt-in only | both uncontaminated: sampling 17.0 % (full source) / 16.7 % (matched) faster in wall and CPU, cold charge not slower, 0 added loss |
| L06-L07-with-exact-controls | heuristic | info | S0 → H4 | inconclusive | — | host load |

In every inconclusive comparison, host load is the *only* reason: no coverage, determinism,
semantic, sufficient-budget, lost-sample or status problem was found.

### 3.4 Timing diagnostics (contaminated; not adoption evidence)

Shown because the direction and cost accounting are informative, and because a later clean
session should be read against them — not as speedups. Sentinel (full source) warm solve
median over 10 samples, s:

| algorithm | R | E1 (L02) | E2 (+L03) | E3 (+L04) | E4 (+L05) | S0 | H1 | H2 | H3 | H4 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| direct | 0.012 | 0.003 | 0.003 | 0.004 | 0.004 | — | — | — | — | — |
| single_path | 0.223 | 0.158 | 0.133 | 0.086 | 0.086 | — | — | — | — | — |
| direct_split | 0.210 | 0.043 | 0.036 | 0.009 | 0.009 | — | — | — | — | — |
| path_split | 0.843 | 0.537 | 0.424 | 0.232 | 0.233 | — | — | — | — | — |
| incremental_graph | 1.116 | 0.760 | 0.649 | 0.441 | 0.315 | — | — | — | — | — |
| uni_sor_port | 3.759 | 2.467 | 1.900 | 0.446 | 0.448 | 3.752 | — | — | — | — |
| uni_sor_fast | — | — | — | — | — | — | 0.651 | 0.495 | 1.618 | 0.120 |

- Held-out matrix improvements by the comparator (wall; CPU within ±0.01 except where noted):
  R→E1 (L02) 0.39–0.78 on the five timed algorithms, every cold charge `not_slower`;
  R→E3 cumulative 0.49 (`incremental_graph`) to 0.92 (`direct_split`) on full source; E3→E4 (L05)
  `incremental_graph` 0.226 full source / 0.174 matched, the other five within ±0.016
  (between-experiment A/A at ≤ 5.62 load, all `no_worthwhile_change`).
- **E1→E2 (L03) reads slower** on full source (−0.35 to −0.41 wall for `single_path` /
  `direct_split` / `path_split`, every cold charge `slower`, and even the spawn start-up,
  which no control touches, doubled for `direct`), but E2 ran at load up to 34.99 with 99 % of
  samples above 5.0; its matched-cohort readings are +0.07…+0.11 (`uni_sor_port` −0.10). These numbers cannot separate L03's effect from the load; no
  conclusion either way is drawn.
- `direct` has no held-out case above the 0.02 s floor in any arm (`insufficient_cases`).
- Heuristic lane vs S0 (S0 itself contaminated): H1 0.82, H2 0.85, H3 0.68, H4 0.96 wall
  improvement; cold charges `not_slower`.

**Cold fresh-worker charge (full-source matrix, median over the 24 cases, s) and solve peak
(max over cases, MiB)** — the per-algorithm complete-response boundary of the controlled arms:

| algorithm | R | E1 | E2 | E3 | E4 | peak R → E3 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| direct | 0.124 | 0.127 | 0.252 | 0.124 | 0.124 | 1.08 → 1.09 |
| single_path | 0.194 | 0.164 | 0.250 | 0.150 | 0.152 | 15.75 → 15.92 |
| direct_split | 0.127 | 0.130 | 0.283 | 0.128 | 0.126 | 59.89 → 60.00 |
| path_split | 0.398 | 0.345 | 0.473 | 0.248 | 0.249 | 82.03 → 82.45 |
| incremental_graph | 0.797 | 0.535 | 0.671 | 0.429 | 0.331 | 484.56 → 484.98 (E4 485.14) |
| uni_sor_port | 0.779 | 0.350 | 0.329 | 0.222 | 0.220 | 268.66 → 259.21 |

`uni_sor_fast` (full-source matrix, cold charge median / peak): H1 0.255 s / 50.3 MiB, H2
0.243 / 41.2, H3 0.318 / 171.0, H4 0.163 / 41.0; S0's `uni_sor_port` 0.776 / 268.7. Sentinel
cold charges: `uni_sor_port` R 3.94 s → E3 0.62 s (peak 298.7 → 239.8 MiB: fewer transient
step objects); `uni_sor_fast` H1 0.80, H2 0.64, H3 1.79, H4 0.27 s. Start-up incl. prepare is
0.12–0.14 s per worker in every arm except under E2's load; prepare is unchanged by every
control (≤ 0.13 ms native algorithms, ≈18.7 ms SOR port/fast), because no control has a
preparation step: all construction and filling is inside the solve. The independent
evaluation (≈0.2–0.7 ms per plan) always runs on the reference path. All per-case numbers
are in the records.

### 3.5 Heuristic quality, coverage and ablations (load-independent, corrected source)

Regret vs S0's `uni_sor_port` on the same bundle, objective and budget (gross; N/A never 0);
full-source and matched-cohort rows are identical (SOR's candidates are the V2/V3 cohort in
both):

| Arm | held-out ok / N/A | losses | max bps | mean bps | tuning losses | sentinel | quotes vs S0 | fallbacks | sampling stops | validations / rejected |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | --- |
| H1 (L06) | 13 / 2 | 2 | 1.125 | 0.171 | 0 | 0 | 0.217 | 2 | — | 0 / 0 |
| H2 (L07 combined) | 13 / 2 | 2 | 1.125 | 0.171 | 0 | 0 | 0.152 | 2 | 23 converged, 2 no_selection_full_grid | 47 / 0 |
| H3 (adaptive-only) | 13 / 2 | 0 | 0 | 0 | 0 | 0 | 0.467 | 2 | 23 converged, 2 no_selection_full_grid | 45 / 0 |
| H4 (H2 + L02–L04) | 13 / 2 | 2 | 1.125 | 0.171 | 0 | 0 | 0.152 | 2 | as H2 | 47 / 0 |

(Quotes, fallbacks, stops and validations: full-source matrix + sentinel, fixed order.)

- The two losses are the ones WHI-1508 recorded: `emp-09bc4e-201eba-low-1` 1.125 bps and
  `bnd-09bc4e-201eba-liq_at` 1.095 bps, per cohort. They are the shortlist's; sampling adds
  none (H1→H2: 0 loss on every split), and the adaptive-only arm, which keeps every ranked
  route, has none.
- The 2 N/A rows per cohort are the reference's `no_route` cases (`bnd-1bdd88-78c1b0-dust`,
  `…-round_below`): every arm took the charged full-table fallback and stated `no_route`
  only over the full table. No ok → failure transition anywhere; no hard-limit stop, soft
  stop or rejected incumbent occurred in the matrix, so those paths remain covered by unit
  tests only.
- L07's corrected-source metadata (seed validation before any stop, `validations`,
  `grid_completion.reason`) is now measured: 47 validations in H2, 0 rejected incumbents.

## 4. Dispositions

| Experiment | Disposition | Evidence / reason |
| --- | --- | --- |
| Six reference algorithms incl. pinned `uni_sor_port` | **Unchanged and available** | R ≡ L01 reference on 900 records; no default, profile, pin or golden changed |
| L02 empty-span skip (`skip_empty_spans=True`) | **Not adopted (inconclusive: host load)**; stays explicit, default-off | exact in every stage; comparator reason is load only |
| L02 nonzero-bitmap-word index | Not built | only relevant after an L02 adoption, which did not happen |
| L03 tick/bin price memo (per solve, 16,384 / 4,096) | **Not adopted (inconclusive: host load)**; stays explicit, default-off | exact; E2 ran at load ≤ 34.99, so its slower-looking readings are not interpretable |
| L03 LB sorted-tree reuse | Not selected (WHI-1505 evidence unchanged) | 0.2–0.7 % instrumented share on this workload |
| L04 CL prefix reuse (per solve, 4,096 / 262,144) | **Not adopted (inconclusive: host load)**; stays explicit, default-off | exact; 0 evictions; no preparation cost |
| L04 amount-only seam; cross-swap prefix reuse | Not needed / out of scope by design (WHI-1506) | — |
| L05 graph score/admission reuse | **Not adopted (inconclusive: host load, E3/E4 max 5.62)**; stays explicit, default-off | exact; declared counter differences only |
| L05 heap/argmax index | Not built (WHI-1507) | — |
| L06 `uni_sor_fast` shortlist `p5.100-k8-d0` | **Opt-in only; not a default; L06 comparison inconclusive (S0 load)** | real held-out losses ≤ 1.125 bps; no owner loss tolerance |
| L06 aggressive `p100-k2-d0`; TVL/base-token budgets | Not carried (losses up to 35.0 bps; untimed) / not implemented (no frozen TVL) | WHI-1508 evidence |
| L07 combined `c25-r1-snone` | **Opt-in only; not a default; L07-combined comparison inconclusive (S0 load)** | sampling ablation vs L06 is `opt_in_only` on a clean pair (17 % faster, 0 added loss); inherits L06's losses |
| L07 adaptive-only `c25-r1-snone` | **Opt-in only; not a default; comparison inconclusive (S0/H3 load)** | 0 held-out loss at 0.467 × reference quotes; no guarantee (narrow-optimum test) |
| L07 aggressive `c50-r1-s250`, soft caps, 50 % grid | Not carried (losses up to 35.7 bps; untimed); whether soft caps are acceptable at all is the owner's decision | WHI-1509 evidence |
| Composition (L06/L07 + L02–L04) | Exact (H2→H4); timing inconclusive (H4 load) | — |

No experiment is labelled "slower" or "rejected for performance": no comparison reached a
`reject`, and an inconclusive timing is not evidence of slowness.

## 5. Remaining gap and exact decisions needed

- **Unmet:** "adequate declared samples … support latency claims." The samples are complete
  and correctly bounded, but no decision comparison has two uncontaminated experiments, so no
  adopt (or performance-reject) verdict exists for L02–L07.
- **Needed to close it (parent/owner decision, not taken here):** either (a) one new run of the
  same frozen L08 v1 session in an actually quiet window (new session id, no threshold or rule
  change; this issue made no retry), or (b) an explicit owner decision on whether any of the
  marginally contaminated experiments (E3/E4 max 5.62; H3 5.73) may be used, as was done for
  the 5.174 case. Neither is assumed.
- Even an adopt-eligible exact control stays default-off until a separate reviewed activation
  change; every heuristic default needs the owner's explicit acceptance of its reported loss.

## 6. Limitations

One snapshot/block (101082044), the L01 matrix (24 cases + sentinel), `daily_gross`, one
shared Apple M2 Pro (10 logical CPUs; cores not pinned). Ten samples per case do not support a
tail, p95 or SLA; nothing here is a universal subsecond claim or an API equivalence. Only the
`per_solve` lifetime was measured. A controlled arm's complete response is its per-algorithm
cold charge, not a six-algorithm CLI total. Held-out cases were visible to earlier work (§2).

## 7. Reproduction

```bash
uv run python -m benchmark.latency session --arms config/latency/l08.yaml \
  --bundle data/corpus/mantle-5src-101082044/bundle --out data/latency-l08
uv run python -m report.latency final data/latency-l08/<session> \
  --json final.json --markdown final.md --records records.jsonl
python3 docs/references/latency-results/tables.py.txt docs/references/latency-results/records-dbac5a10.jsonl.gz
# one arm / one comparison by hand:
uv run python -m benchmark.latency run --protocol config/latency/l01.yaml \
  --arms config/latency/l08.yaml --arm E1 --bundle <parent bundle> --out <dir>
uv run python -m benchmark.latency sufficient --sufficient config/latency/l01-sufficient-budget.yaml \
  --arms config/latency/l08.yaml --arm E1 --bundle <parent bundle> --out <dir>
uv run python -m report.latency compare <R> <E1> --lane exact --sufficient <R SB> <E1 SB>
```
