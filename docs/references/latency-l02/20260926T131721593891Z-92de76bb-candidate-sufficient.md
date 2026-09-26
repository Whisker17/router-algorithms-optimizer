# L01 sufficient-budget evidence — experiment `20260926T131721593891Z-92de76bb`

- Sufficient-budget protocol: `config/latency/l01-sufficient-budget.yaml` L01-SB v1 (sha256 `4059506da09280cdb0e8d4a94ac6ecef1c786113016e8eee430bef38089db43b`); main protocol sha256 `961fb52208c7baac3d0ffe492cef89818498543f1f00f56217d2f99113e27f1b`
- Budget: {'max_candidates': None, 'max_quotes': None, 'time_limit_seconds': 600.0} (fixed budget replaced: {'max_candidates': None, 'max_quotes': 50000, 'time_limit_seconds': 120.0})
- Measured source: `8c7337a69a31e8317eec4a6b0ac21f96324df500` dirty=False; measured 2026-09-26T13:17:22.483184+00:00 → 2026-09-26T13:17:26.090562+00:00; bundles {'full_source/matrix': {'bundle_hash': '72f4f4e4c08d4c34e8480ee56ca8eebab005bb15eb3865349d7df12ba59cfb04'}, 'sor_compatible/matrix': {'bundle_hash': '0d448c4795180ed8c731d29cd07b0627714e48b3ad2be4dc57e518772daefd70'}}
- Report generated 2026-09-26T13:22:36.842823+00:00 from `8c7337a69a31e8317eec4a6b0ac21f96324df500` dirty=False
- Load: max 1-min load 19.54; **contaminated: True** (semantic evidence only; no timing claim)
- Replay: `uv run python -m benchmark.latency sufficient --sufficient config/latency/l01-sufficient-budget.yaml --bundle /Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer/data/corpus/mantle-5src-101082044/bundle --out data/latency-l02/l01`
- Fixed-budget counterpart: experiment `20260926T125952820526Z-e47f54fe` measured on `8c7337a69a31e8317eec4a6b0ac21f96324df500` dirty=False
- Pin problems (this evidence cannot stand in for that experiment in `compare --sufficient`): none

| Cohort | Algorithm | Case | budget | status | score | quotes counted | truncated_by / limit | budget-bound | attempts consistent | solve s |
| --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| full_source | incremental_graph | bnd-78c1b0-201eba-round_at | fixed_budget | ok | 1 | 50000 | max_quotes | True | True | 1.609, 1.605, 1.551, 1.632, 1.628 |
| full_source | incremental_graph | bnd-78c1b0-201eba-round_at | sufficient_budget | ok | 1 | 51052 | — | False | True | 1.744, 1.676 |
| | | | semantic fields differing from the fixed budget: none | | | | | | | |
