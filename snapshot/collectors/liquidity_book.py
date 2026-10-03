"""Block-pinned Merchant Moe Liquidity Book v2.2 collector (WHI-1434): `prepare --source
moe_lb`.

Collects LB pairs of an admitted `liquidity_book_v2` source (today only `moe_lb_v2_2`:
`config/protocols.yaml` `lb_collection.admitted`) whose swap semantics
`pools.liquidity_book.SOURCES` has migrated (WHI-1433). The flow (docs/DESIGN.md §2.2,
§4.4 Prepare), on top of `FixedBlockReader`'s block identity and hash-pinned reads:

1. **Deployment.** `LBFactory` and the `LBPair` implementation code hashes equal the
   catalog pins and `factory.getLBPairImplementation()` is the pinned implementation.
   Every catalog-approved hook implementation must also be one the simulator admits.
2. **Discovery.** For each configured token pair, `factory.getAllLBPairs` (every bin
   step; an empty answer is a recorded omission, a configured bin-step exclusion a
   reasoned omission). Pairs with no liquidity are still collected: their empty book
   is proven by walking the tree to both ends of the id space.
3. **Pair identity** (fail closed, before any state is trusted). The runtime code must be
   *exactly* the `ImmutableClone` of the pinned implementation with immutable args
   `tokenX ++ tokenY ++ uint16 binStep` (lfj-gg/joe-v2 v2.2.0 `LBFactory.createLBPair`),
   so the clone target, the token order and the bin step are read from the bytecode; the
   pair must sit at the CREATE2 address the factory deploys it to (salt
   `keccak256(abi.encode(tokenA, tokenB, binStep))`, tokens sorted);
   `factory.getLBPairInformation(tokenX, tokenY, binStep)` must round-trip to it; its
   `getTokenX/getTokenY/getBinStep/getFactory/implementation` getters must agree; token
   decimals must equal the prepare config's declaration.
4. **State.** `getActiveId`, `getReserves`, `getProtocolFees`, `getStaticFeeParameters`,
   `getVariableFeeParameters` (volatility accumulator/reference, idReference,
   timeOfLastUpdate) and the frozen block timestamp. Each token's `balanceOf(pair)` must
   equal reserves + protocol fees (the pair's `_reserves`): a pending donation would be
   credited to the next swapper, which the migrated transition does not model.
5. **Hooks.** `getLBHooksParameters()`: a swap-flagged hook must be an `ImmutableClone`
   (args start with this pair) of an implementation on the catalog's approved
   `lb_collection.swap_hooks` list, with its pinned code hash and `getLBPair()` round
   trip. That rewarder's `getExtraHooksParameters()` is read too: a swap-flagged extra
   hook must be a clone of an `lb_collection.extra_swap_hooks` implementation whose args
   and `getLBPair()` / `getParentRewarder()` bind it to this pair and rewarder. Anything
   else refuses the pool (`unsupported_hook`). Hook/extra-hook addresses, clone and
   implementation code hashes are recorded in provenance.
6. **Envelope-complete bins.** From the active bin the tree is walked with
   `getNextNonEmptyBin` and every member read with `getBin`, driven by the migrated
   simulator: the largest reference case of each direction is quoted against the
   collected state and the walk grows `walk_chunk_bins` at a time on the side that
   returned `incomplete_snapshot`, until the swap fits or the walk reaches the end of the
   id space (which proves real exhaustion); then `margin_bins` more past the bin the swap
   ends in. More than `max_bins_per_direction` bins on a side refuses publication with
   `incomplete_snapshot` -- the envelope is never truncated silently; a pool that is
   genuinely uncoverable needs a reasoned bin-step exclusion in the prepare config.
7. **Admission.** Every reference case is quoted on every pair of its token pair and,
   when it succeeds, a sequential reverse swap of half its output through the returned
   next state; anything but `ok` / `insufficient_liquidity` / `insufficient_output_amount`
   refuses publication.
8. **Re-verification + atomic publish** through `snapshot.bundle.write_bundle` with a
   deterministic `provenance.json` (catalog pins, clone/CREATE2 identity, hooks,
   completeness bounds, admission, SOR capability -- none: SOR does not route LB).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pools.liquidity_book import AFTER_SWAP_FLAG, BEFORE_SWAP_FLAG, UINT24_MAX, quote_exact_in
from pools.liquidity_book import SOURCES as SIMULATED_SOURCES
from pools.result import QuoteStatus
from snapshot import abi
from snapshot.bundle import write_bundle
from snapshot.collectors.base import PrepareError, PrepareRequest
from snapshot.collectors.classic import clone_create2_address, clone_runtime_code
from snapshot.collectors.fixed_block import FixedBlockReader, http_transport
from snapshot.config import ApprovedHook, ProtocolCatalog, SourceConfig, load_catalog
from snapshot.models import (
    BlockRef,
    Case,
    LBStaticFeeParameters,
    LBVariableFeeParameters,
    LiquidityBookPoolState,
    SnapshotBundle,
)
from snapshot.preflight import redact_url
from snapshot.prepare_config import LBPrepareConfig, load_lb_prepare_config
from snapshot.rpc import BatchRpcTransport, HttpJsonRpcTransport

SOURCE_KEY = "moe_lb_v2_2"
PROVENANCE_SCHEMA = "lb-collection/1"
COLLECTOR = "snapshot.collectors.liquidity_book"
DEFAULT_PREPARE_CONFIG = Path("config/prepare/moe_lb.yaml")
ADMITTED_STATUSES = (
    QuoteStatus.OK,
    QuoteStatus.INSUFFICIENT_LIQUIDITY,
    QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT,
)
SWAP_FLAGS = BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG
ADDRESS_MASK = (1 << 160) - 1
# ImmutableClone runtime: 9-byte prefix, uint16 extra length, 9 bytes, the 20-byte
# implementation, 13-byte suffix, the immutable args, uint16 extra length.
_CLONE_ARGS_OFFSET = 53
_CLONE_IMPL = slice(20, 40)


def parse_clone(code: bytes) -> tuple[str, bytes] | None:
    """`(implementation, immutable args)` when `code` is exactly an `ImmutableClone`
    runtime, else `None`."""
    if len(code) < _CLONE_ARGS_OFFSET + 2:
        return None
    implementation = "0x" + code[_CLONE_IMPL].hex()
    data = code[_CLONE_ARGS_OFFSET:-2]
    return (implementation, data) if clone_runtime_code(implementation, data) == code else None


def pair_salt(token_x: str, token_y: str, bin_step: int) -> bytes:
    """`keccak256(abi.encode(tokenA, tokenB, binStep))` with (tokenA, tokenB) sorted."""
    a, b = sorted((token_x, token_y))
    encoded = abi.pad_address(a) + abi.pad_address(b) + abi.pad_uint(bin_step)
    return bytes.fromhex(abi.keccak256_hex(bytes.fromhex(encoded))[2:])


@dataclass
class _Pair:
    """Mutable collection scratch space for one LB pair (never published as-is)."""

    address: str
    token_x: str
    token_y: str
    bin_step: int
    code_hash: str
    created_by_owner: bool
    ignored_for_routing: bool
    active_id: int = 0
    reserve_x: int = 0
    reserve_y: int = 0
    protocol_fee_x: int = 0
    protocol_fee_y: int = 0
    static_fee: LBStaticFeeParameters | None = None
    variable_fee: LBVariableFeeParameters | None = None
    hooks_parameters: int = 0
    swap_hook_implementation: str | None = None
    extra_hooks_parameters: int = 0
    extra_swap_hook_implementation: str | None = None
    hooks: dict[str, Any] | None = None
    bins: dict[int, tuple[int, int]] = field(default_factory=dict)
    lo: int = 0
    hi: int = 0
    walked: dict[str, int] = field(default_factory=lambda: {"below": 0, "above": 0})
    envelope: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded: dict[str, Any] | None = None  # WHI-1436: set when the exclusion rule applies

    def done(self, side: str) -> bool:
        return self.lo == 0 if side == "below" else self.hi == UINT24_MAX

    def state(self, source_key: str, timestamp: int) -> LiquidityBookPoolState:
        return LiquidityBookPoolState(
            pool_id=self.address,
            source_key=source_key,
            token0=self.token_x,
            token1=self.token_y,
            bin_step=self.bin_step,
            block_timestamp=timestamp,
            active_id=self.active_id,
            reserve_x=self.reserve_x,
            reserve_y=self.reserve_y,
            protocol_fee_x=self.protocol_fee_x,
            protocol_fee_y=self.protocol_fee_y,
            static_fee=self.static_fee,
            variable_fee=self.variable_fee,
            bin_range=(self.lo, self.hi),
            bins=dict(self.bins),
            hooks_parameters=self.hooks_parameters,
            swap_hook_implementation=self.swap_hook_implementation,
            extra_hooks_parameters=self.extra_hooks_parameters,
            extra_swap_hook_implementation=self.extra_swap_hook_implementation,
        )


@dataclass(frozen=True)
class CollectedLBSnapshot:
    block: BlockRef
    bundle_id: str
    pools: tuple[LiquidityBookPoolState, ...]
    cases: tuple[Case, ...]
    provenance: dict[str, Any]


class LBCollector(FixedBlockReader):
    """One fixed-block LB collection run. Construct, then `collect()`; nothing is written
    here (see `publish`)."""

    def __init__(
        self,
        *,
        catalog: ProtocolCatalog,
        source_key: str,
        config: LBPrepareConfig,
        transport: BatchRpcTransport,
        block_number: int,
        expected_block_hash: str | None = None,
        rpc_label: str = "<injected transport>",
    ) -> None:
        self.source = _admitted_source(catalog, source_key)
        if config.source_key != source_key:
            raise PrepareError(
                "invalid_request",
                f"prepare config {config.source_path} is for {config.source_key!r}, "
                f"not {source_key!r}",
            )
        self.config = config
        assert self.source.lb_collection is not None
        self.swap_hooks = {h.implementation: h for h in self.source.lb_collection.swap_hooks}
        self.extra_hooks = {h.implementation: h for h in self.source.lb_collection.extra_swap_hooks}
        self.block: BlockRef | None = None
        self._pairs: dict[str, _Pair] = {}
        super().__init__(
            catalog=catalog,
            transport=transport,
            block_number=block_number,
            expected_block_hash=expected_block_hash,
            rpc_label=rpc_label,
            use_multicall3=config.rpc.use_multicall3,
        )

    def _pinned(self, role: str) -> tuple[str, str]:
        ref = self.source.contracts.get(role)
        if ref is None or ref.code_hash is None:
            raise PrepareError(
                "config_incomplete",
                f"{self.source.key}: catalog must pin {role!r} with a code_hash",
            )
        return ref.address.lower(), ref.code_hash.lower()

    def _words(self, result: str, n: int | None, what: str) -> list[int]:
        try:
            words = abi.decode_words(result)
        except ValueError as exc:
            raise PrepareError("inconsistent_state", f"{what}: {exc}") from exc
        if n is not None and len(words) != n:
            raise PrepareError(
                "inconsistent_state", f"{what}: returned {len(words)} word(s), expected {n}"
            )
        return words

    def _uint(self, word: int, bits: int, what: str) -> int:
        if word >> bits:
            raise PrepareError("inconsistent_state", f"{what}: {word} exceeds uint{bits}")
        return word

    def _address_of(self, result: str, what: str) -> str:
        try:
            return abi.word_to_address(self._words(result, 1, what)[0])
        except ValueError as exc:
            raise PrepareError("identity_mismatch", f"{what}: {exc}") from exc

    # -- 1. deployment -------------------------------------------------------

    def _verify_deployment(self) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for role in ("factory", "pair_implementation"):
            address, pinned = self._pinned(role)
            observed = abi.keccak256_hex(self._code(address))
            if observed.lower() != pinned:
                raise PrepareError(
                    "code_hash_mismatch",
                    f"{role} {address} code hash {observed} != pinned {pinned}",
                )
            record[role] = {"address": address, "code_hash": observed}
        what = "factory.getLBPairImplementation()"
        implementation = self._address_of(
            self._eth_call(
                record["factory"]["address"],
                abi.encode_call(abi.SEL_GET_LB_PAIR_IMPLEMENTATION),
                what,
            ),
            what,
        )
        if implementation != record["pair_implementation"]["address"]:
            raise PrepareError(
                "identity_mismatch",
                f"{what} = {implementation}, catalog pair_implementation "
                f"{record['pair_implementation']['address']}",
            )
        return record

    # -- 2./3. discovery and pair identity ------------------------------------

    def _discover(self, deployment: dict[str, Any]) -> tuple[list[_Pair], list[dict[str, Any]]]:
        factory = deployment["factory"]["address"]
        implementation = deployment["pair_implementation"]["address"]
        pairs: list[_Pair] = []
        omitted: list[dict[str, Any]] = []
        for spec in self.config.pairs:
            what = f"factory.getAllLBPairs {spec.token0}/{spec.token1}"
            words = self._words(
                self._eth_call(
                    factory,
                    abi.encode_call(
                        abi.SEL_GET_ALL_LB_PAIRS,
                        abi.pad_address(spec.token0),
                        abi.pad_address(spec.token1),
                    ),
                    what,
                ),
                None,
                what,
            )
            if len(words) < 2 or words[0] != 0x20 or len(words) != 2 + 4 * words[1]:
                raise PrepareError("inconsistent_state", f"{what}: malformed array")
            found = [tuple(words[2 + 4 * i : 6 + 4 * i]) for i in range(words[1])]
            excluded = dict(spec.excluded_bin_steps)
            if not found:
                omitted.append(
                    {
                        "token0": spec.token0,
                        "token1": spec.token1,
                        "bin_step": None,
                        "pool_id": None,
                        "reason": "factory.getAllLBPairs returned no pair",
                    }
                )
            steps = set()
            for bin_step, pair_word, by_owner, ignored in found:
                address = abi.word_to_address(pair_word)
                steps.add(bin_step)
                if bin_step in excluded:
                    omitted.append(
                        {
                            "token0": spec.token0,
                            "token1": spec.token1,
                            "bin_step": bin_step,
                            "pool_id": address,
                            "reason": f"excluded by prepare config: {excluded[bin_step]}",
                        }
                    )
                    continue
                pairs.append(
                    self._verify_pair(
                        address,
                        bin_step,
                        (spec.token0, spec.token1),
                        factory,
                        implementation,
                        bool(by_owner),
                        bool(ignored),
                    )
                )
            phantom = sorted(set(excluded) - steps)
            if phantom:
                raise PrepareError(
                    "invalid_request",
                    f"prepare config excludes {spec.token0}/{spec.token1} bin step(s) {phantom}, "
                    "which the factory does not list",
                )
        return pairs, omitted

    def _verify_pair(
        self,
        address: str,
        bin_step: int,
        tokens: tuple[str, str],
        factory: str,
        implementation: str,
        created_by_owner: bool,
        ignored_for_routing: bool,
    ) -> _Pair:
        code = self._code(address)
        clone = parse_clone(code)
        if clone is None or clone[0] != implementation or len(clone[1]) != 42:
            raise PrepareError(
                "code_hash_mismatch",
                f"pair {address}: runtime code ({len(code)} bytes, {abi.keccak256_hex(code)}) "
                f"is not an ImmutableClone of the verified {self.source.key} implementation "
                f"{implementation} with (tokenX, tokenY, binStep) args",
            )
        data = clone[1]
        token_x, token_y = "0x" + data[:20].hex(), "0x" + data[20:40].hex()
        arg_step = int.from_bytes(data[40:], "big")
        if tuple(sorted((token_x, token_y))) != tokens or arg_step != bin_step:
            raise PrepareError(
                "identity_mismatch",
                f"pair {address}: clone args ({token_x}, {token_y}, {arg_step}) are not the "
                f"discovered {tokens} bin step {bin_step}",
            )
        exact_hash = abi.keccak256_hex(code)
        example = self.source.contracts.get("example_pair")
        if (
            example is not None
            and example.code_hash is not None
            and example.address.lower() == address
            and exact_hash != example.code_hash.lower()
        ):
            raise PrepareError(
                "code_hash_mismatch",
                f"example pair {address} code hash {exact_hash} != pinned {example.code_hash}",
            )
        predicted = clone_create2_address(
            factory, implementation, data, pair_salt(token_x, token_y, bin_step)
        )
        if predicted != address:
            raise PrepareError(
                "identity_mismatch",
                f"pair {address} is not at the CREATE2 address {predicted} the factory "
                f"deploys ({token_x}, {token_y}, {bin_step}) to",
            )
        results = self._eth_calls(
            [
                (address, abi.encode_call(abi.SEL_GET_TOKEN_X)),
                (address, abi.encode_call(abi.SEL_GET_TOKEN_Y)),
                (address, abi.encode_call(abi.SEL_GET_FACTORY)),
                (address, abi.encode_call(abi.SEL_IMPLEMENTATION)),
                (address, abi.encode_call(abi.SEL_GET_BIN_STEP)),
                (
                    factory,
                    abi.encode_call(
                        abi.SEL_GET_LB_PAIR_INFORMATION,
                        abi.pad_address(token_x),
                        abi.pad_address(token_y),
                        abi.pad_uint(bin_step),
                    ),
                ),
            ],
            f"pair {address} identity",
        )
        observed = tuple(
            self._address_of(r, f"pair {address} {name}()")
            for r, name in zip(
                results[:4], ("getTokenX", "getTokenY", "getFactory", "implementation"), strict=True
            )
        )
        expected = (token_x, token_y, factory, implementation)
        step = self._words(results[4], 1, f"pair {address} getBinStep()")[0]
        if observed != expected or step != bin_step:
            raise PrepareError(
                "identity_mismatch",
                f"pair {address}: (getTokenX, getTokenY, getFactory, implementation, "
                f"getBinStep) = {(*observed, step)}, expected {(*expected, bin_step)}",
            )
        info = self._words(results[5], 4, "factory.getLBPairInformation")
        if (info[0], abi.word_to_address(info[1]), bool(info[2]), bool(info[3])) != (
            bin_step,
            address,
            created_by_owner,
            ignored_for_routing,
        ):
            raise PrepareError(
                "identity_mismatch",
                f"factory.getLBPairInformation({token_x}, {token_y}, {bin_step}) = {info}, "
                f"not the discovered pair {address}",
            )
        pair = _Pair(
            address=address,
            token_x=token_x,
            token_y=token_y,
            bin_step=bin_step,
            code_hash=exact_hash,
            created_by_owner=created_by_owner,
            ignored_for_routing=ignored_for_routing,
        )
        self._read_state(pair)
        self._verify_hooks(pair)
        return pair

    # -- 4. state ------------------------------------------------------------

    def _read_state(self, pair: _Pair) -> None:
        a = pair.address
        results = self._eth_calls(
            [
                (a, abi.encode_call(abi.SEL_GET_ACTIVE_ID)),
                (a, abi.encode_call(abi.SEL_GET_RESERVES)),
                (a, abi.encode_call(abi.SEL_GET_PROTOCOL_FEES)),
                (a, abi.encode_call(abi.SEL_GET_STATIC_FEE_PARAMETERS)),
                (a, abi.encode_call(abi.SEL_GET_VARIABLE_FEE_PARAMETERS)),
                (a, abi.encode_call(abi.SEL_GET_LB_HOOKS_PARAMETERS)),
                (pair.token_x, abi.encode_call(abi.SEL_BALANCE_OF, abi.pad_address(a))),
                (pair.token_y, abi.encode_call(abi.SEL_BALANCE_OF, abi.pad_address(a))),
            ],
            f"pair {a} state",
        )
        pair.active_id = self._uint(self._words(results[0], 1, "getActiveId")[0], 24, "activeId")
        pair.reserve_x, pair.reserve_y = (
            self._uint(w, 128, f"pair {a} getReserves")
            for w in self._words(results[1], 2, f"pair {a} getReserves")
        )
        pair.protocol_fee_x, pair.protocol_fee_y = (
            self._uint(w, 128, f"pair {a} getProtocolFees")
            for w in self._words(results[2], 2, f"pair {a} getProtocolFees")
        )
        static = self._words(results[3], 7, f"pair {a} getStaticFeeParameters")
        for w, bits in zip(static, (16, 12, 12, 14, 24, 14, 20), strict=True):
            self._uint(w, bits, f"pair {a} getStaticFeeParameters")
        pair.static_fee = LBStaticFeeParameters(*static)
        variable = self._words(results[4], 4, f"pair {a} getVariableFeeParameters")
        for w, bits in zip(variable, (20, 20, 24, 40), strict=True):
            self._uint(w, bits, f"pair {a} getVariableFeeParameters")
        pair.variable_fee = LBVariableFeeParameters(*variable)
        assert self.block is not None
        if pair.variable_fee.time_of_last_update > self.block.timestamp:
            raise PrepareError(
                "inconsistent_state",
                f"pair {a}: timeOfLastUpdate {pair.variable_fee.time_of_last_update} is after "
                f"the block timestamp {self.block.timestamp}",
            )
        pair.hooks_parameters = self._words(results[5], 1, f"pair {a} getLBHooksParameters")[0]
        balances = [self._words(r, 1, f"pair {a} balanceOf")[0] for r in results[6:8]]
        expected = [pair.reserve_x + pair.protocol_fee_x, pair.reserve_y + pair.protocol_fee_y]
        if balances != expected:
            raise PrepareError(
                "unsupported_token_behavior",
                f"pair {a}: token balances {balances} != reserves + protocol fees {expected}; "
                "the next swap would credit the difference, which the migrated state "
                "transition (standard tokens, balance == _reserves) does not model",
            )

    # -- 5. hooks ------------------------------------------------------------

    def _hook_clone(
        self, address: str, approved: dict[str, ApprovedHook], role: str, pair: str
    ) -> dict[str, Any]:
        code = self._code(address)
        clone = parse_clone(code)
        implementation = clone[0] if clone is not None else None
        hook = approved.get(implementation or "")
        if clone is None or hook is None:
            raise PrepareError(
                "unsupported_hook",
                f"pair {pair}: {role} {address} (implementation {implementation}) is not on "
                f"the catalog's approved {role} list {sorted(approved)}",
            )
        impl_hash = abi.keccak256_hex(self._code(hook.implementation))
        if impl_hash != hook.code_hash:
            raise PrepareError(
                "code_hash_mismatch",
                f"{role} implementation {hook.implementation} code hash {impl_hash} != "
                f"approved {hook.code_hash}",
            )
        if clone[1][:20] != bytes.fromhex(pair[2:]):
            raise PrepareError(
                "identity_mismatch", f"{role} {address}: clone args are not bound to pair {pair}"
            )
        what = f"{role} {address} getLBPair()"
        back = self._address_of(
            self._eth_call(address, abi.encode_call(abi.SEL_GET_LB_PAIR), what), what
        )
        if back != pair:
            raise PrepareError("identity_mismatch", f"{what} = {back}, not pair {pair}")
        return {
            "address": address,
            "code_hash": abi.keccak256_hex(code),
            "implementation": hook.implementation,
            "implementation_code_hash": impl_hash,
            "contract": hook.contract,
            "clone_args": "0x" + clone[1].hex(),
        }

    def _verify_hooks(self, pair: _Pair) -> None:
        params = pair.hooks_parameters
        address = abi.word_to_address(params & ADDRESS_MASK)
        if params & SWAP_FLAGS == 0:
            # The swap calls no hook (mint/burn/transfer hooks are out of domain); a
            # configured hook is still recorded.
            if not abi.is_zero_address(address):
                pair.hooks = {
                    "parameters": f"0x{params:064x}",
                    "address": address,
                    "code_hash": abi.keccak256_hex(self._code(address)),
                    "called_on_swap": False,
                }
            return
        if abi.is_zero_address(address):
            raise PrepareError(
                "unsupported_hook", f"pair {pair.address}: swap hook flags with no hooks address"
            )
        hook = self._hook_clone(address, self.swap_hooks, "swap hook", pair.address)
        pair.swap_hook_implementation = hook["implementation"]
        what = f"swap hook {address} getExtraHooksParameters()"
        extra_params = self._words(
            self._eth_call(address, abi.encode_call(abi.SEL_GET_EXTRA_HOOKS_PARAMETERS), what),
            1,
            what,
        )[0]
        pair.extra_hooks_parameters = extra_params
        hook["parameters"] = f"0x{params:064x}"
        hook["extra_hooks_parameters"] = f"0x{extra_params:064x}"
        hook["extra"] = None
        extra_address = abi.word_to_address(extra_params & ADDRESS_MASK)
        if extra_params & SWAP_FLAGS:
            if abi.is_zero_address(extra_address):
                raise PrepareError(
                    "unsupported_hook",
                    f"pair {pair.address}: extra hook swap flags with no extra hook address",
                )
            extra = self._hook_clone(extra_address, self.extra_hooks, "extra hook", pair.address)
            if extra["clone_args"][2 + 80 : 2 + 120] != address[2:]:
                raise PrepareError(
                    "identity_mismatch",
                    f"extra hook {extra_address}: clone args do not name rewarder {address}",
                )
            what = f"extra hook {extra_address} getParentRewarder()"
            parent = self._address_of(
                self._eth_call(extra_address, abi.encode_call(abi.SEL_GET_PARENT_REWARDER), what),
                what,
            )
            if parent != address:
                raise PrepareError("identity_mismatch", f"{what} = {parent}, not {address}")
            pair.extra_swap_hook_implementation = extra["implementation"]
            hook["extra"] = extra
        pair.hooks = hook

    # -- 6. envelope-complete bin walk -----------------------------------------

    def _walk(self, tasks: dict[tuple[str, str], int]) -> None:
        """Walk up to `count` more tree members on `side` of each pair's collected range
        (or to the end of the id space) for every `(pair, side) -> count` task, then read
        their bins. `getNextNonEmptyBin` answers are sequential per side, so each round
        batches one step of every unfinished task."""
        limit = self.config.limits.max_bins_per_direction
        found: dict[str, list[int]] = {address: [] for address, _ in tasks}
        remaining = {k: n for k, n in tasks.items() if n > 0 and not self._pairs[k[0]].done(k[1])}
        while remaining:
            calls: list[tuple[str, str]] = []
            for address, side in list(remaining):
                pair = self._pairs[address]
                if pair.walked[side] >= limit:
                    message = (
                        f"pair {address}: the declared envelope needs more than {limit} bins "
                        f"{side} the active bin {pair.active_id}"
                    )
                    rule = self.config.limits.exclusion_rule
                    if rule is None:
                        raise PrepareError(
                            "incomplete_snapshot",
                            f"{message}; refusing to publish a truncated envelope",
                        )
                    pair.excluded = {
                        "pool_id": address,
                        "token_x": pair.token_x,
                        "token_y": pair.token_y,
                        "bin_step": pair.bin_step,
                        "rule": rule,
                        "direction": "x_to_y" if side == "below" else "y_to_x",
                        "bound": {"max_bins_per_direction": limit},
                        "reason": f"excluded by rule {rule}: {message}",
                    }
                    for key in [k for k in remaining if k[0] == address]:
                        del remaining[key]
                    continue
            if not remaining:
                break
            for address, side in remaining:
                pair = self._pairs[address]
                below = side == "below"
                frontier = pair.lo if below else pair.hi
                calls.append(
                    (
                        address,
                        abi.encode_call(
                            abi.SEL_GET_NEXT_NON_EMPTY_BIN,
                            abi.pad_uint(int(below)),
                            abi.pad_uint(frontier),
                        ),
                    )
                )
            results = self._eth_calls(calls, "getNextNonEmptyBin walk")
            for (address, side), raw in zip(list(remaining), results, strict=True):
                pair = self._pairs[address]
                below = side == "below"
                frontier = pair.lo if below else pair.hi
                what = f"pair {address} getNextNonEmptyBin({below}, {frontier})"
                nxt = self._uint(self._words(raw, 1, what)[0], 24, what)
                if (below and nxt == UINT24_MAX) or (not below and nxt == 0):
                    if below:
                        pair.lo = 0
                    else:
                        pair.hi = UINT24_MAX
                    del remaining[(address, side)]
                    continue
                if (below and nxt >= frontier) or (not below and nxt <= frontier):
                    raise PrepareError("inconsistent_state", f"{what} = {nxt} does not advance")
                found[address].append(nxt)
                pair.walked[side] += 1
                if below:
                    pair.lo = nxt
                else:
                    pair.hi = nxt
                remaining[(address, side)] -= 1
                if remaining[(address, side)] == 0:
                    del remaining[(address, side)]
        self._read_bins([(address, i) for address, ids in found.items() for i in ids])

    def _read_bins(self, ids: list[tuple[str, int]]) -> None:
        """`getBin` for `(pair, id)` tree members (the active bin only if non-empty)."""
        if not ids:
            return
        results = self._eth_calls(
            [(a, abi.encode_call(abi.SEL_GET_BIN, abi.pad_uint(i))) for a, i in ids], "getBin"
        )
        for (address, bin_id), raw in zip(ids, results, strict=True):
            pair = self._pairs[address]
            what = f"pair {address} getBin({bin_id})"
            x, y = (self._uint(w, 128, what) for w in self._words(raw, 2, what))
            if x or y:
                pair.bins[bin_id] = (x, y)
            elif bin_id != pair.active_id:
                raise PrepareError(
                    "inconsistent_state", f"pair {address}: bin {bin_id} is in the tree but empty"
                )

    def _cover_envelopes(self, pairs: list[_Pair]) -> None:
        """Grow every pair's walked range until the largest reference case of each
        direction fits (or the book is proven exhausted), plus the margin."""
        assert self.block is not None
        limits = self.config.limits
        for pair in pairs:
            pair.lo = pair.hi = pair.active_id
        self._read_bins([(p.address, p.active_id) for p in pairs])
        open_: list[tuple[_Pair, bool]] = [(p, d) for p in pairs for d in (True, False)]
        while True:
            tasks: dict[tuple[str, str], int] = {}
            for pair, swap_for_y in open_:
                if pair.excluded is not None:
                    continue
                side = "below" if swap_for_y else "above"
                direction = "x_to_y" if swap_for_y else "y_to_x"
                token_in = pair.token_x if swap_for_y else pair.token_y
                token_out = pair.token_y if swap_for_y else pair.token_x
                amounts = [
                    c.amount_in
                    for c in self.config.cases
                    if c.token_in == token_in and c.token_out == token_out
                ]
                entry: dict[str, Any] = {"amount_in": None, "status": "no_reference_case"}
                end_id = pair.active_id
                if amounts:
                    amount = max(amounts)
                    state = pair.state(self.source.key, self.block.timestamp)
                    result = quote_exact_in(state, token_in, amount)
                    if result.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                        tasks[(pair.address, side)] = limits.walk_chunk_bins
                        continue
                    if result.status not in ADMITTED_STATUSES:
                        raise PrepareError(
                            "admission_failed",
                            f"pair {pair.address}: {direction} envelope quote is "
                            f"{result.status.value}: {result.detail}",
                        )
                    entry = {"amount_in": str(amount), "status": result.status.value}
                    if result.new_state is not None:
                        end_id = result.new_state.active_id
                        entry["end_active_id"] = end_id
                pair.envelope[direction] = entry
                beyond = sum(1 for i in pair.bins if (i < end_id if swap_for_y else i > end_id))
                if beyond < limits.margin_bins and not pair.done(side):
                    tasks[(pair.address, side)] = limits.margin_bins - beyond
            if not tasks:
                return
            self._walk(tasks)

    def _check_totals(self, pair: _Pair) -> None:
        total = (sum(b[0] for b in pair.bins.values()), sum(b[1] for b in pair.bins.values()))
        reserves = (pair.reserve_x, pair.reserve_y)
        complete = pair.done("below") and pair.done("above")
        if (complete and total != reserves) or total[0] > reserves[0] or total[1] > reserves[1]:
            raise PrepareError(
                "inconsistent_state",
                f"pair {pair.address}: collected bins hold {total}, reserves are {reserves} "
                f"({'whole tree' if complete else 'partial range'})",
            )

    # -- 7. admission --------------------------------------------------------

    def _admission(self, states: dict[str, LiquidityBookPoolState]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for case in self.config.cases:
            for state in states.values():
                if {state.token0, state.token1} != {case.token_in, case.token_out}:
                    continue
                result = quote_exact_in(state, case.token_in, case.amount_in)
                record: dict[str, Any] = {
                    "case_id": case.case_id,
                    "pool_id": state.pool_id,
                    "status": result.status.value,
                }
                followup = None
                if result.status is QuoteStatus.OK and result.new_state is not None:
                    back = result.amount_out // 2
                    if back > 0:
                        followup = quote_exact_in(result.new_state, case.token_out, back)
                        record["followup_amount_in"] = str(back)
                        record["followup_status"] = followup.status.value
                for what, res in (("case", result), ("sequential follow-up", followup)):
                    if res is not None and res.status not in ADMITTED_STATUSES:
                        raise PrepareError(
                            "incomplete_snapshot"
                            if res.status is QuoteStatus.INCOMPLETE_SNAPSHOT
                            else "admission_failed",
                            f"{what} of {case.case_id!r} on pair {state.pool_id}: "
                            f"{res.status.value}: {res.detail}",
                        )
                records.append(record)
        return records

    def _verify_tokens(self, pairs: list[_Pair]) -> dict[str, dict[str, Any]]:
        used = sorted({t for p in pairs for t in (p.token_x, p.token_y)})
        results = self._eth_calls(
            [(t, abi.encode_call(abi.SEL_DECIMALS)) for t in used], "token decimals()"
        )
        tokens: dict[str, dict[str, Any]] = {}
        for token, result in zip(used, results, strict=True):
            spec = self.config.tokens[token]  # pairs are discovered from declared tokens only
            decimals = self._words(result, 1, f"{token} decimals()")[0]
            if decimals != spec.decimals:
                raise PrepareError(
                    "identity_mismatch",
                    f"token {spec.label} {token}: decimals() = {decimals}, declared "
                    f"{spec.decimals}",
                )
            tokens[token] = {"label": spec.label, "decimals": decimals}
        return tokens

    # -- orchestration -------------------------------------------------------

    def collect(self) -> CollectedLBSnapshot:
        block = self.block = self._resolve_block()
        deployment = self._verify_deployment()
        pairs, omitted = self._discover(deployment)
        if not pairs:
            raise PrepareError("admission_failed", "discovery admitted no pair")
        tokens = self._verify_tokens(pairs)
        self._pairs = {p.address: p for p in pairs}
        self._cover_envelopes(pairs)
        excluded = [p.excluded for p in pairs if p.excluded is not None]
        for p in pairs:
            if p.excluded is not None:
                p.envelope.clear()
                omitted.append(p.excluded)
        pairs = [p for p in pairs if p.excluded is None]
        if not pairs:
            raise PrepareError(
                "incomplete_snapshot",
                "every discovered pair was excluded; a source without an admitted pool is "
                "incomplete and cannot be published",
            )
        for pair in pairs:
            self._check_totals(pair)
        states = {p.address: p.state(self.source.key, block.timestamp) for p in pairs}
        admission = self._admission(states)
        self._reverify_block(block)

        upstream = self.source.upstream
        limits = self.config.limits
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
            **({} if self.multicall_record is None else {"read_batching": self.multicall_record}),
            **(
                {}
                if self.config.limits.exclusion_rule is None
                else {"exclusions": {"rule": self.config.limits.exclusion_rule, "pools": excluded}}
            ),
            "source_capability": {
                "protocol_family": self.source.protocol_family,
                "sor_protocol": self.source.sor_protocol,
            },
            "catalog": {
                **deployment,
                "approved_swap_hooks": sorted(self.swap_hooks),
                "approved_extra_swap_hooks": sorted(self.extra_hooks),
                "upstream": None
                if upstream is None
                else {"repo": upstream.repo, "ref": upstream.ref, "license": upstream.license},
            },
            "prepare_config": {"path": self.config.source_path, "sha256": self.config.sha256},
            "tokens": dict(sorted(tokens.items())),
            "discovery": {
                "method": "factory.getAllLBPairs over configured token pairs (every bin step)",
                "admitted": [
                    {
                        "pool_id": p.address,
                        "token_x": p.token_x,
                        "token_y": p.token_y,
                        "bin_step": p.bin_step,
                        "created_by_owner": p.created_by_owner,
                        "ignored_for_routing": p.ignored_for_routing,
                    }
                    for p in pairs
                ],
                "omitted": omitted,
            },
            "pools": {
                p.address: {
                    "code_hash": p.code_hash,
                    "identity": (
                        "runtime code == ImmutableClone(pair_implementation, tokenX ++ tokenY ++ "
                        "uint16 binStep); address == CREATE2(factory, keccak256(abi.encode("
                        "tokenA, tokenB, binStep))); getLBPairInformation round trip; "
                        "getTokenX/getTokenY/getBinStep/getFactory/implementation agree"
                    ),
                    "balances_equal_reserves_plus_protocol_fees": True,
                    "hooks": p.hooks,
                    "completeness": {
                        "bin_range": [p.lo, p.hi],
                        "tree_bins": len(p.bins),
                        "walk": {
                            side: {"bins_walked": p.walked[side], "reached_end": p.done(side)}
                            for side in ("below", "above")
                        },
                        "limits": {
                            "walk_chunk_bins": limits.walk_chunk_bins,
                            "margin_bins": limits.margin_bins,
                            "max_bins_per_direction": limits.max_bins_per_direction,
                        },
                        "envelope": p.envelope,
                    },
                }
                for p in pairs
            },
            "admission": admission,
        }
        return CollectedLBSnapshot(
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
    if source.protocol_family != "liquidity_book_v2":
        raise PrepareError("invalid_request", f"{source_key!r} is not a liquidity_book_v2 source")
    if source.lb_collection is None or not source.lb_collection.admitted:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no admitted `lb_collection` entry in the catalog",
        )
    simulated = SIMULATED_SOURCES.get(source_key)
    if simulated is None:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no migrated swap semantics in pools.liquidity_book",
        )
    for approved, admitted, role in (
        (source.lb_collection.swap_hooks, simulated.amount_neutral_swap_hooks, "swap_hooks"),
        (
            source.lb_collection.extra_swap_hooks,
            simulated.amount_neutral_extra_swap_hooks,
            "extra_swap_hooks",
        ),
    ):
        unmodeled = sorted({h.implementation for h in approved} - admitted)
        if unmodeled:
            raise PrepareError(
                "config_incomplete",
                f"{source_key}: catalog lb_collection.{role} {unmodeled} are not admitted as "
                "amount-neutral by pools.liquidity_book",
            )
    return source


def publish(output_dir: Path, collected: CollectedLBSnapshot) -> SnapshotBundle:
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


def collect(
    request: PrepareRequest, *, transport: BatchRpcTransport | None = None
) -> SnapshotBundle:
    """`prepare --source moe_lb --block N`."""
    if request.block_number is None:
        raise PrepareError(
            "invalid_request",
            f"{SOURCE_KEY}: an explicit --block is required (collection never reads `latest`)",
        )
    if request.output_dir.exists():
        raise PrepareError("invalid_request", f"{request.output_dir} already exists")
    catalog = load_catalog(request.catalog_path)
    config = load_lb_prepare_config(request.prepare_config or DEFAULT_PREPARE_CONFIG)
    rpc_url = request.rpc_url or catalog.network.default_rpc_url
    http: HttpJsonRpcTransport | None = None
    if transport is None:
        http = http_transport(rpc_url, config.rpc, request.cache_dir)
        transport = http
    collector = LBCollector(
        catalog=catalog,
        source_key=SOURCE_KEY,
        config=config,
        transport=transport,
        block_number=request.block_number,
        expected_block_hash=request.expected_block_hash,
        rpc_label=redact_url(rpc_url) if http is not None else "<injected transport>",
    )
    bundle = publish(request.output_dir, collector.collect())
    if http is not None:
        print(
            f"rpc: {http.call_count} request(s), {http.retries} retry round(s), "
            f"{http.cache_hits} memory / {http.disk_cache_hits} disk cache hit(s)",
            file=sys.stderr,
        )
    return bundle
