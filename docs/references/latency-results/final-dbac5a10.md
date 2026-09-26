# L08 final latency comparisons — session `20260926T200553953133Z-dbac5a10` (complete)

- Arms file `config/latency/l08.yaml` L08 v1 (sha256 `e7add86add573033f04c6790f705cb2951712ffc272a4498d03f01251101beaa`)
- Measured source: `2fda208a6eea1ba21bfc420a02d02ca74e2a2aa1` dirty=False; 2026-09-26T20:05:53.953416+00:00 → 2026-09-26T22:17:09.525220+00:00
- Report generated 2026-09-26T22:23:45.033707+00:00 from `e9f3ebd316254bdaac6098799babca9c16e951aa` dirty=False
- Replay: `uv run python -m benchmark.latency session --arms config/latency/l08.yaml --bundle /Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer/data/corpus/mantle-5src-101082044/bundle --out data/latency-l08`
- Session stop reasons: none
- Arms not measured: none

| Arm | experiment | max 1-min load | contaminated | coverage problems | order/cold/repeat problems |
| --- | --- | ---: | --- | ---: | ---: |
| R | `20260926T200553953878Z-ce605e14-R` | 12.25 | True | 0 | 0 |
| E1 | `20260926T205304665435Z-db26dbac-E1` | 11.46 | True | 0 | 0 |
| E2 | `20260926T211131949286Z-63367a9a-E2` | 34.99 | True | 0 | 0 |
| E3 | `20260926T212930937036Z-19c85727-E3` | 5.62 | True | 0 | 0 |
| E4 | `20260926T213747912600Z-add6ede2-E4` | 5.62 | True | 0 | 0 |
| S0 | `20260926T214531849929Z-070ced63-S0` | 10.78 | True | 0 | 0 |
| H1 | `20260926T220510528495Z-bf485880-H1` | 4.88 | False | 0 | 0 |
| H2 | `20260926T220804235146Z-ab5c9349-H2` | 4.37 | False | 0 | 0 |
| H3 | `20260926T221028914290Z-c88e4a25-H3` | 5.73 | True | 0 | 0 |
| H4 | `20260926T221624951179Z-536bd1f3-H4` | 6.16 | True | 0 | 0 |

| Comparison | lane | role | baseline → candidate | verdict | disposition | reason |
| --- | --- | --- | --- | --- | --- | --- |
| L02 | exact | decision | R → E1 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L03 | exact | decision | E1 → E2 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L04 | exact | decision | E2 → E3 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L05 | exact | decision | E3 → E4 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L02-L04-cumulative | exact | informational | R → E3 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L02-L05-cumulative | exact | informational | R → E4 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| H4-exact-controls | exact | informational | H2 → H4 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L06 | heuristic | decision | S0 → H1 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L07-combined | heuristic | decision | S0 → H2 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L07-adaptive-only | heuristic | decision | S0 → H3 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |
| L07-sampling-ablation | heuristic | informational | H1 → H2 | **opt_in_only** | opt-in only (no default; any loss needs the owner's explicit acceptance) | faster: full_source/matrix uni_sor_fast, sor_compatible/matrix uni_sor_fast; no default loss tolerance: owner must accept the reported regret |
| L06-L07-with-exact-controls | heuristic | informational | S0 → H4 | **inconclusive** | not adopted (inconclusive) | host load exceeded the protocol threshold in an experiment |

## L02 (R → E1, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | -0.017 | not_slower |
| full_source/matrix single_path | 0.586 | 0.586 | 10 | 0.100 | faster | 0.135 | not_slower |
| full_source/matrix direct_split | 0.780 | 0.780 | 6 | 0.100 | faster | 0.240 | not_slower |
| full_source/matrix path_split | 0.517 | 0.517 | 11 | 0.100 | faster | 0.252 | not_slower |
| full_source/matrix incremental_graph | 0.386 | 0.386 | 12 | 0.100 | faster | 0.231 | not_slower |
| full_source/matrix uni_sor_port | 0.695 | 0.696 | 10 | 0.100 | faster | 0.410 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | 0.667 | 0.667 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix direct_split | 0.792 | 0.792 | 6 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix path_split | 0.569 | 0.569 | 10 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.512 | 0.505 | 10 | 0.303 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | 0.662 | 0.667 | 10 | 0.519 | faster | N/A | N/A |

## L03 (E1 → E2, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | -0.997 | slower |
| full_source/matrix single_path | -0.407 | -0.197 | 6 | 0.805 | no_worthwhile_change | -0.838 | slower |
| full_source/matrix direct_split | -0.370 | -0.229 | 5 | 0.158 | slower | -0.914 | slower |
| full_source/matrix path_split | -0.347 | -0.189 | 9 | 0.159 | slower | -0.668 | slower |
| full_source/matrix incremental_graph | 0.035 | 0.038 | 11 | 0.160 | no_worthwhile_change | -0.277 | slower |
| full_source/matrix uni_sor_port | 0.118 | 0.117 | 9 | 0.100 | faster | 0.037 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | 0.073 | 0.073 | 6 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix direct_split | 0.094 | 0.094 | 5 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix path_split | 0.111 | 0.111 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.091 | 0.091 | 9 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix uni_sor_port | -0.095 | 0.014 | 9 | 2.636 | no_worthwhile_change | N/A | N/A |

## L04 (E2 → E3, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | 0.510 | not_slower |
| full_source/matrix single_path | 0.508 | 0.424 | 10 | 0.805 | no_worthwhile_change | 0.495 | not_slower |
| full_source/matrix direct_split | 0.743 | 0.714 | 5 | 0.158 | faster | 0.525 | not_slower |
| full_source/matrix path_split | 0.500 | 0.433 | 9 | 0.159 | faster | 0.498 | not_slower |
| full_source/matrix incremental_graph | 0.151 | 0.149 | 11 | 0.160 | no_worthwhile_change | 0.301 | not_slower |
| full_source/matrix uni_sor_port | 0.525 | 0.526 | 9 | 0.100 | faster | 0.246 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | 0.247 | 0.247 | 6 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix direct_split | 0.651 | 0.651 | 5 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix path_split | 0.398 | 0.398 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.287 | 0.287 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | 0.658 | 0.614 | 9 | 2.636 | no_worthwhile_change | N/A | N/A |

## L05 (E3 → E4, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 125; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | 0.001 | not_slower |
| full_source/matrix single_path | 0.008 | 0.008 | 6 | 0.100 | no_worthwhile_change | 0.001 | not_slower |
| full_source/matrix direct_split | -0.016 | -0.016 | 2 | 0.100 | no_worthwhile_change | -0.003 | not_slower |
| full_source/matrix path_split | 0.003 | 0.003 | 9 | 0.100 | no_worthwhile_change | -0.012 | not_slower |
| full_source/matrix incremental_graph | 0.226 | 0.226 | 10 | 0.100 | faster | 0.086 | not_slower |
| full_source/matrix uni_sor_port | 0.004 | 0.002 | 9 | 0.100 | no_worthwhile_change | -0.003 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | -0.005 | -0.005 | 5 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix direct_split | -0.002 | -0.003 | 1 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix path_split | -0.001 | -0.001 | 8 | 0.100 | no_worthwhile_change | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.174 | 0.175 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | -0.004 | -0.004 | 9 | 0.100 | no_worthwhile_change | N/A | N/A |

## L02-L04-cumulative (R → E3, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | 0.003 | not_slower |
| full_source/matrix single_path | 0.679 | 0.679 | 10 | 0.100 | faster | 0.197 | not_slower |
| full_source/matrix direct_split | 0.915 | 0.915 | 6 | 0.100 | faster | 0.310 | not_slower |
| full_source/matrix path_split | 0.649 | 0.649 | 11 | 0.100 | faster | 0.373 | not_slower |
| full_source/matrix incremental_graph | 0.489 | 0.489 | 12 | 0.100 | faster | 0.314 | not_slower |
| full_source/matrix uni_sor_port | 0.861 | 0.861 | 10 | 0.100 | faster | 0.572 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | 0.736 | 0.736 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix direct_split | 0.928 | 0.928 | 6 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix path_split | 0.755 | 0.755 | 10 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.669 | 0.664 | 10 | 0.303 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | 0.862 | 0.862 | 10 | 0.100 | faster | N/A | N/A |

## L02-L05-cumulative (R → E4, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 1/1; work differences 125; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | 0.005 | not_slower |
| full_source/matrix single_path | 0.681 | 0.681 | 10 | 0.100 | faster | 0.197 | not_slower |
| full_source/matrix direct_split | 0.915 | 0.915 | 6 | 0.100 | faster | 0.308 | not_slower |
| full_source/matrix path_split | 0.650 | 0.650 | 11 | 0.100 | faster | 0.366 | not_slower |
| full_source/matrix incremental_graph | 0.587 | 0.587 | 12 | 0.100 | faster | 0.373 | not_slower |
| full_source/matrix uni_sor_port | 0.861 | 0.861 | 10 | 0.100 | faster | 0.570 | not_slower |
| sor_compatible/matrix direct | N/A | N/A | 0 | 0.100 | insufficient_cases | N/A | N/A |
| sor_compatible/matrix single_path | 0.735 | 0.735 | 9 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix direct_split | 0.929 | 0.929 | 6 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix path_split | 0.755 | 0.755 | 10 | 0.100 | faster | N/A | N/A |
| sor_compatible/matrix incremental_graph | 0.722 | 0.718 | 10 | 0.303 | faster | N/A | N/A |
| sor_compatible/matrix uni_sor_port | 0.861 | 0.861 | 10 | 0.100 | faster | N/A | N/A |

## H4-exact-controls (H2 → H4, exact)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.742 | 0.742 | 10 | 0.100 | faster | 0.239 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.744 | 0.744 | 10 | 0.100 | faster | N/A | N/A |

## L06 (S0 → H1, heuristic)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.821 | 0.821 | 10 | 0.100 | faster | 0.574 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.822 | 0.822 | 10 | 0.123 | faster | N/A | N/A |

| Bundle algorithm | split | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| full_source/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| full_source/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| sor_compatible/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |

## L07-combined (S0 → H2, heuristic)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.851 | 0.851 | 10 | 0.100 | faster | 0.596 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.852 | 0.852 | 10 | 0.123 | faster | N/A | N/A |

| Bundle algorithm | split | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| full_source/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| full_source/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| sor_compatible/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |

## L07-adaptive-only (S0 → H3, heuristic)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.676 | 0.675 | 10 | 0.100 | faster | 0.452 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.679 | 0.679 | 10 | 0.123 | faster | N/A | N/A |

| Bundle algorithm | split | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix uni_sor_fast | held_out | 13 | 2 | 0 | 0 | 0.000 | 0.000 |
| full_source/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| full_source/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | held_out | 13 | 2 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |

## L07-sampling-ablation (H1 → H2, heuristic)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.170 | 0.170 | 10 | 0.100 | faster | 0.051 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.167 | 0.167 | 10 | 0.100 | faster | N/A | N/A |

| Bundle algorithm | split | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix uni_sor_fast | held_out | 13 | 2 | 0 | 0 | 0.000 | 0.000 |
| full_source/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| full_source/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | held_out | 13 | 2 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |

## L06-L07-with-exact-controls (S0 → H4, heuristic)

- Semantic mismatches 0; coverage problems 0; order/cold/repeat 0; budget-bound established 0/0; work differences 0; status regressions 0

| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | cold charged impr. | charged verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| full_source/matrix uni_sor_fast | 0.962 | 0.962 | 10 | 0.100 | faster | 0.692 | not_slower |
| sor_compatible/matrix uni_sor_fast | 0.962 | 0.962 | 10 | 0.123 | faster | N/A | N/A |

| Bundle algorithm | split | cases | N/A | losses | gains | max regret bps | mean regret bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| full_source/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| full_source/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| full_source/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/matrix uni_sor_fast | held_out | 13 | 2 | 2 | 0 | 1.125 | 0.171 |
| sor_compatible/matrix uni_sor_fast | tuning | 9 | 0 | 0 | 0 | 0.000 | 0.000 |
| sor_compatible/sentinel uni_sor_fast | sentinel | 1 | 0 | 0 | 0 | 0.000 | 0.000 |

_No percentile, p95 or SLA is derived: each case has 2 x repeats samples; the figures are medians/min/max of observations on the recorded host._
