"""Shared `quote_exact_in` result vocabulary for every pool family (docs/DESIGN.md
§§2.3, 4.3).

`SwapResult` is the pure output of one `quote_exact_in(state, token_in, amount_in_raw)`
call. DESIGN §2.3 requires results to distinguish *insufficient real liquidity*,
*unsupported semantics* and *missing snapshot state*; those are three different
statuses here, never folded into one another:

- `INSUFFICIENT_LIQUIDITY` -- the snapshot is complete for the path the swap takes
  and the pool genuinely cannot absorb the whole Exact Input amount (a partial fill
  never silently satisfies Exact Input).
- `INCOMPLETE_SNAPSHOT` -- the swap needs state the snapshot does not contain (for
  example a tick-bitmap word outside the collected range, or tick data for an
  initialized tick). The real pool may well have liquidity there; we cannot tell.
- `UNSUPPORTED` -- the state belongs to a source/branch this simulator has not
  admitted (unknown CL source, a hook the source does not declare, ...).
- `REVERTED` -- the migrated Solidity would revert (a `require`/checked-math
  failure) for this input on this state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snapshot.models import PoolState


class QuoteStatus(StrEnum):
    OK = "ok"
    UNSUPPORTED_TOKEN = "unsupported_token"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    INSUFFICIENT_OUTPUT_AMOUNT = "insufficient_output_amount"
    INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
    UNSUPPORTED = "unsupported"
    REVERTED = "reverted"


_EMPTY: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True)
class SwapResult[S: PoolState]:
    """Pure output of one `quote_exact_in` call, generic in the pool-state family.
    `new_state` is `None` unless `status is QuoteStatus.OK` -- a failed quote never
    claims a state transition.

    `features` carries integer execution features (e.g. initialized ticks crossed
    for CL pools); it is empty for families that have none.
    """

    status: QuoteStatus
    amount_in_consumed: int
    amount_out: int
    new_state: S | None
    detail: str = ""
    features: Mapping[str, int] = field(default=_EMPTY)
