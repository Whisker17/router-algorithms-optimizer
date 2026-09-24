"""Plan types: `RoutePlan`/`SwapStep` (docs/DESIGN.md §2.5).

This is experimental data describing a sequence of pool swaps and how funds flow
between them -- not an EVM instruction format. Only the shapes the single-step
`direct` algorithm needs are populated so far (one step, one input reference); the
`inputs` tuple and `ALL_REMAINING` sentinel already match the general multi-input
shape §2.5 describes so later multi-hop/split algorithms (WHI-1435 and friends)
extend this module instead of replacing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ALL_REMAINING: Literal["ALL_REMAINING"] = "ALL_REMAINING"

# The evaluator seeds the fund ledger with the case's full input amount under this
# fixed fund id; every plan's first step consumes from here.
REQUEST_FUND_ID = "REQUEST"


@dataclass(frozen=True)
class FundInput:
    """One reference to a fund entering a step. `amount` is either an explicit
    positive integer or the `ALL_REMAINING` sentinel (docs/DESIGN.md §2.5)."""

    fund_id: str
    amount: int | Literal["ALL_REMAINING"]


@dataclass(frozen=True)
class SwapStep:
    """One pool swap within a plan: a verified pool id, the swap direction, one or
    more input-fund references, and the output fund id the produced amount is
    recorded under (docs/DESIGN.md §2.5)."""

    pool_id: str
    token_in: str
    token_out: str
    inputs: tuple[FundInput, ...]
    output_fund_id: str


@dataclass(frozen=True)
class RoutePlan:
    """An ordered list of swap steps. Step order is the algorithm's chosen
    execution order and is retained verbatim by the evaluator (docs/DESIGN.md
    §2.5)."""

    steps: tuple[SwapStep, ...]
