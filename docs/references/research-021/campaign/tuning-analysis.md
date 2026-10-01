# research-021 campaign analysis, stage T

- analysis source `a3685e539438b40d72d1be279bd4118252efc9c4` dirty=False; manifest `ec2bc3f53ace`, analysis.py `311a92988ffd`
- reconciled: **True**

## Invocations

| id | result | host | max load1 | timing |
| --- | --- | --- | ---: | --- |
| T-roster-full | ok | contaminated | 67.81591796875 | inconclusive (host load, host sleep or no load sample) |
| T-roster-sor | ok | contaminated | 67.81591796875 | inconclusive (host load, host sleep or no load sample) |
| T-e4 | ok | contaminated | 13.02783203125 | inconclusive (host load, host sleep or no load sample) |
| T-s4off | ok | contaminated | 12.02197265625 | inconclusive (host load, host sleep or no load sample) |
| T-sor-ms8 | ok | contaminated | 13.02783203125 | inconclusive (host load, host sleep or no load sample) |
| T-sor-ms2 | ok | contaminated | 67.81591796875 | inconclusive (host load, host sleep or no load sample) |
| T-sor-ms1 | ok | contaminated | 67.81591796875 | inconclusive (host load, host sleep or no load sample) |
| T-l3 | ok | contaminated | 9.26171875 | inconclusive (host load, host sleep or no load sample) |
| T-s3 | ok | contaminated | 7.4228515625 | inconclusive (host load, host sleep or no load sample) |
| T-s3off | ok | contaminated | 67.81591796875 | inconclusive (host load, host sleep or no load sample) |
| T-repair-off-full | ok | contaminated | 27.9951171875 | inconclusive (host load, host sleep or no load sample) |
| T-repair-off-sor | ok | contaminated | 9.9921875 | inconclusive (host load, host sleep or no load sample) |
| T-cfmm-cpmm-full | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| T-cfmm-cpmm-sor | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms8-full | ok | slept | 16.14599609375 | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms8-sor | ok | slept | 16.14599609375 | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms2-full | ok | contaminated | 10.32958984375 | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms2-sor | ok | contaminated | 8.53173828125 | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms1-full | ok | contaminated | 8.53173828125 | inconclusive (host load, host sleep or no load sample) |
| T-alloc-ms1-sor | ok | contaminated | 8.53173828125 | inconclusive (host load, host sleep or no load sample) |
| T-net-full | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| T-net-sor | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| T-quote-smoke | ok | contaminated | 8.375 | inconclusive (host load, host sleep or no load sample) |
| T-quote-smoke.report | ok | - | - | - |
| T-quote-smoke.replay | ok | contaminated | 6.32861328125 | inconclusive (host load, host sleep or no load sample) |
| T-quote-smoke.order | ok | - | - | - |

## Unconditional statuses

| arm | scheduled | statuses |
| --- | ---: | --- |
| T-roster-full/direct | 96 | {"no_route": 24, "ok": 72} |
| T-roster-full/single_path | 96 | {"ok": 96} |
| T-roster-full/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-roster-full/path_split | 96 | {"ok": 96} |
| T-roster-full/incremental_graph | 96 | {"ok": 96} |
| T-roster-full/uni_sor_port | 96 | {"invalid_plan": 2, "ok": 94} |
| T-roster-full/uni_sor_adaptive | 96 | {"ok": 96} |
| T-roster-full/uni_sor_optimized | 96 | {"ok": 96} |
| T-roster-full/metis_inspired | 96 | {"ok": 96} |
| T-roster-full/metis_history | 96 | {"ok": 96} |
| T-roster-full/direct_split_certified | 96 | {"no_route": 24, "unsupported": 72} |
| T-roster-full/incremental_graph_repair | 96 | {"ok": 96} |
| T-roster-full/uni_sor_cycle_safe | 96 | {"ok": 96} |
| T-roster-full/cfmm_dual | 96 | {"ok": 96} |
| T-roster-sor/direct | 96 | {"no_route": 24, "ok": 72} |
| T-roster-sor/single_path | 96 | {"ok": 96} |
| T-roster-sor/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-roster-sor/path_split | 96 | {"ok": 96} |
| T-roster-sor/incremental_graph | 96 | {"ok": 96} |
| T-roster-sor/uni_sor_port | 96 | {"invalid_plan": 2, "ok": 94} |
| T-roster-sor/uni_sor_adaptive | 96 | {"ok": 96} |
| T-roster-sor/uni_sor_optimized | 96 | {"ok": 96} |
| T-roster-sor/metis_inspired | 96 | {"ok": 96} |
| T-roster-sor/metis_history | 96 | {"ok": 96} |
| T-roster-sor/direct_split_certified | 96 | {"no_route": 24, "ok": 6, "unsupported": 66} |
| T-roster-sor/incremental_graph_repair | 96 | {"ok": 96} |
| T-roster-sor/uni_sor_cycle_safe | 96 | {"ok": 96} |
| T-roster-sor/cfmm_dual | 96 | {"ok": 96} |
| T-e4/metis_inspired | 96 | {"ok": 96} |
| T-s4off/metis_history | 96 | {"ok": 96} |
| T-sor-ms8/uni_sor_port | 96 | {"invalid_plan": 4, "ok": 92} |
| T-sor-ms8/uni_sor_adaptive | 96 | {"ok": 96} |
| T-sor-ms8/uni_sor_optimized | 96 | {"ok": 96} |
| T-sor-ms8/uni_sor_cycle_safe | 96 | {"ok": 96} |
| T-sor-ms2/uni_sor_port | 96 | {"ok": 96} |
| T-sor-ms2/uni_sor_adaptive | 96 | {"ok": 96} |
| T-sor-ms2/uni_sor_optimized | 96 | {"ok": 96} |
| T-sor-ms2/uni_sor_cycle_safe | 96 | {"ok": 96} |
| T-sor-ms1/uni_sor_port | 96 | {"ok": 96} |
| T-sor-ms1/uni_sor_adaptive | 96 | {"ok": 96} |
| T-sor-ms1/uni_sor_optimized | 96 | {"ok": 96} |
| T-sor-ms1/uni_sor_cycle_safe | 96 | {"ok": 96} |
| T-l3/metis_inspired | 96 | {"ok": 96} |
| T-s3/metis_history | 96 | {"ok": 96} |
| T-s3off/metis_history | 96 | {"ok": 96} |
| T-repair-off-full/incremental_graph_repair | 96 | {"ok": 96} |
| T-repair-off-sor/incremental_graph_repair | 96 | {"ok": 96} |
| T-cfmm-cpmm-full/cfmm_dual | 96 | {"ok": 96} |
| T-cfmm-cpmm-sor/cfmm_dual | 96 | {"ok": 96} |
| T-alloc-ms8-full/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms8-full/path_split | 96 | {"ok": 72, "timeout": 24} |
| T-alloc-ms8-full/direct_split_certified | 96 | {"no_route": 24, "unsupported": 72} |
| T-alloc-ms8-sor/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms8-sor/path_split | 96 | {"ok": 77, "timeout": 19} |
| T-alloc-ms8-sor/direct_split_certified | 96 | {"no_route": 24, "ok": 6, "unsupported": 66} |
| T-alloc-ms2-full/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms2-full/path_split | 96 | {"ok": 96} |
| T-alloc-ms2-full/direct_split_certified | 96 | {"no_route": 24, "unsupported": 72} |
| T-alloc-ms2-sor/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms2-sor/path_split | 96 | {"ok": 96} |
| T-alloc-ms2-sor/direct_split_certified | 96 | {"no_route": 24, "ok": 6, "unsupported": 66} |
| T-alloc-ms1-full/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms1-full/path_split | 96 | {"ok": 96} |
| T-alloc-ms1-full/direct_split_certified | 96 | {"no_route": 24, "unsupported": 72} |
| T-alloc-ms1-sor/direct_split | 96 | {"no_route": 24, "ok": 72} |
| T-alloc-ms1-sor/path_split | 96 | {"ok": 96} |
| T-alloc-ms1-sor/direct_split_certified | 96 | {"no_route": 24, "ok": 6, "unsupported": 66} |
| T-net-full/direct_split_certified | 96 | {"unsupported": 96} |
| T-net-full/cfmm_dual | 96 | {"unsupported": 96} |
| T-net-sor/direct_split_certified | 96 | {"unsupported": 96} |
| T-net-sor/cfmm_dual | 96 | {"unsupported": 96} |
| T-quote-smoke/direct | 1 | {"ok": 1} |
| T-quote-smoke/single_path | 1 | {"ok": 1} |
| T-quote-smoke/direct_split | 1 | {"ok": 1} |
| T-quote-smoke/path_split | 1 | {"ok": 1} |
| T-quote-smoke/incremental_graph | 1 | {"ok": 1} |
| T-quote-smoke/uni_sor_port | 1 | {"invalid_plan": 1} |
| T-quote-smoke/uni_sor_adaptive | 1 | {"ok": 1} |
| T-quote-smoke/uni_sor_optimized | 1 | {"ok": 1} |
| T-quote-smoke/metis_inspired | 1 | {"ok": 1} |
| T-quote-smoke/metis_history | 1 | {"ok": 1} |
| T-quote-smoke/direct_split_certified | 1 | {"unsupported": 1} |
| T-quote-smoke/incremental_graph_repair | 1 | {"ok": 1} |
| T-quote-smoke/uni_sor_cycle_safe | 1 | {"ok": 1} |
| T-quote-smoke/cfmm_dual | 1 | {"ok": 1} |
| T-quote-smoke.replay/direct | 1 | {"ok": 1} |
| T-quote-smoke.replay/single_path | 1 | {"ok": 1} |
| T-quote-smoke.replay/direct_split | 1 | {"ok": 1} |
| T-quote-smoke.replay/path_split | 1 | {"ok": 1} |
| T-quote-smoke.replay/incremental_graph | 1 | {"ok": 1} |
| T-quote-smoke.replay/uni_sor_port | 1 | {"invalid_plan": 1} |
| T-quote-smoke.replay/uni_sor_adaptive | 1 | {"ok": 1} |
| T-quote-smoke.replay/uni_sor_optimized | 1 | {"ok": 1} |
| T-quote-smoke.replay/metis_inspired | 1 | {"ok": 1} |
| T-quote-smoke.replay/metis_history | 1 | {"ok": 1} |
| T-quote-smoke.replay/direct_split_certified | 1 | {"unsupported": 1} |
| T-quote-smoke.replay/incremental_graph_repair | 1 | {"ok": 1} |
| T-quote-smoke.replay/uni_sor_cycle_safe | 1 | {"ok": 1} |
| T-quote-smoke.replay/cfmm_dual | 1 | {"ok": 1} |

## Comparisons

### same_grid.full (same_grid_allocation, equal_value)
- same_domain: common-OK 0/96, higher/equal/lower 0/0/0, median bps None, min None, max None; transitions {"no_route->no_route": 24, "ok->unsupported": 72}
- gate: **pass** {"compared": 0, "differing": 0, "not_compared": {"no_route->no_route": 24, "ok->unsupported": 72}, "gate": "pass", "evaluable": false}
### same_grid.sor (same_grid_allocation, equal_value)
- same_domain: common-OK 6/96, higher/equal/lower 0/6/0, median bps 0.0, min 0.0, max 0.0; transitions {"no_route->no_route": 24, "ok->ok": 6, "ok->unsupported": 66}
- gate: **pass** {"compared": 6, "differing": 0, "not_compared": {"no_route->no_route": 24, "ok->unsupported": 66}, "gate": "pass", "evaluable": true}
### repair.off_identity.full (repair_off_on, identity)
- gate: **pass** {"keys": 4, "checked": 96, "identical": 96, "differing": 0, "missing": 0, "budget_bound_excluded": 0, "gate": "pass"}
### repair.off_identity.sor (repair_off_on, identity)
- gate: **pass** {"keys": 4, "checked": 96, "identical": 96, "differing": 0, "missing": 0, "budget_bound_excluded": 0, "gate": "pass"}
### repair.p1.full (repair_off_on, not_below)
- gate: **pass** {"below_without_budget_cut": 0, "ok_to_failure_without_budget_cut": 0, "budget_cut_listed_apart": 0, "gate": "pass"}
### repair.p1.sor (repair_off_on, not_below)
- gate: **pass** {"below_without_budget_cut": 0, "ok_to_failure_without_budget_cut": 0, "budget_cut_listed_apart": 0, "gate": "pass"}
### repair.paired.full (repair_off_on, paired)
- same_domain: common-OK 96/96, higher/equal/lower 23/73/0, median bps 0.0, min 0.0, max 9.775899057511976; transitions {"ok->ok": 96}
### repair.paired.sor (repair_off_on, paired)
- same_domain: common-OK 96/96, higher/equal/lower 17/79/0, median bps 0.0, min 0.0, max 8.285250913040608; transitions {"ok->ok": 96}
### cycle.sor (matched_sor_cycle_safe, cycle_safe)
- same_domain: common-OK 94/96, higher/equal/lower 1/93/0, median bps 0.0, min 0.0, max 0.4253730244673315; transitions {"invalid_plan->ok": 2, "ok->ok": 94}
- view {"reference_trajectory": {"diverged": 11, "identical": 85}, "publication": {"published": 96, "withheld_by_cs2": 0}, "hard_kills": {"reference": 0, "variant": 0}, "variant_invalid_plan": 0, "cases_with_rejections": 11}
- identity on comparable identical cases: **pass** (85 checked, 0 differing)
### cycle.full (matched_sor_cycle_safe, cycle_safe)
- same_domain: common-OK 94/96, higher/equal/lower 1/93/0, median bps 0.0, min 0.0, max 0.4253730244673315; transitions {"invalid_plan->ok": 2, "ok->ok": 94}
- view {"reference_trajectory": {"diverged": 11, "identical": 85}, "publication": {"published": 96, "withheld_by_cs2": 0}, "hard_kills": {"reference": 0, "variant": 0}, "variant_invalid_plan": 0, "cases_with_rejections": 11}
- identity on comparable identical cases: **pass** (85 checked, 0 differing)
### cfmm.stage.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 96/96, higher/equal/lower 88/6/2, median bps 16028.79327138219, min -4216.194305608853, max 1115334693.5942814; transitions {"ok->ok": 96}
### cfmm.stage.sor (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 96/96, higher/equal/lower 88/6/2, median bps 16028.79327138219, min -4216.194305608853, max 1115334693.5942814; transitions {"ok->ok": 96}
### cfmm.vs_path_split.sor (cfmm_cpmm_vs_cl, paired)
- incomparable_domain: common-OK 96/96, higher/equal/lower 50/24/22, median bps 0.03467027648380813, min -4557.225090672544, max 28.7468966418398; transitions {"ok->ok": 96}
### cfmm.vs_incremental_graph.sor (cfmm_cpmm_vs_cl, paired)
- incomparable_domain: common-OK 96/96, higher/equal/lower 39/14/43, median bps 0.0, min -4557.225090672544, max 27.436634439508754; transitions {"ok->ok": 96}
### cfmm.vs_path_split.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 96/96, higher/equal/lower 34/19/43, median bps 0.0, min -4557.225090672544, max 26.126714565643372; transitions {"ok->ok": 96}
### cfmm.vs_incremental_graph.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 96/96, higher/equal/lower 21/13/62, median bps -1.7616507115226674, min -4557.225090672544, max 23.50790126681468; transitions {"ok->ok": 96}
### coverage.certified.full (expanded_protocol_coverage, paired)
- expanded_protocol: common-OK 0/96, higher/equal/lower 0/0/0, median bps None, min None, max None; transitions {"ok->no_route": 24, "ok->unsupported": 72}
### coverage.cycle_safe.full (expanded_protocol_coverage, paired)
- expanded_protocol: common-OK 96/96, higher/equal/lower 1/22/73, median bps -5.146511917709849, min -55.47702149573464, max 1.4199629915939185; transitions {"ok->ok": 96}
### depth.tuning (E3_E4_L3_L4_S3_S4, decomposition)
- common-OK 96/96
  - E3->L3: L3/E3=1.0000019407372909
  - E3->S3: S3/E3=1.0000018859585207
  - E3->E4: E4/E3=1.0003070202386832
  - E3->E4->L4: E4/E3=1.0003070202386832 x L4/E4=1.0000078983804894
  - E3->E4->S4: E4/E3=1.0003070202386832 x S4/E4=1.000014666704131
  - E3->L3->L4: L3/E3=1.0000019407372909 x L4/L3=1.000312979699433
  - E3->S3->S4: S3/E3=1.0000018859585207 x S4/S3=1.0003198048841297
### depth.s3off_identity (E3_E4_L3_L4_S3_S4, identity)
- gate: **pass** {"keys": 3, "checked": 96, "identical": 96, "differing": 0, "missing": 0, "budget_bound_excluded": 0, "gate": "pass"}
### depth.s4off_identity (E3_E4_L3_L4_S3_S4, identity)
- gate: **pass** {"keys": 3, "checked": 9, "identical": 9, "differing": 0, "missing": 0, "budget_bound_excluded": 87, "gate": "pass"}
### max_splits.same_grid_allocation (max_splits_scan, nominee)
- nominee **8** (applied): smallest value with zero shortfall and the fewest failures (0); v=1: failures 0, shortfall 48, v=2: failures 0, shortfall 29, v=4: failures 0, shortfall 6, v=8: failures 0, shortfall 0
### max_splits.matched_sor_cycle_safe (max_splits_scan, nominee)
- nominee **4** (applied): no value saturates every cell: canonical 4 retained; v=1: failures 0, shortfall 104, v=2: failures 0, shortfall 64, v=4: failures 2, shortfall 32, v=8: failures 4, shortfall 4
### max_splits.path_split (max_splits_scan, nominee)
- nominee **4** (informational): no value saturates every cell: canonical 4 retained; v=1: failures 0, shortfall 110, v=2: failures 0, shortfall 69, v=4: failures 0, shortfall 9, v=8: failures 43, shortfall 43
### max_splits.optimized_recipes (max_splits_scan, nominee)
- nominee **4** (informational): no value saturates every cell: canonical 4 retained; v=1: failures 0, shortfall 103, v=2: failures 0, shortfall 54, v=4: failures 0, shortfall 19, v=8: failures 0, shortfall 4
### net.full (unsupported_scope, all_unsupported)
- gate: **pass** [{"arm": "T-net-full/direct_split_certified", "algorithm": "direct_split_certified", "scheduled": 96, "statuses": {"unsupported": 96}, "unsupported_reasons": {"objective_not_gross_only": 96}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}, {"arm": "T-net-full/cfmm_dual", "algorithm": "cfmm_dual", "scheduled": 96, "statuses": {"unsupported": 96}, "unsupported_reasons": {"objective": 96}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}]
### net.sor (unsupported_scope, all_unsupported)
- gate: **pass** [{"arm": "T-net-sor/direct_split_certified", "algorithm": "direct_split_certified", "scheduled": 96, "statuses": {"unsupported": 96}, "unsupported_reasons": {"objective_not_gross_only": 96}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}, {"arm": "T-net-sor/cfmm_dual", "algorithm": "cfmm_dual", "scheduled": 96, "statuses": {"unsupported": 96}, "unsupported_reasons": {"objective": 96}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}]
