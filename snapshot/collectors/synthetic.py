"""The `synthetic` prepare source: a small, fully offline, hand-designed
constant-product bundle (docs/DESIGN.md §2.4: "Synthetic cases are a separate
correctness suite, not mixed into empirical performance averages."). No network
access and no external state -- every pool/case value below is a literal.

`tests/fixtures/synthetic/` is the checked-in output of running this collector
once; `tests/test_synthetic_run.py` asserts the two stay byte-identical, so this
module is the single source of truth for the fixture's contents.
"""

from __future__ import annotations

from pathlib import Path

from snapshot.bundle import write_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BUNDLE_ID = "synthetic-direct-v1"

TOKEN_A = "TKA"
TOKEN_B = "TKB"
TOKEN_D = "TKD"  # deliberately has no pool with TOKEN_A: exercises `direct`'s
# typed no-route path (docs/DESIGN.md §2.6 acceptance criterion).

# A synthetic bundle carries the same BlockRef shape a real one does; chain_id 0
# and an all-zero hash mark it as clearly not a real chain identity.
_BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)

_POOLS = [
    # Better price: ~3 TKB per TKA before fees.
    ConstantProductPoolState(
        pool_id="pool_a",
        token0=TOKEN_A,
        token1=TOKEN_B,
        reserve0=1_000_000,
        reserve1=3_000_000,
        fee_bps=30,
    ),
    # Worse price: ~1.5 TKB per TKA before fees. `direct` must prefer pool_a.
    ConstantProductPoolState(
        pool_id="pool_b",
        token0=TOKEN_A,
        token1=TOKEN_B,
        reserve0=2_000_000,
        reserve1=3_000_000,
        fee_bps=30,
    ),
]

_CASES = [
    Case(case_id="direct_best_pool", token_in=TOKEN_A, token_out=TOKEN_B, amount_in=100_000),
    Case(case_id="direct_no_route", token_in=TOKEN_A, token_out=TOKEN_D, amount_in=100_000),
]


def collect(output_dir: Path) -> SnapshotBundle:
    return write_bundle(
        output_dir,
        bundle_id=BUNDLE_ID,
        kind="synthetic",
        block=_BLOCK,
        pools=list(_POOLS),
        cases=list(_CASES),
    )
