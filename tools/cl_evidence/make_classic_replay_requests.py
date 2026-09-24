"""Write the fork-replay request file for a published Merchant Moe Classic bundle
(WHI-1432).

    uv run python tools/cl_evidence/make_classic_replay_requests.py <bundle_dir> <out.json>

One request per (case x pair of the case's token pair): the inputs only -- pair,
direction, raw amount -- plus the bundle's block identity, and two uint112 overflow
probes per direction on the catalog example pair (the largest input the pair can still
absorb, and one more). `CaptureMoeClassicReplay.t.sol` executes every request on a fork
at that block with the deployed MoePair / MoeRouter bytecode; no output value comes from
here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.config import load_catalog  # noqa: E402
from snapshot.models import ConstantProductPoolState  # noqa: E402

UINT112_MAX = (1 << 112) - 1


def build_requests(bundle_dir: Path) -> dict[str, object]:
    bundle = load_bundle(bundle_dir)
    pools = [p for p in bundle.pools.values() if isinstance(p, ConstantProductPoolState)]
    case_ids: list[str] = []
    request_pools: list[str] = []
    zero_for_one: list[bool] = []
    amounts: list[str] = []
    for case in bundle.cases:
        for pool in bundle.pools_for_pair(case.token_in, case.token_out):
            case_ids.append(case.case_id)
            request_pools.append(pool.pool_id)
            zero_for_one.append(case.token_in == pool.token0)
            amounts.append(str(case.amount_in))
    source = load_catalog(REPO / "config" / "protocols.yaml").source("moe_classic_v1")
    example = bundle.pools[source.contracts["example_pool"].address.lower()]
    assert isinstance(example, ConstantProductPoolState)
    overflow_zfo: list[bool] = []
    overflow_amounts: list[str] = []
    for zfo, reserve_in in ((True, example.reserve0), (False, example.reserve1)):
        for amount in (UINT112_MAX - reserve_in, UINT112_MAX - reserve_in + 1):
            overflow_zfo.append(zfo)
            overflow_amounts.append(str(amount))
    return {
        "bundle_id": bundle.bundle_id,
        "bundle_hash": bundle.bundle_hash,
        "chain_id": bundle.block.chain_id,
        "block_number": bundle.block.number,
        "block_hash": bundle.block.hash,
        "router": source.contracts["router"].address,
        "pools": [p.pool_id for p in pools],
        "case_ids": case_ids,
        "request_pools": request_pools,
        "zero_for_one": zero_for_one,
        "amounts": amounts,
        "overflow_pool": example.pool_id,
        "overflow_zero_for_one": overflow_zfo,
        "overflow_amounts": overflow_amounts,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    requests = build_requests(Path(argv[1]))
    Path(argv[2]).write_text(json.dumps(requests, indent=2) + "\n")
    print(f"wrote {len(requests['case_ids'])} request(s) to {argv[2]}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
