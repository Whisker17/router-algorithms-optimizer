"""Minimal typed data shared by the offline bundle/case seam (docs/DESIGN.md §§2.1,
2.2, 2.5, 4.3). Only what the synthetic constant-product slice (WHI-1427) needs is
implemented here: a single pool family (`ConstantProductPoolState`) and a `Case`. CL
(WHI-1428) and LB (WHI-1433) add their own pool-state types alongside this one without
changing it.

Every dataclass is frozen: a snapshot is immutable input data, never mutated in place
(docs/DESIGN.md §2.2/§3). Amounts/reserves are plain Python `int` — protocol money is
never floating point (docs/DESIGN.md §2.12).
"""

from __future__ import annotations

from dataclasses import dataclass


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
    pools: dict[str, ConstantProductPoolState]
    cases: tuple[Case, ...]
    bundle_hash: str
    source_path: str

    def pools_for_pair(self, token_a: str, token_b: str) -> tuple[ConstantProductPoolState, ...]:
        """All admitted pools directly connecting `token_a` and `token_b`, in a
        stable (insertion) order."""
        pair = {token_a, token_b}
        return tuple(p for p in self.pools.values() if {p.token0, p.token1} == pair)

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.case_id == case_id:
                return c
        raise KeyError(f"no case with id {case_id!r} in bundle {self.bundle_id!r}")
