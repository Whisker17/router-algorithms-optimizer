# L01 comparison (exact lane): **inconclusive**

- host load exceeded the protocol threshold in an experiment
- Baseline `20260926T122008634477Z-08a7dfe8` measured on `299b88a787b63900229280f79a3d741e5710f169` dirty=False; candidate `20260926T125952820526Z-e47f54fe` measured on `8c7337a69a31e8317eec4a6b0ac21f96324df500` dirty=False
- Report generated 2026-09-26T13:22:43.745082+00:00 from `8c7337a69a31e8317eec4a6b0ac21f96324df500` dirty=False
- Coverage problems: none
- Order/cold/repeat inconsistencies: none
- Semantic mismatches: none
- Fixed-budget completion differences (reported apart from exactness): none
- Budget-bound records needing sufficient-budget exactness: 1; established 1
- Unproven budget-bound exactness: none
- Sufficient-budget mismatches: none
- Work-counter differences: 0; status regressions ok → failure: 0

| Bundle algorithm | reference | wall improvement | CPU improvement | cases | threshold | verdict | cold charged improvement | charged verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | direct | N/A | N/A | 0 | 0.100 | insufficient_cases | -0.016 | not_slower |
| full_source/matrix single_path | single_path | 0.585 | 0.585 | 10 | 0.100 | faster | 0.138 | not_slower |
| full_source/matrix direct_split | direct_split | 0.781 | 0.781 | 6 | 0.100 | faster | 0.247 | not_slower |
| full_source/matrix path_split | path_split | 0.542 | 0.542 | 11 | 0.122 | faster | 0.273 | not_slower |
| full_source/matrix incremental_graph | incremental_graph | 0.454 | 0.437 | 12 | 0.510 | no_worthwhile_change | 0.238 | not_slower |
| full_source/matrix uni_sor_port | uni_sor_port | 0.714 | 0.712 | 10 | 0.336 | faster | 0.407 | not_slower |
| sor_compatible/matrix direct | direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | single_path | 0.721 | 0.688 | 10 | 1.601 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix direct_split | direct_split | 0.809 | 0.808 | 6 | 0.113 | faster | N/A | N/A |
| sor_compatible/matrix path_split | path_split | 0.597 | 0.596 | 10 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | incremental_graph | 0.531 | 0.530 | 10 | 0.102 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | uni_sor_port | 0.704 | 0.704 | 10 | 0.100 | faster | N/A | N/A |

Held-out paired regret (fixed order) against the same-scope reference:

| Bundle algorithm | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix direct | 12 | 3 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix direct_split | 12 | 3 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix incremental_graph | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix path_split | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix single_path | 14 | 1 | 0 | 0 | 0.00 | 0.00 |
| full_source/matrix uni_sor_port | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix direct | 11 | 4 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix direct_split | 11 | 4 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix incremental_graph | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix path_split | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix single_path | 13 | 2 | 0 | 0 | 0.00 | 0.00 |
| sor_compatible/matrix uni_sor_port | 13 | 2 | 0 | 0 | 0.00 | 0.00 |

_No percentile, p95 or SLA is derived: each case has 2 x repeats samples; the figures are medians/min/max of observations on the recorded host._
