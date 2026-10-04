# L01 comparison (heuristic lane): **inconclusive**

- host load exceeded the protocol threshold in an experiment
- Baseline `20261004T070453575881Z-093fe4e8` measured on `46d6e2e628b2768caab124986c3ce50d3c2f891d` dirty=False; candidate `20261004T105149465914Z-7b2b0081` measured on `46d6e2e628b2768caab124986c3ce50d3c2f891d` dirty=False
- Report generated 2026-10-04T13:51:31.013707+00:00 from `46d6e2e628b2768caab124986c3ce50d3c2f891d` dirty=False
- Coverage problems: none
- Order/cold/repeat inconsistencies: none
- Semantic mismatches: none
- Fixed-budget completion differences (reported apart from exactness): none
- Budget-bound records needing sufficient-budget exactness: 0; established 0
- Unproven budget-bound exactness: none
- Sufficient-budget mismatches: none
- Work-counter differences: 0; status regressions ok → failure: 0

| Bundle algorithm | reference | wall improvement | CPU improvement | cases | threshold | verdict | cold charged improvement | charged verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix single_path_bounded | single_path | 0.607 | 0.607 | 12 | 0.100 | faster | 0.421 | not_slower |
| full_source/matrix incremental_graph_bounded | incremental_graph | 0.144 | 0.143 | 13 | 0.514 | no_worthwhile_change | 0.051 | not_slower |
| full_source/matrix metis_history_bounded | metis_history | 0.182 | 0.182 | 12 | 0.831 | no_worthwhile_change | 0.002 | not_slower |
| sor_compatible/matrix single_path_bounded | single_path | 0.693 | 0.692 | 12 | 0.853 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix incremental_graph_bounded | incremental_graph | 0.211 | 0.209 | 12 | 0.864 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix metis_history_bounded | metis_history | 0.196 | 0.196 | 12 | 0.872 | no_worthwhile_change | N/A | N/A |

Held-out paired regret (fixed order) against the same-scope reference:

| Bundle algorithm | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix incremental_graph_bounded | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix metis_history_bounded | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix single_path_bounded | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix incremental_graph_bounded | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix metis_history_bounded | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix single_path_bounded | 13 | 2 | 0 | 0 | 0.00 | 0.00 |

_No percentile, p95 or SLA is derived: each case has 2 x repeats samples; the figures are medians/min/max of observations on the recorded host._
