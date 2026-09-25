"""Minimal typed data shared by the offline bundle/case seam (docs/DESIGN.md §§2.1,
2.2, 2.5, 4.3): the constant-product pool family (`ConstantProductPoolState`,
WHI-1427), the concentrated-liquidity family (`ConcentratedPoolState`, WHI-1428),
the Liquidity Book family (`LiquidityBookPoolState`, WHI-1433) and a `Case`.
`PoolState` is the union every pool-agnostic caller (evaluator, bundle) uses.

Every dataclass is frozen: a snapshot is immutable input data, never mutated in place
(docs/DESIGN.md §2.2/§3). Amounts/reserves are plain Python `int` — protocol money is
never floating point (docs/DESIGN.md §2.12).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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

    `source_key` names the admitted real source whose migrated semantics govern the
    pool (`pools.constant_product.SOURCES`, e.g. `moe_classic_v1`, WHI-1432); `None`
    is the generic, source-free formula used by the synthetic correctness suite.
    """

    pool_id: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    fee_bps: int
    source_key: str | None = None

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
            return replace(self, reserve0=new_reserve_in, reserve1=new_reserve_out)
        if token_in == self.token1:
            return replace(self, reserve0=new_reserve_out, reserve1=new_reserve_in)
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


@dataclass(frozen=True)
class LBStaticFeeParameters:
    """`LBPair.getStaticFeeParameters()` (the static half of the packed `_parameters`
    word, `PairParameterHelper` offsets 0-111): `baseFactor` uint16, `filterPeriod` and
    `decayPeriod` uint12 (seconds), `reductionFactor` uint14 (basis points),
    `variableFeeControl` uint24, `protocolShare` uint14 (basis points of the fee, <= 2500),
    `maxVolatilityAccumulator` uint20."""

    base_factor: int
    filter_period: int
    decay_period: int
    reduction_factor: int
    variable_fee_control: int
    protocol_share: int
    max_volatility_accumulator: int


@dataclass(frozen=True)
class LBVariableFeeParameters:
    """`LBPair.getVariableFeeParameters()`: `volatilityAccumulator` / `volatilityReference`
    uint20, `idReference` uint24 and `timeOfLastUpdate` uint40 (unix seconds). These evolve
    with every swap -- they are pool *state*, not configuration."""

    volatility_accumulator: int
    volatility_reference: int
    id_reference: int
    time_of_last_update: int


@dataclass(frozen=True)
class LiquidityBookPoolState:
    """One Liquidity Book v2.2 pair's frozen swap state (docs/DESIGN.md §2.2: bins, active
    bin, static/variable fee parameters and the timestamp-dependent accumulators).

    `token0`/`token1` are the pair's `getTokenX()`/`getTokenY()` -- *not* sorted by
    address; X is the base token, prices are Y per X. `bin_step` is the clone's immutable
    `getBinStep()` in basis points. `block_timestamp` is the frozen snapshot time every
    swap on this state executes at (`block.timestamp`); the fee state itself still evolves
    within a plan through the returned `new_state`.

    `reserve_x`/`reserve_y` are `getReserves()` (the pair's `_reserves` minus
    `_protocolFees`); `protocol_fee_x`/`protocol_fee_y` are `getProtocolFees()`.

    `static_fee`/`variable_fee` are `None` when that part of the fee state was not
    collected; a swap on such a state is an incomplete snapshot, never a default fee.

    Completeness is explicit: `bin_range` is the inclusive id range over which the pair's
    bin tree (`getNextNonEmptyBin`) was fully walked. `bins` maps every tree member inside
    that range to its `getBin(id)` reserves `(x, y)`; an id inside the range that is absent
    is known not to be in the tree (an empty bin), an id outside it is *unknown*. A range
    reaching `0` / `2**24 - 1` covers that whole side of the book.

    `hooks_parameters` is the raw `getLBHooksParameters()` word (hooks address in the low
    160 bits, flags above). `swap_hook_implementation` is the implementation the hooks
    clone delegates to, as resolved by the collector; a swap hook is only simulated when
    that implementation is admitted as amount-neutral (`pools.liquidity_book.SOURCES`).
    An admitted `LBHooksRewarder` forwards `beforeSwap` to its own extra hook:
    `extra_hooks_parameters` is the rewarder's raw `getExtraHooksParameters()` word (0 =
    none) and `extra_swap_hook_implementation` the implementation that clone delegates to
    (WHI-1434); a swap-flagged extra hook is simulated only when admitted too.

    The oracle (`oracleId`, samples) is deliberately not modeled: it is written after
    the swap loop and never read by swap math (`docs/references/liquidity-book-migration.md`).
    """

    pool_id: str
    source_key: str
    token0: str
    token1: str
    bin_step: int
    block_timestamp: int
    active_id: int
    reserve_x: int
    reserve_y: int
    protocol_fee_x: int
    protocol_fee_y: int
    static_fee: LBStaticFeeParameters | None
    variable_fee: LBVariableFeeParameters | None
    bin_range: tuple[int, int]
    bins: Mapping[int, tuple[int, int]] = field(default_factory=dict)
    hooks_parameters: int = 0
    swap_hook_implementation: str | None = None
    extra_hooks_parameters: int = 0
    extra_swap_hook_implementation: str | None = None

    def __post_init__(self) -> None:
        lo, hi = self.bin_range
        if not (0 <= lo <= hi <= (1 << 24) - 1):
            raise ValueError(f"pool {self.pool_id!r}: invalid bin_range {self.bin_range}")
        outside = [i for i in self.bins if not (lo <= i <= hi)]
        if outside:
            raise ValueError(
                f"pool {self.pool_id!r}: bins {sorted(outside)[:5]} lie outside the collected "
                f"range {self.bin_range}"
            )
        object.__setattr__(self, "bins", _freeze_mapping(self.bins))

    def other_token(self, token: str) -> str:
        if token == self.token0:
            return self.token1
        if token == self.token1:
            return self.token0
        raise ValueError(f"pool {self.pool_id!r} does not hold token {token!r}")


PoolState = ConstantProductPoolState | ConcentratedPoolState | LiquidityBookPoolState


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
