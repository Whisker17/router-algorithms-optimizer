"""Write the fork-replay request file for a published CL bundle (WHI-1429/1430/1431).

    uv run python tools/cl_evidence/make_replay_requests.py <bundle_dir> <out.json>

One request per (case x pool of the case's pair): the inputs only -- pool, direction,
raw amount -- plus the bundle's block identity and each pool's collected bitmap word
range. `CaptureAgniReplay.t.sol` / `CaptureFusionXReplay.t.sol` /
`CaptureUniswapV3Replay.t.sol` execute every request on a fork at that block with the
deployed pool bytecode; no output value comes from here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import ConcentratedPoolState  # noqa: E402


def build_requests(bundle_dir: Path) -> dict[str, object]:
    bundle = load_bundle(bundle_dir)
    pools = [p for p in bundle.pools.values() if isinstance(p, ConcentratedPoolState)]
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
    return {
        "bundle_id": bundle.bundle_id,
        "bundle_hash": bundle.bundle_hash,
        "chain_id": bundle.block.chain_id,
        "block_number": bundle.block.number,
        "block_hash": bundle.block.hash,
        "pools": [p.pool_id for p in pools],
        "word_lo": [p.bitmap_word_range[0] for p in pools],
        "word_hi": [p.bitmap_word_range[1] for p in pools],
        "case_ids": case_ids,
        "request_pools": request_pools,
        "zero_for_one": zero_for_one,
        "amounts": amounts,
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
