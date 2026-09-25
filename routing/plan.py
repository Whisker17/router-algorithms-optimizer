"""Plan types: `RoutePlan`/`SwapStep` (docs/DESIGN.md §2.5).

This is experimental data describing a sequence of pool swaps and how funds flow
between them -- not an EVM instruction format. The request amount starts in the
`REQUEST` fund; several steps referencing one fund split it, one step referencing
several same-token funds merges them into a single swap, and every step records its
output under a new fund id that later steps consume (multi-hop). Several steps may
name the same physical pool; each later use sees the state the earlier one left.
`routing.evaluator.evaluate` defines the exact rules (docs/DESIGN.md §2.5, WHI-1435).
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
    non-negative integer or the `ALL_REMAINING` sentinel -- the fund's whole balance at
    that point of the replay, which is how an integer remainder gets its explicit final
    allocation (docs/DESIGN.md §2.5). A step whose references total zero is a
    deterministic zero-output step that makes no pool call."""

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
