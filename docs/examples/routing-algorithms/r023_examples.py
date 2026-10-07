"""Executable worked examples for the 0.2.3 post-processors of the routing guide (WHI-1628).

`split_polish` (E1, guide §23) and `marginal_activation` (E2, guide §24), contract `R023-C/1`.
Every example calls the REAL runtime code (the registered factories, or the module seams
`split_polish.polish_plan` / `marginal_activation.run_activation` that the factories run), replays
every emitted plan with a FRESH `routing.evaluator.evaluate`, and checks each published number
against an expectation that does not come from the code under test:

- this module's own fee-free constant-product formula and exhaustive integer split oracles for
  the two teaching fixtures (§23.5, §24.5);
- the exact protocol quote seam (`pools.quote.quote_exact_in`) and the `r021_examples` hand quote
  for the real-state legs of the §11.5 request, and the §11.1 base row;
- for the campaign figures (§23.9, §24.9), the committed `research-023/campaign/
  report-analysis.json`, each figure also found in its row of the byte-identically regenerated
  `report-tables.md` (so neither a different source nor a different rounding can pass).

Counters that only the factory can report (simulations, quotes, invocations, the activation log)
are published as *factory counters*: regression-pinned outputs, not independently derived
teaching numbers. **No timing is asserted or claimed.**

Run: `uv run python docs/examples/routing-algorithms/r023_examples.py` (also called by
`run_examples.py`, sections 23-26). `collect()` returns every example as plain JSON-shaped data.
Offline: tracked files only, no RPC, Dune, credentials or `data/`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _path in (ROOT, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import r021_examples as r21  # noqa: E402  (the shared independent toolkit)
import yaml  # noqa: E402

from benchmark.objective import gross_only  # noqa: E402
from benchmark.profile import strategy_group  # noqa: E402
from benchmark.strategies import R021_ADDITIONS, R022_ADDITIONS, derive  # noqa: E402
from pools.quote import metered_quotes, quote_exact_in  # noqa: E402
from pools.result import QuoteStatus  # noqa: E402
from routing.algorithms import marginal_activation as ma  # noqa: E402
from routing.algorithms import split_polish as sp  # noqa: E402
from routing.algorithms.base import SolveContext, SolveStatus  # noqa: E402
from routing.algorithms.registry import ALGORITHMS  # noqa: E402
from routing.evaluator import EvalStatus, Evaluation, evaluate  # noqa: E402
from routing.plan import REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle  # noqa: E402

ExampleError = r21.ExampleError
check = r21.check
equal = r21.equal

CORPUS = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
R023 = ROOT / "docs" / "references" / "research-023"
CAMPAIGN = R023 / "campaign"
O = gross_only()  # noqa: E741

# the nominees of contract §4.7 / §5.2, written out (the guide's §23.7 / §24.7)
E1_NOMINEE: dict[str, Any] = {
    "base": "incremental_graph",
    "solver": "brent",
    "rounds": 2,
    "tolerance": 0.0001,
    "grid": 1_000_000_000,
    "maxiter": 60,
}
E2_NOMINEE: dict[str, Any] = {
    **E1_NOMINEE,
    "mode": "pf",
    "activations": 2,
    "top_k": 3,
    "delta_share": 0.0001,
    "seed_share": 0.0001,
    "arm": "treatment",
}
R023_IDS = ("split_polish", "marginal_activation")


def fee_free_out(reserve_in: int, reserve_out: int, amount: int) -> int:
    """This module's own exact-input fee-free constant-product quote."""
    return reserve_out * amount // (reserve_in + amount) if amount > 0 else 0


def step(pid: str, fund: str, amount: Any, out: str) -> SwapStep:
    return SwapStep(pid, "S", "T", (FundInput(fund, amount),), out)


def fixture(case: Case, pools: Mapping[str, tuple[int, int]]) -> SnapshotBundle:
    return r21.bundle_of(
        "r023", *(r21.cp(p, "S", "T", x, y, 0) for p, (x, y) in pools.items()), cases=(case,)
    )


def ok_eval(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> Evaluation:
    ev = evaluate(bundle, case, plan, O)
    check(ev.status is EvalStatus.OK, f"plan does not replay ok: {ev.error}")
    return ev


def legs(plan: RoutePlan, ev: Evaluation) -> list[list[Any]]:
    return [
        [s.pool_id, t.amount_in, t.amount_out] for s, t in zip(plan.steps, ev.trace, strict=True)
    ]


# ====================================================================== 23. split_polish (§23.5)

F7_CASE = Case("retain", "S", "T", 300)
F7_POOLS = {"a": (1000, 2000), "b": (1000, 100), "c": (1000, 100)}
F7_PLAN = RoutePlan(tuple(step(p, REQUEST_FUND_ID, 100, p.upper()) for p in F7_POOLS))


def _f7_oracle() -> tuple[int, list[tuple[int, int, int]]]:
    """Every integer allocation of 300 over `a`, `b`, `c`: the best gross and its argmax."""
    best, where = -1, []
    for x in range(301):
        for y in range(301 - x):
            gross = sum(
                fee_free_out(*F7_POOLS[p], v)
                for p, v in zip("abc", (x, y, 300 - x - y), strict=True)
            )
            if gross > best:
                best, where = gross, [(x, y, 300 - x - y)]
            elif gross == best:
                where.append((x, y, 300 - x - y))
    return best, where


def example_split_polish() -> dict[str, Any]:
    """§23.5: the F7 fixture, base 100 / 100 / 100, under the nominee (Brent) and golden."""
    bundle = fixture(F7_CASE, F7_POOLS)
    ev = ok_eval(bundle, F7_CASE, F7_PLAN)
    hand = [fee_free_out(*F7_POOLS[p], 100) for p in F7_POOLS]
    equal([t.amount_out for t in ev.trace], hand, "§23.5 base outputs vs the hand formula")
    equal(sum(hand), ev.gross_output, "§23.5 base gross")
    oracle, argmax = _f7_oracle()
    solvers: dict[str, Any] = {}
    for solver in sp.SOLVERS:
        settings = sp.Settings.from_options({**E1_NOMINEE, "solver": solver})
        published: list[RoutePlan] = []
        ledger = sp.Ledger(None)
        outcome = sp.polish_plan(bundle, F7_CASE, ev, ledger, settings, published.append)
        sequence = []
        for plan in published:
            replay = ok_eval(bundle, F7_CASE, plan)
            row = legs(plan, replay)
            equal(
                [o for _, _, o in row],
                [fee_free_out(*F7_POOLS[p], a) for p, a, _ in row],
                f"§23.5 {solver}: accepted plan outputs vs the hand formula",
            )
            sequence.append({"legs": row, "gross": replay.gross_output})
        check(outcome.incumbent is not None, "§23.5: an incumbent exists")
        assert outcome.incumbent is not None
        equal(outcome.incumbent.gross, oracle, f"§23.5 {solver}: final gross vs the oracle")
        solvers[solver] = {
            "sequence": sequence,
            "factory_counters": {
                "simulations": outcome.work["simulations"],
                "quotes": ledger.used,
                "accepted": outcome.work["accepted"],
                "polish_calls": outcome.work["polish_calls"],
            },
        }
    equal(solvers["brent"]["sequence"], solvers["golden"]["sequence"], "§23.5 same result")
    caps = {}
    golden = sp.Settings.from_options({**E1_NOMINEE, "solver": "golden"})
    for cap in (50, 80, 140):
        ledger = sp.Ledger(cap)
        outcome = sp.polish_plan(bundle, F7_CASE, ev, ledger, golden)
        final = ev.gross_output if outcome.incumbent is None else outcome.incumbent.gross
        replay = ok_eval(
            bundle, F7_CASE, F7_PLAN if outcome.incumbent is None else outcome.incumbent.plan
        )
        equal(replay.gross_output, final, f"§23.5 cap {cap}: replay")
        check(ledger.used <= cap and outcome.truncated_by == "max_quotes", f"cap {cap} truncates")
        caps[cap] = final
    equal(
        list(caps.values()),
        [ev.gross_output, *(s["gross"] for s in solvers["golden"]["sequence"])],
        "§23.5 retention: each cap keeps the last accepted plan",
    )
    return {
        "request": 300,
        "pools": {p: list(r) for p, r in F7_POOLS.items()},
        "base": {"outputs": hand, "gross": ev.gross_output},
        "oracle": {"gross": oracle, "argmax": [list(a) for a in argmax]},
        "nominee": dict(E1_NOMINEE),
        "solvers": solvers,
        "caps_golden": caps,
    }


# ====================================================================== 24. marginal_activation


E24_CASE = Case("doc", "S", "T", 10**6)
E24_POOLS = {"p": (10**7, 10**7), "q": (10**7, 10**7)}
E24_PLAN = RoutePlan((step("p", REQUEST_FUND_ID, 10**6, "P"),))


def _incumbent(bundle: SnapshotBundle, case: Case, ev: Evaluation) -> sp.Incumbent:
    canon, _ = sp.canonical_from_evaluation(ev)
    incumbent = sp.Incumbent(canon, ev.gross_output, None, None)  # type: ignore[arg-type]
    sp.rebuild(incumbent, case)
    return incumbent


def _activation_run(bundle: SnapshotBundle, mode: str) -> dict[str, Any]:
    """E1 then the activation stage on one ledger (the factory's own sequence), with the
    label search's terminals recorded."""
    settings = ma.Settings.from_options({**E2_NOMINEE, "mode": mode}, 3)
    ev = ok_eval(bundle, E24_CASE, E24_PLAN)
    ledger = sp.Ledger(None)
    published: list[RoutePlan] = []
    terminals: list[list[Any]] = []
    real = ma.top_paths

    def spy(*args: Any) -> Any:
        found = real(*args)
        terminals.append([[amount, [e[0] for e in path]] for amount, path in found])
        return found

    ma.top_paths = spy  # type: ignore[assignment]
    try:
        e1 = sp.polish_plan(bundle, E24_CASE, ev, ledger, settings.polish, published.append)
        e1_quotes = ledger.used
        assert e1.incumbent is not None and e1.truncated_by is None
        e1_gross = e1.incumbent.gross
        activation = ma.run_activation(
            bundle, E24_CASE, e1.incumbent, ledger, settings, ma.adjacency(bundle), O
        )
    finally:
        ma.top_paths = real
    final = e1.incumbent.plan
    replay = ok_eval(bundle, E24_CASE, final)
    equal(replay.gross_output, e1.incumbent.gross, f"§24.5 {mode}: fresh replay")
    equal(published, [final], f"§24.5 {mode}: the accepted plan is published once")
    return {
        "e1": {"gross": e1_gross, "quotes": e1_quotes, "accepted": e1.work.get("accepted", 0)},
        "terminals": terminals,
        "log": activation["log"],
        "legs": legs(final, replay),
        "gross": replay.gross_output,
        "factory_counters": {
            "activation_quotes": activation["quotes"],
            "simulations": activation["work"]["simulations"],
            "invocations": activation["invocations"],
        },
    }


def example_marginal_activation() -> dict[str, Any]:
    """§24.5: two fee-free CPMM pools, the base plan all through `p`."""
    bundle = fixture(E24_CASE, E24_POOLS)
    amount = E24_CASE.amount_in
    base = fee_free_out(10**7, 10**7, amount)
    equal(ok_eval(bundle, E24_CASE, E24_PLAN).gross_output, base, "§24.5 base gross")
    delta = max(1, amount // 10**4)
    p_after = (10**7 + amount, 10**7 - base)
    proposals = {"p": fee_free_out(*p_after, delta), "q": fee_free_out(10**7, 10**7, delta)}
    seed_p = amount * 9999 // 10**4
    seed = fee_free_out(10**7, 10**7, seed_p) + fee_free_out(10**7, 10**7, amount - seed_p)
    best = max(
        fee_free_out(10**7, 10**7, x) + fee_free_out(10**7, 10**7, amount - x)
        for x in range(amount + 1)
    )
    half = fee_free_out(10**7, 10**7, amount // 2)
    equal(2 * half, best, "§24.5 the exhaustive oracle's best split is the half split")
    after_half = (10**7 + amount // 2, 10**7 - half)
    second = fee_free_out(*after_half, delta)
    runs = {mode: _activation_run(bundle, mode) for mode in ma.MODES}
    for mode, run in runs.items():
        equal(run["e1"]["gross"], base, f"§24.5 {mode}: E1 has nothing to split")
        equal(run["e1"]["quotes"], 1, f"§24.5 {mode}: E1's only quote is the reconstruction")
        equal(
            run["terminals"],
            [
                [[proposals["q"], ["q"]], [proposals["p"], ["p"]]],
                [[second, ["p"]], [second, ["q"]]],
            ],
            f"§24.5 {mode}: label-search terminals vs the hand formula",
        )
        equal(
            run["legs"],
            [["p", amount // 2, half], ["q", amount // 2, half]],
            f"§24.5 {mode}: final plan",
        )
        equal(run["gross"], best, f"§24.5 {mode}: final gross vs the oracle")
        equal(
            run["log"][0],
            [0, 0, f"accepted novel_pools=1 reuse=none flow={amount // 2}"],
            f"§24.5 {mode}: iteration 1 accepted",
        )
        equal(run["log"][-1], [1, -1, "stop_no_candidate"], f"§24.5 {mode}: iteration 2 stops")
    # the controls from the same E1 incumbent (contract §5.3): E1 has nothing to split
    ev = ok_eval(bundle, E24_CASE, E24_PLAN)
    snapshot = ma.Snapshot.of(_incumbent(bundle, E24_CASE, ev), sp.Ledger(None, used=1))
    pf = runs["pf"]["factory_counters"]
    target = {"invocations": pf["invocations"], "quotes": pf["activation_quotes"]}
    controls = {}
    for kind in ("work_matched", "call_matched"):
        _, control = ma.run_control(
            bundle,
            E24_CASE,
            snapshot,
            kind,
            target,
            None,
            None,
            ma.Settings.from_options(E2_NOMINEE, 3).polish,
        )
        equal(int(control["gross"]), base, f"§24.5 {kind}: stays at the base")
        controls[kind] = {
            "gross": int(control["gross"]),
            "quotes": control["quotes"],
            "calls_started": control["calls_started"],
        }
    return {
        "request": amount,
        "pools": {p: list(r) for p, r in E24_POOLS.items()},
        "base_gross": base,
        "delta": delta,
        "post_plan_proposals": proposals,
        "seed": {"p": seed_p, "q": amount - seed_p, "gross": seed},
        "oracle_best": best,
        "half_output": half,
        "second_iteration_proposal": second,
        "runs": runs,
        "controls": controls,
        "control_target": target,
    }


# ====================================================================== 25. the §11.5 request


def usdt0(raw: int) -> str:
    """A raw USDT0 amount (6 decimals) as the guide's tables print it."""
    return f"{raw // 10**6}.{raw % 10**6:06d}"


def walkthrough_profile() -> dict[str, Any]:
    """The explicit profile of §11.5: `config/daily_gross.yaml`'s values with only `algorithms`
    replaced and the two nominees' `algorithm_options` added."""
    document = yaml.safe_load((ROOT / r21.SOURCE_PROFILE).read_text(encoding="utf-8"))
    document["algorithms"] = ["incremental_graph", *R023_IDS]
    document["algorithm_options"] = {
        "split_polish": dict(E1_NOMINEE),
        "marginal_activation": dict(E2_NOMINEE),
    }
    return dict(document)


def _walkthrough() -> dict[str, Any]:
    bundle = load_bundle(CORPUS)
    case = Case("real_usdc_usdt0_10k", r21.USDC, r21.USDT0, 10_000_000_000)
    raw = yaml.safe_dump(walkthrough_profile(), sort_keys=False).encode()
    _, profile = derive(
        yaml.safe_load(raw),
        "profile",
        source_path="r023-walkthrough.yaml",
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    equal(list(profile.algorithms), ["incremental_graph", *R023_IDS], "literal profile roster")
    pools = {p.pool_id[:6]: p for p in bundle.pools_for_pair(r21.USDC, r21.USDT0)}
    agni, lb, moe, dead = pools["0x36f6"], pools["0x368b"], pools["0x69a7"], pools["0x1bc3"]
    assert isinstance(moe, ConstantProductPoolState)  # the Moe Classic pool of §19.5
    base_legs = [
        quote_exact_in(agni, r21.USDC, 9_950_000_000).amount_out,
        quote_exact_in(lb, r21.USDC, 50_000_000).amount_out,
    ]
    terminals: list[list[Any]] = []
    real = ma.top_paths

    def spy(*args: Any) -> Any:
        found = real(*args)
        terminals.append([[amount, [e[0][:6] for e in path]] for amount, path in found])
        return found

    rows: dict[str, Any] = {}
    ma.top_paths = spy  # type: ignore[assignment]
    try:
        for name in profile.algorithms:
            factory = ALGORITHMS[name]
            config = profile.algorithm_config(factory)
            prepared = factory.prepare(bundle, config) if factory.prepare is not None else None
            with metered_quotes(None) as meter:
                result = factory.solve(
                    case, SolveContext(bundle, profile.objective, prepared), profile.budget
                )
            check(result.status is SolveStatus.OK and result.plan is not None, f"{name} ok")
            assert result.plan is not None
            replay = ok_eval(bundle, case, result.plan)
            equal(replay.gross_output, result.score, f"§11.5 {name}: fresh replay")
            rows[name] = {
                "score": result.score,
                "quotes_counted": meter.counted,
                "legs": [[p[:6], a, o] for p, a, o in legs(result.plan, replay)],
                "plan": r21.plan_view(result.plan),
                "stats": json.loads(json.dumps(result.search_stats.get(name), default=str)),
                "base": json.loads(json.dumps(result.search_stats.get("base"), default=str)),
            }
    finally:
        ma.top_paths = real
    equal(rows["incremental_graph"]["score"], sum(base_legs), "§11.5 base row = §11.1")
    equal(rows["incremental_graph"]["quotes_counted"], 264, "§11.5 base row quotes = §11.1")
    polished = rows["split_polish"]
    agni_in, lb_in = (leg[1] for leg in polished["legs"])
    equal(agni_in + lb_in, case.amount_in, "§11.5 the polished legs spend the input")
    exact = [quote_exact_in(agni, r21.USDC, agni_in), quote_exact_in(lb, r21.USDC, lb_in)]
    equal(
        [leg[2] for leg in polished["legs"]],
        [r.amount_out for r in exact],
        "§11.5 polished legs vs exact protocol quotes",
    )
    equal(polished["score"], sum(r.amount_out for r in exact), "§11.5 polished gross")
    # an independent oracle: every split of the same two pools on a 0.1-USDC grid of the LB leg
    grid = max(
        (
            quote_exact_in(agni, r21.USDC, case.amount_in - x).amount_out
            + (quote_exact_in(lb, r21.USDC, x).amount_out if x else 0),
            x,
        )
        for x in range(0, 50_000_001, 100_000)
    )
    check(polished["score"] >= grid[0], "§11.5 E1 is at least the best 0.1-USDC grid split")
    equal(rows["marginal_activation"]["legs"], polished["legs"], "§11.5 E2 keeps the E1 plan")
    for name in R023_IDS:
        equal(rows[name]["base"]["quotes"], 264, f"§11.5 {name}: base row quotes")
        equal(rows[name]["stats"]["base_gross"], str(sum(base_legs)), f"§11.5 {name}: base gross")
    e1 = polished["stats"]
    e2 = rows["marginal_activation"]["stats"]
    equal(e2["e1"]["quotes"], e1["quotes"], "§11.5 E2's E1 stage is E1")
    equal(rows["split_polish"]["quotes_counted"], 264 + e1["quotes"], "§11.5 one E1 ledger")
    equal(
        rows["marginal_activation"]["quotes_counted"],
        264 + e2["e1"]["quotes"] + e2["activation"]["quotes"],
        "§11.5 one E2 ledger",
    )
    # the activation proposals: delta quotes on the post-plan states, recomputed here
    delta = max(1, case.amount_in // 10**4)
    agni_post, lb_post = exact[0].new_state, exact[1].new_state
    assert agni_post is not None and lb_post is not None
    want = [
        [quote_exact_in(agni_post, r21.USDC, delta).amount_out, ["0x36f6"]],
        [quote_exact_in(lb_post, r21.USDC, delta).amount_out, ["0x368b"]],
        [r21.hand_out(moe, r21.USDC, delta), ["0x69a7"]],
    ]
    equal(terminals, [want], "§11.5 E2 terminals vs exact quotes on the post-plan states")
    dead_status = quote_exact_in(dead, r21.USDC, delta).status
    check(dead_status is not QuoteStatus.OK, "§11.5 the fourth direct pool fails at delta")
    gain = polished["score"] - rows["incremental_graph"]["score"]
    return {
        "request": {"token_in": "USDC", "token_out": "USDT0", "amount_in": case.amount_in},
        "bundle": {"block": bundle.block.number, "pools": len(bundle.pools)},
        "profile": walkthrough_profile(),
        "rows": {
            n: {
                "score": r["score"],
                "usdt0": usdt0(r["score"]),
                "quotes_counted": r["quotes_counted"],
                "legs": r["legs"],
            }
            for n, r in rows.items()
        },
        "plan_split_polish": polished["plan"],
        "base_legs": base_legs,
        "gain_raw": gain,
        "gain_bps_4dp": f"{gain * 10**4 / rows['incremental_graph']['score']:.4f}",
        "moved_raw": 50_000_000 - lb_in,
        "grid_oracle": {
            "step_usdc": "0.1",
            "gross": grid[0],
            "lb_in": grid[1],
            "below_e1": polished["score"] - grid[0],
        },
        "e1_counters": {k: e1[k] for k in ("quotes", "scope", "truncated_by")}
        | {k: e1["work"][k] for k in ("polish_calls", "simulations", "accepted")}
        | {"brent_calls": sum(e1["work"]["brent_status"].values())},
        "e2_counters": {
            "e1_quotes": e2["e1"]["quotes"],
            "activation_quotes": e2["activation"]["quotes"],
            "simulations": e2["activation"]["work"]["simulations"],
            "invocations": e2["activation"]["invocations"],
            "log": e2["activation"]["log"],
            "not_reached": e2["not_reached"],
        },
        "delta": delta,
        "terminals": terminals[0],
        "moe_output_reserve": moe.reserve1 if moe.token1 == r21.USDT0 else moe.reserve0,
        "dead_pool_status": dead_status.value,
    }


def example_walkthrough() -> dict[str, Any]:
    """§11.5 plus the roster facts of §13: `all` is still the 17 rows, neither 0.2.3 identity is
    in it, and the frozen 0.2.1 / 0.2.2 campaign rosters are untouched."""
    _document, profile, _sha = r21.all_profile()
    roster = list(profile.algorithms)
    equal(len(roster), 17, "`--strategies all` of daily_gross.yaml")
    check(not set(R023_IDS) & set(roster), "`all` adds neither 0.2.3 identity")
    check(not set(R023_IDS) & {*R021_ADDITIONS, *R022_ADDITIONS}, "no 0.2.3 roster addition")
    equal([strategy_group(n) for n in R023_IDS], ["custom"] * 2, "0.2.3 identities are `custom`")
    schedule = yaml.safe_load((ROOT / "config/research_022/schedule.yaml").read_text())
    equal(list(schedule["rosters"]["all17"]), roster, "research-022 frozen all17 roster")
    frozen = [
        p
        for d in ("tools/research_021", "tools/research_022")
        for p in sorted((ROOT / d).glob("*.py"))
    ]
    check(bool(frozen), "the frozen campaign tools exist")
    for path in frozen:
        text = path.read_text(encoding="utf-8")
        check(not any(n in text for n in R023_IDS), f"{path.name} names no 0.2.3 identity")
    return {"all_roster_size": len(roster), "walkthrough": _walkthrough()}


# ====================================================================== 26. campaign figures


ANALYSIS = json.loads((CAMPAIGN / "report-analysis.json").read_text(encoding="utf-8"))
TABLES = (CAMPAIGN / "report-tables.md").read_text(encoding="utf-8")


def f3(value: float) -> str:
    """The analysis tables' own rounding (`r023_analysis._f`)."""
    return f"{value:.3f}"


def comparison(candidate: str, baseline: str) -> dict[str, Any]:
    """One comparison of `report-analysis.json`, each figure checked against its regenerated
    `report-tables.md` row."""
    (c,) = [
        x
        for x in ANALYSIS["comparisons"]
        if x["candidate"] == candidate and x["baseline"] == baseline
    ]
    out = {
        "question": c["question"],
        "scheduled": c["scheduled"],
        "common_ok": c["common_ok"],
        "zero_baseline": len(c["zero_baseline_na"]),
        "scored": c["higher"] + c["equal"] + c["lower"],
        "hel": f"{c['higher']}/{c['equal']}/{c['lower']}",
        "mean": f3(c["bps"]["mean"]),
        "p5": f3(c["bps"]["p5"]),
        "p50": f3(c["bps"]["p50"]),
        "p95": f3(c["bps"]["p95"]),
        "families": f"{c['families']['net_win']} / {c['families']['net_loss']}",
        "work_p50": f3(c["work_ratio"]["p50"]),
        "transitions": dict(c["transitions"]),
    }
    equal(out["scored"], c["bps"]["n"], f"{candidate} vs {baseline}: bps denominator")
    equal(out["scored"], out["common_ok"] - out["zero_baseline"], f"{candidate}: scored cells")
    (row,) = [
        line
        for line in TABLES.splitlines()
        if line.startswith(f"| `{candidate}` vs `{baseline}` |")
    ]
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    equal(
        cells[4:9],
        [out["hel"], out["mean"], out["p5"], out["p50"], out["p95"]],
        f"{candidate} vs {baseline}: report-tables.md row",
    )
    equal(
        cells[9],
        f"{c['families']['net_win']}/{c['families']['net_loss']} of 32",
        f"{candidate} vs {baseline}: families",
    )
    return out


# (guide label, candidate arm, baseline arm) of the guide's tables
Q1 = [
    ("A0 (`incremental_graph`, chunks 50)", "E1b-A0", "A0"),
    ("C100 (`incremental_graph`, chunks 100)", "E1b-C100", "C100"),
    ("M4 (`metis_inspired`)", "E1b-M4", "M4"),
    ("S4 (`metis_history`)", "E1b-S4", "S4"),
    ("REP (`incremental_graph_repair`)", "E1b-REP", "REP"),
    ("PS (`path_split`)", "E1b-PS", "PS"),
]
Q2 = [
    ("`split_polish(A0)` vs C100", "E1b-A0", "C100"),
    ("`split_polish(A0)` vs C200", "E1b-A0", "C200"),
    ("golden E1 (A0) vs C100", "E1g-A0", "C100"),
    ("golden E1 (A0) vs C200", "E1g-A0", "C200"),
    ("Brent E1 (A0) vs golden E1 (A0) (Q5)", "E1b-A0", "E1g-A0"),
]
Q3 = [
    ("PF (A0) vs work-matched", "E2pf-A0", "E2pf-A0-wm"),
    ("PF (A0) vs call-matched", "E2pf-A0", "E2pf-A0-cm"),
    ("full (A0) vs work-matched", "E2full-A0", "E2full-A0-wm"),
    ("full (A0) vs call-matched", "E2full-A0", "E2full-A0-cm"),
    ("PF (C100) vs work-matched", "E2pf-C100", "E2pf-C100-wm"),
    ("PF (C100) vs call-matched", "E2pf-C100", "E2pf-C100-cm"),
]
Q4_TREATMENTS = [("PF (A0)", "E2pf-A0"), ("PF (C100)", "E2pf-C100"), ("full (A0)", "E2full-A0")]
Q4_REFERENCES = ["C100", "C200", "M4", "S4", "REP"]


def _row(label: str, c: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {c['scored']} / {c['scheduled']} | {c['hel']} | {c['mean']} | "
        f"{c['p50']} | {c['p95']} | {c['families']} |"
    )


def _q4_cell(c: Mapping[str, Any]) -> str:
    sign = "" if c["mean"].startswith("-") else "+"
    return f"{c['hel']}, {sign}{c['mean']}"


def example_campaign() -> dict[str, Any]:
    """The research-023 report-split figures the guide cites, and the guide's table rows."""
    equal(
        (ANALYSIS["stage"], ANALYSIS["scheduled_cases"], ANALYSIS["problems"]),
        ("R", 302, []),
        "report stage",
    )
    statuses = {a: s["statuses"] for a, s in ANALYSIS["statuses"].items()}
    equal(len(statuses), 23, "23 arms")
    check(all(s == {"no_route": 1, "ok": 301} for s in statuses.values()), "301 ok + 1 no_route")
    dispositions = {k: v["disposition"] for k, v in ANALYSIS["dispositions"].items()}
    equal(
        dispositions,
        {"marginal_activation": "keep_experimental", "split_polish": "keep_experimental"},
        "§10 dispositions",
    )
    check(
        all(
            not v["reject"] and not v["inconclusive"] for v in ANALYSIS["arm_dispositions"].values()
        ),
        "no arm has a reject or inconclusive reason",
    )
    base_rows = ANALYSIS["base_rows"]
    equal(len(base_rows), 16, "16 E1/E2 arms")
    check(
        all(r["compared"] == 302 and not r["differences"] for r in base_rows.values()),
        "every base row equals its reference record",
    )
    gates = ANALYSIS["gates"]
    check(all(not any(g["failures"].values()) for g in gates.values()), "no gate failure")
    identity = ANALYSIS["identity"]
    truncated = {a: dict(i["truncated_by"]) for a, i in identity.items() if i["truncated_by"]}
    not_reached = {a: i["not_reached"] for a, i in identity.items() if i["not_reached"]}
    controls = ANALYSIS["controls"]
    check(
        all(
            not c["over_work_target"]
            and not c["call_count_mismatch"]
            and not c["embedded_activation_differs"]
            and not c["target_differs_from_treatment"]
            for c in controls.values()
        ),
        "every control is matched",
    )
    q1 = {cand: comparison(cand, base) for _, cand, base in Q1}
    for _, cand, _ in Q1:
        equal(identity[cand]["improved"], int(q1[cand]["hel"].split("/")[0]), f"{cand} improved")
        equal(q1[cand]["hel"].split("/")[2], "0", f"{cand}: never below its base")
    q2 = {f"{c} vs {b}": comparison(c, b) for _, c, b in Q2}
    q3 = {f"{c} vs {b}": comparison(c, b) for _, c, b in Q3}
    q4 = {t: {r: comparison(t, r) for r in Q4_REFERENCES} for _, t in Q4_TREATMENTS}
    own_c100 = comparison("E2pf-C100", "C100")
    q5 = ANALYSIS["q5"]
    physical = {
        a: {k: f3(v) for k, v in ANALYSIS["physical"][a]["vs_base"].items()}
        for a in ("E1b-A0", "E2pf-A0", "E2full-A0")
    }
    for arm, ratios in physical.items():
        (row,) = [line for line in TABLES.splitlines() if line.startswith(f"| `{arm}` | 302 |")]
        equal(
            row.split("|")[6:9],
            [f" {ratios[k]} " for k in ("quotes_executed", "cl_swap_steps", "lb_bins_swapped")],
            f"{arm} work pass",
        )
    verdicts = identity["E2pf-A0"]["activation"]
    gains = {c: f3(v["gain_over_e1_bps"]["mean"]) for c, v in controls.items()}
    rows = {
        "q1": [_row(label, q1[cand]) for label, cand, _ in Q1],
        "q2": [_row(label, q2[f"{c} vs {b}"]) for label, c, b in Q2],
        "q3": [_row(label, q3[f"{c} vs {b}"]) for label, c, b in Q3],
        "q4": [
            "| " + " | ".join([label, *(_q4_cell(q4[t][r]) for r in Q4_REFERENCES)]) + " |"
            for label, t in Q4_TREATMENTS
        ],
    }
    return {
        "arms": len(statuses),
        "cases": ANALYSIS["scheduled_cases"],
        "dispositions": dispositions,
        "truncated_by": truncated,
        "not_reached": not_reached,
        "q1": q1,
        "q2": q2,
        "q3": q3,
        "q4": q4,
        "e2pf_c100_vs_own_base": own_c100,
        "q5": {
            "golden_over_brent_p50": f3(q5["golden_over_brent"]["p50"]),
            "golden_over_brent_p95": f3(q5["golden_over_brent"]["p95"]),
            "polish_quotes": dict(q5["polish_quotes"]),
        },
        "physical": physical,
        "control_gain_over_e1": gains,
        "pf_a0_activation": {
            "cases_activated": verdicts["cases_activated"],
            **{
                k: verdicts["verdicts"][k]
                for k in (
                    "accepted",
                    "dag_cycle",
                    "no_gain_or_zero_flow",
                    "stop_no_candidate",
                    "budget_before_validation",
                )
            },
        },
        "guide_rows": rows,
    }


# ====================================================================== output

EXAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "split_polish": example_split_polish,
    "marginal_activation": example_marginal_activation,
    "walkthrough": example_walkthrough,
    "campaign": example_campaign,
}


def collect() -> dict[str, Any]:
    """Every example as plain data (each has already checked its independent expectations)."""
    return {name: fn() for name, fn in EXAMPLES.items()}


def _p(*parts: Any) -> None:
    print("".join(str(p) for p in parts))


def _print(data: Mapping[str, Any]) -> None:
    s = data["split_polish"]
    _p("--- 23. split_polish (E1): the F7 worked example (§23.5) ---")
    _p(
        f"300 S->T over a/b/c {s['pools']}, base 100/100/100 -> {s['base']['outputs']} = "
        f"{s['base']['gross']}; exhaustive oracle {s['oracle']}"
    )
    for solver, row in s["solvers"].items():
        _p(
            f"  {solver}: accepted {[x['legs'] for x in row['sequence']]} -> "
            f"{[x['gross'] for x in row['sequence']]}; factory counters {row['factory_counters']}"
        )
    _p(f"  golden under a quote cap: {s['caps_golden']} [OK]\n")
    m = data["marginal_activation"]
    _p("--- 24. marginal_activation (E2): the worked example (§24.5) ---")
    _p(
        f"10**6 S->T, p and q at 10**7/10**7, base all through p = {m['base_gross']}; delta "
        f"{m['delta']}; post-plan proposals {m['post_plan_proposals']}; seed {m['seed']}"
    )
    for mode, run in m["runs"].items():
        _p(
            f"  {mode}: terminals {run['terminals']}, log {run['log']}, final {run['legs']} = "
            f"{run['gross']} (oracle {m['oracle_best']}); factory counters "
            f"{run['factory_counters']}"
        )
    _p(f"  controls from the E1 incumbent: {m['controls']} [OK]\n")
    w = data["walkthrough"]["walkthrough"]
    _p("--- 25. The section 11.5 request through an explicit profile (10 000 USDC -> USDT0) ---")
    for name, row in w["rows"].items():
        _p(f"  {name:20s} {row['score']} raw, {row['quotes_counted']} quotes, legs {row['legs']}")
    _p(
        f"  gain over the base row {w['gain_raw']} raw ({w['gain_bps_4dp']} bps), "
        f"{w['moved_raw']} raw moved from the LB leg to Agni V3"
    )
    _p(f"  E1 counters {w['e1_counters']}")
    _p(f"  E2 counters {w['e2_counters']}; delta {w['delta']}; terminals {w['terminals']}")
    _p(
        f"  --strategies all: {data['walkthrough']['all_roster_size']} rows, neither 0.2.3 "
        "identity; frozen 0.2.1 / 0.2.2 rosters untouched [OK]\n"
    )
    c = data["campaign"]
    _p("--- 26. research-023 report-split figures (report-analysis.json) ---")
    _p(
        f"{c['arms']} arms x {c['cases']} cases; dispositions {c['dispositions']}; truncated "
        f"{c['truncated_by']}; E1 truncated (not_reached) {c['not_reached']}"
    )
    for key in ("q1", "q2", "q3", "q4"):
        for row in c["guide_rows"][key]:
            _p(f"  {row}")
    _p(f"  Q5 {c['q5']}; work pass {c['physical']} [OK]")
    _p("All R023 worked-example suites passed (independent expectations; no timing claimed)")


def run_all() -> dict[str, Any]:
    data = collect()
    _print(data)
    return data


if __name__ == "__main__":
    run_all()
