# Base, optimized and experimental strategy groups (WHI-1528, WHI-1540)

The ordinary CLI compares strategies in separate groups and, by default, runs them all:

- **Base strategies**: the six mandatory algorithms of DESIGN §2.6 (`direct`,
  `single_path`, `direct_split`, `path_split`, `incremental_graph`, `uni_sor_port`). They
  are unchanged and remain the references.
- **Optimized strategies**: two named, **experimental** recipes of the opt-in `uni_sor_fast`
  heuristic that the 0.1.2 latency work registered and measured (L08,
  [`latency-optimization-results.md`](latency-optimization-results.md)):

  | name | L08 arm | search | exact quote controls |
  | --- | --- | --- | --- |
  | `uni_sor_adaptive` | H3 | near-full candidates (vacuous shortlist: coarse probes 25/50/75/100, 10⁶ routes per probe, 0 extra direct routes) + adaptive percentage sampling (`coarse_step` 25, `refine_radius` 1, no soft cap) | none |
  | `uni_sor_optimized` | H4 | L06 shortlist (probes 5/100, 8 routes per probe, 0 extra direct routes) + the same adaptive sampling | L02, L03, L04 (no L05) |

- **Experimental and other strategies** (the `custom` group): any other algorithm a profile
  lists (such as a configured `uni_sor_fast`) and, since WHI-1540, the **Metis-inspired**
  `metis_inspired` of WHI-1449: an experimental Python variant, **NOT Jupiter Metis**, with no
  production-equivalence claim. It is neither a seventh base reference nor an SOR
  optimization.

How the two recipes search, with hand-worked and test-verified examples, is explained in
[`routing-algorithms.md`](routing-algorithms.md) §8 (`uni_sor_adaptive`) and §9
(`uni_sor_optimized`). `metis_inspired`'s label search is explained the same way in §10 of that
guide. Its contract is [`jupiter-metis-challenge.md`](jupiter-metis-challenge.md) §9.2, and the
frozen WHI-1449 results are in [`metis-challenge-results.md`](metis-challenge-results.md).

The owner asked for this layout: the optimized strategies are **not** default strategies
and are kept apart from the base ones, but the CLI runs them all. The owner then asked
(WHI-1540) that the ordinary comparison also include `metis_inspired`. Nothing here adopts a
strategy, changes a router default or accepts a loss tolerance. The L08 results and
dispositions for H3 and H4, and the WHI-1449 verdict for Metis-inspired, apply only to the
scope they recorded. A run of these strategies is a new comparison, not new adoption
evidence.

## Selecting strategies

`main.py run` and `main.py quote` take `--strategies all|base|optimized|profile`:

| mode | runs |
| --- | --- |
| `all` (default) | the profile's base algorithms (and any other algorithm it lists, such as a configured `uni_sor_fast`) in profile order, then `uni_sor_adaptive`, `uni_sor_optimized`, then `metis_inspired`, then each implemented 0.2.1 identity in contract order (so far `metis_history`, `direct_split_certified`, `incremental_graph_repair`) |
| `base` | only the profile's base algorithms (an intentional subset stays a subset) |
| `optimized` | only the two optimized strategies |
| `profile` | the profile's exact algorithm selection: the pre-WHI-1528 behaviour, used for replays |

A standard six-algorithm profile such as `config/daily_gross.yaml` therefore runs twelve
strategies: six base, two optimized, then `metis_inspired`, `metis_history`,
`direct_split_certified` and `incremental_graph_repair`. They run one after the other,
each in its own isolated worker, under the profile's own objective, budget, measurement
(`run`), worker and `search.*` values. Every registry entry is not added automatically, and
a name the profile already lists is not repeated. `uni_sor_fast` keeps working with its own
`shortlist` / `sampling` sections when a profile names it; it never replaces a named
strategy and never receives a named strategy's settings.

### 0.2.1 experimental identities under `all`

`incremental_graph_repair` (WHI-1554) is `incremental_graph`'s complete incumbent plus a
bounded checkpoint-and-suffix repair (`docs/references/research-021/suffix-repair.md`). It is
in the `custom` group, receives the shared `search.*` / `graph.chunks` values like
`incremental_graph`, and under `all` (when the source does not configure it) the sha256-pinned
bounded preset `config/incremental_graph_repair/preset_v1.yaml` as its `algorithm_options`.
The repair-off control and the stress caps are the explicit profiles
`config/incremental_graph_repair/repair_off.yaml` and `stress.yaml` (`--strategies profile`);
`repair_on.yaml` runs the preset next to `incremental_graph`. Saved effective profiles,
including the earlier eight- and nine-strategy ones, replay literally and never gain it.

`metis_history` (WHI-1550) is `metis_inspired`'s chunk allocation with a history/admission-aware
per-chunk label search (`docs/references/research-021/history-labels.md`; NOT Jupiter Metis).
It is in the `custom` group, runs right after `metis_inspired`, receives the shared `search.*`,
`graph.chunks` and `graph.label_hops` (never `graph.label_pruning`), and under `all` (when the
source does not configure it) the sha256-pinned bounded preset
`config/metis_history/preset_v1.yaml` (`dominance: history`, one label per signature, a
1,024-label frontier: a visibly capped approximation on concentrated/liquidity-book regions).
The disabled-mechanism control is `config/metis_history/history_off.yaml` (`dominance: "off"`
next to `metis_inspired` with `label_pruning: false`); `history_on.yaml` runs the preset next
to `metis_inspired`'s label search (`--strategies profile`). Saved effective profiles,
including the earlier eight-, nine- and ten-strategy ones, replay literally and never gain it.

`direct_split_certified` (WHI-1552) is a certified integer branch and bound over
`direct_split`'s own allocation grid (`docs/references/research-021/integer-allocation.md`): an
ordinary plan plus a validated same-domain value bound (`certified [lower, upper] gap g` with its
termination). It is in the `custom` group, runs after `metis_history`, receives the shared
`search.max_splits` / `search.percent_step`, and under `all` (when the source does not configure
it) the sha256-pinned bounded preset `config/direct_split_certified/preset_v1.yaml`
(`domain: repository_grid`, 100,000 expanded nodes, 100,000 open nodes). Its scope is a request
whose admitted direct pools are ALL constant product under `gross_only`; every other case
(concentrated/liquidity-book direct pools, net objectives) is a visible `unsupported` row with
its reason, never a CPMM-subset approximation, so in the frozen corpus it proves only
single-pool CPMM cases. Untruncated, its value equals `direct_split`'s on the same grid (the
contribution is the certificate, not a better value). `config/direct_split_certified/grid.yaml`
runs the preset next to `direct_split`; the `raw_integer` expanded stress domain
(`raw_stress.yaml`) runs only by explicit `--strategies profile`. Saved effective profiles,
including the earlier eight- to eleven-strategy ones, replay literally and never gain it.

### Metis-inspired settings under `all`

`metis_inspired` needs `graph.label_hops`, `graph.label_pruning` and `graph.chunks` next to
the shared `search.*` values. A value the source profile declares always wins. A value it
does not declare is copied from the registered WHI-1449 arm profile
`config/metis_challenge/m4.yaml` (read only after its bytes match sha256
`661311df…4471`): `label_hops: 4`, `label_pruning: true`, and `chunks: 50` only when the
source has no `chunks`. The added keys reach `metis_inspired` alone; every other algorithm
receives exactly the values it received before.

- `config/daily_gross.yaml` keeps its `chunks: 200`, `search.max_hops: 2`, 120 s /
  50,000-quote budget and measurement, and gains `label_hops: 4`, `label_pruning: true`. Its
  Metis-inspired search therefore reaches 4 hops while the other algorithms search at most
  2: the search domains differ, and the reports say so.
- This is a new profile-derived comparison, **not** a rerun of the frozen M4 arm: M4 also
  used `full_gross` values (`max_hops: 3`, `chunks: 50`, 900 s / 300,000 quotes), and those
  are not borrowed.
- Nothing is re-tuned. `label_hops` must be `>= search.max_hops`. A source with
  `search.max_hops: 5` and no `label_hops` is refused under `all` before anything is
  written, not lowered to fit; declare `graph.label_hops` explicitly, or use
  `--strategies base` / `profile`.
- A profile that lists `metis_inspired` itself keeps its position and every setting; the
  optimized strategies are appended after the listed algorithms (`[direct, metis_inspired]`
  runs `direct`, `metis_inspired`, `uni_sor_adaptive`, `uni_sor_optimized`). A saved `all`
  effective profile (`selection.mode: all`) keeps its recorded order, so deriving `all` from
  it again changes nothing.
- No checked-in profile was edited to add Metis-inspired settings, and the WHI-1449 profiles
  and results are unchanged.

Everything is validated before anything is written or any worker starts:

- an empty selection (for example `--strategies base` on a profile without base
  algorithms) is refused;
- the optimized strategies need `search.max_hops`, `search.max_splits` and
  `search.percent_step`, and a `percent_step` grid containing their recipe percents (H4
  probes 5 %, so the grid step must divide 5). A profile such as `config/smoke.yaml` (no
  `search`) or one with `percent_step: 10` is refused under `all` / `optimized`. The error
  suggests a compatible profile, `--strategies base`, or `--strategies profile`. No
  default is invented and no recipe is re-tuned to fit.

## Where the settings come from

The recipe settings are not module constants. Each named strategy's factory
(`routing/algorithms/uni_sor_strategies.py`) pins only its recipe identity: the
`config/latency/l08.yaml` path, its v1 sha256
`e7add86add573033f04c6790f705cb2951712ffc272a4498d03f01251101beaa`, key `L08`, version 1 and
the arm. The CLI copies that arm's `shortlist`, `sampling` and `controls` into the
effective profile's `strategies.<name>` entry. The profile loader
(`benchmark/profile.py`) re-reads the pinned file, refuses it if its sha256 changed, and
refuses any entry whose settings differ from the arm. A strategy name therefore always
means exactly its recipe, and each strategy's settings are its own: the profile-wide
`shortlist` / `sampling` sections never reach them. No profile, L01, L01-SB or L08 file is
edited.

## How they run

- **Same solver.** A named strategy calls the unchanged `uni_sor_fast.solve` with
  `uni_sor_fast.prepare` of its recipe settings, then relabels the result with its own name.
  The runner refuses a mismatched name. Each record's `search.strategy` gives the name,
  group, recipe, controls and per-solve control statistics.
- **Per-solve exact controls.** `uni_sor_optimized` installs L02–L04 through the same
  installer the L08 driver uses (`pools/exact_controls.py`). Fresh memo instances are built
  inside the timed solve, and the reference quote kernels are restored in a `finally`,
  after errors and hard limits too. The runner's independent final evaluation runs in the
  parent process on the reference path. Base strategies run in their own workers and never
  see a control.
- **Honest scope.** The capabilities are `uni_sor_port`'s (V2/V3 only), so Liquidity Book
  cases are `unsupported`, as for the reference. The search is declared an approximation in
  every record and in the provenance. Integer plans, hard time/quote limits, `timeout`,
  `cancelled` and `unsupported` keep their ordinary meaning.
- **L08 is unchanged.** Its arms keep their registered algorithms. The quote-CLI stage of
  arm R now passes `--strategies profile`, so it still measures the pinned six algorithms.

## What is saved and how it replays

The effective profile is the source profile plus the derived `algorithms`, every selected
strategy's `strategies.<name>` entry, any Metis-inspired `graph.*` values it added and a
`selection` record (mode, source profile path and sha256, `base` / `optimized` / `custom`
groups). Its header comment names the M4 file and hash those values came from:

- `run` saves it as `<results-dir>/<run id>/profile.yaml`; the manifest's `profile_path`
  / `profile_sha256` name that file. The profile's measurement, order, seed and budgets are
  kept.
- `quote` saves it as `<quote dir>/profile.yaml`, with the one-attempt override. It also
  records `strategies: {mode, groups}` in `quote.json`. Each selected solver is called
  exactly once, with no warmup, retry or memory pass.

The replay command in the manifest runs that saved file with `--strategies profile`. It
therefore reproduces the saved algorithms and settings, never whatever a later default
would expand to. The replay's resolved profile equals the original. An effective profile
saved before WHI-1540 (six base plus two optimized) replays as exactly those eight; only
running a profile under `all` again would add `metis_inspired`. A profile-mode run
keeps its old form: no `selection` record, and the manifest names the source profile. A
programmatic `run_experiment(bundle, profile)` runs the profile's algorithms exactly and
never expands them.

## Reports

Groups are read from each run's own `resolved_profile.selection`, never from the current
registry:

- **Terminal** (`quote`, and `report` of a quote run): the compact table shows *Base
  strategies*, *Optimized strategies* and *Experimental and other strategies* blocks;
  `--details` has group headings. Every row has its status, output, solve latency and
  quote count, and failures stay in their row. The header names each optimized strategy's
  recipe and controls, and prints `metis_inspired` as the Metis-inspired variant (NOT
  Jupiter Metis) with its recorded `label_hops` / `chunks` / `label_pruning` next to the
  shared `search.max_hops`. It does not claim one shared search when the hop bounds differ. Groups are a presentation,
  not the schedule: the header also prints the recorded execution order. With a custom
  profile such as `[direct, uni_sor_fast, path_split]`, the run order is `direct`,
  `uni_sor_fast`, `path_split`, then the optimized strategies and `metis_inspired`, while
  the blocks read base, optimized, then experimental and other.
- **HTML**: a *Strategy groups* section gives, per group, every algorithm's status counts
  over the full schedule (failures included). It also shows paired gross versus `direct`
  and versus `uni_sor_port` on common-success cases, each optimized strategy's recorded
  recipe settings, and `metis_inspired`'s recorded label settings with its NOT-Jupiter-Metis
  label. The pairwise table still covers every cross-group pair.
- **CSV**: `strategy_groups.csv` (group, algorithm, recipe, the algorithm's recorded
  `settings`, status counts, paired medians). It is written only when a reported run has
  groups.

Runs without the new metadata (older records, `--strategies profile`) render exactly as
before.
