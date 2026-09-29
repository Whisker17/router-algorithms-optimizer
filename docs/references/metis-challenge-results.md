# Metis-inspired challenge: results (WHI-1449)

Result of the narrow Metis-*inspired* hypothesis H-M1 that
[`jupiter-metis-challenge.md`](jupiter-metis-challenge.md) (WHI-1448, PR #50, `c5b5636`)
approved. `metis_inspired` is an **experimental Python variant, NOT Jupiter Metis**. It
reuses `incremental_graph`'s chunk/carry/merged-plan/evaluation semantics and replaces only
the per-chunk path choice with a hop-layered, quote-driven label search (memo §9.2). Nothing
here claims production equivalence, global optimality or source parity with Metis, and the
variant is not a default: it runs only from `config/metis_challenge/*.yaml` and is in neither
strategy group.

**Empirical source.** Every measured record, diagnostic and verdict below was produced by
the clean commit `446674e80e66ad93834786e1493f223dc38de3eb` (algorithm, profiles, driver).
This document and `metis-challenge-results/` were written later, in a documentation commit
that changes no algorithm, profile, pool, evaluator or runner code (§9). Raw evidence lives
outside the repository, at the durable root in §8. It is hashed there and not committed.

The original IMPLEMENTER session ran S1 through the held-out arms. After a host restart, a
same-role recovery fallback worker on the same model and runtime did the rest: it ran the
registered held-out analysis once, recovered and completed the interrupted optional context
run, archived the evidence and wrote this document.

## 1. Verdict

| Gate (memo §10.6) | Outcome | Evidence |
| --- | --- | --- |
| S1 correctness | **pass** | X1–X7 (+X4b) and the regressions at `446674e` (§2) |
| S2 H-M1a (tuning, M3 vs A0) | **pass**: 89 identical + 7 attributed cases; 4,800 chunks = 4,588 agree + 212 tie; 0 unexplained | `s2-diagnostic.json` |
| S3 work | **pass**: medians A0 218,591 `paths_scored` vs M3 9,229.5 `label_relaxations` (different units); M3 < A0 on 96/96 | `s3-work-gate.json` |
| S4 tractability (tuning, report only) | M4 and M4-off 96/96 `ok`, 0 timeouts/limits. M4-off also fits the caps, so **no pruning-to-tractability claim**; its gross differs from M4 on 12/96 | `s4-disposition.json` |
| H4 diagnostic (the 12 M4/M4-off differences) | **pass**: 0 unexplained chunks; every divergence in the H≥4 classes | `h4-diagnostic.json` |
| **H-M1b held-out (report split, once)** | **better**: 132 wins, 48 losses, 121 ties over 301 paired `ok` cases; exact two-sided sign test p = 2.93 × 10⁻¹⁰ | `heldout-analysis.json` |
| Decision (§10.6) | **keep** the variant as an experimental opt-in result. S2 and S3 passed and the verdict is better. This is not a default adoption. | — |

What "keep" means here: the registered H-M1b statement holds on this frozen corpus and
block. At `label_hops: 4`, the variant's evaluated gross exceeded `incremental_graph` at 3 hops
more often than it fell short. 131 of the 132 wins used a final plan with a 4-hop chunk. The
effect is small: the paired median is 0 bps and the mean is +1.41 bps. It is gross-only and
comes from one block. The boundary stratum is 7 wins versus 8 losses. The comparison changes two
things together, the mechanism and the hop bound (§5), so it does not attribute the gain to
the label mechanism alone. Pruning is not what made 4 hops tractable here: the unpruned
four-hop search also finished (S4).

## 2. Inputs, arms and settings

| Item | Value |
| --- | --- |
| Corpus | `717c21f3…3143`, block 101082044, five sources |
| `bundle_tuning` | `ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b` (96 cases): S2–S4 and H4 only |
| `bundle_report` | `85202b20c13685af6fd999fe3d228ca6de37abb5914b5459621e9a67a71971c0` (302 cases: 80 low, 80 medium, 80 large, 62 boundary): held-out verdict, computed once |
| Objective | gross-only (`full_gross.yaml` twin); no net/empirical-cost view was run |
| Search / graph | max_hops 3, max_splits 4, percent_step 5, chunks 50 |
| Budget | 900 s / 300,000 quotes / candidates null per case |
| Measurement | warmup 0, repeats 1, seed 1447, fixed order; spawn worker, scope algorithm |
| A0 | `incremental_graph` (`config/metis_challenge/a0.yaml`, sha256 `c0c4973b…e641`) |
| M3 | `metis_inspired`, label_hops 3, pruning true (`m3.yaml`, `0cbff3c7…df96`) |
| M4 | `metis_inspired`, label_hops 4, pruning true (`m4.yaml`, `661311df…4471`) |
| M4-off | `metis_inspired`, label_hops 4, pruning false = exhaustive enumeration ablation (`m4_off.yaml`) |
| Context | `direct`, `path_split`, `uni_sor_port` with `full_gross.yaml` values (§7) |

No value was tuned. `label_hops`/`label_pruning` are the memo's registered values, and every
other value is reused unchanged. Counters use declared units: A0 and M4-off count `paths_scored`
(full paths), while M3 and M4 count `label_relaxations` (quote-backed relaxations). The two
are never compared as the same unit.

**S1.** The final implementation checkpoint at `446674e` passed Ruff, mypy, the strict driver
mypy, 176 focused tests and 1,242 relevant tests (3 skips): `tests/routing/test_metis_inspired.py`
X1–X7 plus X4b, and `tests/routing/test_metis_challenge_tool.py`. The parent reproduced this
(Linear WHI-1449, 2026-09-28T15:29Z). A four-case measured fixture check
(`fix-446674e/s2-fixture-measured.json`: identical 4, X7 true) confirmed that the corrected
S2 driver binds measured records. An earlier three-case smoke at `4b446ff`
(`stage1-4b446ff/`) was superseded by that fix and is kept only as history.

## 3. S2 and S3 (tuning, M3 vs A0)

Campaign `campaign-s2s3-446674e-20260928T153446Z`. A0 run `20260928T153506438699Z-27c22c96`
and M3 run `20260928T155909984208Z-2b4216df` are both complete, 96/96 `ok`, with no
truncation or limit hit. The S2 diagnostic ran as a separate labeled correctness pass
(`tools/metis_challenge.py diagnose`) over the measured records, with its own counters. It
never touched the timed solves.

- **S2:** 89 cases identical and 7 attributed. The 4,800 chunks classify as 4,588 `agree`
  and 212 class-1 `tie`, with no other class. There were no unexplained chunks, no missing
  evidence and no quote-subset violation, and `s2_gate.established` is true.
  Allowed classes at H=3 are `tie` and `quote_failure`. Exact gross differs on 3/96 cases,
  all consequences of registered tie-state choices. `x7_identical` is null in the full
  tuning run because X7 was proven by the fixtures and the four-case check, not by that run.
- **S3:** the condition holds (medians above, M3 below A0 on 96/96 paired cases).
  `quotes_executed` M3 ≤ A0 on 96/96 (medians 17,491.5 vs 27,175.5).

## 4. S4 tractability and the H4 attribution (tuning, M4 and M4-off)

Campaign `campaign-s4-446674e-20260928T163229Z`. M4 run `20260928T163515702121Z-4f11fff5`
and M4-off run `20260928T164602394426Z-c3649bf2` are both 96/96 `ok`, with 0 timeouts,
0 limit hits, 0 declared truncation, `accounting_matches_evaluation` true on 96/96, and
completion markers `exit_code 0`.

| Arm | work unit / median | quotes median / max | max solve s (secondary) | final plans with a 4-hop chunk |
| --- | --- | --- | --- | --- |
| A0 | paths_scored 218,591 | 27,175.5 / 58,955 | 51.8 | — |
| M4 | label_relaxations 13,080.5 | 19,945 / 34,360 | 20.0 | 64 |
| M4-off | paths_scored 1,778,207.5 | 116,745 / 264,947 | 270.9 | 64 |

- M4 vs M4-off exact gross: 84 equal, 7 M4 higher, 5 M4-off higher. The unpruned four-hop
  arm also finishes every case inside the caps, so this campaign gives **no positive
  attribution of tractability to pruning**. The registered literal rule ("M4-off finishes
  with the same gross as M4 on every case") is not met because 12 cases differ. Both arms
  have the same A0-relative pattern on tuning (56 higher / 27 equal / 13 lower,
  descriptive, not the verdict).
- **H4 diagnostic** (`campaign-h4diag-446674e-20260928T185905Z`, exit 0, 18:59:05Z–19:13:13Z,
  before any held-out run). It re-ran the unchanged `diagnose_case` at M4's settings, only on
  the 12 differing cases. M4's trajectory replayed exactly on each case, and every chunk
  classified as agree 398, tie 174, token_revisit 22, prefix_admission 6, with 0 unexplained
  chunks and no findings. The allowed H≥4 classes are `tie`, `quote_failure`,
  `token_revisit` and `prefix_admission`. `prefix_admission` is the implementation-found
  H≥4 limitation (fixture X4b): an atomic cycle admission of a relaxation prefix can differ
  from enumeration once four-hop prefixes exist. It is kept as an explicit limitation, not
  widened into S2's H=3 classes. Limitation: this is local same-state attribution per chunk
  on M4's trajectory, not a proof of every final-plan counterfactual.

## 5. Held-out H-M1b (report split, once)

Campaign `campaign-heldout-446674e-20260928T191326Z`. `plan.json` (sha256 `c05f7b2e…d9f5`,
written 19:15:37Z) and `heldout_analysis.py` (`7b12a48c…9a6d`, frozen in the plan) were
written before any held-out run started. The arms ran sequentially, with no competing
benchmark:

| Arm | Run | Window (UTC) | Exit | Statuses | 1-min load max |
| --- | --- | --- | --- | --- | --- |
| A0 | `20260928T191548206673Z-c0f7ec41` | 19:15:47–20:26:37 | 0 | 301 ok, 1 no_route | 3.85 (71 samples) |
| M4 | `20260928T202655510485Z-eedca7fe` | 20:26:55–21:11:45 | 0 | 301 ok, 1 no_route | 3.75 (44 samples) |

Both manifests load strictly: complete, the cases checksum matches, and bundle, source
(`446674e`, clean) and effective settings match the plan. The analysis ran exactly once
(2026-09-29T02:39:01Z, exit 0), over A0 and M4 only. Its optional context argument was left
out because the context run is incomplete. Its strict loader would refuse that run, and the
context never enters the verdict (§7).

- **Denominators:** 302 scheduled per arm, with every status kept. There are no limit hits,
  declared truncations or `last_valid_candidate`. The one non-`ok` case is
  `bnd-1bdd88-78c1b0-dust`, `no_route` in both arms: all 469 cycle-free paths within
  max_hops 3 fail. It is a state-derived boundary and is excluded from pairing only.
- **Paired `ok` = 301: 132 wins / 48 losses / 121 ties.** Ties are excluded, so
  n = 180 and k = 48, giving an exact two-sided p = 2.930 × 10⁻¹⁰ (the exact fraction is in
  the artifact). The result is **better**.
- **bps** (10⁴·(M4−A0)/A0; A0 = 0 on 10 paired boundary dust/round cases, where both arms
  are 0 and the outcome is a tie, excluded from bps only). Overall n=291: mean +1.41,
  p50 0.00, p95 +10.35, min −10.07, max +55.56.

  | Stratum | scheduled | paired | wins | ties | losses | bps n | mean | p50 | p95 |
  | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
  | low | 80 | 80 | 49 | 18 | 13 | 80 | +3.68 | +0.31 | +21.83 |
  | medium | 80 | 80 | 45 | 20 | 15 | 80 | +1.37 | +0.06 | +10.38 |
  | large | 80 | 80 | 31 | 37 | 12 | 80 | +0.19 | 0.00 | +3.81 |
  | boundary | 62 | 61 | 7 | 46 | 8 | 51 | −0.16 | 0.00 | +2.33 |

- **Four-hop attribution:** 131/132 wins (99.2 %) have a final plan that is M4's own
  incremental plan with ≥ 1 four-hop chunk. Retained simpler plans never count. 161 M4 final
  plans contain a four-hop chunk. Of the 48 losses, 30 are M4's own incremental plan (all
  with a four-hop chunk) and 18 are M4 falling back to its retained `path_split` plan where
  A0's 3-hop incremental plan was better. Final-plan sources: A0 incremental 197 /
  single_path 98 / path_split 5 / direct_split 1; M4 incremental 197 / single_path 83 /
  path_split 20 / direct_split 1.
- **Work and accounting:** `accounting_matches_evaluation` true on 301/301 `ok` per arm.
  `quotes_executed` medians A0 26,815.5 vs M4 19,978 (max 192,277 vs 61,125). Medians of
  the work counters, in different units: A0 `paths_scored` 231,560 vs M4
  `label_relaxations` 13,224.5. M4's incremental trajectories, counted whatever final plan
  was chosen, allocated 13,135 chunks: 5,945 one-hop, 2,990 two-hop, 2,400 three-hop and
  1,800 four-hop. A0 records carry no per-chunk hop histogram.
- **Timing (secondary):** both windows stayed below the L01 5.0 one-minute load threshold
  at one-minute sampling. They are still single-shot (warmup 0, repeats 1) with no A/A noise
  run, so they do not meet L01's speed-claim protocol, and **no speed claim is made**.
  Wall seconds are descriptive (median solve A0 11.8 s, M4 8.1 s).

## 6. What the evidence does not show

- It is not Jupiter Metis and has no Metis code, binary or API. The C1 "modifications", the
  split optimizer and Metis's budgets remain unknown (memo §13).
- It does not separate the effects of mechanism and hop bound. H-M1b compares M4 (label
  search, 4 hops) with A0 (enumeration, 3 hops). The same-hop ablation (M4-off) matched M4
  on 84/96 tuning cases and also fit the caps. M4-off was not run on the report split,
  which the registered campaign bound (§10.6) does not include.
- It has no net/empirical-cost result, no matched V2/V3 cohort (`sor_cohort_report`) view,
  and no second block or corpus.
- It has no global-optimality claim. Token-revisit and prefix-admission losses exist by
  design (X4, X4b, H4 classes).
- It has no speed claim: S2/S3 and S4 windows are contaminated (§8), and the held-out windows
  are single-shot.

## 7. Context rows (optional, scope-limited, never the verdict)

The context rows are `direct`, `path_split` and `uni_sor_port` with `full_gross.yaml` values on
the full five-source `bundle_report`. The algorithms have different capability universes:
`direct` uses one pool without splits, `path_split` has no shared pools, and `uni_sor_port`
is V2/V3 only with no Liquidity Book. A gap against them is therefore a capability
difference. It is **not** matched-capability search-quality evidence. No matched V2/V3
cohort (`sor_cohort_report`) arm was run, and none of these rows enters the verdict.

**Interruption.** The original context run is `20260928T211201673200Z-4ffd7323`, started
21:12:01Z by the same wrapper. It stopped without finishing. `cases.jsonl` holds **727
complete, parseable records**: 302 `direct`, 302 `path_split` and 123 `uni_sor_port`. They
form the exact schedule prefix, with no malformed or torn line and no duplicate. The last
record was written at 23:01:15Z and the last load sample at 23:01:51Z. The host then
restarted (`kern.boottime` 2026-09-29T02:24:07Z), for an unknown reason. There is no
`context.completion.json`, so **no exit code was observed and none is claimed**. The
manifest still says `running`. Its `case_count` 604 and `status_counts` are the runner's last
periodic write, taken after `path_split`. The run was never finalized, rewritten or relabeled
complete. A later check found no live `main.py run` process. The additive
`context-interruption-recovery.json` (sha256 `9cb8bb6d…22cc`) records the SHA-256 and size of
every raw file, a line hash for each of the 727 records, the 7 `invalid_plan` records and the
exact 179 missing `uni_sor_port` ids. Its `live_processes` list shows only the recovery
script's own process chain, which matched on its path argument.

**Supplemental completion.** The existing runner cannot append-resume a run, so it was not
patched. Instead, `snapshot.corpus.subset_corpus_bundle` derived a new immutable bundle. It
keeps **all** `bundle_report` pools, and `pools.json`/`prices.json` are byte-identical. It
holds exactly the 179 missing cases in their original fixed order, and its per-case metadata,
envelope and cohorts are equal. The bundle is
`mantle-5src-101082044-091b0759-report-split-ctx-sor-completion-179`, `bundle_hash`
`b6ee6a79d4e408a1762b4871e0714eab873c9d4da8f6de9f3ae4734042133ca2`, `subset_of`
`bundle_report`. It has a **changed case-set identity** and is not a full run. The profile is
`context_sor_supplement.yaml` (sha256 `516543a3…da47`), the original context profile with only
`algorithms` reduced to `uni_sor_port`. Objective, budgets, search/graph, measurement and worker
values are equal, and this is checked against the plan's resolved profile. Run
`20260929T024301783999Z-be6f4cdd` used source `446674e` (clean) and ran 02:43:01Z–03:45:11Z
with **exit 0**. It is complete, with 179/179 records: 172 `ok`, 5 `invalid_plan`, 2 `no_route`.

The supplement has these boundaries:
- It ran in a new process with a cold `uni_sor_port` worker.
- Its `schedule_index` restarts at 0.
- The per-case solver seed is unchanged: `sha256(1447:algorithm:case_id)`, which is
  independent of position.
- It ran in a different wall-clock window.

The combination script `context_analysis.py` (sha256 `dc337947…fac3`) was frozen at 02:46:30Z,
when 13 supplemental records existed. It ran once (exit 0) and wrote `context-analysis.json`
(sha256 `a5b79ed1…facae`). It keeps exactly one record per (original case, algorithm) over a
denominator of **906**: 727 original records plus 179 supplemental ones, each labeled with its
component.

| Algorithm | Scheduled | ok | no_route | invalid_plan | Component | A0 higher / equal / lower (paired ok) | M4 higher / equal / lower |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `direct` | 302 | 277 | 25 | 0 | original 302 | 201 / 76 / 0 (277) | 210 / 67 / 0 (277) |
| `path_split` | 302 | 301 | 1 | 0 | original 302 | 197 / 104 / 0 (301) | 197 / 104 / 0 (301) |
| `uni_sor_port` | 302 | 288 | 2 | 12 | original 123 (116 ok, 7 invalid_plan) + supplement 179 (172 ok, 5 invalid_plan, 2 no_route) | 198 / 90 / 0 (288) | 212 / 74 / 2 (288) |

The runs had no limit hits and no `last_valid_candidate`. Status details:

- **`direct` no_route (25):** the 24 no-direct-pool pairs (`nod-*`) and
  `bnd-1bdd88-78c1b0-dust`.
- **`path_split` no_route (1):** `bnd-1bdd88-78c1b0-dust`.
- **`uni_sor_port` no_route (2):** `bnd-1bdd88-78c1b0-{dust,round_below}` ("no complete
  selection over 0 valid quote entries", B-S10). On the five-source cut, A0 and M4 route
  `round_below` through an LB pool, and the V2/V3-only port cannot use it.
- **`uni_sor_port` invalid_plan (12):** the common evaluator rejects an economic token cycle
  (DESIGN §2.5). The cases are
  `emp-09bc4e-201eba-{low,medium}-{1,3}`, `emp-201eba-cda86a-low-{1,3}`, `emp-78c1b0-09bc4e-low-3`,
  `emp-78c1b0-deadde-low-{1,3,4}` and `bnd-09bc4e-201eba-liq_{at,above}`. The upstream-parity
  port combines routes by pool id only, so the union of its selected routes can form a token
  cycle. This is an existing reference-domain failure, recorded in
  [`v1-acceptance.md`](v1-acceptance.md) §9 and `docs/DEFERRED_ISSUES.md`. The same 12 ids
  appear there for an earlier revision. That is a cross-reference, not a matched control. The
  records are kept as `invalid_plan`, never dropped, relabeled or counted `ok`. The reference
  solver and evaluator are unchanged, and the records never touch the H-M1b denominator.

`uni_sor_port` exceeds M4 on 2 cases, `emp-779ded-09bc4e-large-{1,3}`, by 177 and 112 raw
units. A0 exceeds both on those cases, which are among M4's 48 losses.

## 8. Timing validity, interruption and reproduction

| Window | 1-min load max (samples > 5.0) | Validity |
| --- | --- | --- |
| S2/S3 A0 (tuning) | 5.73 (2); prelaunch 5.04, 5.11 | contaminated |
| S2/S3 M3 | 4.12 | same pre-contaminated host; no claim |
| S4 M4 | 6.64 (2) | contaminated |
| S4 M4-off | 6.1 (4) | contaminated |
| Held-out A0 / M4 | 3.85 / 3.75 (0) | below threshold; single-shot, no speed claim |
| Context original (to the last sample) | 3.92 (0) | single-shot context; no claim |
| Context supplement | 19.9 (12) | contaminated: concurrent non-benchmark host work (rustc builds, browser; `host_observations.txt`) |

The 900 s wall-clock budget makes status and coverage host-load-sensitive. Here no case came
near it: the held-out solve maximum was 54.1 s for A0 and 33.8 s for M4.

### Durable evidence and inventory

Raw evidence (run records, manifests, logs, markers, load samples, diagnostics, the derived
bundle and copies of the two input bundles) sits outside the disposable worktree, at

`/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-artifacts/metis-challenge/`
(`ROOT`, 136 files, 61 MB, read-only).

Paths map as follows:

- A path recorded inside the evidence as `data/metis_challenge/<X>` is now `ROOT/<X>`.
- The corpus inputs `…/router-algorithms-optimizer/data/corpus/mantle-5src-101082044/bundle_{tuning,report}`
  are copied to `ROOT/inputs/bundle_{tuning,report}`. Their bundle hashes were re-verified
  with `load_bundle`.

The tracked files in [`metis-challenge-results/`](metis-challenge-results/):

| File | Content |
| --- | --- |
| `SHA256SUMS` | per-file inventory of `ROOT` (paths relative to `ROOT`) |
| `path-map.json` | old → durable paths |
| `summary.json` | every number above, each with its source artifact's SHA-256 |
| `heldout-per-case.csv` | 302 rows: statuses, gross, outcome, bps, final-plan source, 4-hop |
| `heldout-plan.json` | the registered held-out plan |
| `scripts/*.txt` | exact copies of every campaign script |
| `build_summary.py.txt` | stdlib-only builder of `summary.json` and the CSV |

The worktree originals were copied, never moved, deleted or edited. The copy was verified
file by file (124 campaign files identical to the worktree, plus 12 input-bundle files).

### Commands (usable after the worktree is removed)

```bash
ROOT=/Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer-artifacts/metis-challenge
# 1. verify the archive (from a checkout of this repository)
(cd "$ROOT" && shasum -a 256 -c --quiet "$OLDPWD/docs/references/metis-challenge-results/SHA256SUMS")
# 2. rebuild the tracked summary from the archive (stdlib only)
python3 docs/references/metis-challenge-results/build_summary.py.txt "$ROOT" /tmp/metis-summary
# 3. a replay checkout of the empirical source; recorded relative paths resolve via the link
git worktree add ../metis-replay 446674e80e66ad93834786e1493f223dc38de3eb
cd ../metis-replay && uv sync && ln -s "$ROOT" data/metis_challenge
# 4. recompute a write-once analysis in a scratch copy (outputs refuse to overwrite)
cp -R "$ROOT/campaign-heldout-446674e-20260928T191326Z" /tmp/ho && chmod -R u+w /tmp/ho && rm /tmp/ho/heldout-analysis.json
PYTHONPATH=. uv run python /tmp/ho/heldout_analysis.py /tmp/ho "$ROOT/inputs/bundle_report" \
  /tmp/ho/runs-a0/20260928T191548206673Z-c0f7ec41 /tmp/ho/runs-m4/20260928T202655510485Z-eedca7fe
shasum -a 256 /tmp/ho/heldout-analysis.json   # 6072cb188d6b5f76283f1dedac7649d75c9c41705a6ec47296c7b18d02c4e9f8
```

Step 4 was run once after archiving, from `ROOT`, into a scratch copy. It reproduced
`heldout-analysis.json` byte for byte (`6072cb18…e9f8`). It is a reproduction check, not a
second verdict. The measured arms are **not** to be re-run for the verdict (one pass, §10.6).
Their exact commands are in `heldout-plan.json` `commands`, in each manifest's
`replay_command`, and in `context-interruption-recovery.json` `supplement.command`. With
the link in step 3 in place, point `--bundle` at `ROOT/inputs/…` and `--results-dir` at a
new directory. A replay gets a new run identity.

| Artifact | SHA-256 |
| --- | --- |
| `campaign-s2s3…/s2-diagnostic.json` | `651629a4af3677aa959b56ba5d69eaedbb57181584fde89b1df4ef916b93f65a` |
| `campaign-s2s3…/s3-work-gate.json` | `3eed525b7ea6c0ea5db8be3af95035b808a56553143449653f898ea5cf3237a0` |
| `campaign-s4…/s4-disposition.json` | `4c69df9a68f49a6751f6a30c5bd99b2c0098527d371c90c4d5613114428bafc4` |
| `campaign-h4diag…/h4-diagnostic.json` | `a0306f02ce2ee5ee0abaf05b662d4ec102293cf9517285e5f542ee1576596d9a` |
| `campaign-heldout…/plan.json` | `c05f7b2e9fa10461d27fbd4ad01179ac3c3ada69f77eca18de99b63079f8d9f5` |
| `campaign-heldout…/heldout-analysis.json` | `6072cb188d6b5f76283f1dedac7649d75c9c41705a6ec47296c7b18d02c4e9f8` |
| A0 held-out manifest / cases | `d9ff3d5b54474f8669a0319792a8f6479a00ebf440caa5fdd07389ae81ead461` / `67dedc7aee59cb57a914d5326cdee4d4b156be4506902bf52db19cef235661ec` |
| M4 held-out manifest / cases | `d34e7c6f8a79ec4669d4d2917156e09cf1e5ec1fe3a78105185452551644af7f` / `97bc64e0eccd0de1a114fc38f056c8d67030661c7a80065356cbf521b219c3d2` |
| `campaign-ctxsor…/context-interruption-recovery.json` | `9cb8bb6d2faddd6d1c5ae9cef471438c7b0e8148b6912e55cf4bb3f8f25822cc` |
| `campaign-ctxsor…/context-analysis.json` | `a5b79ed1c92f5a83ec48f9341c75321c9cc96240356ac89e862f47b3cc9facae` |
| original context `cases.jsonl` (727 records, never finalized) | `5602eaf06205e9a1cb978c7a76153066717e078e3ac27bb26b108eea82704458` |

Full per-file hashes are in `SHA256SUMS`.

## 9. Source equivalence after the base update

The measurements ran on the branch frozen at `446674e` (base `c5b5636`). After all
measurement and archiving, `origin/dev` `aa70104` (PR #52: optimized-SOR docs, examples and
`tests/docs` only) was merged into the same branch, in merge commit `43298ff` with no force
push. The merge changed only `docs/examples/routing-algorithms/run_examples.py`,
`docs/references/{routing-algorithms,single-request,strategy-groups}.md` and
`tests/docs/test_routing_algorithm_examples.py`. The git tree objects of `routing/`,
`benchmark/`, `pools/`, `snapshot/`, `config/`, `report/`, `tools/`, `main.py`,
`pyproject.toml` and `uv.lock` are identical at `446674e` and at the merge. The code, profiles,
pools, evaluator and runner behind the evidence are therefore unchanged, and no frozen run was
regenerated. The documentation commit adds only this file and `metis-challenge-results/`.
Release baseline B `ec42cef7468335a8290d23aa27213fdbaa2e7fb3` is untouched. The B..H ledger
must disclose this intervening `dev` integration.
