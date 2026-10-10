"""Executable checks for the 0.2.4 parts of the routing guide (WHI-1634).

Release 0.2.4 (contract `R024-C/1`) selected presets for the two 0.2.3 post-processors
`split_polish` (E1) and `marginal_activation` (E2) and appended both to `--strategies all`
(19 rows). This module checks every new figure of the guide against an expectation that does not
come from the code under test:

- section 27 (guide §11, the 19-row walkthrough on the 19-pool FIXTURE): the roster derived by the
  CLI's own `derive`; each preset's identity re-read from its file (sha256 computed here); the two
  new rows run through their registered factories, every leg checked against the exact protocol
  quote seam (`pools.quote.quote_exact_in`), the polished gross against an independent 0.1-USDC
  grid oracle of the same two pools, the activation proposals against exact quotes on the
  post-plan states and the `r021_examples` hand quote;
- section 28 (guide §§23.10, 24.10, the selection): the pinned stage-T / stage-I analyses of
  `research-024/selection/`, each figure also found in its row of the generated `tables.md`;
- section 29 (guide §§23.10, 24.10, §13): the committed
  `research-024/campaign/report-analysis.json`, each figure also found in its row of the
  byte-identically regenerated `report-tables.md`, and the timing outcomes and statements of
  `timing-analysis.json` (`timing-tables.md`).

Counters only a factory can report (simulations, quotes, the activation log) are *factory
counters*: regression-pinned outputs, not independently derived. The §11 rows are one exploratory
request under `config/daily_gross.yaml` on the fixture; they are never a campaign result, and the
campaign figures are never fixture figures. **No timing is asserted or claimed**: section 29 reads
only the units' outcomes and the two statements of contract §8.7.

Run: `uv run python docs/examples/routing-algorithms/r024_examples.py` (also called by
`run_examples.py`, sections 27-29). `collect()` returns every example as plain JSON-shaped data.
Offline: tracked files only, no RPC, Dune, credentials or `data/`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _path in (ROOT, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import r021_examples as r21  # noqa: E402  (the shared independent toolkit)
import yaml  # noqa: E402

from benchmark.profile import parse_profile, strategy_group  # noqa: E402
from benchmark.strategies import (  # noqa: E402
    R021_ADDITIONS,
    R022_ADDITIONS,
    R024_ADDITIONS,
    derive,
    frozen_roster,
)
from pools.quote import metered_quotes, quote_exact_in  # noqa: E402
from routing.algorithms import marginal_activation as ma  # noqa: E402
from routing.algorithms.base import SolveContext, SolveStatus  # noqa: E402
from routing.algorithms.registry import ALGORITHMS  # noqa: E402
from routing.evaluator import EvalStatus, evaluate  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import Case, ConstantProductPoolState  # noqa: E402

ExampleError = r21.ExampleError
check = r21.check
equal = r21.equal

CORPUS = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
R024 = ROOT / "docs" / "references" / "research-024"
SELECTION = R024 / "selection"
CAMPAIGN = R024 / "campaign"
BASES = {"split_polish": "metis_inspired", "marginal_activation": "incremental_graph"}


def usdt0(raw: int) -> str:
    """A raw USDT0 amount (6 decimals) as the guide's tables print it."""
    return f"{raw // 10**6}.{raw % 10**6:06d}"


# ============================================================ 27. roster, presets, §11 rows


def presets() -> dict[str, dict[str, Any]]:
    """Each selected preset as its file states it, with the file's sha256 computed here."""
    out = {}
    for name in R024_ADDITIONS:
        path = f"config/{name}/preset_v1.yaml"
        raw = (ROOT / path).read_bytes()
        document = yaml.safe_load(raw)
        equal(document["algorithm"], name, f"{path}: algorithm")
        out[name] = {
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "key": document["key"],
            "version": document["version"],
            "options": dict(document["options"]),
            # the guide prints exact decimals (`tolerance 0.00001`), never `1e-05`
            "decimals": {
                k: format(Decimal(str(v)), "f")
                for k, v in document["options"].items()
                if isinstance(v, float)
            },
        }
    return out


def _roster(pins: Mapping[str, Any]) -> dict[str, Any]:
    with frozen_roster((*R021_ADDITIONS, *R022_ADDITIONS)):
        frozen = list(r21.all_profile()[1].algorithms)
    document, profile, _ = r21.all_profile()
    roster = list(profile.algorithms)
    equal(len(frozen), 17, "the 0.2.2 / 0.2.3 roster of daily_gross.yaml")
    equal(roster, [*frozen, *R024_ADDITIONS], "0.2.4 appends exactly R024_ADDITIONS, last")
    equal(list(R024_ADDITIONS), ["split_polish", "marginal_activation"], "R024_ADDITIONS order")
    equal([strategy_group(n) for n in R024_ADDITIONS], ["custom"] * 2, "both are `custom`")
    equal(document["selection"]["mode"], "all", "the effective document is an `all` document")
    entries = {}
    for name, pin in pins.items():
        entry = profile.algorithm_options[name]
        equal(entry["options"], pin["options"], f"{name}: `all` writes the preset's options out")
        equal(
            entry["source"],
            {"kind": "preset", **{k: pin[k] for k in ("path", "sha256", "key", "version")}},
            f"{name}: preset provenance under `all`",
        )
        entries[name] = {"source": dict(entry["source"]), "settings": entry["settings_sha256"]}
    # D1-F2: the same options in an explicit (`--strategies profile`) document stay an override
    source = yaml.safe_load((ROOT / r21.SOURCE_PROFILE).read_text(encoding="utf-8"))
    source["algorithms"] = [*BASES.values(), *R024_ADDITIONS]
    source["graph"] = {**source["graph"], "label_hops": 4, "label_pruning": True}  # m4.yaml's
    source["algorithm_options"] = {n: dict(p["options"]) for n, p in pins.items()}
    raw = yaml.safe_dump(source, sort_keys=False).encode()
    _, explicit = derive(
        yaml.safe_load(raw),
        "profile",
        source_path="explicit.yaml",
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    for name in R024_ADDITIONS:
        entry = explicit.algorithm_options[name]
        equal(entry["source"], {"kind": "override"}, f"{name}: equal options outside `all`")
        equal(entry["settings_sha256"], entries[name]["settings"], f"{name}: same settings")
    # a saved `all` document replays literally and keeps the preset provenance
    saved = parse_profile(yaml.safe_load(yaml.safe_dump(document, sort_keys=False)), "saved")
    for name in R024_ADDITIONS:
        equal(saved.algorithm_options[name]["source"]["kind"], "preset", f"{name}: saved replay")
    params = {n: dict(profile.algorithm_config(ALGORITHMS[n]).params) for n in R024_ADDITIONS}
    for name, base in BASES.items():
        equal(
            params[name],
            dict(profile.algorithm_config(ALGORITHMS[base]).params),
            f"{name}: its base's own configuration under daily_gross.yaml",
        )
    return {
        "size": len(roster),
        "frozen_size": len(frozen),
        "last_two": roster[-2:],
        "options": entries,
        "params": params,
    }


def _rows(pins: Mapping[str, Any]) -> dict[str, Any]:
    bundle = load_bundle(CORPUS)
    _, profile, _ = r21.all_profile()
    case = Case("real_usdc_usdt0_10k", r21.USDC, r21.USDT0, 10_000_000_000)
    pools = {p.pool_id[:6]: p for p in bundle.pools_for_pair(r21.USDC, r21.USDT0)}
    agni, lb, moe, dead = pools["0x36f6"], pools["0x368b"], pools["0x69a7"], pools["0x1bc3"]
    assert isinstance(moe, ConstantProductPoolState)
    base_gross = sum(
        quote_exact_in(p, r21.USDC, a).amount_out
        for p, a in ((agni, 9_950_000_000), (lb, 50_000_000))
    )
    terminals: list[list[Any]] = []
    real = ma.top_paths

    def spy(*args: Any) -> Any:
        found = real(*args)
        terminals.append([[amount, [e[0][:6] for e in path]] for amount, path in found])
        return found

    rows: dict[str, Any] = {}
    ma.top_paths = spy  # type: ignore[assignment]
    try:
        for name in (*BASES.values(), *R024_ADDITIONS):
            factory = ALGORITHMS[name]
            config = profile.algorithm_config(factory)
            prepared = factory.prepare(bundle, config) if factory.prepare is not None else None
            with metered_quotes(None) as meter:
                result = factory.solve(
                    case, SolveContext(bundle, profile.objective, prepared), profile.budget
                )
            check(result.status is SolveStatus.OK and result.plan is not None, f"{name} ok")
            assert result.plan is not None
            replay = evaluate(bundle, case, result.plan, profile.objective)
            check(replay.status is EvalStatus.OK, f"{name}: the plan replays ok")
            equal(replay.gross_output, result.score, f"§11 {name}: fresh replay")
            legs = [
                [s.pool_id[:6], t.amount_in, t.amount_out]
                for s, t in zip(result.plan.steps, replay.trace, strict=True)
            ]
            rows[name] = {
                "score": result.score,
                "usdt0": usdt0(int(result.score)),
                "quotes_counted": meter.counted,
                "legs": legs,
                "plan": r21.plan_view(result.plan),
                "stats": json.loads(json.dumps(result.search_stats.get(name), default=str)),
                "base": json.loads(json.dumps(result.search_stats.get("base"), default=str)),
            }
    finally:
        ma.top_paths = real
    for base in BASES.values():
        equal(rows[base]["score"], base_gross, f"§11 {base}: the §11.1 two-leg gross")
    # the 0.1-USDC grid oracle of the same two pools (the guide's §11.5 comparison)
    grid = max(
        quote_exact_in(agni, r21.USDC, case.amount_in - x).amount_out
        + (quote_exact_in(lb, r21.USDC, x).amount_out if x else 0)
        for x in range(0, 50_000_001, 100_000)
    )
    out: dict[str, Any] = {"base_gross": base_gross, "grid_oracle": grid}
    for name, base in BASES.items():
        row = rows[name]
        (agni_in, agni_out_), (lb_in, lb_out_) = ((leg[1], leg[2]) for leg in row["legs"])
        equal([leg[0] for leg in row["legs"]], ["0x36f6", "0x368b"], f"§11 {name}: same pools")
        equal(agni_in + lb_in, case.amount_in, f"§11 {name}: the legs spend the input")
        exact = [quote_exact_in(agni, r21.USDC, agni_in), quote_exact_in(lb, r21.USDC, lb_in)]
        equal([agni_out_, lb_out_], [r.amount_out for r in exact], f"§11 {name}: exact legs")
        check(row["score"] > base_gross, f"§11 {name}: strictly above its base")
        check(row["score"] >= grid, f"§11 {name}: at least the best 0.1-USDC grid split")
        equal(row["base"]["algorithm"], base, f"§11 {name}: its base row")
        equal(row["stats"]["base_gross"], str(base_gross), f"§11 {name}: recorded base gross")
        stats = row["stats"]
        own = stats["quotes"]
        equal(row["quotes_counted"], row["base"]["quotes"] + own, f"§11 {name}: one ledger")
        equal(row["quotes_counted"], rows[base]["quotes_counted"] + own, f"§11 {name}: base row")
        out[name] = {
            "score": row["score"],
            "usdt0": row["usdt0"],
            "quotes_counted": row["quotes_counted"],
            "own_quotes": own,
            "base_quotes": row["base"]["quotes"],
            "legs": row["legs"],
            "plan": row["plan"],
            "gain_raw": row["score"] - base_gross,
            "moved_raw": 50_000_000 - lb_in,
            "options_key": pins[name]["key"],
            "table_row": (
                f"| `{name}` | `ok` | **{row['usdt0']}** | **{row['score']}** | "
                f"{row['quotes_counted']} |"
            ),
        }
    e1 = rows["split_polish"]["stats"]
    equal((e1["base"], e1["solver"], e1["tolerance"]), ("metis_inspired", "golden", 1e-05), "E1")
    out["split_polish"]["factory_counters"] = {
        k: e1["work"][k] for k in ("polish_calls", "simulations", "golden_iters", "accepted")
    } | {"scope": e1["scope"], "truncated_by": e1["truncated_by"]}
    e2 = rows["marginal_activation"]["stats"]
    act = e2["activation"]
    equal(e2["e1"]["gross"], str(rows["marginal_activation"]["score"]), "E2 keeps its E1 plan")
    equal(e2["quotes"], e2["e1"]["quotes"] + act["quotes"], "E2: E1 + activation quotes")
    share = Fraction(str(pins["marginal_activation"]["options"]["delta_share"]))
    delta = max(1, int(share * case.amount_in))
    (agni_in, _), (lb_in, _) = ((leg[1], leg[2]) for leg in rows["marginal_activation"]["legs"])
    post = [
        quote_exact_in(agni, r21.USDC, agni_in).new_state,
        quote_exact_in(lb, r21.USDC, lb_in).new_state,
    ]
    assert post[0] is not None and post[1] is not None
    want = [
        [quote_exact_in(post[0], r21.USDC, delta).amount_out, ["0x36f6"]],
        [quote_exact_in(post[1], r21.USDC, delta).amount_out, ["0x368b"]],
        [r21.hand_out(moe, r21.USDC, delta), ["0x69a7"]],
    ]
    equal(terminals, [want], "§11 E2 terminals vs exact quotes on the post-plan states")
    dead_status = quote_exact_in(dead, r21.USDC, delta).status.value
    check(dead_status != "ok", "§11 the fourth direct pool fails at delta")
    out["marginal_activation"]["factory_counters"] = {
        "e1_quotes": e2["e1"]["quotes"],
        "e1_accepted": e2["e1"]["work"]["accepted"],
        "e1_simulations": e2["e1"]["work"]["simulations"],
        "activation_quotes": act["quotes"],
        "activation_simulations": act["work"]["simulations"],
        "invocations": act["invocations"],
        "log": act["log"],
        "not_reached": e2["not_reached"],
    }
    out["marginal_activation"]["delta"] = delta
    out["marginal_activation"]["terminals"] = terminals[0]
    out["marginal_activation"]["dead_pool_status"] = dead_status
    out["moe_output_reserve"] = moe.reserve1 if moe.token1 == r21.USDT0 else moe.reserve0
    out["split_polish_vs_marginal_activation_raw"] = (
        rows["marginal_activation"]["score"] - rows["split_polish"]["score"]
    )
    return out


def example_walkthrough() -> dict[str, Any]:
    """§11: the 19-row roster, the presets' provenance and the two new rows on the fixture."""
    pins = presets()
    return {"presets": pins, "roster": _roster(pins), "rows": _rows(pins)}


# ============================================================ 28. the selection (research-024)


SELECTION_TABLES = (SELECTION / "tables.md").read_text(encoding="utf-8")
T_ANALYSIS = json.loads((SELECTION / "T-analysis.json").read_text(encoding="utf-8"))


def _cells(text: str, prefix: str) -> list[str]:
    """The cells of the one table row starting with `prefix` (a `|` inside backticks, as in a
    candidate id, does not separate cells)."""
    (row,) = [line for line in text.splitlines() if line.startswith(prefix)]
    cells, cell, code = [], "", False
    for char in row.strip().strip("|"):
        code ^= char == "`"
        if char == "|" and not code:
            cells.append(cell.strip())
            cell = ""
        else:
            cell += char
    return [*cells, cell.strip()]


def _candidate_id(name: str, options: Mapping[str, Any]) -> str:
    """The registered candidate id of a preset's options (`selection.md` § 4 naming)."""
    tol = f"t{options['tolerance']:.0e}".replace("e-0", "e-")
    if name == "split_polish":
        return f"sp|{options['base']}|{options['solver']}|r{options['rounds']}|{tol}"
    delta = f"d{options['delta_share']:.0e}".replace("e-0", "e-")
    return (
        f"ma|{options['base']}|{options['mode']}|k{options['activations']}|"
        f"top{options['top_k']}|{delta}"
    )


def example_selection() -> dict[str, Any]:
    """The selected presets and how they were chosen (contract §5.7, §5.12; WHI-1631)."""
    equal((T_ANALYSIS["stage"], len(T_ANALYSIS["case_ids"]), T_ANALYSIS["problems"]),
          ("T", 96, []), "selection stage T")  # fmt: skip
    pins = presets()
    out: dict[str, Any] = {}
    for name in R024_ADDITIONS:
        ident = T_ANALYSIS["identities"][name]
        winner = ident["winner"]
        equal(ident["outcome"], "selected", f"{name}: outcome")
        equal(_candidate_id(name, pins[name]["options"]), winner, f"{name}: preset = winner")
        candidates = [c for c, v in T_ANALYSIS["candidates"].items() if v["identity"] == name]
        cand = T_ANALYSIS["candidates"][winner]
        equal(cand["options"], pins[name]["options"], f"{name}: the winner's options")
        gain = cand["own_base_gain"]
        q = {c: float(Fraction(v)) for c, v in ident["q"].items()}
        figures = {
            "candidates": len(candidates),
            "eligible": len(ident["eligible"]),
            "ineligible": sorted(set(ident["ineligible"].values())),
            "ineligible_count": len(ident["ineligible"]),
            "ineligible_ids": sorted(ident["ineligible"]),
            "highest_q": max(q, key=lambda c: q[c]),
            "band": len(ident["band"]),
            "q_star": f"{float(Fraction(ident['q_star'])):+.4f}",
            "q_winner": f"{q[winner]:+.4f}",
            "margin": f"{float(Fraction(ident['margin'])):+.4f}",
            "rank_of_winner": ident["ranking"].index(winner) + 1,
            "own_base": {
                "base": cand["base"],
                "mean": f"{gain['mean_bps']:+.3f}",
                "hel": f"{gain['higher']}/{gain['equal']}/{gain['lower']}",
                "families": f"{gain['families']['net_win']} / {gain['families']['net_loss']}",
            },
        }
        cells = _cells(SELECTION_TABLES, f"| `{name}` | selected |")
        equal(
            [cells[2], cells[3], cells[4], cells[6], cells[7], cells[8]],
            [f"`{winner}`", figures["q_star"], figures["q_winner"], figures["margin"],
             str(figures["eligible"]), str(figures["band"])],
            f"{name}: selection tables.md outcome row",
        )  # fmt: skip
        sensitivity = {}
        stage_i = SELECTION_TABLES.split(f"### `{name}`: final outcome", 1)[1].split("\n### ")[0]
        for chunks in ("c100", "c200"):
            row = _cells(stage_i, f"| {chunks} |")
            sensitivity[chunks] = {"own_base": row[9], "hel": row[10], "base_arm": row[2]}
        figures["sensitivity"] = sensitivity
        out[name] = figures
    nominees = {  # the 0.2.3 nominees (R023-C/1 §4.7, §5.2) as registered candidates
        "split_polish": "sp|incremental_graph|brent|r2|t1e-4",
        "marginal_activation": "ma|incremental_graph|pf|k2|top3|d1e-4",
    }
    for name, nominee in nominees.items():
        ident = T_ANALYSIS["identities"][name]
        out[name]["nominee_rank"] = ident["ranking"].index(nominee) + 1
    return out


# ============================================================ 29. the campaign (research-024)


ANALYSIS = json.loads((CAMPAIGN / "report-analysis.json").read_text(encoding="utf-8"))
TABLES = (CAMPAIGN / "report-tables.md").read_text(encoding="utf-8")
TIMING = json.loads((CAMPAIGN / "timing-analysis.json").read_text(encoding="utf-8"))
TIMING_TABLES = (CAMPAIGN / "timing-tables.md").read_text(encoding="utf-8")
RESULTS = (R024 / "results.md").read_text(encoding="utf-8")


def f3(value: float) -> str:
    """The analysis tables' own rounding."""
    return f"{value:.3f}"


SECTIONS = {  # comparison kind -> its `report-tables.md` section
    "base_control": "### C9(c)",
    "attribution": "### C11 — E2 vs",
    "ranked": "### C9(a) — new identity vs every other row, rankable",
    "envelope": "### C9(b)",
}


def comparison(kind: str, candidate: str, baseline: str) -> dict[str, Any]:
    """One comparison of `report-analysis.json`, checked against its `report-tables.md` row."""
    (c,) = [
        x
        for x in ANALYSIS["comparisons"][kind]
        if x["candidate"] == candidate and x["baseline"] == baseline
    ]
    out = {
        "scheduled": c["scheduled"],
        "common_ok": c["common_ok"],
        "zero_baseline": c["common_ok"] - c["bps"]["n"],
        "scored": c["bps"]["n"],
        "hel": f"{c['higher']}/{c['equal']}/{c['lower']}",
        "mean": f3(c["bps"]["mean"]),
        "min": f3(c["bps"]["min"]),
        "p50": f3(c["bps"]["p50"]),
        "p95": f3(c["bps"]["p95"]),
        "max": f3(c["bps"]["max"]),
        "families": f"{c['families']['net_plus']} / {c['families']['net_minus']}",
    }
    equal(c["higher"] + c["equal"] + c["lower"], c["bps"]["n"], f"{candidate}: H/E/L = n")
    section = TABLES.split(SECTIONS[kind], 1)[1].split("\n### ")[0]
    cells = _cells(section, f"| {c['cohort']} | `{candidate}` | `{baseline}` |")
    equal(
        [cells[i] for i in (3, 4, 7, 8, 9, 10, 12, 13, 14)],
        [str(out["scheduled"]), str(out["common_ok"]), str(out["zero_baseline"]), out["hel"],
         out["mean"], out["min"], out["p50"], out["p95"], out["max"]],
        f"{candidate} vs {baseline}: report-tables.md row",
    )  # fmt: skip
    equal(
        cells[15],
        f"{c['families']['net_plus']}/{c['families']['net_minus']} of {c['families']['n']}",
        f"{candidate} vs {baseline}: families",
    )
    return out


def _signed(mean: str) -> str:
    return mean if mean.startswith("-") else f"+{mean}"


def _work(row: str) -> dict[str, int]:
    w = ANALYSIS["rows"][row]["work"]
    out = {k: w[k] for k in ("quotes", "cl_swap_steps", "lb_bins_swapped")}
    section = TABLES.split("### C8 / C13", 1)[1].split("\n### ")[0]
    cells = _cells(section, f"| `{row}` | ")
    equal(cells[1:4], [f"{v:,}" for v in out.values()], f"{row}: report-tables.md work row")
    return out


# (guide label, comparison kind, candidate, baseline) of the guide's 0.2.4 tables
SP, MA = "Q19-full/split_polish", "Q19-full/marginal_activation"
OWN_BASE = [
    ("`full_source`", "base_control", SP, "Q19-full/metis_inspired"),
    ("`sor_compatible`", "base_control", "Q19-sor/split_polish", "Q19-sor/metis_inspired"),
    ("`full_source`", "base_control", MA, "Q19-full/incremental_graph"),
    (
        "`sor_compatible`",
        "base_control",
        "Q19-sor/marginal_activation",
        "Q19-sor/incremental_graph",
    ),
]
ATTRIBUTION = [
    ("`E2-E1only` (E2's own E1 stage on its base)", "attribution", MA, "E2-E1only"),
    ("`E2-wm` (work-matched control)", "attribution", MA, "E2-wm"),
    ("`E2-cm` (call-matched control)", "attribution", MA, "E2-cm"),
]
OTHERS = {
    "split_polish": ["incremental_graph", "incremental_graph_repair", "metis_history",
                     "marginal_activation"],
    "marginal_activation": ["incremental_graph_repair", "metis_inspired", "metis_history",
                            "split_polish"],
}  # fmt: skip


def _own_row(label: str, c: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {c['scored']} / {c['scheduled']} | {c['hel']} | {_signed(c['mean'])} | "
        f"{c['p50']} | {c['p95']} | {c['families']} |"
    )


def example_campaign() -> dict[str, Any]:
    """The research-024 report-split figures the guide cites, its table rows, and the timing
    outcomes and §8.7 statements."""
    equal((ANALYSIS["stage"], ANALYSIS["problems"]), ("R", []), "report stage")
    dispositions = {k: v["disposition"] for k, v in ANALYSIS["dispositions"].items()}
    equal(dispositions, {n: "keep_experimental" for n in R024_ADDITIONS}, "§9.1 dispositions")
    check(
        all(not v["reject"] and not v["inconclusive"] for v in ANALYSIS["dispositions"].values()),
        "no reject or inconclusive reason",
    )
    statuses = {n: ANALYSIS["rows"][f"Q19-full/{n}"]["statuses"] for n in R024_ADDITIONS}
    equal(statuses, {n: {"no_route": 1, "ok": 301} for n in R024_ADDITIONS}, "301 ok, 1 no_route")
    branches = {
        n: ANALYSIS["rows"][f"Q19-full/{n}"]["disclosures"]["branches"] for n in R024_ADDITIONS
    }
    gates = {k: v for k, v in ANALYSIS["gates"].items()}
    check(all(not g["failures"] for g in gates.values()), "no gate failure")
    own = {f"{c} vs {b}": comparison(k, c, b) for _, k, c, b in OWN_BASE}
    attribution = {b: comparison(k, c, b) for _, k, c, b in ATTRIBUTION}
    others = {
        name: {o: comparison("ranked", f"Q19-full/{name}", f"Q19-full/{o}") for o in rows}
        for name, rows in OTHERS.items()
    }
    envelope = {}
    for name in R024_ADDITIONS:
        (c,) = [x for x in ANALYSIS["comparisons"]["envelope"]
                if x["candidate"] == f"Q19-full/{name}"]  # fmt: skip
        envelope[name] = {
            "equal": c["equal"],
            "scored": c["bps"]["n"],
            "mean": f3(c["bps"]["mean"]),
        }
        section = TABLES.split(SECTIONS["envelope"], 1)[1].split("\n### ")[0]
        cells = _cells(section, f"| full | `Q19-full/{name}` | `envelope(")
        equal((cells[8], cells[9]), (f"0/{c['equal']}/{c['lower']}", f3(c["bps"]["mean"])),
              f"{name}: envelope row")  # fmt: skip
    work = {
        "metis_inspired": _work("Q19-full/metis_inspired"),
        "split_polish": _work(SP),
        "incremental_graph": _work("Q19-full/incremental_graph"),
        "E2-E1only": _work("E2-E1only"),
        "marginal_activation": _work(MA),
    }
    ratios = {
        name: {k: f3(work[name][k] / work[base][k]) for k in work[name]}
        for name, base in (("split_polish", "metis_inspired"),
                           ("marginal_activation", "incremental_graph"),
                           ("E2-E1only", "incremental_graph"))
    }  # fmt: skip
    controls = {
        k: {
            "classes": dict(v["classes"]),
            "defects": v["defects"],
            "audits": dict(v["matched_audits"]),
            "charged": v["charged_quotes"]["sum"],
            "embedded_uncharged": v["embedded_uncharged_activation_quotes"]["sum_of_present"],
        }
        for k, v in ANALYSIS["controls"].items()
    }
    determinism = ANALYSIS["determinism_vs_0_2_2"]
    cells_checked = sum(v["cells"] for v in determinism.values())
    differing = sum(v["differing"] for v in determinism.values())
    # timing: the units' outcomes and the two statements only (contract §8.7)
    outcomes = {u: v["outcome"]["outcome"] for u, v in TIMING["units"].items()}
    statements = dict(TIMING["statements"])
    equal(statements["usable_latency_obtained"], ["U6", "UQ"], "§8.7 (b) units")
    equal(statements["measurement_attempted_correctly"], True, "§8.7 (a)")
    equal(outcomes["UP"], "inconclusive_cap_exhausted", "UP (post-processor overhead)")
    check("(b) usable latency obtained (units with outcome `valid`): **U6, UQ**" in TIMING_TABLES,
          "timing-tables.md statement (b)")  # fmt: skip
    for unit, outcome in outcomes.items():
        check(f"| {unit} | outcome | {outcome} |" in TIMING_TABLES, f"{unit}: timing-tables.md")
    rows = {
        "own_base": [_own_row(label, own[f"{c} vs {b}"]) for label, _, c, b in OWN_BASE],
        "attribution": [_own_row(label, attribution[b]) for label, _, _, b in ATTRIBUTION],
    }
    return {
        "dispositions": dispositions,
        "statuses": statuses,
        "branches": branches,
        "own_base": own,
        "attribution": attribution,
        "others": others,
        "envelope": envelope,
        "work": work,
        "ratios": ratios,
        "controls": controls,
        "determinism": {"cells": cells_checked, "differing": differing},
        "timing": {"outcomes": outcomes, "statements": statements},
        "guide_rows": rows,
    }


def statement_b() -> str:
    """Statement (b) of `results.md` §4.3, verbatim (whitespace normalised)."""
    section = RESULTS.split("### 4.3 The two statements (§8.7)", 1)[1].split("\n## ", 1)[0]
    (para,) = [p for p in section.split("\n\n") if p.startswith("**(b) Usable latency obtained")]
    return " ".join(para.split())


# ============================================================ output

EXAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "walkthrough": example_walkthrough,
    "selection": example_selection,
    "campaign": example_campaign,
}


def collect() -> dict[str, Any]:
    """Every example as plain data (each has already checked its independent expectations)."""
    return {name: fn() for name, fn in EXAMPLES.items()}


def _p(*parts: Any) -> None:
    print("".join(str(p) for p in parts))


def _print(data: Mapping[str, Any]) -> None:
    w = data["walkthrough"]
    r = w["roster"]
    _p("--- 27. The 19-row walkthrough: the two 0.2.4 rows on the section 11 FIXTURE request ---")
    _p(
        f"--strategies all of daily_gross.yaml: {r['size']} rows ({r['frozen_size']} of "
        f"0.2.2/0.2.3 + {r['last_two']}); presets "
        f"{[(p['key'], p['sha256'][:12]) for p in w['presets'].values()]}; equal options "
        "outside `all` stay {kind: override}"
    )
    rows = w["rows"]
    for name in R024_ADDITIONS:
        row = rows[name]
        _p(
            f"  {name:20s} {row['score']} raw (+{row['gain_raw']} over its base), "
            f"{row['quotes_counted']} quotes ({row['base_quotes']} base + {row['own_quotes']}), "
            f"legs {row['legs']}; factory counters {row['factory_counters']}"
        )
    m = rows["marginal_activation"]
    _p(f"  E2 delta {m['delta']}; terminals {m['terminals']}; 0.1-USDC grid {rows['grid_oracle']}")
    _p("  (one exploratory request under daily_gross.yaml on the 19-pool fixture) [OK]\n")
    s = data["selection"]
    _p("--- 28. The selected presets (research-024/selection, tuning split, under P*) ---")
    for name, f in s.items():
        _p(f"  {name}: {f}")
    _p("  [OK]\n")
    c = data["campaign"]
    _p("--- 29. research-024 report-split figures (report-analysis.json) and timing outcomes ---")
    _p(f"dispositions {c['dispositions']}; work ratios {c['ratios']}")
    for key in ("own_base", "attribution"):
        for row in c["guide_rows"][key]:
            _p(f"  {row}")
    _p(f"  timing outcomes {c['timing']['outcomes']}; statements {c['timing']['statements']} [OK]")
    _p("All R024 checks passed (independent expectations; no timing claimed)")


def run_all() -> dict[str, Any]:
    data = collect()
    _print(data)
    return data


if __name__ == "__main__":
    run_all()
