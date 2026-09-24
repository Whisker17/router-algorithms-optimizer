"""Scripted JSON-RPC node for the fixed-block CL collector tests (WHI-1429, WHI-1430).

`FakeChain` serves a set of `ConcentratedPoolState`s (typically a published fixture
bundle's) at one block, answering exactly the calls `ConcentratedCollector` makes. Every
state read must be EIP-1898 pinned to that block's hash; anything else fails the test.
Pool runtime code is synthesized with every immutable as a `PUSH32` (solc 0.7.6 layout)
unless a test patches in other bytes.
"""

from __future__ import annotations

from typing import Any

from snapshot import abi
from snapshot.models import ConcentratedPoolState


def word(value: int) -> str:
    return format(value & ((1 << 256) - 1), "064x")


def ret(*values: int) -> str:
    return "0x" + "".join(word(v) for v in values)


def addr_int(a: str) -> int:
    return int(a, 16)


def signed256(v: int) -> int:
    return v - (1 << 256) if v >= 1 << 255 else v


class FakeChain:
    """A JSON-RPC node serving `pools` at one block. Every state read must be EIP-1898
    pinned to that block's hash with `requireCanonical`; any other shape (a block
    number, `latest`, a different hash) fails the test."""

    def __init__(
        self,
        pools: dict[str, ConcentratedPoolState],
        *,
        block_number: int,
        block_hash: str,
        timestamp: int,
    ) -> None:
        self.pools = pools
        self.block_number = block_number
        self.hashes_by_number = [block_hash]  # successive answers for the header by number
        self.block_hash = block_hash
        self.timestamp = timestamp
        self.finalized = block_number + 100
        self.factory = "0x" + "fa" * 20
        self.deployer = "0x" + "de" * 20
        self.max_liq = 10**30
        # identity overrides for rejection tests
        self.deployer_answer: str | None = None  # what factory.poolDeployer() returns
        self.factory_of: dict[str, str] = {}  # what pool.factory() returns, per pool
        self.extra_pools: dict[tuple[str, str, int], str] = {}
        self.code_patch: dict[str, bytes] = {}
        self.log: list[tuple[str, list[Any]]] = []
        self.spacings = {100: 1, 500: 10, 2500: 50, 10000: 200}
        # LM hooks (Agni/FusionX): each non-zero `lm_pool` is a live contract whose
        # `pool()` points back at its CL pool unless overridden here.
        self.lm_backref: dict[str, str] = {
            p.lm_pool.lower(): pool_id
            for pool_id, p in pools.items()
            if p.lm_pool is not None and int(p.lm_pool, 16) != 0
        }
        self.lm_code: dict[str, bytes] = {lm: b"\x60\x03lmpool" for lm in self.lm_backref}

    # fake runtime code: every immutable as a PUSH32, as solc 0.7.6 lays them out
    def pool_code(self, pool_id: str) -> bytes:
        if pool_id in self.code_patch:
            return self.code_patch[pool_id]
        p = self.pools[pool_id]
        values = [
            addr_int(self.factory),
            addr_int(p.token0),
            addr_int(p.token1),
            p.fee,
            p.tick_spacing,
            self.max_liq,
        ]
        return b"".join(b"\x7f" + v.to_bytes(32, "big") for v in values) + b"\x00\x5b"

    def code(self, address: str) -> bytes:
        if address == self.factory:
            return b"\x60\x01factory"
        if address == self.deployer:
            return b"\x60\x02deployer"
        if address in self.lm_backref:
            return self.lm_code.get(address, b"")
        return self.pool_code(address)

    def _check_pin(self, pin: Any) -> None:
        assert pin == {"blockHash": self.block_hash, "requireCanonical": True}, pin

    def _header(self, block_hash: str) -> dict[str, str]:
        return {
            "number": hex(self.block_number),
            "hash": block_hash,
            "timestamp": hex(self.timestamp),
        }

    def call(self, method: str, params: list[Any]) -> Any:
        self.log.append((method, params))
        if method == "eth_chainId":
            return hex(5000)
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(self.finalized), "hash": "0x" + "00" * 32, "timestamp": "0x0"}
            assert params[0] == hex(self.block_number), params
            h = (
                self.hashes_by_number.pop(0)
                if len(self.hashes_by_number) > 1
                else self.hashes_by_number[0]
            )
            return self._header(h)
        if method == "eth_getBlockByHash":
            return self._header(params[0])
        if method == "eth_getCode":
            self._check_pin(params[1])
            return "0x" + self.code(params[0].lower()).hex()
        if method == "eth_call":
            self._check_pin(params[1])
            return self._eth_call(params[0]["to"].lower(), params[0]["data"])
        raise AssertionError(f"unscripted RPC method {method}")

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(m, p) for m, p in calls]

    def _eth_call(self, to: str, data: str) -> str:
        sel, args = data[2:10], data[10:]
        argv = [int(args[i : i + 64], 16) for i in range(0, len(args), 64)]
        if to == self.factory:
            if sel == abi.SEL_POOL_DEPLOYER:
                return ret(addr_int(self.deployer_answer or self.deployer))
            if sel == abi.SEL_FEE_AMOUNT_TICK_SPACING:
                return ret(self.spacings.get(argv[0], 0))
            if sel == abi.SEL_GET_POOL:
                t0, t1, fee = f"0x{argv[0]:040x}", f"0x{argv[1]:040x}", argv[2]
                for p in self.pools.values():
                    if (p.token0, p.token1, p.fee) == (t0, t1, fee):
                        return ret(addr_int(p.pool_id))
                return ret(addr_int(self.extra_pools.get((t0, t1, fee), "0x" + "00" * 20)))
            raise AssertionError(f"unscripted factory selector {sel}")
        if to in self.lm_backref:
            assert sel == abi.SEL_POOL, f"unscripted LM-pool selector {sel}"
            return ret(addr_int(self.lm_backref[to]))
        p = self.pools[to]
        simple = {
            abi.SEL_FACTORY: lambda: ret(addr_int(self.factory_of.get(to, self.factory))),
            abi.SEL_TOKEN0: lambda: ret(addr_int(p.token0)),
            abi.SEL_TOKEN1: lambda: ret(addr_int(p.token1)),
            abi.SEL_FEE: lambda: ret(p.fee),
            abi.SEL_TICK_SPACING: lambda: ret(p.tick_spacing),
            abi.SEL_MAX_LIQUIDITY_PER_TICK: lambda: ret(self.max_liq),
            abi.SEL_SLOT0: lambda: ret(p.sqrt_price_x96, p.tick, 0, 1, 1, p.fee_protocol, 1),
            abi.SEL_LIQUIDITY: lambda: ret(p.liquidity),
            abi.SEL_FEE_GROWTH_GLOBAL0_X128: lambda: ret(p.fee_growth_global0_x128),
            abi.SEL_FEE_GROWTH_GLOBAL1_X128: lambda: ret(p.fee_growth_global1_x128),
            abi.SEL_PROTOCOL_FEES: lambda: ret(p.protocol_fees0, p.protocol_fees1),
            abi.SEL_LM_POOL: lambda: ret(addr_int(p.lm_pool or "0x" + "00" * 20)),
        }
        if sel in simple:
            return simple[sel]()
        if sel == abi.SEL_TICK_BITMAP:
            return ret(p.tick_bitmap.get(signed256(argv[0]), 0))
        if sel == abi.SEL_TICKS:
            t = signed256(argv[0])
            info = p.ticks.get(t)
            if info is None:
                return ret(0, 0, 0, 0, 0, 0, 0, 0)
            return ret(
                info.liquidity_gross,
                info.liquidity_net,
                info.fee_growth_outside0_x128,
                info.fee_growth_outside1_x128,
                0,
                0,
                0,
                1,
            )
        raise AssertionError(f"unscripted pool selector {sel}")
