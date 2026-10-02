# research-021 campaign analysis, stage R

- analysis source `aa5726c5706ea7fb8c5a9c4dc49243fdd1570136` dirty=False; manifest `85b382b4d6f5`, analysis.py `311a92988ffd`
- reconciled: **True**

## Invocations

| id | result | host | max load1 | timing |
| --- | --- | --- | ---: | --- |
| R-roster-full | ok | contaminated | 15.00927734375 | inconclusive (host load, host sleep or no load sample) |
| R-roster-full.report | ok | - | - | - |
| R-roster-sor | ok | contaminated | 15.00927734375 | inconclusive (host load, host sleep or no load sample) |
| R-roster-sor.report | ok | - | - | - |
| R-e4 | ok | contaminated | 15.00927734375 | inconclusive (host load, host sleep or no load sample) |
| R-e4.report | ok | - | - | - |
| R-s4off | ok | contaminated | 11.4833984375 | inconclusive (host load, host sleep or no load sample) |
| R-s4off.report | ok | - | - | - |
| R-l3 | ok | contaminated | 9.421875 | inconclusive (host load, host sleep or no load sample) |
| R-l3.report | ok | - | - | - |
| R-s3 | ok | contaminated | 6.53759765625 | inconclusive (host load, host sleep or no load sample) |
| R-s3.report | ok | - | - | - |
| R-s3off | ok | contaminated | 6.53759765625 | inconclusive (host load, host sleep or no load sample) |
| R-s3off.report | ok | - | - | - |
| R-repair-off-full | ok | contaminated | 6.44873046875 | inconclusive (host load, host sleep or no load sample) |
| R-repair-off-full.report | ok | - | - | - |
| R-repair-off-sor | ok | contaminated | 6.44873046875 | inconclusive (host load, host sleep or no load sample) |
| R-repair-off-sor.report | ok | - | - | - |
| R-cfmm-cpmm-full | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| R-cfmm-cpmm-full.report | ok | - | - | - |
| R-cfmm-cpmm-sor | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| R-cfmm-cpmm-sor.report | ok | - | - | - |
| R-net-full | ok | clean | 3.8623046875 | descriptive only (one sample per case: no A/A noise floor, no speed verdict) |
| R-net-full.report | ok | - | - | - |
| R-net-full.replay | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| R-net-full.order | ok | - | - | - |
| R-net-sor | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| R-net-sor.report | ok | - | - | - |
| R-net-sor.replay | ok | unknown | None | inconclusive (host load, host sleep or no load sample) |
| R-net-sor.order | ok | - | - | - |
| R-same-grid-nominee-full | ok | clean | 3.546875 | descriptive only (one sample per case: no A/A noise floor, no speed verdict) |
| R-same-grid-nominee-full.report | ok | - | - | - |
| R-same-grid-nominee-sor | ok | contaminated | 5.13623046875 | inconclusive (host load, host sleep or no load sample) |
| R-same-grid-nominee-sor.report | ok | - | - | - |

## Unconditional statuses

| arm | scheduled | statuses |
| --- | ---: | --- |
| R-roster-full/direct | 302 | {"no_route": 25, "ok": 277} |
| R-roster-full/single_path | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/direct_split | 302 | {"no_route": 25, "ok": 277} |
| R-roster-full/path_split | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/incremental_graph | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/uni_sor_port | 302 | {"invalid_plan": 12, "no_route": 2, "ok": 288} |
| R-roster-full/uni_sor_adaptive | 302 | {"no_route": 2, "ok": 300} |
| R-roster-full/uni_sor_optimized | 302 | {"no_route": 2, "ok": 300} |
| R-roster-full/metis_inspired | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/metis_history | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/direct_split_certified | 302 | {"no_route": 24, "unsupported": 278} |
| R-roster-full/incremental_graph_repair | 302 | {"no_route": 1, "ok": 301} |
| R-roster-full/uni_sor_cycle_safe | 302 | {"no_route": 2, "ok": 300} |
| R-roster-full/cfmm_dual | 302 | {"no_route": 14, "ok": 288} |
| R-roster-sor/direct | 302 | {"no_route": 26, "ok": 276} |
| R-roster-sor/single_path | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/direct_split | 302 | {"no_route": 26, "ok": 276} |
| R-roster-sor/path_split | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/incremental_graph | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/uni_sor_port | 302 | {"invalid_plan": 12, "no_route": 2, "ok": 288} |
| R-roster-sor/uni_sor_adaptive | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/uni_sor_optimized | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/metis_inspired | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/metis_history | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/direct_split_certified | 302 | {"no_route": 26, "ok": 20, "unsupported": 256} |
| R-roster-sor/incremental_graph_repair | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/uni_sor_cycle_safe | 302 | {"no_route": 2, "ok": 300} |
| R-roster-sor/cfmm_dual | 302 | {"no_route": 14, "ok": 288} |
| R-e4/metis_inspired | 302 | {"no_route": 1, "ok": 301} |
| R-s4off/metis_history | 302 | {"no_route": 1, "ok": 301} |
| R-l3/metis_inspired | 302 | {"no_route": 1, "ok": 301} |
| R-s3/metis_history | 302 | {"no_route": 1, "ok": 301} |
| R-s3off/metis_history | 302 | {"no_route": 1, "ok": 301} |
| R-repair-off-full/incremental_graph_repair | 302 | {"no_route": 1, "ok": 301} |
| R-repair-off-sor/incremental_graph_repair | 302 | {"no_route": 2, "ok": 300} |
| R-cfmm-cpmm-full/cfmm_dual | 302 | {"no_route": 23, "ok": 279} |
| R-cfmm-cpmm-sor/cfmm_dual | 302 | {"no_route": 23, "ok": 279} |
| R-net-full/direct_split_certified | 302 | {"unsupported": 302} |
| R-net-full/cfmm_dual | 302 | {"unsupported": 302} |
| R-net-full.replay/direct_split_certified | 302 | {"unsupported": 302} |
| R-net-full.replay/cfmm_dual | 302 | {"unsupported": 302} |
| R-net-sor/direct_split_certified | 302 | {"unsupported": 302} |
| R-net-sor/cfmm_dual | 302 | {"unsupported": 302} |
| R-net-sor.replay/direct_split_certified | 302 | {"unsupported": 302} |
| R-net-sor.replay/cfmm_dual | 302 | {"unsupported": 302} |
| R-same-grid-nominee-full/direct_split | 302 | {"no_route": 25, "ok": 277} |
| R-same-grid-nominee-full/direct_split_certified | 302 | {"no_route": 24, "unsupported": 278} |
| R-same-grid-nominee-sor/direct_split | 302 | {"no_route": 26, "ok": 276} |
| R-same-grid-nominee-sor/direct_split_certified | 302 | {"no_route": 26, "ok": 20, "unsupported": 256} |

## Comparisons

### same_grid.nominee.full (same_grid_allocation, equal_value)
- same_domain: common-OK 0/302, higher/equal/lower 0/0/0, median bps None, min None, max None; transitions {"no_route->no_route": 24, "no_route->unsupported": 1, "ok->unsupported": 277}
- gate: **pass** {"compared": 0, "differing": 0, "not_compared": {"no_route->no_route": 24, "no_route->unsupported": 1, "ok->unsupported": 277}, "gate": "pass", "evaluable": false}
### same_grid.nominee.sor (same_grid_allocation, equal_value)
- same_domain: common-OK 20/302, higher/equal/lower 0/20/0, median bps 0.0, min 0.0, max 0.0; transitions {"no_route->no_route": 26, "ok->ok": 20, "ok->unsupported": 256}
- gate: **pass** {"compared": 20, "differing": 0, "not_compared": {"no_route->no_route": 26, "ok->unsupported": 256}, "gate": "pass", "evaluable": true}
### same_grid.full (same_grid_allocation, equal_value)
- same_domain: common-OK 0/302, higher/equal/lower 0/0/0, median bps None, min None, max None; transitions {"no_route->no_route": 24, "no_route->unsupported": 1, "ok->unsupported": 277}
- gate: **pass** {"compared": 0, "differing": 0, "not_compared": {"no_route->no_route": 24, "no_route->unsupported": 1, "ok->unsupported": 277}, "gate": "pass", "evaluable": false}
### same_grid.sor (same_grid_allocation, equal_value)
- same_domain: common-OK 20/302, higher/equal/lower 0/20/0, median bps 0.0, min 0.0, max 0.0; transitions {"no_route->no_route": 26, "ok->ok": 20, "ok->unsupported": 256}
- gate: **pass** {"compared": 20, "differing": 0, "not_compared": {"no_route->no_route": 26, "ok->unsupported": 256}, "gate": "pass", "evaluable": true}
### repair.off_identity.full (repair_off_on, identity)
- gate: **pass** {"keys": 4, "checked": 302, "identical": 302, "differing": 0, "missing": 0, "budget_bound_excluded": 0, "gate": "pass"}
### repair.off_identity.sor (repair_off_on, identity)
- gate: **pass** {"keys": 4, "checked": 302, "identical": 302, "differing": 0, "missing": 0, "budget_bound_excluded": 0, "gate": "pass"}
### repair.p1.full (repair_off_on, not_below)
- gate: **pass** {"below_without_budget_cut": 0, "ok_to_failure_without_budget_cut": 0, "budget_cut_listed_apart": 0, "gate": "pass"}
### repair.p1.sor (repair_off_on, not_below)
- gate: **pass** {"below_without_budget_cut": 0, "ok_to_failure_without_budget_cut": 0, "budget_cut_listed_apart": 0, "gate": "pass"}
### repair.paired.full (repair_off_on, paired)
- same_domain: common-OK 301/302, higher/equal/lower 52/239/0, median bps 0.0, min 0.0, max 9.007826611612543; transitions {"no_route->no_route": 1, "ok->ok": 301}
### repair.paired.sor (repair_off_on, paired)
- same_domain: common-OK 300/302, higher/equal/lower 41/247/0, median bps 0.0, min 0.0, max 9.01106192044205; transitions {"no_route->no_route": 2, "ok->ok": 300}
### cycle.sor (matched_sor_cycle_safe, cycle_safe)
- same_domain: common-OK 288/290, higher/equal/lower 0/276/0, median bps 0.0, min 0.0, max 0.0; transitions {"no_route->no_route": 2, "ok->ok": 288}
- view {"reference_trajectory": {"diverged": 29, "identical": 261}, "publication": {"published": 288, "withheld_by_cs2": 0}, "hard_kills": {"reference": 0, "variant": 0}, "variant_invalid_plan": 0, "cases_with_rejections": 29}
- identity on comparable identical cases: **pass** (261 checked, 0 differing)
### cycle.full (matched_sor_cycle_safe, cycle_safe)
- same_domain: common-OK 288/290, higher/equal/lower 0/276/0, median bps 0.0, min 0.0, max 0.0; transitions {"no_route->no_route": 2, "ok->ok": 288}
- view {"reference_trajectory": {"diverged": 29, "identical": 261}, "publication": {"published": 288, "withheld_by_cs2": 0}, "hard_kills": {"reference": 0, "variant": 0}, "variant_invalid_plan": 0, "cases_with_rejections": 29}
- identity on comparable identical cases: **pass** (261 checked, 0 differing)
### cfmm.stage.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 279/302, higher/equal/lower 239/32/8, median bps 731.3456506603642, min -4218.348016628848, max 4514262032.063293; transitions {"no_route->no_route": 14, "no_route->ok": 9, "ok->ok": 279}
### cfmm.stage.sor (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 279/302, higher/equal/lower 239/32/8, median bps 731.3456506603642, min -4218.348016628848, max 4514262032.063293; transitions {"no_route->no_route": 14, "no_route->ok": 9, "ok->ok": 279}
### cfmm.vs_path_split.sor (cfmm_cpmm_vs_cl, paired)
- incomparable_domain: common-OK 288/302, higher/equal/lower 126/85/77, median bps 0.0, min -4564.124311015829, max 43.099001146478265; transitions {"no_route->no_route": 2, "ok->no_route": 12, "ok->ok": 288}
### cfmm.vs_incremental_graph.sor (cfmm_cpmm_vs_cl, paired)
- incomparable_domain: common-OK 288/302, higher/equal/lower 88/71/129, median bps 0.0, min -4564.124311015829, max 26.36646059145487; transitions {"no_route->no_route": 2, "ok->no_route": 12, "ok->ok": 288}
### cfmm.vs_path_split.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 288/302, higher/equal/lower 87/66/135, median bps 0.0, min -9714.285714285714, max 29.23624668700004; transitions {"no_route->no_route": 1, "ok->no_route": 13, "ok->ok": 288}
### cfmm.vs_incremental_graph.full (cfmm_cpmm_vs_cl, paired)
- expanded_protocol: common-OK 288/302, higher/equal/lower 46/60/182, median bps -1.9242228178246032, min -9714.285714285714, max 24.892381655914857; transitions {"no_route->no_route": 1, "ok->no_route": 13, "ok->ok": 288}
### coverage.certified.full (expanded_protocol_coverage, paired)
- expanded_protocol: common-OK 0/302, higher/equal/lower 0/0/0, median bps None, min None, max None; transitions {"no_route->unsupported": 1, "ok->no_route": 24, "ok->unsupported": 277}
### coverage.cycle_safe.full (expanded_protocol_coverage, paired)
- expanded_protocol: common-OK 300/302, higher/equal/lower 1/80/209, median bps -4.906296472813432, min -10000.0, max 7.24770730858805; transitions {"no_route->no_route": 1, "ok->no_route": 1, "ok->ok": 300}
### depth.report (E3_E4_L3_L4_S3_S4, decomposition)
- common-OK 291/302
  - E3->L3: L3/E3=0.9999997334961066
  - E3->S3: S3/E3=1.00000210922965
  - E3->E4: E4/E3=1.0002634379885085
  - E3->E4->L4: E4/E3=1.0002634379885085 x L4/E4=0.9998775183510513
  - E3->E4->S4: E4/E3=1.0002634379885085 x S4/E4=1.0000035723624612
  - E3->L3->L4: L3/E3=0.9999997334961066 x L4/L3=1.0001411906147617
  - E3->S3->S4: S3/E3=1.00000210922965 x S4/S3=1.0002649015036773
### depth.s3off_identity (E3_E4_L3_L4_S3_S4, identity)
- gate: **pass** {"keys": 3, "checked": 298, "identical": 298, "differing": 0, "missing": 0, "budget_bound_excluded": 4, "gate": "pass"}
### depth.s4off_identity (E3_E4_L3_L4_S3_S4, identity)
- gate: **pass** {"keys": 3, "checked": 18, "identical": 18, "differing": 0, "missing": 0, "budget_bound_excluded": 284, "gate": "pass"}
### net.full (unsupported_scope, all_unsupported)
- gate: **pass** [{"arm": "R-net-full/direct_split_certified", "algorithm": "direct_split_certified", "scheduled": 302, "statuses": {"unsupported": 302}, "unsupported_reasons": {"objective_not_gross_only": 302}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}, {"arm": "R-net-full/cfmm_dual", "algorithm": "cfmm_dual", "scheduled": 302, "statuses": {"unsupported": 302}, "unsupported_reasons": {"objective": 302}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}]
### net.sor (unsupported_scope, all_unsupported)
- gate: **pass** [{"arm": "R-net-sor/direct_split_certified", "algorithm": "direct_split_certified", "scheduled": 302, "statuses": {"unsupported": 302}, "unsupported_reasons": {"objective_not_gross_only": 302}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}, {"arm": "R-net-sor/cfmm_dual", "algorithm": "cfmm_dual", "scheduled": 302, "statuses": {"unsupported": 302}, "unsupported_reasons": {"objective": 302}, "timeout_by_limit": {}, "last_valid_candidates": 0, "failures": 0}]
