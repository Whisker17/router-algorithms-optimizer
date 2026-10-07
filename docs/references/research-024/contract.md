# Research contract `R024-C/1`: preset selection and the 19-strategy experiment for `split_polish` and `marginal_activation`

| Item | Value |
| --- | --- |
| Issue / Release | WHI-1630, Release 0.2.4 (`0beaa4c3-2721-4618-b687-ac70d4d54e6a`) |
| Downstream | WHI-1631 (selection, §5), WHI-1632 (roster and provenance, §6), WHI-1633 (campaign and timing, §7–§8), WHI-1634 (guide, §9.3); Release 0.2.5 `R025-C/1` (WHI-1636) is written as a delta of this contract |
| Repository base | `release/v0.2.4` @ `dbc9432620fad36cabe7cdf0f4024b819326e2eb` (= v0.2.3 content) |
| Domain | frozen corpus `mantle-5src-101082044` (block 101082044), exact input, **gross-only** |
| Machine-readable registry | §12 (one YAML block; every value a later issue applies is there) |
| Executable check | `tests/docs/test_research_024_contract.py`: recomputes P\* and the base configurations with the loader, expands both candidate grids through the real option validators, runs the §5.7 rule on the §12 worked examples, checks the earlier campaigns' `check` outputs, the timing units and the release plan |
| Independent review | §13 (rounds, dispositions, reviewer provenance); verbatim reports in [`reviews/`](reviews/) |

This contract fixes, before any new observation, how the presets of `split_polish` (E1) and
`marginal_activation` (E2) are selected, how both identities join the ordinary comparison, and how the
19-strategy campaign measures quality, deterministic work and latency. It implements no runtime code and
changes no existing strategy, profile, preset or historical record. It claims no optimality, no speed
gain, no out-of-block validity and no default adoption. The mechanisms of E1/E2 are `R023-C/1` §3–§6,
unchanged.

**Normative words.** "Must", "is" and "never" bind the downstream issues. Every number a later issue
applies is in §12; where prose and §12 differ, §12 wins and the difference is a defect of this
contract. A later issue that finds a case this contract does not decide stops and reports it; it does
not decide it after seeing results (§11.3).

## 1. Scope and what each later issue reads

| Issue | Reads | Inherits (all in §12) |
| --- | --- | --- |
| WHI-1631 | §3, §4, §5, §10.1, §12 `pstar`, `selection` | P\* hashes; candidate grids (90 + 108); the objective, limits, ε, tie-break order; the reuse list; worked examples its rule tests must reproduce |
| WHI-1632 | §4.4, §6, §12 `roster` | `R024_ADDITIONS` order; the provenance invariants I1–I4; the 9 `research_021` finding identities |
| WHI-1633 | §7, §8, §9, §10.2–§10.4, §12 `campaign`, `timing` | arm matrix, reporting checklist C1–C15, gates, timing units, N = 3, triggers, outcomes |
| WHI-1634 | §5.12, §8.7, §9 | the bounded claim wording; the P\* non-transfer rule; timing wording |
| WHI-1636 (0.2.5) | the whole contract | every rule applies unchanged unless `R025-C/1` states a delta with a reason |

## 2. Premises re-derived from the repository

The issue's design statements were re-derived on `dbc9432`; scripts and outputs are in
`router-algorithms-optimizer-artifacts/research-024/whi-1630/premise/` (outside the repository).

| # | Statement of the issue design | Outcome |
| --- | --- | --- |
| P1 | P\* = the `config/full_gross.yaml` values: 3 hops / 4 splits / 5 %, `graph.chunks` 50, the M4 label settings, 900 s / 300,000 quotes, seed 1447 | **Holds, with one precision.** `full_gross.yaml` itself declares no label settings: `--strategies all` adds `graph.label_hops: 4` and `graph.label_pruning: true` from `config/metis_challenge/m4.yaml` (`benchmark/strategies.py` `METIS_SETTINGS`, sha256-pinned). The base-option pins are `config/metis_history/preset_v1.yaml` and `config/incremental_graph_repair/preset_v1.yaml`. The complete effective P\* and its hashes are in §4 |
| P2 | Presets are options-only; shared `search.*` / `graph.*` are profile-owned (R021-C/1 §7) | **Holds.** R021-C/1 §7.1: a preset holds "only its `algorithm_options`"; `benchmark/strategies.py::effective_document` writes only `algorithm_options` for an added identity |
| P3 | A0 / C100 / C200 differ only in `graph.chunks` | **Holds**, but the evidence is R023's generated profiles, not R021-C/1 §7: `config/research_023/profiles/{a0,c100,c200}.yaml` (and `e1b-a0` vs `e1b-c100`) differ only in `graph.chunks` and the header comment |
| P4 | E1 adds +0.529 / +0.494 bps on S4 / M4 vs +0.447 on A0 | **Holds** (`research-023/results.md` §0 Q1, §3 Q1) |
| P5 | Full mode beats PF by ≈ 0.05 bps at 2.26× vs 1.31× base quotes | **Holds approximately; stated precisely:** activation over the work-matched control +0.257 (full) vs +0.210 (PF), Δ 0.047; over the call-matched control +0.258 vs +0.195, Δ 0.063; mean gain over A0 +0.723 vs +0.660, Δ 0.063 (`research-023/campaign/report-analysis.json`). Report-split quote ratios vs A0 2.260 vs 1.305 (`results.md` §3, physical work). So "≈ 0.05–0.06 bps" |
| P6 | Golden matches Brent's gross at ≈ 1.2× polish quotes | **Holds:** Brent vs golden 107/110/74, mean +0.004 bps; golden spends a paired median 1.207× Brent's polish quotes (`results.md` Q5) |
| P7 | The same `incremental_graph` case with 37,015 quotes took 11.81 s in 0.2.2 and 10.19 s in 0.2.3 | **Holds:** case `emp-09bc4e-201eba-large-1`, `quotes.counted` 37,015 in both; `solve_seconds` 11.815 (0.2.2 `R-A1-ig-full`, `cases.jsonl` sha256 `97e85a50…d4d3`, pinned in `research-022/campaign/report-analysis.json`) vs 10.191 (0.2.3 `R-A0`, sha256 `879db006…1081`, pinned in `research-023/campaign/report-SHA256SUMS`): +15.9 %. 0.2.2's own timing stage was `inconclusive` (`research-022/results.md` §7) |
| P8 | D1-F2: registering a preset equal to an old override flips `source.kind` and changes old resolved-profile hashes | **Holds; reproduced in memory.** `benchmark/profile.py::options_entry` matches against the *currently* registered `options_preset` and `historical_presets`. Registering a preset equal to `config/research_023/profiles/e1b-a0.yaml`'s options turns its `source` from `{kind: override}` into `{kind: preset, …}`; `settings_sha256` is unchanged, the canonical resolved-profile sha256 changes `9d20d2aa…5148` → `29fc4958…b9c7`. `tools/research_021/campaign.py::resolved_identity` hashes exactly that resolved profile |
| P9 | `research_021` `check` reports 9 known findings | **Holds.** Identities in §6.4 and §12 `roster.research_021_known_findings`; `research_022` and `research_023` `check`: 0 problems |
| P10 | Runtime estimates come from committed 0.2.2/0.2.3 ledgers and logs | **Holds.** §10 cites only committed ledgers/analyses and raw records pinned by committed hashes. Nothing was run except the read-only `check`s and in-memory loader calls |
| P11 | (needed for reuse, §5.9) The runtime code did not change after R023's stage T | **Holds:** `git diff 1bfd3f97 dbc9432 -- routing pools snapshot benchmark report main.py tools/research_022/pruning_work.py uv.lock config/metis_challenge config/metis_history config/incremental_graph_repair config/full_gross.yaml` is empty |

## 3. Exposure: what 0.2.3 already informed

Everything in this contract was written after these committed observations were known. They are
disclosed so that the bounded claim of §5.12 can be judged; none of them is a selection input.

1. **The nominees** (`R023-C/1` §4.7, §5.2: Brent R = 2, tolerance 10⁻⁴; PF, K = 2, top-3, δ = A/10⁴)
   were chosen after inspecting the tuning probes. They are the centre of every ladder in §5.2.
2. **The base sets** are implementation facts, not observations: E1 accepts the five `split_polish.BASES`
   (WHI-1623, WHI-1626); E2 accepts only `split_polish.E1_BASES` (`incremental_graph`, `path_split`).
3. **Tuning-split work ratios** (`research-023/campaign/tuning-tables.md`, physical work; total quotes
   vs A0): E1b-A0 1.292, E1b-M4 1.066, E1b-S4 1.094, E1b-REP 1.654, E1b-PS 0.591, E1g-A0 1.370,
   E2pf-A0 1.326, **E2full-A0 2.458**. The work limit of §5.6 (2.0) was set knowing that the
   0.2.3 full-mode configuration exceeds it on quotes, while every 0.2.3 E1 and PF configuration is
   within it.
4. **Report-split figures** (P4–P6, Q2 "E1 loses to C100 on counts", E2 negative mean vs M4/S4) were
   known. The only report-derived knowledge a rule value uses is the size of the tuning-to-report drift
   of the E1/E2 gains (`results.md` §4: "within a few hundredths of a bps"), cited as one reason for
   ε = 0.01 bps (§5.7). The report split itself is never read by the selection (§5.3).
5. **Stage-T wall times** feed only the scheduling register (§10), which is not a ceiling.

The tuning split (96 cases) is `exploration_data` already used to choose the 0.2.3 nominees. The report
split is `previously_exposed` (0.1.0, 0.2.0, 0.2.1, 0.2.2, 0.2.3).

## 4. The selection profile P\*

### 4.1 Definition

P\* is the shared part of the effective profile that `main.py run --strategies all` derives from
`config/full_gross.yaml` on `dbc9432`:

| Section | Value |
| --- | --- |
| objective | `gross_only` (resolved `fixed_cost: 0`) |
| search | `max_hops` 3, `max_splits` 4, `percent_step` 5 |
| graph | `chunks` 50 (source), `label_hops` 4, `label_pruning` true (from `m4.yaml` during derivation) |
| budget | `time_limit_seconds` 900, `max_quotes` 300,000, `max_candidates` null |
| measurement | warmup 0, repeats 1, seed 1447, order `fixed`, `memory_pass` false |
| worker | `spawn`, scope `algorithm`, `prepare_time_limit_seconds` 120 |

Hashes (canonical JSON = `json.dumps(value, sort_keys=True)`, the loader's convention in
`tools/research_021/campaign.py::_canonical_sha256`):

- `config/full_gross.yaml` sha256 `51a8a65184ae765867fd9d7763776852b2b4576e3cf7116a8dc15ccd87028177`;
  `config/metis_challenge/m4.yaml` sha256 `661311df6ff7a36a43954fff0e42f6b30b824d35d96c06f0e215960f256d4471`.
- **`pstar.shared_sha256`** = sha256 of the canonical JSON of
  `{schema_version, objective, budget, measurement, worker, search, graph}` of
  `parse_profile(derive(full_gross, "all")).resolved()`:
  `e198f7e774f89f30eac0e7ccfd40d2e8a5f97b06d2d43fe00d870ff82fcc7cb1`.
- The full 17-identity resolved profile of that derivation on `dbc9432`:
  `aa188fa82d333cfdb0b33a3ba8907b745fae341e2449406ba18ea8941c234f82`. It changes legitimately when
  WHI-1632 adds two rows; with `R024_ADDITIONS` removed it must stay equal (invariant I1, §6.3).

### 4.2 Base configurations under P\*

The five E1 base identities are run exactly as the ordinary rows of the P\* derivation:

| Base (arm) | `algorithm_config.params` | options |
| --- | --- | --- |
| `incremental_graph` (A0) | chunks 50, max_hops 3, max_splits 4, percent_step 5 | none |
| `path_split` (PS) | max_hops 3, max_splits 4, percent_step 5 | none |
| `metis_inspired` (M4) | chunks 50, label_hops 4, label_pruning true, max_hops 3, max_splits 4, percent_step 5 | none |
| `metis_history` (S4) | chunks 50, label_hops 4, max_hops 3, max_splits 4, percent_step 5 | preset v1 (`f4510b51…5799`), `settings_sha256` `183bb1ff…ceb6` |
| `incremental_graph_repair` (REP) | chunks 50, max_hops 3, max_splits 4, percent_step 5 | preset v1 (`13774bcd…786a`), `settings_sha256` `89af5028…4abf` |

A `split_polish` base with options receives them as `base_options` = the preset v1 options written out
(exactly as R023's E1b-S4 / E1b-REP profiles did).

### 4.3 Candidate effective configuration (CEC)

For a parsed profile `p` and an identity `id` in it, `CEC(p, id)` is the canonical JSON of

```text
{algorithm: id, objective, budget, measurement, worker, search      (from p.resolved()),
 params: p.resolved().algorithm_config[id].params,
 options: p.algorithm_options[id].options or null,
 settings_sha256: p.algorithm_options[id].settings_sha256 or null}
```

and `cec_sha256` its sha256. CEC deliberately excludes `graph` keys the identity does not read, the
options `source` (override vs preset) and the `selection` record. Two configurations are **equivalent
under P\*** iff their CEC bytes are identical. The CEC hashes of the five bases are in §12
`pstar.bases`; the contract test recomputes them both from a P\*-rendered single-identity profile and
from the `all` derivation.

### 4.4 Rendering a P\* profile

A P\*-rendered profile for identity `id` with options `o` is: `schema_version: 2`, `algorithms: [id]`,
`objective`, `search`, `budget`, `measurement`, `worker` copied verbatim from `config/full_gross.yaml`,
`graph` = the three P\* graph keys, `algorithm_options: {id: o}` (omitted when `o` is null). It runs
with `--strategies profile`. A configuration is **representable under P\*** iff it renders, parses,
and every `graph.*` key its identity requires (`graph_params` / `graph_params_for`) is a P\* key.

### 4.5 Non-transfer

The ranking of §5 is a ranking under P\*. It does not transfer to another profile. A chunk-sensitivity
arm under otherwise unchanged P\* (§5.10) is not validation under `config/daily_gross.yaml`, which also
differs in hops (2), chunks (200) and budget (120 s / 50,000 quotes). A result under another profile is
reported as a result under those settings, never as evidence that the preset is best there.

## 5. Preset selection (WHI-1631)

### 5.1 What is selected

One **options-only** preset per identity, `{key, version, algorithm, options}` in the format of
`benchmark.profile.preset_options`:

- `config/split_polish/preset_v1.yaml`, key `R024-P01-split_polish`, version 1;
- `config/marginal_activation/preset_v1.yaml`, key `R024-P02-marginal_activation`, version 1.

`options` is exactly the winner's normalized options (`validated_options` output), including
`base_options` for an S4/REP base and `arm: treatment` for E2. Shared `search.*` / `graph.*` stay
profile-owned. "Selected" always means "selected under P\*" (§4.5, §5.12).

### 5.2 Candidate grids

Full Cartesian products of the ladders below (§12 `selection.identities`). A candidate id is the
`id_format` string filled with the ladder **labels**; ids are compared by Python string order.

| Identity | Varied (ladder) | Fixed | Count |
| --- | --- | --- | ---: |
| `split_polish` | `base` ∈ {`incremental_graph`, `path_split`, `metis_inspired`, `metis_history`, `incremental_graph_repair`} × `solver` ∈ {brent, golden} × `rounds` ∈ {1, 2, 4} × `tolerance` ∈ {10⁻³, 10⁻⁴, 10⁻⁵} | `grid` 10⁹, `maxiter` 60; `base_options` = the base's preset v1 for S4 / REP | **90** |
| `marginal_activation` | `base` ∈ {`incremental_graph`, `path_split`} × `mode` ∈ {pf, full} × `activations` (K) ∈ {1, 2, 4} × `top_k` ∈ {1, 3, 9} × `delta_share` (δ) ∈ {10⁻³, 10⁻⁴, 10⁻⁵} | E1 stage `solver` brent, `rounds` 2, `tolerance` 10⁻⁴, `grid` 10⁹, `maxiter` 60; `seed_share` 10⁻⁴; `arm` treatment | **108** |

**Ladder rule.** Each numeric knob takes its 0.2.3 nominee and one step either side on a fixed ladder:
×2 for rounds and K (1, 2, 4), ×3 for top-k (1, 3, 9), a decade for tolerance and δ. Tolerance 10⁻⁴ is
the "1 BPS precision" / "0.01 %" of `R023-C/1` §1 (M7, U1). Chunk variants (C100, C200) are not
candidates; they are sensitivity arms (§5.10).

**Fixed values, with reasons.**

- `grid` 10⁹: interior points lie on D = 10⁹ share units. The finest tolerance (10⁻⁵) is 10⁴ grid
  units, so D never binds the tolerance; changing D only moves rounding below the tolerance.
- `maxiter` 60: on the 0.2.3 report split every Brent call on A0 converged (SciPy flag 0, 8,730 calls).
  At the finest tolerance a bounded search over ≤ 10⁹ units with `xatol` ≥ 5·10³ needs about
  log_φ(2·10⁵) ≈ 26 golden steps, below 60. Non-zero Brent flags are reported (§5.11), never a gate.
- E2's E1 stage = the E1 nominee: it keeps the activation knobs separate from the polishing knobs (the
  E1 grid explores those) and keeps E2's matched controls, which are defined relative to its own E1
  stage, comparable with `R023-C/1` §5.3.
- `seed_share` 10⁻⁴: a starting point. PF searches the new share over the whole interval [0, 1] with
  t = 0 evaluated exactly; full mode polishes every pair. The seed moves the start, not the domain.
- `arm` treatment: the controls are campaign arms (§7.2), never presets.

### 5.3 Runs and records

1. **Schedule first.** `config/research_024/selection.yaml` (inputs, candidates, generated profiles,
   reuse list, this rule's parameters) and the generated candidate profiles are committed and **pushed
   before the first new tuning solve**; every stage runs from a fresh clone of exactly that commit.
2. **The only bundle read** is `tuning_full` (`bundle_tuning`, bundle_hash
   `ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b`, 96 cases, `full_source`).
   No selection ledger, manifest or input may name a report bundle (`85202b20…71c0`,
   `8213b7b0…e640`).
3. **Per non-reused candidate:** exactly one ordinary run (`main.py run --strategies profile` on its
   P\*-rendered profile) and exactly one untimed work pass (`tools/research_022/pruning_work.py run`,
   same profile). **Reference arms:** the five bases of §4.2, ordinary run + work pass each.
4. **Executor:** the 0.2.1 executor (`tools/research_021/campaign.py`: ledger, lanes, 30-s load
   sampler, `caffeinate`, `pmset` capture). The only re-execution is its infrastructure retry: once,
   only for an attempt without a complete manifest, reason recorded. A completed run is never repeated.
   Wall time is never a selection input.

### 5.4 Gates (per candidate, every tuning case)

**Record branches.** A post-processor record (`split_polish`, `marginal_activation`, an E2 control) is
classified by the fields it carries, never by defaults for missing fields (`split_polish.py::_solve`,
`marginal_activation.py::_solve`; §12 `selection.record_branches` holds one example per branch, the
first two taken from saved R023 records):

| Branch | Recognised by | Produced by |
| --- | --- | --- |
| B0 `terminated` | `search.<id>` or `search.base` absent | a runner hard limit (`timeout`), a worker error, an `algorithm_error` |
| B1 `base_passthrough` | `search.base.status` ≠ `ok` | the base's own non-`ok` status, returned unchanged (e.g. R023 `R-E1b-A0` `bnd-1bdd88-78c1b0-dust`: `no_route`, no `base_gross`) |
| B2 `not_reached_before_polish` | `search.base.status` = `ok` and no `search.<id>.base_gross` | the cooperative budget ending while the base plan is replayed (`scope: not_reached`); the base result is returned |
| B3 `polished` | `search.base.status` = `ok` and `search.<id>.base_gross` present | every other return: improved, unchanged, refused (`unsupported_topology`), truncated, E2 `not_reached` after E1 |

**Gates** and the branches they apply to (§12 `selection.gate_applicability`; "n/a" = not evaluated).
A candidate's *base reference record* is the record of the same case in the reference arm (§5.3 item 3)
of the identity named by its `base` option (for a sensitivity arm: §5.10).

| Gate | Condition | B0 | B1 | B2 | B3 | Failure |
| --- | --- | --- | --- | --- | --- | --- |
| G1 completeness | ordinary run and work pass each have a complete manifest and exactly one record per tuning case | ✓ | ✓ | ✓ | ✓ | `ineligible: incomplete` |
| G2 status pass-through | the record's status equals its base reference record's status | ✓ | ✓ | ✓ | ✓ | `ineligible: status` |
| G3 never-worse | B2: gross = base reference gross; B3: gross ≥ base reference gross | n/a | n/a | ✓ | ✓ | `defect` |
| G4 base identity | `search.base` (algorithm, status, quotes, search) equals the base reference record (algorithm, status, `quotes.counted`, search); B3 also `search.<id>.base_gross` = its gross | n/a | ✓ | ✓ | ✓ | `defect` |
| G5 ledger | B1: `quotes.counted` = `search.base.quotes`; B2: `quotes.counted` ≥ `search.base.quotes` (the charged replay has no own-quotes field; the difference is reported); B3: `quotes.counted` = `search.base.quotes` + `search.<id>.quotes` | n/a | ✓ | ✓ | ✓ | `defect` |
| G6 pair reconciliation | the rule P of §5.4.1 between the ordinary record and its work-pass twin | ✓ | ✓ | ✓ | ✓ | `defect` only for P5; P2–P4 are labels |
| G7 independent evaluation and refusal | status `ok`: `evaluation.gross_output` = `search.<id>.gross`; `scope: unsupported_topology`: `search.<id>.gross` = `base_gross` | n/a | n/a | n/a | ✓ | `defect` |

"Gross" of a record is `int(evaluation.gross_output)` when its status is `ok`, else 0.

#### 5.4.1 Missing values and pair reconciliation

**Rule M (missing values).** A value a record does not carry is *missing*: never 0, never a default.
Per cell, `quotes` is the ordinary record's `quotes.counted` when it is not null (a runner hard limit
writes `{"attempted": null, "counted": null}`, `search: {}`); `cl_swap_steps` and `lb_bins_swapped` are
the work-pass record's `search.r022_work` fields when that block is present (the work-pass wrapper adds
it only when the solve returns). A sum over cells with any missing cell is missing.

**Rule P (pair reconciliation)**, for every arm that has a work-pass twin (selection candidates, the
reference arms, every campaign arm with a `WP-*` twin, whatever the identity), per cell, first match:

| Case | Condition | Outcome |
| --- | --- | --- |
| P1 `reconciled` | the work pass carries `r022_work`, the ordinary record has non-null `quotes.counted`, and status, score, `evaluation` (canonical JSON: the runner's independent replay of the submitted plan) are equal and `r022_work.quotes_executed` = `quotes.counted` | all three units available |
| P2 `work_pass_terminated` | the work pass lacks `r022_work`; the ordinary record has non-null `quotes.counted` | label; `quotes` available, CL and LB missing |
| P3 `both_terminated` | the work pass lacks `r022_work`; the ordinary `quotes.counted` is null | label; all three units missing |
| P4 `ordinary_terminated` | the work pass carries `r022_work`; the ordinary `quotes.counted` is null | label; `quotes` missing, CL and LB available |
| P5 `differs` | both carry their fields and any compared value differs | `defect` |

P2–P4 are labelled outcomes, reported (C7, C13), never compared, never defects. §12
`selection.pair_examples` holds one example per case.

### 5.5 Objective

- **Common reference.** `ref(c)` = the largest gross on case `c` among the five base reference
  records of §4.2 (the reference set of §12 `selection.quality`); 0 when none is `ok`.
  `C⁺ = {c : ref(c) > 0}`.
- **Per case.** `bps(c) = 10⁴ · (g(c) − ref(c)) / ref(c)`, with `g(c)` the candidate's gross (0 for any
  non-`ok` status: a failure scores zero output).
- **Objective.** `Q = (1/|C⁺|) Σ_{c∈C⁺} bps(c)`: equal weight per case (the tuning split has 32
  directed pairs × 3 cases, so this is also equal weight per pair). Zero-reference cases are counted
  and reported, never scored. `|C⁺| = 0` ⇒ no selection (§5.8).
- **Arithmetic.** Exact rationals (`fractions.Fraction`) end to end; no floating point in the rule.
- Gain over the candidate's own base is reported separately (§5.11) and is never the objective.

### 5.6 Work constraint

- **Totals.** For candidate `x` and unit `u`, `W_u(x)` = the sum over all 96 cases (every status) of
  the per-cell values of Rule M: `quotes` from the ordinary record (base + post-processing, one
  ledger), `cl_swap_steps` and `lb_bins_swapped` from the work pass. A candidate with any missing
  `W_u` is `ineligible: work_unavailable`.
- **Reference.** The `incremental_graph` reference arm (A0) under P\*. If any `W_u(A0)` is missing,
  both identities end `no_selection` (cause `reference_work_unavailable`).
- **Ratio.** `ρ_u(x) = W_u(x) / W_u(A0)`; 0/0 = 1; a positive total over 0 = +∞.
- **Limit.** `x` is eligible on work iff `ρ_u(x) ≤ 2` for **each** of the three units (exact compare;
  2 itself passes). Reason: a post-processor may at most double the total work of the ordinary
  `incremental_graph` row; quotes, CL steps and LB bins are separate limits because they are different
  costs (R021-C/1 §5.2: never added or divided across units). Exposure: §3 item 3.

### 5.7 The rule

1. `E` = candidates with no `ineligible` and no `defect` outcome under §5.4 that pass the work limit.
2. If `E` is empty: no selection (§5.8).
3. `Q* = max_{x∈E} Q(x)`; tie band `T = {x ∈ E : Q(x) ≥ Q* − ε}`, **ε = 1/100 bps** (exact).
4. **Winner** = the lexicographic minimum over `T` of
   `(W_quotes, W_cl_swap_steps, W_lb_bins_swapped, candidate id)`.

Reason for ε: a mean difference below 0.01 bps is economically negligible (0.01 USD per 10,000 USD
traded), smaller than the tuning-to-report drift of the 0.2.3 gains (§3 item 4), and so does not justify
more work. Inside the band the cheaper candidate wins; outside it quality wins.

### 5.8 No winner, defects

Per identity the outcome is exactly one of, with precedence `blocked_defect` > `no_selection` >
`selected` (a later defect turns a `selected` identity into `blocked_defect`):

- `blocked_defect`: a `defect` of the identity: any of its candidates has a `defect` outcome (G3–G7 on
  an applicable branch, a P5 `differs`, §5.4), a sensitivity arm has one (§5.10), or the preset run
  differs from its candidate (§6.2); a P5 `differs` or another `defect` in a reference arm is a
  `defect` of both identities. A 0.2.4 fix issue is filed (the WHI-1629 escape clause). After the fix
  merges, the identity's whole grid is re-run from a newly pushed schedule at the fix commit, without
  reusing any pre-fix record; this rule applies unchanged;
- `no_selection`: `E` empty, `|C⁺| = 0`, a reference arm unavailable (G1 failure after its retry), or
  `reference_work_unavailable` (§5.6);
- `selected`: otherwise, the §5.7 winner.

`no_selection` and `blocked_defect` publish the full record (§5.11) and keep WHI-1632 blocked for that
identity. A limit is never loosened, ε never widened and a nominee never installed. With fewer than two
selected identities the 19-strategy campaign (§7) is not started: the release objective R1 is reported
unmet and the owner decides; any reduced campaign needs an amendment (§11.3) before it observes
anything.

### 5.9 Reuse of R023 tuning records

A configuration's 0.2.3 stage-T records (`router-algorithms-optimizer-artifacts/research-023/campaign/1bfd3f97…/T/`)
are reused instead of a fresh run only if **all** hold, for its ordinary run **and** its work pass:

- **R1 configuration:** the CEC bytes of the R023 profile equal those of the P\*-rendered candidate
  (§4.3; "byte-equivalent configuration under P\*").
- **R2 code and environment:** `git diff --quiet 1bfd3f97fb205d21bb93acb754dfafcc1d851265 <schedule commit> --
  routing pools snapshot benchmark main.py tools/research_022/pruning_work.py uv.lock` succeeds, and the
  manifest's `environment` (`python_version`, `machine`, `dependencies`) equals the selection runs'.
- **R3 inputs:** manifest `bundle_hash` = `ee7afa7e…279b` and the 96 case ids are `tuning_full`'s.
- **R4 integrity:** the manifest is complete, and the sha256 of each reused `cases.jsonl` and
  `manifest.json` equals its line in `docs/references/research-023/campaign/tuning-SHA256SUMS`.

Reuse is all-or-nothing per configuration; a failure means a fresh run (both records). Every reused
configuration is listed in the selection ledger with its source paths and the outcome of R1–R4. The
reusable set (§12 `selection.reuse`) is 13 configurations: the five reference arms (R023 A0, PS, M4,
S4, REP); E1 `brent|r2|t1e-4` on all five bases and `golden|r2|t1e-4` on `incremental_graph`; E2
`pf` and `full` on `incremental_graph` with K 2, top-3, δ 10⁻⁴. All other campaign records of 0.2.4
are fresh (§7.1).

### 5.10 Sensitivity arms

After the winner is fixed, for each chunk value `k` ∈ {100, 200}: the **winner arm** (the winner's
options, P\* with `graph.chunks: k`, otherwise unchanged) and the **base arm** (the winner's base
identity with its §4.2 options, the same rendering). Ordinary runs only, no work pass. A base that does
not read `graph.chunks` (`path_split`) gets `not_applicable` with that reason and neither arm runs.

Validation (§12 `selection.sensitivity`):

- G1 checks the scheduled ordinary records only (complete manifest, one record per tuning case);
  G6 does not apply.
- On the winner arm, G2–G5 and G7 apply per branch with the **base arm of the same `k`** as its base
  reference record (the embedded base runs at chunks `k`, so it is never compared with the P\* reference
  arms). On the base arm, only G1.
- A `defect` there is handled as in §5.8.

Reported: own-base gain against the same-`k` base arm, and `Q` against the unchanged P\* common
reference of §5.5 (the five P\* reference arms). Sensitivity arms never change the winner and are never
selectable. §5.9 applies with the sensitivity rendering (R023's C100, C200, E1b-C100 and E2pf-C100
records qualify only if CEC-equal).

### 5.11 What is published

`docs/references/research-024/selection.md` with pinned ledgers and analysis (SHA256SUMS), per
candidate: id, options, fresh run ids or reuse source, G1–G7 outcomes, status counts, `Q`, own-base
gain (mean bps over cases with own-base gross > 0, H/E/L, improved count, families net +/−), `W_u` and
`ρ_u` per unit, eligibility reason, `truncated_by` counts, refusal counts, Brent status counts; then
the full ranking (`E` by `(−Q, W_quotes, W_cl, W_lb, id)`, ineligible by id), the feasible frontier
(members of `E` not dominated in `(Q, W_quotes)`; `y` dominates `x` iff `Q(y) ≥ Q(x)` and
`W_quotes(y) ≤ W_quotes(x)` with at least one strict), the winner, its **margin**
(`Q(winner) − max{Q(x) : x ∈ E \ T}`, `none` when `E = T`), its **concession** (`Q* − Q(winner)`,
in [0, ε]), and the sensitivity arms. Frontier, margin and sensitivity never change the winner. The
rule implementation must reproduce the selection from the pinned records and its tests must reproduce
§12 `selection.worked_examples`.

### 5.12 The claim

Exactly: **"selected by the registered rule `R024-C/1` §5.7 among these 90 (`split_polish`) / 108
(`marginal_activation`) candidates under P\*, on the already exposed tuning split."** Not claimed:
global optimality, optimality among unregistered values, validity outside block 101082044, best under
any other profile (§4.5), or adoption.

## 6. Roster and historical provenance (WHI-1632)

### 6.1 Roster

`benchmark/strategies.py` gains `R024_ADDITIONS = ("split_polish", "marginal_activation")`, appended
under `--strategies all` after `R022_ADDITIONS`, each identity once, `custom` group, with its preset
written out as `algorithm_options` like every earlier addition. From `config/full_gross.yaml` this gives
the 19 rows of §12 `roster.all19`, in that order. An identity whose outcome is not `selected` is not
added.

### 6.2 Equality with the selected candidate

Under P\*, `CEC(all-derived profile, id)` equals `CEC(P*-rendered winner, id)` for both identities,
and the preset run's tuning records equal the candidate's on status, error, score, `evaluation`
(canonical JSON), `quotes.counted`, `search.<id>` and `search.base`. A difference is a `defect`
(§5.8): WHI-1632 does not proceed for that identity.

### 6.3 Historical provenance invariants (D1-F2)

- **I1.** Every run-profile document in the repository at `dbc9432` (`config/**/*.yaml` that parses as a
  run profile, including the 0.2.3 explicit profiles) has the same canonical resolved-profile sha256
  after the change as at `dbc9432`; so does `derive(full_gross, "all")` with `R024_ADDITIONS` removed
  (§4.1).
- **I2.** Every saved stage record of `research_021`, `research_022` and `research_023` reconciles with
  its own tool exactly as before: the static `check` **and** the reconciliation against saved
  `manifest.resolved_profile`s.
- **I3.** `source` stays derived from document content. A document can never claim a preset identity
  for other settings; legitimate explicit overrides stay allowed; preset-file tampering and false
  metadata are refused; no check is disabled globally; no frozen profile, manifest or evidence hash is
  rewritten.
- **I4.** Tests cover (a) a registered preset equal to an R023 nominee (use a test preset equal to
  `e1b-a0.yaml`'s options regardless of the actual winner) and (b) one different from every R023
  nominee.

The mechanism is WHI-1632's choice within I1–I4. One admissible mechanism: an R024 pin is recognised as
`preset` only inside a document whose `selection.mode` is `all`, so the selection-free 0.2.3 explicit
profiles keep `{kind: override}`.

### 6.4 Earlier campaigns

`research_022`'s registered `all17` and `research_023`'s checks stay at **0 problems** by freezing their
roster derivation to their own release's additions (as `tests/research_021` already does for
`R022_ADDITIONS`), never by editing a registered roster or schedule. `research_021`'s `check` reports
exactly the 9 findings of §12 `roster.research_021_known_findings`. A finding's **identity** is
`(invocation id, registered roster)`; the `derives […]` list in its message is not part of the
identity. Each of the 9 is a pre-existing `--strategies all` roster mismatch (registered 14 identities,
derived 17 on `dbc9432`): `T-roster-full`, `T-roster-sor`, `T-quote-smoke`, `R-roster-full`,
`R-roster-sor`, `M-roster`, `I-all-details`, `I-all-compact`, `I-memory-gross`. A tenth finding, a
missing one, or a changed identity fails.

### 6.5 Visibility

Batch (`main.py run`), single quote (`main.py quote --details`), report and replay show both rows
exactly once, with their counters and options provenance. Old saved profiles (8/9/14/17-strategy and
the 0.2.3 explicit ones) replay literally.

## 7. Campaign matrix and accounting checklist (WHI-1633)

### 7.1 Stages and freezing

- `config/research_024/campaign.yaml` (inputs, arms, profiles, rules) is pushed before stage T.
- **T** = every quality arm on the tuning bundles. **Freeze** = the schedule plus the final analysis
  code, pushed **before the first report-split solve**. **R** = every quality arm on the report bundles,
  **once**, from a fresh clone of the freeze commit. **L** = timing (§8), after R, alone on the host.
- No record of an earlier campaign or of WHI-1631 is reused in T, R or L. The only re-execution is the
  infrastructure retry of §5.3 item 4.

### 7.2 Arms

| Arm | Content | Bundles |
| --- | --- | --- |
| `Q19-full` | the 19 rows of `--strategies all` from `config/full_gross.yaml` (one run, or registered group runs whose resolved per-identity values equal the derived roster's; `check` enforces it) | `tuning_full`, `report_full` |
| `Q19-sor` | the same on the matched V2/V3 cohort | `tuning_sor`, `report_sor` |
| `BASE-E1`, `BASE-E2` | identically configured base control of each selected post-processor: the `Q19` row of its base identity when, restricted to the keys the base factory declares (`search_params`, `graph_params`, `graph_params_for`), its `algorithm_config.params` equal those the post-processor hands its base, and its options equal the preset's `base_options` (none when absent); otherwise a separate arm with that configuration (P\* rendering) | as `Q19-full` and `Q19-sor` |
| `E2-E1only` | `split_polish` with E2's base and E2's six E1 keys (= E2's E1 stage); the `Q19` `split_polish` row serves when CEC-equal, otherwise a separate arm | full cohort, both splits |
| `E2-wm`, `E2-cm` | `marginal_activation` with the E2 preset and `arm` `work_matched` / `call_matched` (`R023-C/1` §5.3) | full cohort, both splits |
| `WP-*` | an untimed work pass for every arm above except `E2-wm` / `E2-cm` | as its arm |

Bundles: `tuning_full` `ee7afa7e…279b`, `tuning_sor` `b900b866…7ade`, `report_full` `85202b20…71c0`,
`report_sor` `8213b7b0…e640` (96 / 96 / 302 / 302 cases; full hashes in §12 `campaign.inputs`).

**Not scheduled:** historical mechanism ablations (R021 E/L/S, same-grid allocation, repair off/on,
cycle-safe SOR, CFMM CPMM-vs-CL, `max_splits` scan; R022 A2–A5; R023 golden control, E2-full ablation,
C100/C200 arms, CFMM + E1). They are not re-run; their earlier results stand as history.

### 7.3 Domains and ranking

- **Protocol ceiling** of an identity: its `capabilities.protocols` (`V2` → `constant_product`, `V3` →
  `concentrated`; absent = all three families). A pairwise comparison on a cohort is **rankable** iff
  both identities' ceilings intersected with the families present in that cohort's bundle are equal.
  On `full_source` the four `uni_sor_*` identities and `cfmm_dual` (CPMM + CL) and
  `direct_split_certified` (CPMM) are therefore not rankable against the all-protocol identities;
  on `sor_compatible` everything except `direct_split_certified` is.
- Rankable comparisons are "output quality under P\*". A pure search-improvement (mechanism) claim
  needs the R021-C/1 §3.4 class `same_domain`, which this campaign does not establish between different
  identities. Non-rankable comparisons are reported in a separate expanded-protocol section, side by
  side, never ranked or pooled.
- Like-for-like statements about the SOR identities come only from `Q19-sor`.

### 7.4 Reporting checklist (each item present in `results.md`)

| # | Item |
| --- | --- |
| C1 | Unconditional status counts for every arm × cohort × split (every scheduled cell, every status) |
| C2 | Matched-success denominators per comparison: scheduled, common `ok`, only-A `ok`, only-B `ok` |
| C3 | Zero-reference handling: baseline gross 0 counted apart (`zero_baseline_na`), excluded from bps |
| C4 | Per-case distributions: n, mean, min, p5, p50, p95, max (nearest rank) and H/E/L |
| C5 | Directed-pair families (family = the case's `(token_in, token_out)`): net + / net − counts per comparison; a per-family table for each new identity vs its base control and vs **every** rankable other row (the C9(a) set), plus vs the C9(b) envelope labelled as an envelope |
| C6 | Strata per comparison: stratum = `bnd` or `nod` for those case-id prefixes, else the size word (`low` / `medium` / `large`) of an `emp-` case id |
| C7 | Truncation, refusal and fallback disclosures: `truncated_by`, `unsupported_topology`, `not_reached`, runner `timeout` by limit, last-valid candidates, each identity's own fallback counters, the branch counts B0–B3 (§5.4) and the Rule P labels P2–P4 (§5.4.1) |
| C8 | Work units separately: `quotes`, CL swap steps, LB bins, and each identity's search counters; ratios only within one unit |
| C9 | Comparisons: (a) each new identity vs each of the other 18 rows per cohort and split (rankable per §7.3, else the expanded-protocol section); (b) every row vs the per-case best of the rows rankable with it on its cohort (an oracle envelope, not a portfolio); (c) each new identity vs its base control |
| C10 | Base control: per case, `search.base` equals the `BASE-*` record (G4) |
| C11 | E2 attribution: E2 vs `E2-E1only`, vs `E2-wm`, vs `E2-cm` (C2–C6 rules), and the control audit of §7.5 for every control row: matched rows get the target and spend audits, every other class is kept in the denominators as unmatched with its class |
| C12 | The gates of §5.4 per arm, on the branches they apply to, each against the base record of the **same cohort and split**: `Q19` `split_polish` / `marginal_activation` rows and a separate `E2-E1only` arm: G2–G7; `E2-wm` / `E2-cm`: G2–G5 and G7, no G6 (no work pass, §7.2), plus the §7.5 control audit and C14; `BASE-*` and the other `Q19` rows: completeness, C1 and C13 only (they are the base references the post-processors are checked against, C10) |
| C13 | Work-pass reconciliation: Rule P (§5.4.1) on every cell of every arm with a `WP-*` twin, whatever the identity (post-processor rows and base rows alike): P1 counts, the P2–P4 labels with their cells, every P5 `differs` (a `defect`, a `check` problem); missing units stay missing in every work table (Rule M) |
| C14 | Control work: `E2-wm` / `E2-cm` are excluded from physical-work and timing comparisons; their charged quotes and their embedded uncharged activation are reported as separate columns |
| C15 | Cross-campaign determinism (descriptive): the 17 earlier identities' report-split records vs 0.2.2's (status, score, `evaluation`); differences listed |

Tables are regenerated by a pinned script from pinned raw records (SHA256SUMS). Every scheduled arm ×
case has exactly one quality outcome. `check` problems are exactly: a missing or duplicate scheduled
record and every `defect` (a §5.4 gate on an applicable branch per C12, a P5 `differs`, a K4 row, a
failed §7.5 audit). Branches B0–B3, the P2–P4 labels, K0–K3 and `treatment_unavailable` are reported
outcomes, not problems.

### 7.5 Control audit (E2 controls; the R023 auditor's order, `tools/research_023/r023_analysis.py::control_audit`)

Every `E2-wm` / `E2-cm` row is classified, first match, by the fields it carries:

| Class | Condition | Outcome |
| --- | --- | --- |
| K0 `terminated` | no `search.marginal_activation` (branch B0) | unmatched, kept with its status |
| K1 `not_reached` | `search.marginal_activation.not_reached` is set | unmatched ("activation/control not reached: E1 truncated") |
| K2 `non_ok` | status ≠ `ok` (e.g. B1 base pass-through: the saved `R-E2pf-A0-wm` / `-cm` `bnd-1bdd88-78c1b0-dust` rows, `no_route`, no `activation` or `control` block) | unmatched, kept with its status |
| K3 `refused` | `scope` = `unsupported_topology` (E1 refused: no control ran) | unmatched |
| K4 `missing_control_block` | none of the above and no `control` block | `defect` |
| K5 `matched` | a `control` block is present | audits below |

On K5 rows: **target audit**: work-matched `control.quotes` ≤ `control.target_quotes`; call-matched
`control.calls_started` = `control.target_calls` unless `control.stop` is set. **Spend audit**, only when
the treatment record of the same case and cohort carries an `activation` block (otherwise the row is
labelled `treatment_unavailable`, unmatched, not a defect): the embedded `activation` without its
`charged` key equals the treatment's `activation`, and the target equals the treatment's
`activation.quotes` (work-matched) or `activation.invocations` (call-matched). A failed audit is a
`defect`. §12 `campaign.control_examples` holds one example per class and
`campaign.control_audit_examples` one per audit outcome; the contract test executes both.

## 8. Latency protocol `L01-R024` (WHI-1633)

### 8.1 Protocol and profile

`config/research_024/latency/l01-r024.yaml` is generated from `config/latency/l01.yaml` (sha256
`961fb52208c7baac3d0ffe492cef89818498543f1f00f56217d2f99113e27f1b`) with only `key` (`L01-R024`) and
the pinned `profile` replaced (the L01-R022 precedent; a test enforces it). The profile is the effective
19-identity `--strategies all` document of `config/full_gross.yaml` at the schedule commit, written out
(it carries the selected presets). The arms file defines the timing units of §8.2 as arms.

### 8.2 Coverage: timing units

All 19 identities are timed end to end; a post-processor's solve includes its internal base solve.
The stage is split into fixed **timing units**, each with its own launch, validity and attempts
(§8.4). A single window for all 19 is not realistic on this host: one attempt is estimated at 29–35 h
(§10.3), and R022's 6 h 52 min window was already contaminated.

| Unit | Content |
| --- | --- |
| `UP` (paired) | baseline experiment = the distinct bases of the two selected presets (P\* ordinary-row configuration, roster order); candidate experiment = `split_polish`, `marginal_activation`; then `report.latency compare --lane heuristic` pairing each post-processor with its base |
| `U1` | `direct`, `single_path`, `direct_split`, `path_split`, `incremental_graph` |
| `U2` | `uni_sor_port` |
| `U3` | `uni_sor_adaptive`, `uni_sor_optimized`, `metis_inspired`, `metis_history`, `direct_split_certified` |
| `U4` | `incremental_graph_repair` |
| `U5` | `uni_sor_cycle_safe` |
| `U6` | `cfmm_dual`, `single_path_bounded`, `incremental_graph_bounded`, `metis_history_bounded` |
| `UQ` | 5 sequential `main.py quote --details --strategies all --profile config/full_gross.yaml` invocations, sentinel USDC → USDT 1000 on the full bundle (`bundle`, `717c21f3…3143`): the complete-request CLI, one solve per identity per invocation |

Order: `UP`, `U1` … `U6`, `UQ`. `U1`–`U6` partition the 17 earlier identities; the two new rows come from
`UP`. A base timed in `UP` is also timed in its `U*` unit; the paired verdict uses only `UP`'s.

### 8.3 Kept from L01 (verbatim)

The 24-case matrix (9 tuning + 15 held-out twins), the sentinel, both cohorts (`full_source`,
`sor_compatible`); warm process, warmup 1 + 5 repeats per case, orders `fixed` and `reverse`; the A/A
noise floor `|ln geomean(reverse/fixed)|` per experiment; the timing floor 0.02 s; the comparison
thresholds (held-out decision split, 10 % minimum worthwhile improvement, 2 × noise); preparation,
warm solve wall and CPU, cold charged time (fresh worker per (identity, case), prepare + solve) on
`full_source`, the separate tracemalloc memory pass, and the complete-request CLI (`UQ`); one-solve
quote semantics; `heuristic_default_loss_tolerance: null`. The arms file pins
`config/latency/l01-sufficient-budget.yaml` as L01-R022 did; the `UP` comparison pairs different
identities and therefore uses the heuristic lane.

### 8.4 Launch, monitoring and re-attempts

**Host.** One timing lane. During any unit attempt nothing else of this release computes on the host:
no selection or quality runs, no work passes, no test suite, no other agent jobs. `caffeinate -i -m -s`
is held for the whole stage and `pmset -g log` sleep/wake events are captured.

**Launch gate (per attempt).** Five one-minute load samples 30 s apart, none above 3.0; otherwise
re-sample every 300 s; if no gate passes within 21,600 s the unit ends `inconclusive (no_launch)` and
the next unit in the order opens its own launch window. Launch polling is bounded by that window and is
not an attempt.

**Departure from R022 (stated as such).** R022 forbade repeating a completed timing run (pruning
contract §11.1) and recorded "host load is never a retry reason" (`research-022/results.md` §7). R024
departs from that operational rule: a unit attempt invalidated by a registered event may be re-attempted,
bounded as below. The evidence standard is unchanged: an invalid attempt supports no speed claim, a
valid attempt must pass the unchanged gates, and no result can trigger another attempt.

1. **N and the attempt boundary.** An **attempt** is one started execution of one unit: from the start
   of its first experiment (after its launch gate passed) to the end of its last experiment (for `UP`
   including the compare; for `UQ` the 5th invocation), or to its abort. **N = 3 started attempts per
   unit**, aborted ones included (at most 24 for the stage). N is fixed here, before timing.
2. **Exhaustive triggers.** An attempt is invalid iff at least one holds within its window:
   - **T1 load:** any 30-s sample of the one-minute load average above 0.5 × logical CPUs (5.0 on the
     10-CPU host);
   - **T2 sampling coverage:** a gap above 90 s between consecutive samples, counting the window's
     start and end as boundaries;
   - **T3 sleep:** any `Sleep`, `Wake` or `DarkWake` entry of the `pmset` capture inside the window;
   - **T4 sleep prevention or capture missing:** the `caffeinate` assertion not held for the whole
     window, or the `pmset` capture missing or failed;
   - **T5 incomplete execution:** a registered experiment, run manifest or invocation of the unit is
     missing or not complete (a crash, an `interrupted` experiment, an abort).
   **Never a trigger:** a solver timeout, a regression, an insufficient speedup, a noisy or
   `inconclusive` L01 comparison, `insufficient_cases`, a high A/A noise floor, a semantic difference
   between orders (that is a determinism finding, reported), or any other result.
   An attempt may be aborted early only on a detected T1–T5 event; the abort and its reason are
   recorded. An abort for any other reason ends the unit `inconclusive (protocol_breach)`.
3. **First valid attempt.** The earliest attempt, by start time, that satisfies the complete execution
   and monitoring protocol supplies the unit's result. Validity never depends on speedup or statistical
   conclusiveness; a valid attempt whose comparison is noise-`inconclusive` ends the unit.
4. **Whole attempts only.** A unit's result comes from one attempt; clean portions of different
   attempts are never combined. Code (the freeze commit), recipes, profile, cases, order rules and
   thresholds are identical across attempts.
5. **Terminal outcomes per unit:** `valid (attempt id)`; `inconclusive (no_launch)`, with the earlier
   invalid attempts listed; `inconclusive (cap_exhausted)`, with each attempt's triggers;
   `inconclusive (protocol_breach)`.
6. **Reporting.** All attempt ids (`L-<unit>-a<k>`), raw records, timestamps, launch-gate samples,
   load samples and coverage, `caffeinate` and `pmset` evidence, invalidation reasons and the selected
   attempt id per unit are retained.

### 8.5 Record keys (D2-F1)

Quality uniqueness is per scheduled quality arm × case (§7). A timing record is keyed by
`(unit, attempt id, experiment, stage ∈ {timing, cold, memory, quote_cli}, cohort, bundle ∈ {matrix,
sentinel}, order, phase ∈ {warmup, measured}, case, identity, repetition)`. Timing repetitions and
retained invalid attempts are not duplicate quality outcomes.

### 8.6 What a valid unit yields

- `UP`: per pair, the L01 comparison over the held-out decision cases: overhead ratio (geometric mean of
  per-case median post-processor / base solve), wall and CPU, both noise floors, the threshold and the
  comparison's label, reported as is (for a post-processor `slower` is the expected direction; the
  ratio is the quantity), plus prepare, cold charged and memory for both sides.
- `U1`–`U6`: per identity, descriptive medians (warm wall and CPU, prepare, cold charged, memory) per
  cohort; no verdict. Comparisons across units are across windows and are labelled `cross_window`,
  never paired.
- `UQ`: per identity, the 5 single-request solve times and the CLI wall time, descriptive.

### 8.7 Two separate statements

`results.md` states (a) **"measurement attempted correctly"**: every unit reached a terminal outcome
by this protocol, with no `protocol_breach`; and (b) **"usable latency obtained"**: the list of units
with outcome `valid`. Nothing is said about the latency of an identity whose unit is not `valid`.

## 9. Dispositions, claims and evidence boundaries

### 9.1 Dispositions (R021 vocabulary; one per new identity, on the report split)

- `reject`: a `defect` outcome of C12 or C13 (a §5.4 gate on an applicable branch, a P5 `differs`, a
  §7.5 control-audit failure) on any record of the identity or its controls; an `invalid_plan` or `algorithm_error` where its base record of the same
  cohort and split is not the same status; a control above its work target or not call-matched; a
  refusal whose gross differs from the base record.
- `inconclusive`: no `reject`, but a question cannot be evaluated: more than 10 % of the identity's
  scheduled `report_full` cases are truncated (`truncated_by` set), `unsupported_topology`, branch B2,
  branch B0 where the base record is `ok` (a runner `timeout` is a budget outcome, not a defect), or
  carry a P2–P4 label (missing work, Rule M); or a base or control arm is missing; or a scheduled arm is
  incomplete.
- `keep_experimental`: otherwise. No win is required.

The timing verdict is separate (§8.6–§8.7) and never changes the disposition.

### 9.2 Evidence boundaries (carried verbatim into `results.md`)

Report split `previously_exposed` (0.1.0, 0.2.0, 0.2.1, 0.2.2, 0.2.3); presets selected on the
already exposed tuning split under P\* (§5.12); pinned source, settings and analysis hashes; every
scheduled status kept; single block; no speed claim beyond §8.6–§8.7; no new-block generalisation;
inclusion in `--strategies all` is not adoption.

### 9.3 Wording for the guide (WHI-1634)

The presets are described with the §5.12 claim, conditional on P\*. Results under other profiles
(e.g. `daily_gross.yaml`) are presented as results under those settings (§4.5). Timing appears only as
`results.md` states it. The 19-pool fixture walkthrough and the full-corpus results stay separate.

## 10. Scheduling register

Scheduling information, **not a runtime ceiling** and not an acceptance threshold. Estimates come only
from committed ledgers and from raw records pinned by committed hashes; no new run was made.

### 10.1 Selection (WHI-1631)

| Item | Value |
| --- | --- |
| Candidates | 90 E1 + 108 E2 = 198; reused (§5.9) 8 candidates + 5 reference arms; fresh 190 candidates |
| Invocations | 380 fresh (190 × (ordinary + work pass)) on 96 tuning cases each; + 2 preset-equality runs; + up to 8 sensitivity runs (§5.10) |
| Per-configuration estimate (lane-minutes, ordinary + work pass; R023 stage T, 6 lanes, 1-min load 7–10, `research-023/campaign/tuning-ledger.jsonl`) | E1 on A0 40.2 (golden 36.9), PS 18.2, M4 25.4, S4 27.0, REP 66.4; E2 on A0 pf 39.8, full 63.6; E2 on PS pf ≈ 24.3, full ≈ 48.1 (PS base + the A0 activation increment) |
| Total estimate | E1 ≈ 49 lane-h, E2 ≈ 77 lane-h (rounds, tolerance, K, top-k, δ assumed cost-neutral: a known under-estimate for K 4 / top-9); ≈ 127 lane-h ≈ **21 h at 6 lanes** |
| Concurrency | 6 lanes (R023 precedent); never during a timing attempt |

### 10.2 Quality campaign (WHI-1633, stages T and R)

| Item | Value |
| --- | --- |
| Invocations | `Q19-full`, `Q19-sor`, `E2-wm`, `E2-cm`, `E2-E1only` / `BASE-*` when separate, and their work passes, per split (group runs allowed, §7.2) |
| Solve-hour basis (sequential solve time, report split) | 17 earlier identities: 11.94 h `full_source`, 9.98 h `sor_compatible` (0.2.2 `R-A1-*`, `R-B14-*` records, pinned in `research-022/campaign/report-analysis.json`); the new rows ≈ +2.1 h per cohort (0.2.3 `R-E1b-A0` 61.3 min, `R-E2pf-A0` 62.5 min); tuning ≈ 96/302 of report |
| Total estimate | ≈ 73 lane-h including work passes and controls; R023's stage R (40 invocations) took 7 h 7 min at 6 lanes → **≈ 12–15 h at 6 lanes** |

### 10.3 Timing (WHI-1633, stage L)

| Item | Value |
| --- | --- |
| Calibration | R022 stage L: `L-ref` (single_path, incremental_graph, metis_history) 3.78 h, `L-bnd` 2.99 h (`research-022/campaign/timing-ledger.jsonl`) = 1.94× and 1.72× those identities' report `full_source` solve hours |
| Per-unit estimate (one attempt, ≈ 1.94 × report solve hours) | `U1` ≈ 3.5 h, `U2` ≈ 4.5 h, `U3` ≈ 3.9 h, `U4` ≈ 3.4 h, `U5` ≈ 4.5 h, `U6` ≈ 3.4 h, `UP` ≈ 6–11 h (depends on the selected bases), `UQ` < 0.5 h; **≈ 29–35 h for one attempt of every unit** |
| Launch waits | up to 6 h per attempt; R022 needed 9 failed gates: ≈ 65 min from the first sample to the passing gate, of which 45 min were the 300-s re-sample sleeps |
| Worst case | 3 attempts and 3 launch windows per unit (bounded, §8.4) |
| Concurrency | 1 lane, nothing else on the host (§8.4) |

### 10.4 Resume rules and storage

- **Resume:** the executor's ledger decides. A completed invocation is never re-run. An interrupted
  quality stage resumes by launching only the invocations without a complete manifest, from the same
  commit, recorded in the ledger. An interrupted timing attempt counts as an aborted attempt (T5).
- **Raw records** (outside the repository): `router-algorithms-optimizer-artifacts/research-024/selection/<commit>/…`
  and `…/research-024/campaign/<commit>/{T,R,L}/…`. **Committed:** schedules and generated profiles
  under `config/research_024/`; ledgers, analyses, tables and `*-SHA256SUMS` under
  `docs/references/research-024/{selection,campaign}/`.
- Strict mypy covers `tools/research_024/*.py` and `tools/research_023/*.py` through the configured
  list (WHI-1631; D1-F7). The full test suite runs outside every timing window.

## 11. Generalisation, follow-ups and change control

1. **Generalisation.** A new held-out block is not required for the bounded 0.2.4 claims (§5.12, §9.2).
   It is required for any out-of-block claim, and it is recorded as an explicit follow-up: collect and
   freeze a new block, then re-run the frozen presets and the 19-strategy comparison on it.
2. **Follow-ups outside 0.2.4.** Selection under other profiles is a different, larger question
   (§4.5). Default adoption is an owner decision outside this vocabulary.
3. **Change control.** An amendment is made only before the observation it governs (selection rules
   before WHI-1631's schedule push; campaign and timing rules before WHI-1633's schedule push), is
   recorded here as `A<n>` with date, reason and the changed §12 values, and passes the contract test.
   After the push nothing in its scope changes. A case this contract does not decide, found later, stops
   the issue and is reported (the normative-words paragraph above §1); it is never decided after observing.

## 12. Registry

```yaml
schema: r024.contract/1
key: R024-C/1
issue: WHI-1630
release: {version: 0.2.4, id: 0beaa4c3-2721-4618-b687-ac70d4d54e6a}
base_commit: dbc9432620fad36cabe7cdf0f4024b819326e2eb
pstar:
  source: {path: config/full_gross.yaml,
           sha256: 51a8a65184ae765867fd9d7763776852b2b4576e3cf7116a8dc15ccd87028177}
  derivation: all
  m4_settings: {path: config/metis_challenge/m4.yaml, keys: [label_hops, label_pruning],
                sha256: 661311df6ff7a36a43954fff0e42f6b30b824d35d96c06f0e215960f256d4471}
  shared:
    schema_version: 2
    objective: {mode: gross_only, fixed_cost: 0}
    search: {max_hops: 3, max_splits: 4, percent_step: 5}
    graph: {chunks: 50, label_hops: 4, label_pruning: true}
    budget: {time_limit_seconds: 900.0, max_quotes: 300000, max_candidates: null}
    measurement: {warmup: 0, repeats: 1, seed: 1447, order: fixed, memory_pass: false}
    worker: {start_method: spawn, scope: algorithm, prepare_time_limit_seconds: 120.0}
  shared_sha256: e198f7e774f89f30eac0e7ccfd40d2e8a5f97b06d2d43fe00d870ff82fcc7cb1
  all17_resolved_profile_sha256: aa188fa82d333cfdb0b33a3ba8907b745fae341e2449406ba18ea8941c234f82
  bases:
    incremental_graph:
      cec_sha256: f9eb025ab2389641d9591603d6edf18c884b438a09fec9606551b4581831ace6
    path_split:
      cec_sha256: 5f7be55df139998af4baa8bc0acb9de4fd921e850425f492b7653342f73b351e
    metis_inspired:
      cec_sha256: aa83997fdbabf4f1e119524b210257757a208d3b5a4a9ed8c227ad5ca2e6dd05
    metis_history:
      cec_sha256: a46b1d84eca3f0b6336c3ad2322d9da259acc5824e6ce26d88c54c697c74d35a
      preset: {path: config/metis_history/preset_v1.yaml,
               sha256: f4510b5181b1152786b0a637cd462734e31988b7890487c6ee6d396d37965799}
      settings_sha256: 183bb1ff3bb8b0baee7b72720a2c27fd306d5656f71e44cb96767094bb1fceb6
    incremental_graph_repair:
      cec_sha256: dc0926033ccf359eb538d0f30c7014403e29da987e085e9c22c77a93421383b4
      preset: {path: config/incremental_graph_repair/preset_v1.yaml,
               sha256: 13774bcd2225ee17d3635bae509ac2a5a888d5c2801006a200e01788ca35786a}
      settings_sha256: 89af50289837bb6929f1f184f342db974da0cbe134dda5ed4497b12df7264abf
selection:
  split: {input: tuning_full, cohort: full_source, cases: 96,
          bundle_hash: ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b}
  forbidden_bundle_hashes:
    - 85202b20c13685af6fd999fe3d228ca6de37abb5914b5459621e9a67a71971c0
    - 8213b7b07222c98a5ec99f0bae53d0876d2d14dc7dfd3cb7aa6cec1e20afe640
  identities:
    split_polish:
      preset: {path: config/split_polish/preset_v1.yaml, key: R024-P01-split_polish, version: 1}
      id_format: "sp|{base}|{solver}|r{rounds}|t{tolerance}"
      grid:
        base: [incremental_graph, path_split, metis_inspired, metis_history,
               incremental_graph_repair]
        solver: [brent, golden]
        rounds: [1, 2, 4]
        tolerance: {"1e-3": 0.001, "1e-4": 0.0001, "1e-5": 0.00001}
      fixed: {grid: 1000000000, maxiter: 60}
      base_options_from_preset: [metis_history, incremental_graph_repair]
      candidates: 90
    marginal_activation:
      preset: {path: config/marginal_activation/preset_v1.yaml,
               key: R024-P02-marginal_activation, version: 1}
      id_format: "ma|{base}|{mode}|k{activations}|top{top_k}|d{delta_share}"
      grid:
        base: [incremental_graph, path_split]
        mode: [pf, full]
        activations: [1, 2, 4]
        top_k: [1, 3, 9]
        delta_share: {"1e-3": 0.001, "1e-4": 0.0001, "1e-5": 0.00001}
      fixed: {solver: brent, rounds: 2, tolerance: 0.0001, grid: 1000000000, maxiter: 60,
              seed_share: 0.0001, arm: treatment}
      candidates: 108
  quality:
    reference_set: [incremental_graph, path_split, metis_inspired, metis_history,
                    incremental_graph_repair]
    failure_gross: 0
    weighting: equal_per_case
  work:
    reference: incremental_graph
    units: [quotes, cl_swap_steps, lb_bins_swapped]
    limits: {quotes: 2, cl_swap_steps: 2, lb_bins_swapped: 2}
    zero_over_zero: 1
    positive_over_zero: infinite
  rule:
    epsilon_bps: "1/100"
    tie_break: [quotes, cl_swap_steps, lb_bins_swapped, candidate_id]
  gate_applicability:
    B0_terminated: [G1, G2, G6]
    B1_base_passthrough: [G1, G2, G4, G5, G6]
    B2_not_reached_before_polish: [G1, G2, G3, G4, G5, G6]
    B3_polished: [G1, G2, G3, G4, G5, G6, G7]
  # One record skeleton per branch (only the fields the classification and G5 read). The first two
  # are saved R023 report-split records; any gate implementation must classify them as `branch`.
  record_branches:
    - source: "saved: R023 R-E1b-A0, bnd-1bdd88-78c1b0-dust"
      identity: split_polish
      record: {status: no_route, quotes: {counted: 4},
               search: {base: {algorithm: incremental_graph, status: no_route, quotes: 4},
                        split_polish: {scope: null, truncated_by: null}}}
      branch: B1_base_passthrough
    - source: "saved: R023 R-E2pf-C100-wm, bnd-cda86a-201eba-round_at"
      identity: marginal_activation
      record: {status: ok, quotes: {counted: 300000},
               search: {base: {algorithm: incremental_graph, status: ok, quotes: 300000},
                        marginal_activation: {scope: fixed_funding_topology, base_gross: "1",
                                              gross: "1", quotes: 0, truncated_by: max_quotes,
                                              not_reached: "activation/control not reached: E1 truncated"}}}
      branch: B3_polished
    - source: "synthetic: runner hard limit (benchmark/runner.py failure record shape)"
      identity: split_polish
      record: {status: timeout, quotes: {attempted: null, counted: null}, search: {},
               evaluation: null, score: null}
      branch: B0_terminated
    - source: "synthetic: the budget ends while the base plan is replayed"
      identity: split_polish
      record: {status: ok, quotes: {counted: 300000},
               search: {base: {algorithm: metis_history, status: ok, quotes: 299990},
                        split_polish: {scope: not_reached, truncated_by: max_quotes}}}
      branch: B2_not_reached_before_polish
  # Rule P (§5.4.1): one ordinary / work-pass pair per case, with the expected case and the units
  # that stay available (Rule M: the others are missing, never 0).
  pair_examples:
    - case: P1_reconciled
      ordinary: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 10}}
      work_pass: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 10},
                  search: {r022_work: {quotes_executed: 10, cl_swap_steps: 5, lb_bins_swapped: 1}}}
      available: [quotes, cl_swap_steps, lb_bins_swapped]
    - case: P2_work_pass_terminated
      ordinary: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 10}}
      work_pass: {status: timeout, score: null, evaluation: null,
                  quotes: {attempted: null, counted: null}, search: {}}
      available: [quotes]
    - case: P3_both_terminated
      ordinary: {status: timeout, score: null, evaluation: null,
                 quotes: {attempted: null, counted: null}, search: {}}
      work_pass: {status: timeout, score: null, evaluation: null,
                  quotes: {attempted: null, counted: null}, search: {}}
      available: []
    - case: P4_ordinary_terminated
      ordinary: {status: timeout, score: null, evaluation: null,
                 quotes: {attempted: null, counted: null}, search: {}}
      work_pass: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 10},
                  search: {r022_work: {quotes_executed: 10, cl_swap_steps: 5, lb_bins_swapped: 1}}}
      available: [cl_swap_steps, lb_bins_swapped]
    - case: P5_differs
      ordinary: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 10}}
      work_pass: {status: ok, score: "100", evaluation: {gross_output: "100"}, quotes: {counted: 11},
                  search: {r022_work: {quotes_executed: 11, cl_swap_steps: 5, lb_bins_swapped: 1}}}
      available: null
  reuse:
    stage_t_revision: 1bfd3f97fb205d21bb93acb754dfafcc1d851265
    sums: {path: docs/references/research-023/campaign/tuning-SHA256SUMS,
           sha256: 13c4b378b1e9e6145ebeb3e9ea7194937745c9524b280e5a96859e925bff9556}
    code_paths: [routing, pools, snapshot, benchmark, main.py,
                 tools/research_022/pruning_work.py, uv.lock]
    configurations:
      - {candidate: "ref|incremental_graph", r023_profile: a0, runs: [T-A0, T-WP-A0]}
      - {candidate: "ref|path_split", r023_profile: ps, runs: [T-PS, T-WP-PS]}
      - {candidate: "ref|metis_inspired", r023_profile: m4, runs: [T-M4, T-WP-M4]}
      - {candidate: "ref|metis_history", r023_profile: s4, runs: [T-S4, T-WP-S4]}
      - {candidate: "ref|incremental_graph_repair", r023_profile: rep, runs: [T-REP, T-WP-REP]}
      - {candidate: "sp|incremental_graph|brent|r2|t1e-4", r023_profile: e1b-a0,
         runs: [T-E1b-A0, T-WP-E1b-A0]}
      - {candidate: "sp|path_split|brent|r2|t1e-4", r023_profile: e1b-ps,
         runs: [T-E1b-PS, T-WP-E1b-PS]}
      - {candidate: "sp|metis_inspired|brent|r2|t1e-4", r023_profile: e1b-m4,
         runs: [T-E1b-M4, T-WP-E1b-M4]}
      - {candidate: "sp|metis_history|brent|r2|t1e-4", r023_profile: e1b-s4,
         runs: [T-E1b-S4, T-WP-E1b-S4]}
      - {candidate: "sp|incremental_graph_repair|brent|r2|t1e-4", r023_profile: e1b-rep,
         runs: [T-E1b-REP, T-WP-E1b-REP]}
      - {candidate: "sp|incremental_graph|golden|r2|t1e-4", r023_profile: e1g-a0,
         runs: [T-E1g-A0, T-WP-E1g-A0]}
      - {candidate: "ma|incremental_graph|pf|k2|top3|d1e-4", r023_profile: e2pf-a0,
         runs: [T-E2pf-A0, T-WP-E2pf-A0]}
      - {candidate: "ma|incremental_graph|full|k2|top3|d1e-4", r023_profile: e2full-a0,
         runs: [T-E2full-A0, T-WP-E2full-A0]}
  sensitivity:
    graph_chunks: [100, 200]
    not_applicable_bases: [path_split]
    work_pass: false
    gates: {winner_arm: [G1, G2, G3, G4, G5, G7], base_arm: [G1]}
    base_reference: base_arm_same_chunks
    q_reference: pstar_reference_arms
    # Pinned R023 tuning records, case emp-09bc4e-201eba-large-3: the c100 embedded base equals the
    # c100 base arm on the G4 fields and differs from the P* A0 arm, as it must (not a defect).
    example:
      case: emp-09bc4e-201eba-large-3
      embedded_base_quotes: {run: T-E1b-C100, value: 37827}
      same_chunk_base_quotes: {run: T-C100, value: 37827}
      pstar_base_quotes: {run: T-A0, value: 38098}
  # Worked examples of the §5.7 rule. Gross per case (null = a non-ok status), the common reference
  # per case and work totals are synthetic; `gates` is the G1-G7 outcome. Any rule implementation
  # (WHI-1631, R025-C/1) must reproduce `expected`.
  worked_examples:
    - id: WE1-tie-band            # within epsilon the cheaper candidate wins
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we1|a", gates: pass, gross: [1000020, 2000040, 0],
           work: {quotes: 1500, cl_swap_steps: 600, lb_bins_swapped: 120}}
        - {id: "we1|b", gates: pass, gross: [1000019, 2000040, 0],
           work: {quotes: 1200, cl_swap_steps: 600, lb_bins_swapped: 120}}
      expected: {outcome: selected, winner: "we1|b", eligible: ["we1|a", "we1|b"]}
    - id: WE2-work-limit          # a ratio of exactly 2 passes, above 2 fails
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we2|c", gates: pass, gross: [1000100, 2000200, 0],
           work: {quotes: 2001, cl_swap_steps: 500, lb_bins_swapped: 100}}
        - {id: "we2|d", gates: pass, gross: [1000050, 2000100, 0],
           work: {quotes: 2000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      expected: {outcome: selected, winner: "we2|d", eligible: ["we2|d"]}
    - id: WE3-tie-break-order     # equal quality: quotes first, then CL, LB, id
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 1000, lb_bins_swapped: 100}}
      candidates:
        - {id: "we3|b", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1100, cl_swap_steps: 900, lb_bins_swapped: 100}}
        - {id: "we3|c", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1200, cl_swap_steps: 600, lb_bins_swapped: 100}}
        - {id: "we3|a", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1100, cl_swap_steps: 900, lb_bins_swapped: 100}}
      expected: {outcome: selected, winner: "we3|a", eligible: ["we3|b", "we3|c", "we3|a"]}
    - id: WE4-failures-and-gates  # a failed case scores 0; gate and limit failures are ineligible
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we4|h", gates: pass, gross: [1000000, null, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
        - {id: "we4|i", gates: fail, gross: [1000100, 2000200, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
        - {id: "we4|j", gates: pass, gross: [1000100, 2000200, 0],
           work: {quotes: 3000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      expected: {outcome: selected, winner: "we4|h", eligible: ["we4|h"]}
    - id: WE5-no-selection        # nothing eligible: no winner, no fallback
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we5|k", gates: fail, gross: [1000100, 2000200, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
        - {id: "we5|l", gates: pass, gross: [1000100, 2000200, 0],
           work: {quotes: 1000, cl_swap_steps: 1001, lb_bins_swapped: 100}}
      expected: {outcome: no_selection, winner: null, eligible: []}
    - id: WE6-zero-denominator    # 0/0 = 1 passes; positive/0 = infinite fails
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 0}}
      candidates:
        - {id: "we6|m", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 0}}
        - {id: "we6|n", gates: pass, gross: [1000500, 2001000, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 1}}
      expected: {outcome: selected, winner: "we6|m", eligible: ["we6|m"]}
    - id: WE7-missing-candidate-work   # a missing total (Rule M) makes the candidate ineligible
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we7|o", gates: pass, gross: [1000500, 2001000, 0],
           work: {quotes: 1000, cl_swap_steps: null, lb_bins_swapped: 100}}
        - {id: "we7|p", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      expected: {outcome: selected, winner: "we7|p", eligible: ["we7|p"]}
    - id: WE8-missing-reference-work   # a missing A0 total: no selection at all
      reference: {gross: [1000000, 2000000, 0],
                  work: {quotes: null, cl_swap_steps: 500, lb_bins_swapped: 100}}
      candidates:
        - {id: "we8|q", gates: pass, gross: [1000010, 2000020, 0],
           work: {quotes: 1000, cl_swap_steps: 500, lb_bins_swapped: 100}}
      expected: {outcome: no_selection, winner: null, eligible: []}
roster:
  additions: [split_polish, marginal_activation]
  all19: [direct, single_path, direct_split, path_split, incremental_graph, uni_sor_port,
          uni_sor_adaptive, uni_sor_optimized, metis_inspired, metis_history,
          direct_split_certified, incremental_graph_repair, uni_sor_cycle_safe, cfmm_dual,
          single_path_bounded, incremental_graph_bounded, metis_history_bounded,
          split_polish, marginal_activation]
  research_021_registered_roster: &r021roster
    [direct, single_path, direct_split, path_split, incremental_graph, uni_sor_port,
     uni_sor_adaptive, uni_sor_optimized, metis_inspired, metis_history, direct_split_certified,
     incremental_graph_repair, uni_sor_cycle_safe, cfmm_dual]
  research_021_known_findings:
    - {invocation: T-roster-full, registered: *r021roster}
    - {invocation: T-roster-sor, registered: *r021roster}
    - {invocation: T-quote-smoke, registered: *r021roster}
    - {invocation: R-roster-full, registered: *r021roster}
    - {invocation: R-roster-sor, registered: *r021roster}
    - {invocation: M-roster, registered: *r021roster}
    - {invocation: I-all-details, registered: *r021roster}
    - {invocation: I-all-compact, registered: *r021roster}
    - {invocation: I-memory-gross, registered: *r021roster}
  zero_problem_checks: [research_022, research_023]
campaign:
  inputs:
    full: {bundle_hash: 717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143, cases: 398}
    tuning_full: {bundle_hash: ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b, cases: 96}
    tuning_sor: {bundle_hash: b900b866d4a0ef6a3b33546f1d262ed3fc836847ef30da9e975c5bae75107ade, cases: 96}
    report_full: {bundle_hash: 85202b20c13685af6fd999fe3d228ca6de37abb5914b5459621e9a67a71971c0, cases: 302}
    report_sor: {bundle_hash: 8213b7b07222c98a5ec99f0bae53d0876d2d14dc7dfd3cb7aa6cec1e20afe640, cases: 302}
  arms: [Q19-full, Q19-sor, BASE-E1, BASE-E2, E2-E1only, E2-wm, E2-cm, WP]
  no_work_pass: [E2-wm, E2-cm]
  checklist: [C1, C2, C3, C4, C5, C6, C7, C8, C9, C10, C11, C12, C13, C14, C15]
  disposition_truncation_limit: "1/10"
  # §7.5 control classes; the K2 example is a saved R023 report-split control record.
  control_examples:
    - class: K0_terminated
      record: {status: timeout, quotes: {attempted: null, counted: null}, search: {}}
    - class: K1_not_reached
      source: "saved: R023 R-E2pf-C100-wm, bnd-cda86a-201eba-round_at"
      record: {status: ok, search: {base: {status: ok},
               marginal_activation: {arm: work_matched, scope: fixed_funding_topology,
                                     not_reached: "activation/control not reached: E1 truncated"}}}
    - class: K2_non_ok
      source: "saved: R023 R-E2pf-A0-wm, bnd-1bdd88-78c1b0-dust"
      record: {status: no_route, quotes: {counted: 4},
               search: {base: {status: no_route, quotes: 4},
                        marginal_activation: {arm: work_matched, scope: null, not_reached: null}}}
    - class: K3_refused
      record: {status: ok, search: {base: {status: ok},
               marginal_activation: {arm: call_matched, scope: unsupported_topology,
                                     not_reached: null}}}
    - class: K4_missing_control_block
      record: {status: ok, search: {base: {status: ok},
               marginal_activation: {arm: call_matched, scope: fixed_funding_topology,
                                     not_reached: null}}}
    - class: K5_matched
      record: {status: ok, search: {base: {status: ok},
               marginal_activation: {arm: work_matched, scope: fixed_funding_topology,
                                     not_reached: null,
                                     control: {kind: work_matched, quotes: 90, target_quotes: 100}}}}
  # §7.5 audits of K5 rows: control block, embedded activation, the treatment's activation
  # (null = the treatment record carries none) and the expected outcome.
  control_audit_examples:
    - id: pass
      control: {kind: work_matched, quotes: 90, target_quotes: 100, stop: null}
      embedded: {quotes: 100, invocations: 2, charged: false}
      treatment: {quotes: 100, invocations: 2}
      outcome: ok
    - id: work-target-overrun
      control: {kind: work_matched, quotes: 101, target_quotes: 100, stop: work_target}
      embedded: {quotes: 100, invocations: 2, charged: false}
      treatment: {quotes: 100, invocations: 2}
      outcome: defect
    - id: call-mismatch-without-stop
      control: {kind: call_matched, calls_started: 2, target_calls: 3, stop: null}
      embedded: {quotes: 100, invocations: 3, charged: false}
      treatment: {quotes: 100, invocations: 3}
      outcome: defect
    - id: call-mismatch-with-stop
      control: {kind: call_matched, calls_started: 2, target_calls: 3, stop: max_quotes}
      embedded: {quotes: 100, invocations: 3, charged: false}
      treatment: {quotes: 100, invocations: 3}
      outcome: ok
    - id: embedded-differs
      control: {kind: work_matched, quotes: 90, target_quotes: 100, stop: null}
      embedded: {quotes: 99, invocations: 2, charged: false}
      treatment: {quotes: 100, invocations: 2}
      outcome: defect
    - id: treatment-unavailable
      control: {kind: work_matched, quotes: 90, target_quotes: 100, stop: null}
      embedded: {quotes: 100, invocations: 2, charged: false}
      treatment: null
      outcome: treatment_unavailable
timing:
  protocol: {key: L01-R024, source: config/latency/l01.yaml, replaced: [key, profile],
             source_sha256: 961fb52208c7baac3d0ffe492cef89818498543f1f00f56217d2f99113e27f1b}
  units:
    - {unit: UP, kind: paired, candidate: [split_polish, marginal_activation],
       baseline: bases_of_selected_presets}
    - {unit: U1, kind: descriptive,
       algorithms: [direct, single_path, direct_split, path_split, incremental_graph]}
    - {unit: U2, kind: descriptive, algorithms: [uni_sor_port]}
    - {unit: U3, kind: descriptive,
       algorithms: [uni_sor_adaptive, uni_sor_optimized, metis_inspired, metis_history,
                    direct_split_certified]}
    - {unit: U4, kind: descriptive, algorithms: [incremental_graph_repair]}
    - {unit: U5, kind: descriptive, algorithms: [uni_sor_cycle_safe]}
    - {unit: U6, kind: descriptive,
       algorithms: [cfmm_dual, single_path_bounded, incremental_graph_bounded,
                    metis_history_bounded]}
    - {unit: UQ, kind: quote_cli, invocations: 5, strategies: all}
  max_started_attempts_per_unit: 3
  launch: {samples: 5, interval_seconds: 30, headroom_load1: 3.0,
           resample_every_seconds: 300, max_wait_seconds: 21600}
  validity: {max_load1_per_cpu: 0.5, sample_interval_seconds: 30, max_sample_gap_seconds: 90,
             sleep_entries_allowed: 0, caffeinate: "caffeinate -i -m -s"}
  triggers: [T1_load, T2_sampling_coverage, T3_sleep, T4_sleep_prevention_or_capture,
             T5_incomplete_execution]
  never_triggers: [solver_timeout, regression, insufficient_speedup, noisy_comparison,
                   l01_inconclusive, insufficient_cases, aa_noise, semantic_difference]
  outcomes: [valid, inconclusive_no_launch, inconclusive_cap_exhausted,
             inconclusive_protocol_breach]
```

## 13. Independent review and dispositions

**Reviewer and provenance.** Role `REVIEWER`, dispatched by the implementer with
`scripts/agent-dispatch.sh REVIEWER <prompt> --effort high`. `config/agent-roles.conf` maps it to runtime
`pi`, model **`mantle/gpt-6-astra`**, native thinking **`medium`** (`REVIEWER_EFFORT_HIGH="medium"`,
owner mapping of WHI-1625); the dispatcher's argv is `pi -p --model mantle/gpt-6-astra --thinking
medium`. **Fresh context:** one new dispatch per round, never a review inside the implementer's
context; its working directory was a read-only detached worktree at the round's head, removed
afterwards, and the reviewer reported it clean before and after. **Disclosure:** the same model
co-designed `R023-C/1` (its four review rounds, [`../research-023/reviews/`](../research-023/reviews/))
and reviewed the 0.2.4 issue design (design-review rounds 1–4); it is not independent of the designs
this contract carries forward. Neither the design review nor this contract review replaces or consumes
the final release review (budget 3 reviews / 2 fix batches). Prompts, raw outputs and their SHA256SUMS:
`router-algorithms-optimizer-artifacts/research-024/whi-1630/contract-review/`; verbatim reports in
[`reviews/`](reviews/).

| Round | Reviewed head | Verdict | Findings | Report |
| --- | --- | --- | --- | --- |
| 1 | `0d0bae25abf9025fc455370052f035a717ef4097` | DISAGREE | 5 blocking (C1-F1…F5) | [`reviews/round-1.md`](reviews/round-1.md) |
| 2 | `1d043a19e8d807743f5c45a41c36934375aa528b` | DISAGREE | C1-F1, C1-F2 partly resolved; C1-F3…F5 resolved; 3 blocking (C2-F1…F3) | [`reviews/round-2.md`](reviews/round-2.md) |
| 3 | `92adee0154d4c3feac6696d897d861bf91d2183e` | DISAGREE | C2-F1…F3, C1-F1, C1-F2 resolved; 1 blocking (C3-F1), 1 suggestion (C3-F2) | [`reviews/round-3.md`](reviews/round-3.md) |

| Finding | Severity | Disposition (where) |
| --- | --- | --- |
| C1-F1 | blocking | **Accepted.** Record branches B0–B3 recognised by the fields a record carries, a gate-by-branch applicability table (B1 base pass-through has no `base_gross`; B2 has no own quotes; B0 has no `search.base`), the work-pass-only termination case, and saved-record examples in §12 `selection.record_branches` that the contract test classifies (§5.4, §9.1, §12) |
| C1-F2 | blocking | **Accepted.** C12 is now an arm-to-gate matrix: G6 only for arms with a `WP-*` twin, E2 controls get G2–G5, G7, C11 and C14, base rows are references only; each check uses the same cohort and split (§7.4 C12) |
| C1-F3 | blocking | **Accepted.** The per-family table compares with every rankable other row (the C9(a) set) and with the C9(b) envelope, labelled as such; no "best row" choice remains (§7.4 C5) |
| C1-F4 | blocking | **Accepted.** Corrected from the committed ledger: ≈ 65 min from the first sample to the passing gate, 45 min of which were re-sample sleeps (§10.3) |
| C1-F5 | blocking | **Accepted.** This section records role, model, native effort, fresh context, each round's head and the R023 / design-review participation, and states the separate release-review budget; the contract test now requires a non-empty review record with these fields (§13) |
| C2-F1 | blocking | **Accepted.** Pair reconciliation is one field-based Rule P (P1 reconciled, P2 `work_pass_terminated`, P3 `both_terminated`, P4 `ordinary_terminated`, P5 `differs` = `defect`) for every arm with a work-pass twin, base rows included; C13 applies it; missing units stay missing (Rule M); P2–P4 count toward `inconclusive`, P5 toward `reject`; §12 `selection.pair_examples` has one example per case, checked by the contract test (§5.4.1, §7.4 C13, §9.1) |
| C2-F2 | blocking | **Accepted.** Rule M defines every work value from the fields a record carries (the runner's null `quotes.counted` included); a candidate with any missing total is `ineligible: work_unavailable`; any missing A0 total ends both identities `no_selection` (`reference_work_unavailable`); a P5 in a reference arm is a `defect` of both identities; the B0 example now has the runner's null-count shape; worked examples WE7 and WE8 cover the missing-work outcomes (§5.4.1, §5.6, §5.8, §12) |
| C2-F3 | blocking | **Accepted.** §7.5 registers the control audit in the R023 auditor's order: K0 terminated, K1 `not_reached`, K2 non-`ok` (the saved `no_route` control rows), K3 refused, K4 missing control block (`defect`), K5 matched (target and spend audits; `treatment_unavailable` when the treatment row has no `activation`); §12 `campaign.control_examples` has one example per class, checked by the contract test (§7.4 C11, C12, §7.5) |
| C3-F1 | blocking | **Accepted, fixed after round 3; not re-reviewed** (three rounds is the cap). §5.10 now gives sensitivity arms their own validation: G1 on the ordinary records only, no G6, the winner arm's G2–G5/G7 against the base arm at the **same** `graph.chunks`, the base arm G1 only, `Q` still against the P\* reference arms; §12 `selection.sensitivity` pins it with the pinned R023 example the reviewer reproduced (c100 embedded base 37,827 quotes = `T-C100`, ≠ `T-A0` 38,098), and the contract test checks it (§5.4, §5.10, §12) |
| C3-F2 | suggestion | **Accepted.** §12 `campaign.control_audit_examples` covers a pass, a work-target overrun, a call mismatch with and without `stop`, an embedded-activation mismatch and an absent treatment activation; the contract test executes the §7.5 audits on them (§7.5, §12) |

## 14. Residual risks

Single block; the tuning split is already exposed and the ladders are centred on tuning-chosen
nominees (§3); the E2 grid does not vary its E1 stage; cost-neutral runtime assumptions under-estimate
K 4 / top-9 (§10.1); a long timing stage on a shared host may end `inconclusive` in several units
(§8.4); cross-unit latency is cross-window (§8.6); the work limit and ε are judgements made before
observation, with the exposure of §3; the CEC excludes the options `source`, so provenance is checked
separately by I1–I4. The post-round-3 edit for C3-F1 (§5.10) has passed the contract test but no further
independent contract review (three rounds is the cap); the release review covers it.
