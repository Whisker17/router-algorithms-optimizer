"""Write the fork request file for the corpus token-semantics probe (WHI-1436).

    uv run python tools/cl_evidence/make_corpus_token_requests.py <corpus_bundle> <out.json>

Inputs only: the corpus block, its universe tokens, and for each token its candidate
holders -- every admitted corpus pool holding it, in pool-id order. No expected value is
written here; `CaptureCorpusTokens.t.sol` reads balances and transfers on the fork.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from snapshot.bundle import load_bundle  # noqa: E402


def build_requests(bundle_dir: Path) -> dict[str, object]:
    bundle = load_bundle(bundle_dir)
    assert bundle.corpus is not None, "not a corpus bundle"
    tokens = list(bundle.corpus["universe"]["tokens"])
    cand_tokens: list[str] = []
    cand_holders: list[str] = []
    for token in tokens:
        for pid in sorted(bundle.pools):
            pool = bundle.pools[pid]
            if token in (pool.token0, pool.token1):
                cand_tokens.append(token)
                cand_holders.append(pid)
    return {
        "chain_id": bundle.block.chain_id,
        "block_number": bundle.block.number,
        "block_hash": bundle.block.hash,
        "tokens": tokens,
        "candidate_tokens": cand_tokens,
        "candidate_holders": cand_holders,
    }


def main() -> None:
    bundle_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_requests(bundle_dir), indent=2) + "\n")


if __name__ == "__main__":
    main()
