"""Block-pinned concentrated-liquidity collector (WHI-1429; shared with WHI-1430/1431).

Source-parametrized: `collect_for_source(source_key, request)` collects any
Uniswap-v3-family source whose `config/protocols.yaml` entry carries
`cl_collection.admitted: true` and whose swap semantics `pools.concentrated.SOURCES`
has migrated. Today that is `agni_v3` (WHI-1429), `fusionx_v3` (WHI-1430) and
`uniswap_v3` (WHI-1431); sharing this code admits nothing else.

The flow (docs/DESIGN.md §2.2, §4.4 Prepare):

1. **Block identity.** Chain id must equal the catalog's. The requested block number is
   resolved once to its hash/timestamp (an expected hash, if given, must match) and must
   be at or below the node's `finalized` head. Every later state read is an EIP-1898
   `{"blockHash": h, "requireCanonical": true}` call, so a node can only answer from
   exactly that block; nothing ever reads `latest`, and there is no fallback block.
2. **Deployment verification.** Factory and pool-deployer code hashes equal the catalog
   pins and `factory.poolDeployer()` round-trips (a source whose factory deploys its
   own pools -- Uniswap v3, `cl_collection.factory_deploys_pools` -- pins the factory
   only).
3. **Discovery.** For each configured pair x fee tier, `factory.getPool` (zero address ->
   recorded omission). Each candidate must: have runtime code whose
   immutable-normalized hash equals `cl_collection.pool_code_normalized_hash` (the
   example pool additionally matches its exact pinned hash); report `factory()`,
   sorted `token0()/token1()`, `fee()` and `tickSpacing()` (==
   `factory.feeAmountTickSpacing(fee)` == the catalog's expected tier) consistently; and
   be unlocked at the block boundary.
4. **State + complete tick recovery.** `slot0`, `liquidity`, both `feeGrowthGlobal`,
   `protocolFees`, `lmPool` (a non-zero LM hook must have code and its `pool()` must
   round-trip to this pool; its code hash is recorded), then a `tickBitmap` + `ticks()`
   walk outward from the current word. The walk is driven by the migrated simulator
   itself: the largest reference case in each direction is quoted against the collected
   state and the range grows one word toward the side that returned
   `incomplete_snapshot`, until the swap fits (then `margin_words` more) or
   `max_words_per_direction` is exceeded -- which refuses publication with
   `incomplete_snapshot` instead of truncating the envelope.
5. **Admission.** Every reference case is quoted on every pool of its pair; anything but
   `ok`/`insufficient_liquidity` (i.e. incomplete, unsupported or reverting) refuses
   publication.
6. **Re-verification + atomic publish.** The block header is re-read live by number and
   by hash; a different hash (reorg) aborts. Only then is the bundle written through
   `snapshot.bundle.write_bundle` (temp dir, self-validating round trip, rename), with
   every pool record stamped with the block identity and a deterministic
   `provenance.json` (catalog pins, discovery, completeness bounds, admission).

Reads go through `snapshot.rpc.HttpJsonRpcTransport`: small JSON-RPC batches, bounded
retries with backoff for the public endpoint's rate limits, and an on-disk cache of
hash-pinned results so a re-run after a failure does not re-fetch finished work.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pools.concentrated import SOURCES as SIMULATED_SOURCES
from pools.concentrated import ProtocolFeeRule, quote_exact_in
from pools.result import QuoteStatus
from snapshot import abi
from snapshot.bundle import write_bundle
from snapshot.collectors.base import PrepareError, PrepareRequest
from snapshot.config import ProtocolCatalog, SourceConfig, load_catalog
from snapshot.models import BlockRef, Case, ConcentratedPoolState, SnapshotBundle, TickInfo
from snapshot.preflight import redact_url
from snapshot.prepare_config import ClPrepareConfig, load_prepare_config
from snapshot.rpc import (
    BatchRpcTransport,
    HttpJsonRpcTransport,
    RetryPolicy,
    RpcDiskCache,
    RpcError,
    RpcTransportError,
)

PROVENANCE_SCHEMA = "cl-collection/1"
COLLECTOR = "snapshot.collectors.concentrated"
DEFAULT_PREPARE_CONFIGS = {
    "agni_v3": Path("config/prepare/agni.yaml"),
    "fusionx_v3": Path("config/prepare/fusionx.yaml"),
    "uniswap_v3": Path("config/prepare/uniswap_v3.yaml"),
}


def _floor_word(tick: int, spacing: int) -> int:
    """`TickBitmap.position(tick / tickSpacing)` word index, with Solidity's
    round-toward-negative-infinity compression (`nextInitializedTickWithinOneWord`)."""
    return (tick // spacing) >> 8


@dataclass
class _Pool:
    """Mutable collection scratch space for one pool (never published as-is)."""

    address: str
    token0: str
    token1: str
    fee: int
    spacing: int
    code_hash: str
    normalized_code_hash: str
    immutable_sites: dict[str, int]
    max_liquidity_per_tick: int
    lm_pool: str | None = None
    lm_pool_code_hash: str | None = None
    sqrt_price_x96: int = 0
    tick: int = 0
    liquidity: int = 0
    fee_protocol: int = 0
    fee_growth_global0_x128: int = 0
    fee_growth_global1_x128: int = 0
    protocol_fees0: int = 0
    protocol_fees1: int = 0
    word_lo: int = 0
    word_hi: int = 0
    words: dict[int, int] = field(default_factory=dict)
    ticks: dict[int, TickInfo] = field(default_factory=dict)
    envelope: dict[str, dict[str, Any]] = field(default_factory=dict)

    def state(self, source_key: str) -> ConcentratedPoolState:
        return ConcentratedPoolState(
            pool_id=self.address,
            source_key=source_key,
            token0=self.token0,
            token1=self.token1,
            fee=self.fee,
            tick_spacing=self.spacing,
            sqrt_price_x96=self.sqrt_price_x96,
            tick=self.tick,
            liquidity=self.liquidity,
            fee_protocol=self.fee_protocol,
            fee_growth_global0_x128=self.fee_growth_global0_x128,
            fee_growth_global1_x128=self.fee_growth_global1_x128,
            protocol_fees0=self.protocol_fees0,
            protocol_fees1=self.protocol_fees1,
            bitmap_word_range=(self.word_lo, self.word_hi),
            tick_bitmap={w: v for w, v in self.words.items() if v != 0},
            ticks=dict(self.ticks),
            lm_pool=self.lm_pool,
        )


@dataclass(frozen=True)
class CollectedSnapshot:
    block: BlockRef
    bundle_id: str
    pools: tuple[ConcentratedPoolState, ...]
    cases: tuple[Case, ...]
    provenance: dict[str, Any]


class ConcentratedCollector:
    """One fixed-block collection run. Construct, then `collect()`; nothing is written
    here (see `publish`)."""

    def __init__(
        self,
        *,
        catalog: ProtocolCatalog,
        source_key: str,
        config: ClPrepareConfig,
        transport: BatchRpcTransport,
        block_number: int,
        expected_block_hash: str | None = None,
        rpc_label: str = "<injected transport>",
    ) -> None:
        self.catalog = catalog
        self.source = _admitted_source(catalog, source_key)
        if config.source_key != source_key:
            raise PrepareError(
                "invalid_request",
                f"prepare config {config.source_path} is for {config.source_key!r}, "
                f"not {source_key!r}",
            )
        self.config = config
        self.transport = transport
        if block_number < 0:
            raise PrepareError("invalid_request", f"block number must be >= 0, got {block_number}")
        self.block_number = block_number
        self.expected_block_hash = expected_block_hash.lower() if expected_block_hash else None
        self.rpc_label = rpc_label
        self._pin: dict[str, Any] | None = None

    # -- RPC helpers ---------------------------------------------------------

    def _rpc(self, method: str, params: list[Any]) -> Any:
        try:
            return self.transport.call(method, params)
        except RpcTransportError as exc:
            raise PrepareError("rpc_unavailable", f"{method}: {exc}") from exc
        except RpcError as exc:
            raise PrepareError("rpc_error", f"{method}: {exc}") from exc

    def _eth_calls(self, calls: Sequence[tuple[str, str]], what: str) -> list[str]:
        """Batched `eth_call`s pinned to the frozen block hash."""
        assert self._pin is not None, "block identity must be resolved first"
        requests = [("eth_call", [{"to": to, "data": data}, self._pin]) for to, data in calls]
        try:
            results = self.transport.call_batch(requests)
        except RpcTransportError as exc:
            raise PrepareError("rpc_unavailable", f"{what}: {exc}") from exc
        except RpcError as exc:
            raise PrepareError("call_reverted", f"{what}: {exc}") from exc
        out: list[str] = []
        for (to, data), result in zip(calls, results, strict=True):
            if not isinstance(result, str) or result in ("", "0x"):
                raise PrepareError(
                    "missing_state", f"{what}: eth_call to {to} data={data[:10]} returned no data"
                )
            out.append(result)
        return out

    def _eth_call(self, to: str, data: str, what: str) -> str:
        return self._eth_calls([(to, data)], what)[0]

    def _code(self, address: str) -> bytes:
        assert self._pin is not None
        code = self._rpc("eth_getCode", [address, self._pin])
        if not isinstance(code, str) or abi.byte_length(code) == 0:
            raise PrepareError("missing_code", f"no runtime code at {address} at the frozen block")
        return bytes.fromhex(code.removeprefix("0x"))

    # -- 1. block identity ---------------------------------------------------

    def _header_by_number(self) -> dict[str, Any]:
        header = self._rpc("eth_getBlockByNumber", [hex(self.block_number), False])
        if not isinstance(header, dict) or not header.get("hash"):
            raise PrepareError(
                "block_unavailable", f"block {self.block_number} not returned by the RPC"
            )
        if int(header["number"], 16) != self.block_number:
            raise PrepareError(
                "block_unavailable",
                f"asked for block {self.block_number}, RPC answered {int(header['number'], 16)}",
            )
        return header

    def _resolve_block(self) -> BlockRef:
        chain_id = int(self._rpc("eth_chainId", []), 16)
        if chain_id != self.catalog.network.chain_id:
            raise PrepareError(
                "wrong_chain", f"RPC chain id {chain_id} != catalog {self.catalog.network.chain_id}"
            )
        header = self._header_by_number()
        block_hash = str(header["hash"]).lower()
        if self.expected_block_hash is not None and block_hash != self.expected_block_hash:
            raise PrepareError(
                "block_hash_mismatch",
                f"block {self.block_number} has hash {block_hash}, expected "
                f"{self.expected_block_hash}",
            )
        finalized = self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(finalized, dict) or not finalized.get("number"):
            raise PrepareError(
                "block_not_finalized", "the RPC did not report a finalized head; refusing to freeze"
            )
        finalized_number = int(finalized["number"], 16)
        if self.block_number > finalized_number:
            raise PrepareError(
                "block_not_finalized",
                f"block {self.block_number} is above the finalized head {finalized_number}",
            )
        self._pin = {"blockHash": block_hash, "requireCanonical": True}
        return BlockRef(
            chain_id=chain_id,
            number=self.block_number,
            hash=block_hash,
            timestamp=int(header["timestamp"], 16),
        )

    def _reverify_block(self, block: BlockRef) -> None:
        by_hash = self._rpc("eth_getBlockByHash", [block.hash, False])
        by_number = self._header_by_number()
        observed = str(by_number["hash"]).lower()
        hash_number = by_hash.get("number") if isinstance(by_hash, dict) else None
        if (
            observed != block.hash
            or not isinstance(hash_number, str)
            or int(hash_number, 16) != block.number
        ):
            raise PrepareError(
                "block_hash_mismatch",
                f"block {block.number} changed during collection (hash {block.hash} at start, "
                f"{observed} now); the pending bundle is invalid and was not published",
            )

    # -- 2. deployment -------------------------------------------------------

    def _contract(self, role: str) -> str:
        ref = self.source.contracts.get(role)
        if ref is None:
            raise PrepareError(
                "config_incomplete", f"{self.source.key}: catalog has no {role!r} contract"
            )
        return ref.address.lower()

    def _verify_deployment(self) -> dict[str, Any]:
        assert self.source.cl_collection is not None
        record: dict[str, Any] = {}
        self_deploying = self.source.cl_collection.factory_deploys_pools
        for role in ("factory",) if self_deploying else ("factory", "pool_deployer"):
            ref = self.source.contracts.get(role)
            if ref is None or ref.code_hash is None:
                raise PrepareError(
                    "config_incomplete",
                    f"{self.source.key}: catalog must pin {role!r} with a code_hash",
                )
            observed = abi.keccak256_hex(self._code(ref.address))
            if observed.lower() != ref.code_hash.lower():
                raise PrepareError(
                    "code_hash_mismatch",
                    f"{role} {ref.address} code hash {observed} != pinned {ref.code_hash}",
                )
            record[role] = {"address": ref.address.lower(), "code_hash": observed}
        if self_deploying:
            # Uniswap v3: the factory CREATE2-deploys pools itself; pool identity rests
            # on getPool + pool.factory() + the pool-code fingerprint (_verify_pool).
            record["pool_deployer"] = "factory"
            return record
        factory = self._contract("factory")
        deployer = abi.decode_address(
            self._eth_call(
                factory, abi.encode_call(abi.SEL_POOL_DEPLOYER), "factory.poolDeployer()"
            )
        )
        if deployer is None or deployer.lower() != self._contract("pool_deployer"):
            raise PrepareError(
                "identity_mismatch",
                f"factory.poolDeployer() = {deployer}, catalog pool_deployer "
                f"{self._contract('pool_deployer')}",
            )
        return record

    # -- 3. discovery --------------------------------------------------------

    def _discover(self) -> tuple[list[_Pool], list[dict[str, Any]]]:
        factory = self._contract("factory")
        tiers = self.source.expected_fee_tiers or {}
        pools: list[_Pool] = []
        omitted: list[dict[str, Any]] = []
        for pair in self.config.pairs:
            excluded = dict(pair.excluded_fee_tiers)
            for fee in (*pair.fee_tiers, *excluded):
                if fee not in tiers:
                    raise PrepareError(
                        "invalid_request",
                        f"fee tier {fee} is not a catalog-verified {self.source.key} tier "
                        f"{sorted(tiers)}",
                    )
                spacing_hex, pool_hex = self._eth_calls(
                    [
                        (
                            factory,
                            abi.encode_call(abi.SEL_FEE_AMOUNT_TICK_SPACING, abi.pad_uint(fee)),
                        ),
                        (
                            factory,
                            abi.encode_call(
                                abi.SEL_GET_POOL,
                                abi.pad_address(pair.token0),
                                abi.pad_address(pair.token1),
                                abi.pad_uint(fee),
                            ),
                        ),
                    ],
                    f"factory discovery {pair.token0}/{pair.token1}/{fee}",
                )
                spacing = abi.decode_int24(spacing_hex)
                if spacing != tiers[fee]:
                    raise PrepareError(
                        "fee_tier_mismatch",
                        f"factory.feeAmountTickSpacing({fee}) = {spacing}, catalog {tiers[fee]}",
                    )
                address = abi.decode_address(pool_hex)
                if fee in excluded:
                    omitted.append(
                        {
                            "token0": pair.token0,
                            "token1": pair.token1,
                            "fee": fee,
                            "pool_id": None if abi.is_zero_address(address) else address,
                            "reason": f"excluded by prepare config: {excluded[fee]}",
                        }
                    )
                    continue
                if abi.is_zero_address(address):
                    omitted.append(
                        {
                            "token0": pair.token0,
                            "token1": pair.token1,
                            "fee": fee,
                            "pool_id": None,
                            "reason": "factory.getPool returned the zero address",
                        }
                    )
                    continue
                assert address is not None
                pools.append(
                    self._verify_pool(address.lower(), pair.token0, pair.token1, fee, tiers[fee])
                )
        return pools, omitted

    def _verify_pool(self, address: str, token0: str, token1: str, fee: int, spacing: int) -> _Pool:
        assert self.source.cl_collection is not None
        words = self._eth_calls(
            [
                (address, abi.encode_call(abi.SEL_FACTORY)),
                (address, abi.encode_call(abi.SEL_TOKEN0)),
                (address, abi.encode_call(abi.SEL_TOKEN1)),
                (address, abi.encode_call(abi.SEL_FEE)),
                (address, abi.encode_call(abi.SEL_TICK_SPACING)),
                (address, abi.encode_call(abi.SEL_MAX_LIQUIDITY_PER_TICK)),
            ],
            f"pool {address} identity",
        )
        try:
            ident = {
                "original": int(address, 16),  # NoDelegateCall: address(this)
                "factory": abi.decode_words(words[0])[0],
                "token0": abi.decode_words(words[1])[0],
                "token1": abi.decode_words(words[2])[0],
                "fee": abi.to_unsigned(abi.decode_words(words[3])[0], 24),
                "tickSpacing": abi.decode_words(words[4])[0],
                "maxLiquidityPerTick": abi.to_unsigned(abi.decode_words(words[5])[0], 128),
            }
            observed_spacing = abi.to_signed(ident["tickSpacing"], 24)
            observed = (
                abi.word_to_address(ident["factory"]),
                abi.word_to_address(ident["token0"]),
                abi.word_to_address(ident["token1"]),
                ident["fee"],
                observed_spacing,
            )
        except ValueError as exc:
            raise PrepareError("identity_mismatch", f"pool {address}: {exc}") from exc
        expected = (self._contract("factory"), token0, token1, fee, spacing)
        if observed != expected:
            raise PrepareError(
                "identity_mismatch",
                f"pool {address}: (factory, token0, token1, fee, tickSpacing) = {observed}, "
                f"expected {expected}",
            )

        code = self._code(address)
        exact_hash = abi.keccak256_hex(code)
        example = self.source.contracts.get("example_pool")
        if (
            example is not None
            and example.code_hash is not None
            and example.address.lower() == address
            and exact_hash.lower() != example.code_hash.lower()
        ):
            raise PrepareError(
                "code_hash_mismatch",
                f"example pool {address} code hash {exact_hash} != pinned {example.code_hash}",
            )
        immutables = {name: ident[name] for name in self.source.cl_collection.pool_immutables}
        normalized, sites = abi.immutable_normalized_code_hash(code, immutables)
        pinned = self.source.cl_collection.pool_code_normalized_hash
        if normalized.lower() != pinned.lower():
            raise PrepareError(
                "code_hash_mismatch",
                f"pool {address}: immutable-normalized code hash {normalized} != pinned "
                f"{pinned} (not the verified {self.source.key} pool code)",
            )
        return _Pool(
            address=address,
            token0=token0,
            token1=token1,
            fee=fee,
            spacing=spacing,
            code_hash=exact_hash,
            normalized_code_hash=normalized,
            immutable_sites=sites,
            max_liquidity_per_tick=ident["maxLiquidityPerTick"],
        )

    # -- 4. state + ticks ----------------------------------------------------

    def _read_scalars(self, pool: _Pool) -> None:
        sim = SIMULATED_SOURCES[self.source.key]
        calls = [
            (pool.address, abi.encode_call(abi.SEL_SLOT0)),
            (pool.address, abi.encode_call(abi.SEL_LIQUIDITY)),
            (pool.address, abi.encode_call(abi.SEL_FEE_GROWTH_GLOBAL0_X128)),
            (pool.address, abi.encode_call(abi.SEL_FEE_GROWTH_GLOBAL1_X128)),
            (pool.address, abi.encode_call(abi.SEL_PROTOCOL_FEES)),
        ]
        if sim.has_lm_pool_hook:
            calls.append((pool.address, abi.encode_call(abi.SEL_LM_POOL)))
        results = self._eth_calls(calls, f"pool {pool.address} state")
        try:
            slot0 = abi.decode_words(results[0])
            if len(slot0) != 7:
                raise ValueError(f"slot0 returned {len(slot0)} words, expected 7")
            pool.sqrt_price_x96 = abi.to_unsigned(slot0[0], 160)
            pool.tick = abi.to_signed(slot0[1], 24)
            fee_protocol_bits = (
                32 if sim.protocol_fee_rule is ProtocolFeeRule.PANCAKE_V3_RATIO else 8
            )
            pool.fee_protocol = abi.to_unsigned(slot0[5], fee_protocol_bits)
            unlocked = slot0[6]
            pool.liquidity = abi.to_unsigned(abi.decode_words(results[1])[0], 128)
            pool.fee_growth_global0_x128 = abi.decode_words(results[2])[0]
            pool.fee_growth_global1_x128 = abi.decode_words(results[3])[0]
            fees = abi.decode_words(results[4])
            pool.protocol_fees0 = abi.to_unsigned(fees[0], 128)
            pool.protocol_fees1 = abi.to_unsigned(fees[1], 128)
            if sim.has_lm_pool_hook:
                lm = abi.word_to_address(abi.decode_words(results[5])[0])
                pool.lm_pool = lm
        except (ValueError, IndexError) as exc:
            raise PrepareError("inconsistent_state", f"pool {pool.address}: {exc}") from exc
        if unlocked != 1:
            raise PrepareError(
                "inconsistent_state",
                f"pool {pool.address}: slot0.unlocked is {unlocked} at a block boundary",
            )
        if pool.lm_pool is not None and not abi.is_zero_address(pool.lm_pool):
            self._verify_lm_pool(pool)
        w = _floor_word(pool.tick, pool.spacing)
        margin = self.config.limits.initial_margin_words
        pool.word_lo, pool.word_hi = w - margin, w + margin
        self._read_words(pool, list(range(pool.word_lo, pool.word_hi + 1)))

    def _verify_lm_pool(self, pool: _Pool) -> None:
        """An attached LM hook is called by every swap (`accumulateReward`, and
        `crossLmTick` per crossed tick). The simulator models it as a no-op that cannot
        change the pool's output or storage; the hook must at least be a live contract
        bound to this pool, and its exact code is recorded so a changed hook is visible."""
        assert pool.lm_pool is not None
        code = self._code(pool.lm_pool)
        back = abi.decode_address(
            self._eth_call(
                pool.lm_pool, abi.encode_call(abi.SEL_POOL), f"lm pool {pool.lm_pool} pool()"
            )
        )
        if back is None or back.lower() != pool.address:
            raise PrepareError(
                "identity_mismatch",
                f"pool {pool.address}: lmPool {pool.lm_pool}.pool() = {back}, not this pool",
            )
        pool.lm_pool_code_hash = abi.keccak256_hex(code)

    def _read_words(self, pool: _Pool, words: list[int]) -> None:
        if not words:
            return
        for w in words:
            if not (-(1 << 15) <= w < (1 << 15)):
                raise PrepareError(
                    "incomplete_snapshot",
                    f"pool {pool.address}: bitmap word {w} is out of int16 range",
                )
        bitmaps = self._eth_calls(
            [(pool.address, abi.encode_call(abi.SEL_TICK_BITMAP, abi.pad_int16(w))) for w in words],
            f"pool {pool.address} tickBitmap",
        )
        new_ticks: list[int] = []
        for w, raw in zip(words, bitmaps, strict=True):
            value = abi.decode_words(raw)[0]
            pool.words[w] = value
            for bit in range(256):
                if (value >> bit) & 1:
                    new_ticks.append((w * 256 + bit) * pool.spacing)
        if not new_ticks:
            return
        results = self._eth_calls(
            [(pool.address, abi.encode_call(abi.SEL_TICKS, abi.pad_int24(t))) for t in new_ticks],
            f"pool {pool.address} ticks",
        )
        for t, raw in zip(new_ticks, results, strict=True):
            try:
                fields = abi.decode_words(raw)
                if len(fields) != 8:
                    raise ValueError(f"ticks() returned {len(fields)} words, expected 8")
                gross = abi.to_unsigned(fields[0], 128)
                net = abi.to_signed(fields[1], 128)
                initialized = fields[7]
            except ValueError as exc:
                raise PrepareError(
                    "inconsistent_state", f"pool {pool.address} tick {t}: {exc}"
                ) from exc
            if initialized != 1 or gross == 0:
                raise PrepareError(
                    "inconsistent_state",
                    f"pool {pool.address}: bitmap marks tick {t} initialized but ticks() says "
                    f"initialized={initialized}, liquidityGross={gross}",
                )
            pool.ticks[t] = TickInfo(
                liquidity_gross=gross,
                liquidity_net=net,
                fee_growth_outside0_x128=fields[2],
                fee_growth_outside1_x128=fields[3],
            )

    def _cover_envelope(self, pool: _Pool) -> None:
        limits = self.config.limits
        center = _floor_word(pool.tick, pool.spacing)
        for zero_for_one in (True, False):
            direction = "zero_for_one" if zero_for_one else "one_for_zero"
            token_in = pool.token0 if zero_for_one else pool.token1
            token_out = pool.token1 if zero_for_one else pool.token0
            amounts = [
                c.amount_in
                for c in self.config.cases
                if c.token_in == token_in and c.token_out == token_out
            ]
            if not amounts:
                pool.envelope[direction] = {"amount_in": None, "status": "no_reference_case"}
                continue
            amount = max(amounts)
            while True:
                result = quote_exact_in(pool.state(self.source.key), token_in, amount)
                if result.status is not QuoteStatus.INCOMPLETE_SNAPSHOT:
                    break
                side = center - pool.word_lo if zero_for_one else pool.word_hi - center
                if side >= limits.max_words_per_direction:
                    raise PrepareError(
                        "incomplete_snapshot",
                        f"pool {pool.address}: a {direction} swap of {amount} (largest reference "
                        f"case) still needs state beyond {limits.max_words_per_direction} bitmap "
                        f"words from the current word {center} ({result.detail}); refusing to "
                        "publish a truncated envelope",
                    )
                if zero_for_one:
                    pool.word_lo -= 1
                    self._read_words(pool, [pool.word_lo])
                else:
                    pool.word_hi += 1
                    self._read_words(pool, [pool.word_hi])
            if result.status not in (QuoteStatus.OK, QuoteStatus.INSUFFICIENT_LIQUIDITY):
                raise PrepareError(
                    "admission_failed",
                    f"pool {pool.address}: {direction} envelope quote is {result.status.value}: "
                    f"{result.detail}",
                )
            entry: dict[str, Any] = {"amount_in": str(amount), "status": result.status.value}
            if result.status is QuoteStatus.OK and result.new_state is not None:
                end_tick = result.new_state.tick
                end_word = _floor_word(end_tick, pool.spacing)
                entry["end_tick"] = end_tick
                entry["end_word"] = end_word
                if zero_for_one:
                    target = max(
                        end_word - limits.margin_words, center - limits.max_words_per_direction
                    )
                    extra = list(range(target, pool.word_lo))
                    pool.word_lo = min(pool.word_lo, target)
                else:
                    target = min(
                        end_word + limits.margin_words, center + limits.max_words_per_direction
                    )
                    extra = list(range(pool.word_hi + 1, target + 1))
                    pool.word_hi = max(pool.word_hi, target)
                self._read_words(pool, extra)
            pool.envelope[direction] = entry

    # -- 5. admission --------------------------------------------------------

    def _admission(self, states: dict[str, ConcentratedPoolState]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for case in self.config.cases:
            pair = {case.token_in, case.token_out}
            for state in states.values():
                if {state.token0, state.token1} != pair:
                    continue
                result = quote_exact_in(state, case.token_in, case.amount_in)
                if result.status not in (QuoteStatus.OK, QuoteStatus.INSUFFICIENT_LIQUIDITY):
                    raise PrepareError(
                        "incomplete_snapshot"
                        if result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
                        else "admission_failed",
                        f"case {case.case_id!r} on pool {state.pool_id}: "
                        f"{result.status.value}: {result.detail}",
                    )
                records.append(
                    {
                        "case_id": case.case_id,
                        "pool_id": state.pool_id,
                        "status": result.status.value,
                    }
                )
        return records

    # -- orchestration -------------------------------------------------------

    def collect(self) -> CollectedSnapshot:
        block = self._resolve_block()
        deployment = self._verify_deployment()
        pools, omitted = self._discover()
        if not pools:
            raise PrepareError("admission_failed", "discovery admitted no pool")
        for pool in pools:
            self._read_scalars(pool)
            self._cover_envelope(pool)
        states = {p.address: p.state(self.source.key) for p in pools}
        admission = self._admission(states)
        self._reverify_block(block)

        assert self.source.cl_collection is not None
        upstream = self.source.upstream
        provenance: dict[str, Any] = {
            "schema": PROVENANCE_SCHEMA,
            "collector": COLLECTOR,
            "source_key": self.source.key,
            "block": {
                "chain_id": block.chain_id,
                "number": block.number,
                "hash": block.hash,
                "timestamp": block.timestamp,
            },
            "block_verification": (
                "resolved by number, <= finalized head, every state read EIP-1898 pinned to "
                "the hash with requireCanonical, header re-read by number and hash after "
                "collection"
            ),
            "rpc_endpoint": self.rpc_label,
            "source_capability": {
                "protocol_family": self.source.protocol_family,
                "sor_protocol": self.source.sor_protocol,
            },
            "catalog": {
                **deployment,
                "pool_code_normalized_hash": self.source.cl_collection.pool_code_normalized_hash,
                "pool_immutables": list(self.source.cl_collection.pool_immutables),
                "upstream": None
                if upstream is None
                else {"repo": upstream.repo, "ref": upstream.ref, "license": upstream.license},
            },
            "prepare_config": {"path": self.config.source_path, "sha256": self.config.sha256},
            "token_labels": dict(sorted(self.config.token_labels.items())),
            "discovery": {
                "method": "factory.getPool over configured pairs x catalog fee tiers",
                "admitted": [
                    {"pool_id": p.address, "token0": p.token0, "token1": p.token1, "fee": p.fee}
                    for p in pools
                ],
                "omitted": omitted,
            },
            "pools": {
                p.address: {
                    "code_hash": p.code_hash,
                    "normalized_code_hash": p.normalized_code_hash,
                    "immutable_sites": dict(sorted(p.immutable_sites.items())),
                    "max_liquidity_per_tick": str(p.max_liquidity_per_tick),
                    "lm_pool": p.lm_pool,
                    **(
                        {}
                        if p.lm_pool_code_hash is None
                        else {
                            "lm_pool_identity": {
                                "code_hash": p.lm_pool_code_hash,
                                "pool": p.address,
                                "modeled_as": "no-op hook (accumulateReward, crossLmTick)",
                            }
                        }
                    ),
                    "completeness": {
                        "bitmap_word_range": [p.word_lo, p.word_hi],
                        "tick_range": [
                            p.word_lo * 256 * p.spacing,
                            (p.word_hi + 1) * 256 * p.spacing - 1,
                        ],
                        "initialized_ticks": len(p.ticks),
                        "limits": {
                            "initial_margin_words": self.config.limits.initial_margin_words,
                            "margin_words": self.config.limits.margin_words,
                            "max_words_per_direction": self.config.limits.max_words_per_direction,
                        },
                        "envelope": p.envelope,
                    },
                }
                for p in pools
            },
            "admission": admission,
        }
        return CollectedSnapshot(
            block=block,
            bundle_id=f"{self.config.bundle_id_prefix}-{block.number}-{block.hash[2:10]}",
            pools=tuple(states.values()),
            cases=self.config.cases,
            provenance=provenance,
        )


def _admitted_source(catalog: ProtocolCatalog, source_key: str) -> SourceConfig:
    try:
        source = catalog.source(source_key)
    except KeyError as exc:
        raise PrepareError("invalid_request", str(exc)) from exc
    if source.protocol_family != "v3_concentrated_liquidity":
        raise PrepareError(
            "invalid_request", f"{source_key!r} is not a concentrated-liquidity source"
        )
    if source.cl_collection is None or not source.cl_collection.admitted:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no admitted `cl_collection` entry in the catalog; the shared "
            "CL collector only collects sources whose own admission evidence has landed",
        )
    if source_key not in SIMULATED_SOURCES:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no migrated swap semantics in pools.concentrated",
        )
    return source


def publish(output_dir: Path, collected: CollectedSnapshot) -> SnapshotBundle:
    """Atomically write a collected snapshot as a `real` bundle."""
    return write_bundle(
        output_dir,
        bundle_id=collected.bundle_id,
        kind="real",
        block=collected.block,
        pools=list(collected.pools),
        cases=list(collected.cases),
        provenance=collected.provenance,
    )


def collect_for_source(
    source_key: str,
    request: PrepareRequest,
    *,
    transport: BatchRpcTransport | None = None,
) -> SnapshotBundle:
    """`prepare --source <name>` for an admitted CL source."""
    if request.block_number is None:
        raise PrepareError(
            "invalid_request",
            f"{source_key}: an explicit --block is required (collection never reads `latest`)",
        )
    if request.output_dir.exists():
        raise PrepareError("invalid_request", f"{request.output_dir} already exists")
    catalog = load_catalog(request.catalog_path)
    config_path = request.prepare_config or DEFAULT_PREPARE_CONFIGS.get(source_key)
    if config_path is None:
        raise PrepareError("invalid_request", f"{source_key}: no --prepare-config given")
    config = load_prepare_config(config_path)
    rpc_url = request.rpc_url or catalog.network.default_rpc_url
    http: HttpJsonRpcTransport | None = None
    if transport is None:
        http = HttpJsonRpcTransport(
            rpc_url,
            timeout_seconds=config.rpc.timeout_seconds,
            retry_policy=RetryPolicy(
                max_attempts=config.rpc.max_attempts,
                base_delay_seconds=config.rpc.base_delay_seconds,
                max_delay_seconds=config.rpc.max_delay_seconds,
            ),
            disk_cache=RpcDiskCache(request.cache_dir) if request.cache_dir is not None else None,
            max_batch_size=config.rpc.max_batch_size,
        )
        transport = http
    collector = ConcentratedCollector(
        catalog=catalog,
        source_key=source_key,
        config=config,
        transport=transport,
        block_number=request.block_number,
        expected_block_hash=request.expected_block_hash,
        rpc_label=redact_url(rpc_url) if http is not None else "<injected transport>",
    )
    collected = collector.collect()
    bundle = publish(request.output_dir, collected)
    if http is not None:
        print(
            f"rpc: {http.call_count} request(s), {http.retries} retry round(s), "
            f"{http.cache_hits} memory / {http.disk_cache_hits} disk cache hit(s)",
            file=sys.stderr,
        )
    return bundle
