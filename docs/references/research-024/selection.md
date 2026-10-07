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

## 2. Results

Not yet observed.
