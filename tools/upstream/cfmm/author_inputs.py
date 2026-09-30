"""Write `tests/fixtures/cfmm/author_inputs.json`: the inputs the pinned author reference
(CFMMRouter.jl, `generate.jl`) is run on. Validation-only (WHI-1557).

This script loads no upstream code. It states the small reference cases once, in this
repository's own terms (integer reserves, `fee_bps`, raw amounts, real or synthetic
`ConcentratedPoolState`s), and translates them into the author model's inputs:

- CPMM: `ProductTwoCoin(R, gamma, idx)` with `R` the float reserves and
  `gamma = (10000 - fee_bps) / 10000`.
- Swap objective: `Swap(i_out, j_in, amount_in, n)` = `BasketLiquidation`.
- CL: `UniV3(current_price, lower_ticks, liquidity, gamma, Ai)` from the known-range
  ladder of `tests/routing/cfmm_contract_model.cl_ladder`: `lower_ticks` are the interval
  upper prices P = (sqrtPriceX96 / 2**96)**2 in descending order, `liquidity` holds L**2,
  and a final `0` entry makes the author's implicit last interval [0, P_bottom] empty
  (the author code treats the price above `lower_ticks[1]` as having no liquidity).

Run from the repository root: `uv run python tools/upstream/cfmm/author_inputs.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "routing"))

from cfmm_contract_model import ClLadder, cl_ladder, synthetic_cl  # noqa: E402

from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import ConcentratedPoolState  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "cfmm" / "author_inputs.json"
MANTLE = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"


def univ3_params(ladder: ClLadder) -> dict[str, Any]:
    """The author `UniV3` arguments of a known-range ladder (see module docstring):
    boundaries from the top of the known range down to its bottom; the interval that
    contains the current price has the active liquidity."""
    up, down = ladder.up, ladder.down
    assert up and down, "the fixture price must lie strictly inside a known interval"
    bounds = [s.end for s in reversed(up)] + [s.end for s in down]
    liq = [s.liquidity for s in reversed(up[1:])] + [up[0].liquidity]
    liq += [s.liquidity for s in down[1:]]
    return {
        "current_price": ladder.sqrt_price**2,
        "lower_ticks": [b * b for b in bounds],
        "liquidity": [x * x for x in liq] + [0.0],
        "gamma": ladder.gamma,
    }


def cpmm(pool_id: str, t0: str, t1: str, r0: int, r1: int, fee_bps: int = 30) -> dict[str, Any]:
    return {
        "pool_id": pool_id,
        "token0": t0,
        "token1": t1,
        "reserve0": r0,
        "reserve1": r1,
        "fee_bps": fee_bps,
    }


def main() -> None:
    mantle = load_bundle(MANTLE)
    usdc, usdt, wmnt = (
        "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9",
        "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae",
        "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
    )
    moe = []
    for pid, p in mantle.pools.items():
        if getattr(p, "source_key", None) == "moe_classic_v1":
            moe.append(cpmm(pid, p.token0, p.token1, p.reserve0, p.reserve1, p.fee_bps))  # type: ignore[union-attr]

    cpmm_oracle = [
        {"id": "o-grid38-p1-sell-t0", "R": [134, 190], "fee_bps": 30, "v": [1.0, 1.5]},
        {"id": "o-grid38-p1-sell-t0-near", "R": [134, 190], "fee_bps": 30, "v": [1.4, 1.0]},
        {"id": "o-grid38-p1-band", "R": [134, 190], "fee_bps": 30, "v": [1.418, 1.0]},
        {"id": "o-grid38-p1-sell-t1", "R": [134, 190], "fee_bps": 30, "v": [2.0, 1.0]},
        {"id": "o-fee5-sell-t1", "R": [1000000, 480000000], "fee_bps": 5, "v": [700.0, 1.0]},
        {
            "id": "o-moe-usdt-wmnt-sell-t1",
            "R": [moe[0]["reserve0"], moe[0]["reserve1"]],
            "fee_bps": 30,
            "v": [1.6e12, 1.0],
        },
        {
            "id": "o-moe-usdt-wmnt-sell-t0",
            "R": [moe[0]["reserve0"], moe[0]["reserve1"]],
            "fee_bps": 30,
            "v": [1.4e12, 1.0],
        },
    ]

    univ3_oracle: list[dict[str, Any]] = []
    states = {
        "synthetic": synthetic_cl(),
        "synthetic_missing_tick": synthetic_cl(missing_tick_data=True),
        "real_uniswap_v3_usdt_wmnt": mantle.pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"],
    }
    probes = {
        "synthetic": [
            ("band", 1.0),
            ("sell_t0_near", 0.99),
            ("sell_t0_across_empty", 0.84),
            ("sell_t0_drain", 0.5),
            ("sell_t1_near", 1.02),
            ("sell_t1_drain", 1.3),
        ],
        "synthetic_missing_tick": [("sell_t0_across_empty", 0.84), ("sell_t0_drain", 0.5)],
        "real_uniswap_v3_usdt_wmnt": [
            ("band", 1.0),
            ("sell_t0", 0.97),
            ("sell_t1", 1.03),
            ("sell_t0_drain", 0.01),
            ("sell_t1_drain", 100.0),
        ],
    }
    for name, state in states.items():
        assert isinstance(state, ConcentratedPoolState)
        ladder = cl_ladder(state)
        params = univ3_params(ladder)
        for label, factor in probes[name]:
            # nu0/nu1 = factor * current price: < gamma*P sells token0, > P/gamma sells token1
            univ3_oracle.append(
                {
                    "id": f"cl-{name}-{label}",
                    "state": name,
                    **params,
                    "v": [factor * params["current_price"], 1.0],
                }
            )

    router = [
        {
            "id": "r-grid38",
            "note": "R021 R6 pools; continuous optimum in (59.36, 59.37)",
            "tokens": ["S", "T"],
            "pools": [cpmm("p1", "S", "T", 134, 190), cpmm("p2", "S", "T", 76, 172)],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 38,
            "route_kwargs": {},
        },
        {
            "id": "r-triangle",
            "note": "three tokens: direct, two-hop and a parallel pool",
            "tokens": ["S", "M", "T"],
            "pools": [
                cpmm("st", "S", "T", 1000, 1000),
                cpmm("sm", "S", "M", 1000, 2100),
                cpmm("mt", "M", "T", 2000, 1000),
                cpmm("mt2", "M", "T", 500, 260, 5),
            ],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 150,
            "route_kwargs": {},
        },
        {
            "id": "r-cycle",
            "note": "A/B pools priced apart: the continuous optimum trades a loop",
            "tokens": ["S", "A", "B", "T"],
            "pools": [
                cpmm("sa", "S", "A", 10000, 10000),
                cpmm("ab1", "A", "B", 10000, 12000),
                cpmm("ab2", "A", "B", 12000, 10000),
                cpmm("bt", "B", "T", 10000, 10000),
                cpmm("at", "A", "T", 10000, 10000),
            ],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 500,
            "route_kwargs": {},
        },
        {
            "id": "r-deadend",
            "note": "S-X and X-Y never reach T",
            "tokens": ["S", "T", "X", "Y"],
            "pools": [
                cpmm("st", "S", "T", 5000, 5000),
                cpmm("sx", "S", "X", 5000, 5000),
                cpmm("xy", "X", "Y", 5000, 5000),
            ],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 100,
            "route_kwargs": {},
        },
        {
            "id": "r-tiny",
            "note": "R021 R5: continuous ~198.40, stepwise integer 99",
            "tokens": ["S", "M", "T"],
            "pools": [
                cpmm("h1", "S", "M", 1000, 1000),
                cpmm("h2", "M", "T", 1000000000, 100000000000),
            ],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 2,
            "route_kwargs": {},
        },
        {
            "id": "r-moe-usdc-usdt",
            "note": "real Moe Classic triangle, 6/6/18 decimals, raw units",
            "tokens": [usdc, usdt, wmnt],
            "pools": moe,
            "token_in": usdc,
            "token_out": usdt,
            "amount_in": 20000000,
            "route_kwargs": {},
        },
        {
            "id": "r-grid38-maxiter1",
            "note": "author route! with maxiter=1: no status is returned",
            "tokens": ["S", "T"],
            "pools": [cpmm("p1", "S", "T", 134, 190), cpmm("p2", "S", "T", 76, 172)],
            "token_in": "S",
            "token_out": "T",
            "amount_in": 38,
            "route_kwargs": {"maxiter": 1},
        },
    ]
    doc = {
        "_comment": "Inputs of the pinned CFMMRouter.jl reference run (WHI-1557). Written by "
        "tools/upstream/cfmm/author_inputs.py; consumed by tools/upstream/cfmm/generate.jl.",
        "cpmm_oracle": cpmm_oracle,
        "univ3_oracle": univ3_oracle,
        "router": router,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
