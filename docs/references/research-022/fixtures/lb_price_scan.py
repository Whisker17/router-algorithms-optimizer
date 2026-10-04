"""WHI-1597: exhaustive adjacent-pair monotonicity scan of `get_price_from_id`.

Run from the repository root:
`uv run python docs/references/research-022/fixtures/lb_price_scan.py`.
For each bin step it walks every id whose `pow128` does not revert (|id - 2**23| < 2**20) and
counts adjacent pairs with `price(id + 1) < price(id)`. output-bounds.md §4.3 proves
monotonicity only inside the guard window 2**38 <= price <= 2**218; this reports what the
whole non-reverting range does (no violation is expected or required outside the window).
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from pools.liquidity_book import REAL_ID_SHIFT, LBRevert, get_price_from_id  # noqa: E402

GUARD_LOW, GUARD_HIGH = 1 << 38, 1 << 218


def scan(step: int) -> dict[str, int]:
    previous: int | None = None
    pairs = reverts = violations = inside_guard = 0
    for y in range(-(1 << 20) + 1, 1 << 20):
        try:
            price = get_price_from_id(REAL_ID_SHIFT + y, step)
        except LBRevert:
            previous = None
            reverts += 1
            continue
        if previous is not None:
            pairs += 1
            if price < previous:
                violations += 1
                inside_guard += GUARD_LOW <= price <= GUARD_HIGH
        previous = price
    return {
        "step": step, "pairs": pairs, "reverting_ids": reverts,
        "violations": violations, "violations_inside_guard": inside_guard,
    }  # fmt: skip


if __name__ == "__main__":
    for bin_step in (1, 2, 10, 25, 100):
        print(scan(bin_step))
