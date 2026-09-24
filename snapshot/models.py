"""Minimal typed data shared by the offline bundle/case seam (docs/DESIGN.md §§2.1,
2.2, 2.5, 4.3): the constant-product pool family (`ConstantProductPoolState`,
WHI-1427), the concentrated-liquidity family (`ConcentratedPoolState`, WHI-1428) and a
`Case`. LB (WHI-1433) adds its own pool-state type alongside these without changing
them; `PoolState` is the union every pool-agnostic caller (evaluator, bundle) uses.

Every dataclass is frozen: a snapshot is immutable input data, never mutated in place
(docs/DESIGN.md §2.2/§3). Amounts/reserves are plain Python `int` — protocol money is
never floating point (docs/DESIGN.md §2.12).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class BlockRef:
    """The frozen chain/block identity a bundle's state was read at. A synthetic
    bundle still carries this shape (with an explicit `kind="synthetic"` on the
    owning `SnapshotBundle`) so downstream code never special-cases synthetic vs.
    real bundles structurally."""

    chain_id: int
    number: int
    hash: str
    timestamp: int


@dataclass(frozen=True)
class ConstantProductPoolState:
    """One constant-product (Uniswap-V2-style) pool's frozen reserves and fee.

    `fee_bps` is the swap fee in basis points out of 10_000 (e.g. `30` == 0.3%),
    matching the standard `997/1000` Uniswap V2 constant folded into a configurable
    parameter (docs/DESIGN.md §2.3 requires the protocol's own fee/rounding, not a
    hardcoded 0.3%).
    """

    pool_id: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    fee_bps: int

    def other_token(self, token: str) -> str:
        if token == self.token0:
            return self.token1
        if token == self.token1:
            return self.token0
        raise ValueError(f"pool {self.pool_id!r} does not hold token {token!r}")

    def reserves_for(self, token_in: str) -> tuple[int, int]:
        """Return (reserve_in, reserve_out) for a swap sending `token_in`."""
        if token_in == self.token0:
            return self.reserve0, self.reserve1
        if token_in == self.token1:
            return self.reserve1, self.reserve0
        raise ValueError(f"pool {self.pool_id!r} does not hold token {token_in!r}")

    def with_reserves_for(
        self, token_in: str, new_reserve_in: int, new_reserve_out: int
    ) -> ConstantProductPoolState:
        """Return a *new* state after a swap sending `token_in`; never mutates self."""
        if token_in == self.token0:
            return ConstantProductPoolState(
                pool_id=self.pool_id,
                token0=self.token0,
                token1=self.token1,
                reserve0=new_reserve_in,
                reserve1=new_reserve_out,
                fee_bps=self.fee_bps,
            )
        if token_in == self.token1:
            return ConstantProductPoolState(
                pool_id=self.pool_id,
                token0=self.token0,
                token1=self.token1,
                reserve0=new_reserve_out,
                reserve1=new_reserve_in,
                fee_bps=self.fee_bps,
            )
        raise ValueError(f"pool {self.pool_id!r} does not hold token {token_in!r}")


@dataclass(frozen=True)
class TickInfo:
    """The swap-relevant part of one initialized tick's `Tick.Info` storage
    (`ticks(int24)`): `liquidityGross` (uint128), `liquidityNet` (int128) and the two
    `feeGrowthOutside{0,1}X128` (uint256) accumulators that `Tick.cross` flips.

    The oracle-only fields (`tickCumulativeOutside`, `secondsPerLiquidityOutsideX128`,
    `secondsOutside`) are deliberately not modeled: they depend on `block.timestamp`
    and the observation array, never on swap amounts (see
    docs/references/concentrated-liquidity-migration.md §4)."""

    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside0_x128: int
    fee_growth_outside1_x128: int


def _freeze_mapping(value: Mapping[int, object]) -> Mapping[int, object]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ConcentratedPoolState:
    """One Uniswap-v3-family pool's frozen swap state (docs/DESIGN.md §2.2: price,
    active liquidity, tick spacing, initialized tick data and fee configuration).

    Field widths mirror the Solidity storage: `sqrt_price_x96` uint160 (Q64.96),
    `tick` int24, `liquidity` uint128, `fee` uint24 in hundredths of a bip,
    `fee_growth_global*_x128` uint256 (Q128.128), `protocol_fees*` uint128.
    `fee_protocol` is the *raw* `slot0.feeProtocol`; its layout is source-specific
    (Uniswap v3: two 4-bit `1/x` denominators in a uint8; Agni/FusionX: two 16-bit
    ratios out of 10_000 in a uint32), so it is only interpreted together with
    `source_key` by `pools.concentrated`.

    Completeness is explicit: `bitmap_word_range` is the inclusive range of
    `tickBitmap` words that were read; a word inside the range that is absent from
    `tick_bitmap` is known to be zero, a word outside it is *unknown*. `ticks` holds
    the `TickInfo` of initialized ticks; a swap that needs an unknown word or the data
    of an initialized tick that is missing is an incomplete snapshot, never a zero.

    `lm_pool` is the Agni/FusionX liquidity-mining hook address (`None` for Uniswap v3,
    whose pool has no such slot). The mappings are frozen read-only views; a swap
    returns a new state and never mutates this one."""

    pool_id: str
    source_key: str
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_protocol: int
    fee_growth_global0_x128: int
    fee_growth_global1_x128: int
    protocol_fees0: int
    protocol_fees1: int
    bitmap_word_range: tuple[int, int]
    tick_bitmap: Mapping[int, int] = field(default_factory=dict)
    ticks: Mapping[int, TickInfo] = field(default_factory=dict)
    lm_pool: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tick_bitmap", _freeze_mapping(self.tick_bitmap))
        object.__setattr__(self, "ticks", _freeze_mapping(self.ticks))

    def other_token(self, token: str) -> str:
        if token == self.token0:
            return self.token1
        if token == self.token1:
            return self.token0
        raise ValueError(f"pool {self.pool_id!r} does not hold token {token!r}")


PoolState = ConstantProductPoolState | ConcentratedPoolState


@dataclass(frozen=True)
class Case:
    """One Exact Input request against a frozen snapshot (docs/DESIGN.md §2.1)."""

    case_id: str
    token_in: str
    token_out: str
    amount_in: int


@dataclass(frozen=True)
class SnapshotBundle:
    """An immutable, validated snapshot: block identity plus admitted pool states
    and cases. `bundle_hash` is the SHA-256 over the manifest's own checksum table
    (see `snapshot.bundle`), used as the corpus identity in saved results."""

    bundle_id: str
    kind: str  # "synthetic" | "real" (docs/DESIGN.md §2.2: synthetic corpora are a
    # separate, clearly labeled correctness suite, never mixed into empirical results)
    schema_version: int
    block: BlockRef
    pools: Mapping[str, PoolState]
    cases: tuple[Case, ...]
    bundle_hash: str
    source_path: str

    def pools_for_pair(self, token_a: str, token_b: str) -> tuple[PoolState, ...]:
        """All admitted pools directly connecting `token_a` and `token_b`, in a
        stable (insertion) order."""
        pair = {token_a, token_b}
        return tuple(p for p in self.pools.values() if {p.token0, p.token1} == pair)

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.case_id == case_id:
                return c
        raise KeyError(f"no case with id {case_id!r} in bundle {self.bundle_id!r}")
