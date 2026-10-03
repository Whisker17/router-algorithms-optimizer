# research-021 campaign analysis, stage M

- analysis source `aa5726c5706ea7fb8c5a9c4dc49243fdd1570136` dirty=False; manifest `85b382b4d6f5`, analysis.py `311a92988ffd`
- reconciled: **True**

## Invocations

| id | result | host | max load1 | timing |
| --- | --- | --- | ---: | --- |
| M-roster | ok | contaminated | 22.55322265625 | inconclusive (host load, host sleep or no load sample) |
| M-roster.report | ok | - | - | - |

## Unconditional statuses

| arm | scheduled | statuses |
| --- | ---: | --- |
| M-roster/direct | 24 | {"no_route": 3, "ok": 21} |
| M-roster/single_path | 24 | {"no_route": 1, "ok": 23} |
| M-roster/direct_split | 24 | {"no_route": 3, "ok": 21} |
| M-roster/path_split | 24 | {"no_route": 1, "ok": 23} |
| M-roster/incremental_graph | 24 | {"no_route": 1, "ok": 23} |
| M-roster/uni_sor_port | 24 | {"invalid_plan": 3, "no_route": 2, "ok": 19} |
| M-roster/uni_sor_adaptive | 24 | {"no_route": 2, "ok": 22} |
| M-roster/uni_sor_optimized | 24 | {"no_route": 2, "ok": 22} |
| M-roster/metis_inspired | 24 | {"no_route": 1, "ok": 23} |
| M-roster/metis_history | 24 | {"no_route": 1, "ok": 23} |
| M-roster/direct_split_certified | 24 | {"no_route": 2, "unsupported": 22} |
| M-roster/incremental_graph_repair | 24 | {"no_route": 1, "ok": 23} |
| M-roster/uni_sor_cycle_safe | 24 | {"no_route": 2, "ok": 22} |
| M-roster/cfmm_dual | 24 | {"no_route": 2, "ok": 22} |

## Comparisons

