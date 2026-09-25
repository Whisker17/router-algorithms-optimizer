"""Block-pinned Merchant Moe Classic v1 collector (WHI-1432): `prepare --source
moe_classic`.

Collects constant-product pairs of an admitted `v2_classic` source (today only
`moe_classic_v1`: `config/protocols.yaml` `classic_collection.admitted`) whose swap
semantics `pools.constant_product.SOURCES` has migrated. The flow (docs/DESIGN.md §2.2,
§4.4 Prepare), on top of `FixedBlockReader`'s block identity and hash-pinned reads:

1. **Deployment.** `MoeFactory` and the `MoePair` clone implementation code hashes equal
   the catalog pins, `factory.moePairImplementation()` is the pinned implementation, and
   the catalog's `classic_collection.swap_fee_bps` equals the migrated simulator's fixed
   fee (a catalog/simulator disagreement refuses to run).
2. **Discovery.** For each configured token pair, `factory.getPair` in both argument
   orders (they must agree; zero -> recorded omission, a configured exclusion -> a
   reasoned omission).
3. **Pair identity** (fail closed, before any state is trusted). The pair's runtime code
   must be *exactly* the `ImmutableClone` (lib/dexv2 `ImmutableClone.cloneDeterministic`,
   used by `MoeFactory.createPair`) runtime for the pinned implementation with immutable
   args `token0 ++ token1` -- so the clone target *and* the sorted token order are
   verified from the bytecode itself -- and the pair address must equal the CREATE2
   address `MoeFactory` would deploy it at (salt `keccak256(token0 ++ token1)`). The
   pair's own `token0()/token1()/factory()/implementation()` must agree. Any other
   code (an LB clone, a clone of another implementation, swapped args) is rejected.
4. **State + token behaviour.** `getReserves()` and each token's `balanceOf(pair)`: a
   balance different from its reserve (a pending donation, a rebasing or otherwise
   non-standard token) is refused, since the migrated next state assumes the pair's
   post-swap balances are exactly reserves plus the transfer. Each token's `decimals()`
   must equal the prepare config's declaration; tokens outside the declaration are
   never collected.
5. **Admission.** Every reference case is quoted on every pair of its token pair;
   anything but `ok`, or the pair's own dust refusal (`insufficient_output_amount`,
   on-chain `Moe: INSUFFICIENT_OUTPUT_AMOUNT`), refuses publication.
6. **Re-verification + atomic publish** through `snapshot.bundle.write_bundle`, every
   pool record stamped with `source_key` and the block identity and a deterministic
   `provenance.json` (catalog pins, clone/CREATE2 identity, tokens, discovery,
   admission, SOR capability).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pools.constant_product import SOURCES as SIMULATED_SOURCES
from pools.constant_product import quote_exact_in
from pools.result import QuoteStatus
from snapshot import abi
from snapshot.bundle import write_bundle
from snapshot.collectors.base import PrepareError, PrepareRequest
from snapshot.collectors.fixed_block import FixedBlockReader, http_transport
from snapshot.config import ProtocolCatalog, SourceConfig, load_catalog
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle
from snapshot.preflight import redact_url
from snapshot.prepare_config import ClassicPrepareConfig, load_classic_prepare_config
from snapshot.rpc import BatchRpcTransport, HttpJsonRpcTransport

SOURCE_KEY = "moe_classic_v1"
PROVENANCE_SCHEMA = "classic-collection/1"
COLLECTOR = "snapshot.collectors.classic"
DEFAULT_PREPARE_CONFIG = Path("config/prepare/moe_classic.yaml")
ADMITTED_STATUSES = (QuoteStatus.OK, QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT)

# lib/dexv2 src/libraries/ImmutableClone.sol (pinned by merchant-moe/moe-core@460bf55):
# runtime = PREFIX | extraLength (2) | MIDDLE | implementation (20) | SUFFIX | data |
# extraLength (2), extraLength = len(data) + 2; creation = 61 runSize 3d81600a3d39f3 |
# runtime.
_CLONE_PREFIX = bytes.fromhex("363d3d373d3d3d3d61")
_CLONE_MIDDLE = bytes.fromhex("806035363936013d73")
_CLONE_SUFFIX = bytes.fromhex("5af43d3d93803e603357fd5bf3")
_CLONE_CREATION = bytes.fromhex("3d81600a3d39f3")


def clone_runtime_code(implementation: str, data: bytes) -> bytes:
    """The runtime code `ImmutableClone.cloneDeterministic(implementation, data, _)`
    deploys."""
    extra = (len(data) + 2).to_bytes(2, "big")
    impl = bytes.fromhex(implementation.removeprefix("0x"))
    return _CLONE_PREFIX + extra + _CLONE_MIDDLE + impl + _CLONE_SUFFIX + data + extra


def clone_create2_address(deployer: str, implementation: str, data: bytes, salt: bytes) -> str:
    """`ImmutableClone.predictDeterministicAddress`: the CREATE2 address
    `cloneDeterministic(implementation, data, salt)` deploys to from `deployer` (the same
    library layout in moe-core's lib/dexv2 and lfj-gg/joe-v2 v2.2.0)."""
    runtime = clone_runtime_code(implementation, data)
    creation = b"\x61" + len(runtime).to_bytes(2, "big") + _CLONE_CREATION + runtime
    preimage = (
        b"\xff"
        + bytes.fromhex(deployer.removeprefix("0x"))
        + salt
        + bytes.fromhex(abi.keccak256_hex(creation)[2:])
    )
    return "0x" + abi.keccak256_hex(preimage)[-40:]


def clone_address(factory: str, implementation: str, token0: str, token1: str) -> str:
    """`MoeLibrary.pairFor`: the CREATE2 address of the pair
    `MoeFactory.createPair(token0, token1)` deploys (salt `keccak256(token0 ++ token1)`)."""
    data = bytes.fromhex(token0.removeprefix("0x")) + bytes.fromhex(token1.removeprefix("0x"))
    return clone_create2_address(
        factory, implementation, data, bytes.fromhex(abi.keccak256_hex(data)[2:])
    )


@dataclass(frozen=True)
class _Pair:
    address: str
    token0: str
    token1: str
    code_hash: str
    reserve0: int
    reserve1: int
    block_timestamp_last: int

    def state(self, source_key: str, fee_bps: int) -> ConstantProductPoolState:
        return ConstantProductPoolState(
            pool_id=self.address,
            token0=self.token0,
            token1=self.token1,
            reserve0=self.reserve0,
            reserve1=self.reserve1,
            fee_bps=fee_bps,
            source_key=source_key,
        )


@dataclass(frozen=True)
class CollectedClassicSnapshot:
    block: BlockRef
    bundle_id: str
    pools: tuple[ConstantProductPoolState, ...]
    cases: tuple[Case, ...]
    provenance: dict[str, Any]


class ClassicCollector(FixedBlockReader):
    """One fixed-block Classic collection run. Construct, then `collect()`; nothing is
    written here (see `publish`)."""

    def __init__(
        self,
        *,
        catalog: ProtocolCatalog,
        source_key: str,
        config: ClassicPrepareConfig,
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
        self.fee_bps = SIMULATED_SOURCES[source_key].fee_bps
        super().__init__(
            catalog=catalog,
            transport=transport,
            block_number=block_number,
            expected_block_hash=expected_block_hash,
            rpc_label=rpc_label,
        )

    def _pinned(self, role: str) -> tuple[str, str]:
        ref = self.source.contracts.get(role)
        if ref is None or ref.code_hash is None:
            raise PrepareError(
                "config_incomplete",
                f"{self.source.key}: catalog must pin {role!r} with a code_hash",
            )
        return ref.address.lower(), ref.code_hash.lower()

    def _words(self, result: str, n: int, what: str) -> list[int]:
        try:
            words = abi.decode_words(result)
        except ValueError as exc:
            raise PrepareError("inconsistent_state", f"{what}: {exc}") from exc
        if len(words) != n:
            raise PrepareError(
                "inconsistent_state", f"{what}: returned {len(words)} word(s), expected {n}"
            )
        return words

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
        factory = record["factory"]["address"]
        implementation = self._address_of(
            self._eth_call(
                factory,
                abi.encode_call(abi.SEL_MOE_PAIR_IMPLEMENTATION),
                "factory.moePairImplementation()",
            ),
            "factory.moePairImplementation()",
        )
        if implementation != record["pair_implementation"]["address"]:
            raise PrepareError(
                "identity_mismatch",
                f"factory.moePairImplementation() = {implementation}, catalog "
                f"pair_implementation {record['pair_implementation']['address']}",
            )
        return record

    # -- 2./3. discovery and pair identity ------------------------------------

    def _discover(self, deployment: dict[str, Any]) -> tuple[list[_Pair], list[dict[str, Any]]]:
        factory = deployment["factory"]["address"]
        implementation = deployment["pair_implementation"]["address"]
        pairs: list[_Pair] = []
        omitted: list[dict[str, Any]] = []
        for spec in self.config.pairs:
            forward, backward = self._eth_calls(
                [
                    (
                        factory,
                        abi.encode_call(abi.SEL_GET_PAIR, abi.pad_address(a), abi.pad_address(b)),
                    )
                    for a, b in ((spec.token0, spec.token1), (spec.token1, spec.token0))
                ],
                f"factory.getPair {spec.token0}/{spec.token1}",
            )
            address = self._address_of(forward, "factory.getPair")
            if self._address_of(backward, "factory.getPair") != address:
                raise PrepareError(
                    "identity_mismatch",
                    f"factory.getPair({spec.token0}, {spec.token1}) and the reverse order disagree",
                )
            pool_id = None if abi.is_zero_address(address) else address
            if spec.excluded is not None or pool_id is None:
                omitted.append(
                    {
                        "token0": spec.token0,
                        "token1": spec.token1,
                        "pool_id": pool_id,
                        "reason": "factory.getPair returned the zero address"
                        if spec.excluded is None
                        else f"excluded by prepare config: {spec.excluded}",
                    }
                )
                continue
            pairs.append(
                self._verify_pair(address, spec.token0, spec.token1, factory, implementation)
            )
        return pairs, omitted

    def _verify_pair(
        self, address: str, token0: str, token1: str, factory: str, implementation: str
    ) -> _Pair:
        code = self._code(address)
        data = bytes.fromhex(token0[2:]) + bytes.fromhex(token1[2:])
        expected_code = clone_runtime_code(implementation, data)
        if code != expected_code:
            raise PrepareError(
                "code_hash_mismatch",
                f"pair {address}: runtime code ({len(code)} bytes, "
                f"{abi.keccak256_hex(code)}) is not the ImmutableClone of the verified "
                f"{self.source.key} implementation {implementation} with args "
                f"({token0}, {token1})",
            )
        exact_hash = abi.keccak256_hex(code)
        example = self.source.contracts.get("example_pool")
        if (
            example is not None
            and example.code_hash is not None
            and example.address.lower() == address
            and exact_hash != example.code_hash.lower()
        ):
            raise PrepareError(
                "code_hash_mismatch",
                f"example pool {address} code hash {exact_hash} != pinned {example.code_hash}",
            )
        predicted = clone_address(factory, implementation, token0, token1)
        if predicted != address:
            raise PrepareError(
                "identity_mismatch",
                f"pair {address} is not at the CREATE2 address {predicted} the factory "
                f"deploys ({token0}, {token1}) to",
            )
        results = self._eth_calls(
            [
                (address, abi.encode_call(abi.SEL_TOKEN0)),
                (address, abi.encode_call(abi.SEL_TOKEN1)),
                (address, abi.encode_call(abi.SEL_FACTORY)),
                (address, abi.encode_call(abi.SEL_IMPLEMENTATION)),
                (address, abi.encode_call(abi.SEL_GET_RESERVES)),
                (token0, abi.encode_call(abi.SEL_BALANCE_OF, abi.pad_address(address))),
                (token1, abi.encode_call(abi.SEL_BALANCE_OF, abi.pad_address(address))),
            ],
            f"pair {address} identity and state",
        )
        observed = tuple(
            self._address_of(r, f"pair {address} {name}()")
            for r, name in zip(
                results[:4], ("token0", "token1", "factory", "implementation"), strict=True
            )
        )
        if observed != (token0, token1, factory, implementation):
            raise PrepareError(
                "identity_mismatch",
                f"pair {address}: (token0, token1, factory, implementation) = {observed}, "
                f"expected {(token0, token1, factory, implementation)}",
            )
        reserve0, reserve1, ts_last = self._words(results[4], 3, f"pair {address} getReserves()")
        limit = (1 << SIMULATED_SOURCES[self.source.key].reserve_bits) - 1
        if reserve0 > limit or reserve1 > limit or ts_last >= 1 << 32:
            raise PrepareError(
                "inconsistent_state", f"pair {address}: getReserves() exceeds uint112/uint32"
            )
        balances = [self._words(r, 1, f"pair {address} balanceOf")[0] for r in results[5:]]
        if balances != [reserve0, reserve1]:
            raise PrepareError(
                "unsupported_token_behavior",
                f"pair {address}: token balances {balances} != reserves "
                f"{[reserve0, reserve1]}; the next swap would credit the difference, which "
                "the migrated state transition (standard tokens, balance == reserve) does "
                "not model",
            )
        return _Pair(
            address=address,
            token0=token0,
            token1=token1,
            code_hash=exact_hash,
            reserve0=reserve0,
            reserve1=reserve1,
            block_timestamp_last=ts_last,
        )

    def _verify_tokens(self, pairs: list[_Pair]) -> dict[str, dict[str, Any]]:
        used = sorted({t for p in pairs for t in (p.token0, p.token1)})
        results = self._eth_calls(
            [(t, abi.encode_call(abi.SEL_DECIMALS)) for t in used], "token decimals()"
        )
        tokens: dict[str, dict[str, Any]] = {}
        for token, result in zip(used, results, strict=True):
            spec = self.config.tokens.get(token)
            if spec is None:  # pairs are built from declared tokens only
                raise PrepareError("unsupported_token", f"{token} is not a declared token")
            decimals = self._words(result, 1, f"{token} decimals()")[0]
            if decimals != spec.decimals:
                raise PrepareError(
                    "identity_mismatch",
                    f"token {spec.label} {token}: decimals() = {decimals}, declared "
                    f"{spec.decimals}",
                )
            tokens[token] = {"label": spec.label, "decimals": decimals}
        return tokens

    # -- 5. admission --------------------------------------------------------

    def _admission(self, states: dict[str, ConstantProductPoolState]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for case in self.config.cases:
            for state in states.values():
                if {state.token0, state.token1} != {case.token_in, case.token_out}:
                    continue
                result = quote_exact_in(state, case.token_in, case.amount_in)
                if result.status not in ADMITTED_STATUSES:
                    raise PrepareError(
                        "admission_failed",
                        f"case {case.case_id!r} on pair {state.pool_id}: "
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

    def collect(self) -> CollectedClassicSnapshot:
        block = self._resolve_block()
        deployment = self._verify_deployment()
        pairs, omitted = self._discover(deployment)
        if not pairs:
            raise PrepareError("admission_failed", "discovery admitted no pair")
        tokens = self._verify_tokens(pairs)
        states = {p.address: p.state(self.source.key, self.fee_bps) for p in pairs}
        admission = self._admission(states)
        self._reverify_block(block)

        assert self.source.classic_collection is not None
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
                "swap_fee_bps": self.source.classic_collection.swap_fee_bps,
                "upstream": None
                if upstream is None
                else {"repo": upstream.repo, "ref": upstream.ref, "license": upstream.license},
            },
            "prepare_config": {"path": self.config.source_path, "sha256": self.config.sha256},
            "tokens": dict(sorted(tokens.items())),
            "discovery": {
                "method": "factory.getPair (both argument orders) over configured token pairs",
                "admitted": [
                    {"pool_id": p.address, "token0": p.token0, "token1": p.token1} for p in pairs
                ],
                "omitted": omitted,
            },
            "pools": {
                p.address: {
                    "code_hash": p.code_hash,
                    "identity": (
                        "runtime code == ImmutableClone(pair_implementation, token0 ++ token1); "
                        "address == CREATE2(factory, keccak256(token0 ++ token1)); "
                        "token0/token1/factory/implementation getters agree"
                    ),
                    "block_timestamp_last": p.block_timestamp_last,
                    "balances_equal_reserves": True,
                }
                for p in pairs
            },
            "admission": admission,
        }
        return CollectedClassicSnapshot(
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
    if source.protocol_family != "v2_classic":
        raise PrepareError("invalid_request", f"{source_key!r} is not a v2_classic source")
    if source.classic_collection is None or not source.classic_collection.admitted:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no admitted `classic_collection` entry in the catalog",
        )
    simulated = SIMULATED_SOURCES.get(source_key)
    if simulated is None:
        raise PrepareError(
            "source_not_admitted",
            f"{source_key!r} has no migrated swap semantics in pools.constant_product",
        )
    if simulated.fee_bps != source.classic_collection.swap_fee_bps:
        raise PrepareError(
            "config_incomplete",
            f"{source_key}: catalog swap_fee_bps {source.classic_collection.swap_fee_bps} != "
            f"migrated simulator fee_bps {simulated.fee_bps}",
        )
    return source


def publish(output_dir: Path, collected: CollectedClassicSnapshot) -> SnapshotBundle:
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
    """`prepare --source moe_classic --block N`."""
    if request.block_number is None:
        raise PrepareError(
            "invalid_request",
            f"{SOURCE_KEY}: an explicit --block is required (collection never reads `latest`)",
        )
    if request.output_dir.exists():
        raise PrepareError("invalid_request", f"{request.output_dir} already exists")
    catalog = load_catalog(request.catalog_path)
    config = load_classic_prepare_config(request.prepare_config or DEFAULT_PREPARE_CONFIG)
    rpc_url = request.rpc_url or catalog.network.default_rpc_url
    http: HttpJsonRpcTransport | None = None
    if transport is None:
        http = http_transport(rpc_url, config.rpc, request.cache_dir)
        transport = http
    collector = ClassicCollector(
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
