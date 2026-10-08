# research-024 preset selection (WHI-1631)

Contract: [`R024-C/1`](contract.md) §4, §5, §10.1, §12. Schedule:
`config/research_024/selection.yaml`. Driver: `tools/research_024/selection.py`; rule:
`tools/research_024/r024_rule.py`; tests: `tests/research_024/test_r024_selection.py`.
Pinned evidence: [`selection/`](selection/).

**Claim (§5.12), exactly:** a preset is "selected by the registered rule `R024-C/1` §5.7 among
these 90 (`split_polish`) / 108 (`marginal_activation`) candidates under P\*, on the already
exposed tuning split." Not claimed: global optimality, optimality among unregistered values,
validity outside block 101082044, best under any other profile (§4.5), or adoption.

## 1. Pre-registration (this section was pushed before the first new tuning solve)

- **Freeze.** The schedule, the 203 generated P\*-rendered profiles (90 + 108 candidates, 5
  reference arms), the driver, the rule and its tests are one commit, pushed before any new
  solve on `bundle_tuning`. `selection/freeze.json` pins every file the stage executes and
  analyses from, every candidate's options, profile sha256 and CEC sha256, and the stage-T
  inventory (380 invocations). Every stage runs from a fresh clone of exactly the pushed commit;
  the ledgers record it.
- **Input.** `tuning_full` only (`ee7afa7e…279b`, 96 cases). `check` refuses a report bundle
  (by corpus directory name and by the two contract hashes) in the schedule, a profile, a stage
  ledger, a run manifest or the inputs root.
- **Runs.** One ordinary run (`main.py run --strategies profile`) and one untimed work pass
  (`tools/research_022/pruning_work.py run`) per non-reused configuration, through the 0.2.1
  executor (ledger, 6 lanes, 30-s load samples, `caffeinate`, `pmset` capture). The only
  re-execution is the executor's infrastructure retry (once, for an attempt without a complete
  manifest, its reason recorded). A `timeout` or truncated row is an outcome.
- **Reuse (§5.9).** The 13 registered 0.2.3 configurations (5 reference arms, 6 E1, 2 E2) are
  reused only when R1–R4 all hold for the ordinary run and the work pass; `run` refuses to start
  while one fails, so the registered inventory is the executed one.
- **Rule.** §5.4 gates by record branch, Rule M / Rule P, §5.5 objective, §5.6 work limit (2.0 per
  unit vs A0), §5.7 rule (ε = 1/100 bps; tie-break quotes, CL swap steps, LB bins, candidate id),
  §5.8 precedence, exact rationals throughout. The rule's values are read from the schedule,
  which `check` holds equal to contract §12; the tests reproduce WE1–WE8 and fail when ε, a limit
  or the tie-break is altered.
- **Stage I (after the winners are fixed).** The committed presets
  (`config/split_polish/preset_v1.yaml`, `config/marginal_activation/preset_v1.yaml`, options
  only, rendered from the pinned stage-T analysis) are run under P\* and compared with their
  candidate's records (§6.2); the §5.10 sensitivity arms run at `graph.chunks` 100 and 200. Its
  inventory is a function of the pinned stage-T analysis and the code of the freeze commit; its
  generated profiles are pushed before it runs. Nothing in stage I can change a winner.

## 2. Execution

| Item | Stage T (the grid) | Stage I (preset runs, sensitivity) |
| --- | --- | --- |
| Source commit (fresh clone, detached) | `d7a423c333388170b96be39954456bdcae59f04a` (the schedule commit) | `a4a3bfeb6950c979e4ffbdfc2a50b8fc07b28b72` (adds the pinned T analysis, the presets and the stage-I profiles; tools unchanged) |
| GitHub push (repository activity API) | 2026-10-07T07:22:34Z (`branch_creation`) | 2026-10-08T02:01:49Z (`push`) |
| First solve (ledger) | 2026-10-07T07:29:43Z (`T-sp-incremental_graph_repair-brent-r1-t1e-3`) | 2026-10-08T02:04:58Z |
| Stage end | 2026-10-08T01:57:04Z (18.5 h wall, 110.2 lane-h, 6 lanes) | 2026-10-08T02:48:16Z (0.7 h) |
| Invocations | 380 / 380 `ok` (190 configurations × ordinary + work pass); no infrastructure retry | 8 / 8 `ok` (2 preset runs, 6 sensitivity arms); no retry |
| Registered reuses (§5.9) | 13 / 13 pass R1–R4 (5 reference arms, 6 E1, 2 E2) | 2 / 2 (`T-C100`, `T-C200` = the `incremental_graph` base arms at chunks 100 / 200) |
| `check` | 0 problems (`selection.py check --stage T --out …`) | 0 problems |
| Pinned | `T-analysis.json`, `T-rule-inputs.json`, `T-SHA256SUMS` (760 raw files), `T-ledger.jsonl`, `T-stage-files-SHA256SUMS`, `T-pmset-sleep-wake.txt`, `T-freeze-push.json` | `I-analysis.json`, `I-SHA256SUMS`, `I-ledger.jsonl`, `I-stage-files-SHA256SUMS`, `I-pmset-sleep-wake.txt`, `I-freeze-push.json` |

Raw records: `router-algorithms-optimizer-artifacts/research-024/selection/<commit>/{T,I}/` (outside the
repository); the reused 0.2.3 records stay pinned by `research-023/campaign/tuning-SHA256SUMS`.
`tables.md` is regenerated from the two analyses by `selection.py tables`; re-running `analyze --stage
T` on the raw records reproduces `T-analysis.json` and `T-rule-inputs.json` byte for byte.

**Incident (no effect on any outcome).** Between 2026-10-07 11:37Z and 14:01Z the host recorded
clamshell `Sleep` / `DarkWake` / `Wake` entries (`T-pmset-sleep-wake.txt`) although the executor held
`caffeinate -i -m -s`; the 30-s load sampler shows no gap above 90 s (2,215 samples), every invocation
ended `ok`, no record is a runner `timeout` and no solve was truncated by `time` (the only truncations
are 73 `max_quotes` cells). Wall time is never a selection input (§5.3 item 4), so nothing was re-run.
Load: median 1-minute load 7.3, maximum 141 (short spikes).

## 3. Records and gates

- **Completeness.** Every candidate × tuning case has exactly one outcome: 198 × 96 = 19,008 candidate
  cells (+ 5 × 96 reference cells), each with its work-pass twin. Status: `ok` 19,008 / 19,008; the
  five reference arms `ok` 96 / 96 each; zero-reference cases: 0 (|C⁺| = 96).
- **Branches.** B3 `polished` 19,008; B0 / B1 / B2: 0.
- **Rule P.** P1 `reconciled` on every cell of every arm (19,008 + 480); no P2–P4 label, no P5.
- **Gates.** G1–G7: no failure in any candidate or reference arm, so no `defect` and no `ineligible:
  status` / `incomplete`; every ineligibility is the work limit.
- **Reuse.** The reused `sp|incremental_graph|brent|r2|t1e-4` (R023 `E1b-A0`) reproduces 0.2.3's
  tuning own-base gain exactly: +0.473 bps, H/E/L 72/24/0, quote ratio 1.292
  (`research-023/campaign/tuning-tables.md`).

## 4. Selection (§5.7)

Q is the mean bps against the common reference (the per-case best of the five P\* base arms). For
context, the reference arms' own Q: `metis_history` −0.606, `metis_inspired` −0.674,
`incremental_graph_repair` −3.716, `incremental_graph` −3.819, `path_split` −8.350.

| | `split_polish` | `marginal_activation` |
| --- | --- | --- |
| Outcome | **selected** | **selected** |
| Winner | `sp|metis_inspired|golden|r2|t1e-5` | `ma|incremental_graph|pf|k4|top9|d1e-3` |
| Eligible / ineligible | 88 / 2 (both `work_limit`) | 93 / 15 (all `work_limit`) |
| Q\* (bps) | +0.1249 (`sp|metis_inspired|golden|r4|t1e-5`) | −2.7808 (the winner) |
| Tie band (Q ≥ Q\* − 0.01) | 3: `golden|r2|t1e-5` (W_quotes 3,278,271), `golden|r4|t1e-4` (4,034,725), `golden|r4|t1e-5` (4,436,184), all on `metis_inspired` | 1 |
| Q(winner), concession | +0.1172, 0.0077 bps (the cheapest in the band) | −2.7808, 0 |
| Margin over E \ T | +0.0025 bps (next: `sp|metis_inspired|golden|r4|t1e-3`, +0.1147) | +0.0995 bps (next: `ma|incremental_graph|pf|k2|top9|d1e-3`, −2.8803) |
| ρ quotes / CL / LB vs A0 | 1.267 / 0.704 / 1.024 | 1.380 / 1.067 / 1.096 |
| W quotes / CL / LB | 3,278,271 / 153,729,810 / 3,020,676 | 3,571,681 / 233,008,057 / 3,231,899 |
| Own-base gain (reported apart, never the objective) | +0.791 bps vs `metis_inspired`; H/E/L 81/15/0; families net +29 / −0 of 32 | +1.038 bps vs `incremental_graph`; H/E/L 82/14/0; families net +28 / −0 of 32 |
| Truncations, refusals, Brent | none, none, (golden solver) | none, none, Brent status 0 × 3,362 |
| Feasible frontier (Q, W_quotes) | 19 members: 9 `path_split`, 10 `metis_inspired` | 15 members: 5 `incremental_graph` pf, 10 `path_split` |
| 0.2.3 nominee's rank | `sp|incremental_graph|brent|r2|t1e-4`: 59th of 88 eligible (Q −3.345; its base trails `metis_inspired` by 3.1 bps) | `ma|incremental_graph|pf|k2|top3|d1e-4`: 21st of 93 (Q −3.143) |

**Constraint outcomes.** For `split_polish` the work limit excludes only
`sp|incremental_graph_repair|golden|r4|t1e-4` / `t1e-5` (quotes 2.02 / 2.20) and does not change the
winner. For `marginal_activation` it is binding: 15 of the 18 `full`-mode candidates on
`incremental_graph` with K ≥ 2 (all but K = 2 / top-1) exceed 2.0 on quotes (2.07–3.87;
`full|k4|top9|d1e-4` also on CL, 2.04), and the
highest Q of the whole grid, `ma|incremental_graph|full|k4|top9|d1e-3` (−2.651, quotes 3.65), is among
them. No CL or LB limit binds alone. These are the registered limits (§5.6, §3 item 3); nothing was
loosened.

**Where the winners lie on the ladders (descriptive, never used).** The `marginal_activation` winner
takes the ladder edge on three knobs (K = 4, top-9, δ = 10⁻³), and the `split_polish` winner the finest
tolerance (10⁻⁵); better values may lie outside the registered ladders. This is outside the claim
(§5.12): the selection is among these candidates only.

## 5. Preset runs (§6.2) and sensitivity (§5.10)

- **Presets.** `config/split_polish/preset_v1.yaml` (sha256
  `bdaba97b702c205fe6fae0b6ce2a439f550f8b0f263713c702789358f92dc533`) and
  `config/marginal_activation/preset_v1.yaml` (sha256
  `553ba71c8569eee67710587dda4a7e24e7dccee1f44ead6107944abc3db76cba`): `{key, version, algorithm,
  options}` only, `options` = the winner's normalized options (`check` re-renders them from the pinned
  analysis). Rendered under P\*, each has its candidate's CEC (`d09fafeb…8871` / `633b5de0…5bf6`).
- **Preset-run equality.** `I-preset-split_polish` and `I-preset-marginal_activation` equal their
  candidates' stage-T records on all 96 cases on status, error, score, `evaluation`, `quotes.counted`,
  `search.<id>` and `search.base`: 0 differing fields. Final outcome of both identities: `selected`.
- **Sensitivity arms** (ordinary runs; the winner arm gated against the base arm of the same chunks:
  G1–G5, G7 pass on all 96 cases, all B3; Q against the unchanged P\* common reference):

| Identity | chunks | Q winner arm | Q base arm | own-base gain vs same-chunks base, H/E/L |
| --- | --- | --- | --- | --- |
| `split_polish` (base `metis_inspired`) | 100 | −0.298 | −0.867 | +0.569, 85/11/0 |
| | 200 | −1.656 | −2.014 | +0.358, 85/11/0 |
| `marginal_activation` (base `incremental_graph`; base arms = R023 `T-C100` / `T-C200`, reused) | 100 | −2.946 | −3.708 | +0.763, 82/14/0 |
| | 200 | −3.332 | −3.949 | +0.618, 82/14/0 |

Under P\* (chunks 50) the winners score +0.117 and −2.781. Sensitivity arms never change a winner and
are not selectable; a result at other settings is a result under those settings (§4.5).

## 6. What this does not show

No report-split record was read or produced (no report bundle in any input, ledger or manifest;
`check`). No timing claim. The tuning split was already exposed (it chose the 0.2.3 nominees, §3);
the claim is the bounded one at the top of this page.
