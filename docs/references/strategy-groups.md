# Base and optimized strategy groups (WHI-1528)

The ordinary CLI compares strategies in two separate groups and, by default, runs both:

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

How the two recipes search, with hand-worked and test-verified examples, is explained in
[`routing-algorithms.md`](routing-algorithms.md) §8 (`uni_sor_adaptive`) and §9
(`uni_sor_optimized`).

The owner asked for this layout: the optimized strategies are **not** default strategies
and are kept apart from the base ones, but the CLI runs them all. Nothing here adopts a
strategy, changes a router default or accepts a loss tolerance. The L08 results and
dispositions for H3 and H4 apply only to the scope they recorded. A run of these
strategies is a new comparison, not new adoption evidence.

## Selecting strategies

`main.py run` and `main.py quote` take `--strategies all|base|optimized|profile`:

| mode | runs |
| --- | --- |
| `all` (default) | the profile's base algorithms (and any other algorithm it lists, such as a configured `uni_sor_fast`) in profile order, then `uni_sor_adaptive`, `uni_sor_optimized` |
| `base` | only the profile's base algorithms (an intentional subset stays a subset) |
| `optimized` | only the two optimized strategies |
| `profile` | the profile's exact algorithm selection: the pre-WHI-1528 behaviour, used for replays |

A standard six-algorithm profile such as `config/daily_gross.yaml` therefore runs six
base and then two optimized strategies. They run one after the other, each in its own
isolated worker, under the profile's own objective, budget, measurement (`run`) and
`search.*` values. Every registry entry is not added automatically, and a name the profile
already lists is not repeated. `uni_sor_fast` keeps working with its own `shortlist` /
`sampling` sections when a profile names it; it never replaces a named strategy and never
receives a named strategy's settings.

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
strategy's `strategies.<name>` entry and a `selection` record (mode, source profile path and
sha256, `base` / `optimized` / `custom` groups):

- `run` saves it as `<results-dir>/<run id>/profile.yaml`; the manifest's `profile_path`
  / `profile_sha256` name that file. The profile's measurement, order, seed and budgets are
  kept.
- `quote` saves it as `<quote dir>/profile.yaml`, with the one-attempt override. It also
  records `strategies: {mode, groups}` in `quote.json`. Each selected solver is called
  exactly once, with no warmup, retry or memory pass.

The replay command in the manifest runs that saved file with `--strategies profile`. It
therefore reproduces the saved algorithms and settings, never whatever a later default
would expand to. The replay's resolved profile equals the original. A profile-mode run
keeps its old form: no `selection` record, and the manifest names the source profile. A
programmatic `run_experiment(bundle, profile)` runs the profile's algorithms exactly and
never expands them.

## Reports

Groups are read from each run's own `resolved_profile.selection`, never from the current
registry:

- **Terminal** (`quote`, and `report` of a quote run): the compact table shows *Base
  strategies* and *Optimized strategies* blocks; `--details` has group headings. The
  header names each optimized strategy's recipe and controls. Groups are a presentation,
  not the schedule: the header also prints the recorded execution order. With a custom
  profile such as `[direct, uni_sor_fast, path_split]`, the run order is `direct`,
  `uni_sor_fast`, `path_split`, then the optimized strategies, while the blocks read base,
  optimized, custom.
- **HTML**: a *Strategy groups* section gives, per group, every algorithm's status counts
  over the full schedule (failures included). It also shows paired gross versus `direct`
  and versus `uni_sor_port` on common-success cases, and each strategy's recorded recipe
  settings. The pairwise table still covers every cross-group pair.
- **CSV**: `strategy_groups.csv` (group, algorithm, recipe, status counts, paired
  medians). It is written only when a reported run has groups.

Runs without the new metadata (older records, `--strategies profile`) render exactly as
before.
