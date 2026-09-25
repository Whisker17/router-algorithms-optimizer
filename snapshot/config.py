"""Typed, validated loader for `config/protocols.yaml`.

This is the machine-readable half of the five-source admission catalog (the
human-readable findings/narrative live in `docs/references/protocol-admission.md`).
Loading is fail-fast: an unknown key, a missing required field, or a malformed
address kills the process with a clear error rather than silently continuing with
partial data (docs/DESIGN.md §2.12 / config/README.md convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_HEX_DIGITS = set("0123456789abcdefABCDEF")


class ConfigError(ValueError):
    """The config file is missing, malformed, or fails schema validation."""


def _require_keys(obj: dict[str, Any], required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise ConfigError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")


def _validate_address(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise ConfigError(f"{where}: not a 20-byte 0x-address: {value!r}")
    if not all(c in _HEX_DIGITS for c in value[2:]):
        raise ConfigError(f"{where}: not valid hex: {value!r}")
    return value


def _validate_hash32(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise ConfigError(f"{where}: not a 32-byte 0x-hash: {value!r}")
    if not all(c in _HEX_DIGITS for c in value[2:]):
        raise ConfigError(f"{where}: not valid hex: {value!r}")
    return value


KNOWN_PROTOCOL_FAMILIES = {"v3_concentrated_liquidity", "v2_classic", "liquidity_book_v2"}
KNOWN_CONFIDENCE_LEVELS = {"high", "moderate", "low", "unmatched"}


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    chain_id: int
    default_rpc_url: str


@dataclass(frozen=True)
class CandidateBlock:
    number: int
    hash: str
    timestamp: int
    captured_at: str
    status: str
    note: str


@dataclass(frozen=True)
class ContractRef:
    address: str
    code_hash: str | None = None
    verification: str = ""


@dataclass(frozen=True)
class UpstreamProvenance:
    repo: str
    ref: str
    license: str
    confidence_basis: str


@dataclass(frozen=True)
class Blocker:
    id: str
    description: str
    blocks: tuple[str, ...] = ()


KNOWN_POOL_IMMUTABLES = (
    "factory",
    "token0",
    "token1",
    "fee",
    "tickSpacing",
    "maxLiquidityPerTick",
    # `NoDelegateCall.original` (Uniswap v3-core): `address(this)` captured at
    # construction, so its value is the pool's own address (no getter).
    "original",
)
# Uniswap SOR route protocols a source's pools can enter as (docs/references/
# uni-sor-port-contract.md §2: V3 = Agni/FusionX/Uniswap v3, V2 = Moe Classic; LB none).
KNOWN_SOR_PROTOCOLS = {"V2", "V3"}
# The only pool family each SOR route protocol can represent (V2 = constant-product
# `Pair`, V3 = concentrated-liquidity `Pool`).
_SOR_FAMILY = {"V2": "v2_classic", "V3": "v3_concentrated_liquidity"}


@dataclass(frozen=True)
class ClCollectionConfig:
    """Fixed-block collection admission for one concentrated-liquidity source
    (WHI-1429). `admitted` is the per-source switch the shared collector
    (`snapshot.collectors.concentrated`) refuses to run without: a sibling V3 fork is
    never collected just because the code is shared. Every pool a factory deploys
    embeds its own immutables, so non-example pools are verified by
    `pool_code_normalized_hash` -- the Keccak-256 of the runtime code with every
    `PUSH32` immediate equal to one of `pool_immutables` (read from the pool's own
    getters) zeroed (`snapshot.abi.immutable_normalized_code_hash`).

    `factory_deploys_pools` (WHI-1431): the factory itself CREATE2-deploys pools
    (Uniswap v3's `UniswapV3Factory is UniswapV3PoolDeployer`), so there is no separate
    `pool_deployer` contract to pin or `factory.poolDeployer()` to round-trip."""

    admitted: bool
    pool_code_normalized_hash: str
    pool_immutables: tuple[str, ...]
    verification: str
    factory_deploys_pools: bool = False


@dataclass(frozen=True)
class ClassicCollectionConfig:
    """Fixed-block collection admission for one constant-product (`v2_classic`) source
    (WHI-1432) -- the Classic counterpart of `ClCollectionConfig`. `swap_fee_bps` is
    the pair's fixed swap fee as read from the verified source; the collector refuses
    to run unless it equals the migrated simulator's (`pools.constant_product.SOURCES`),
    so the catalog fact and the Python semantics can never drift apart silently."""

    admitted: bool
    swap_fee_bps: int
    verification: str


@dataclass(frozen=True)
class ApprovedHook:
    """One hooks-clone implementation an LB pair may run on a swap: `implementation` is
    the delegatecall target of the hook's `ImmutableClone`, `code_hash` its pinned
    runtime code hash, `contract` the verified contract name."""

    implementation: str
    code_hash: str
    contract: str
    verification: str


@dataclass(frozen=True)
class LbCollectionConfig:
    """Fixed-block collection admission for one Liquidity Book source (WHI-1434) -- the
    LB counterpart of `ClassicCollectionConfig`. `swap_hooks` is the approved list for a
    pair's `getLBHooksParameters()` hook, `extra_swap_hooks` the approved list for the
    extra hook an `LBHooksRewarder` forwards `beforeSwap` to
    (`getExtraHooksParameters()`). A pool whose swap-flagged hook (or extra hook) is not
    listed is refused; every listed implementation must also be one the simulator
    admits (`pools.liquidity_book.SOURCES`), so the catalog and the Python semantics
    cannot drift apart silently."""

    admitted: bool
    swap_hooks: tuple[ApprovedHook, ...]
    extra_swap_hooks: tuple[ApprovedHook, ...]
    verification: str


@dataclass(frozen=True)
class SourceConfig:
    key: str
    display_name: str
    protocol_family: str
    confidence: str
    contracts: dict[str, ContractRef]
    tokens: dict[str, str] = field(default_factory=dict)
    pool_fee: int | None = None
    expected_fee_tiers: dict[int, int] | None = None
    upstream: UpstreamProvenance | None = None
    blockers: tuple[Blocker, ...] = ()
    notes: str = ""
    cl_collection: ClCollectionConfig | None = None
    classic_collection: ClassicCollectionConfig | None = None
    lb_collection: LbCollectionConfig | None = None
    # Source capability for cohort selection: the Uniswap SOR route protocol this
    # source's pools enter the matched V2/V3 cohort as, or None (never SOR-routable).
    sor_protocol: str | None = None


@dataclass(frozen=True)
class ProtocolCatalog:
    version: int
    network: NetworkConfig
    candidate_block: CandidateBlock
    sources: tuple[SourceConfig, ...]

    def source(self, key: str) -> SourceConfig:
        for source in self.sources:
            if source.key == key:
                return source
        raise KeyError(f"no source configured with key {key!r}")

    def sor_protocols(self) -> dict[str, str]:
        """Source key -> SOR route protocol for every SOR-compatible source: the
        capability cohort selection uses to form the matched V2/V3 cohort."""
        return {s.key: s.sor_protocol for s in self.sources if s.sor_protocol is not None}


def _parse_network(obj: Any) -> NetworkConfig:
    _require_keys(obj, {"name", "chain_id", "default_rpc_url"}, set(), "network")
    if not isinstance(obj["chain_id"], int) or isinstance(obj["chain_id"], bool):
        raise ConfigError(f"network.chain_id: expected int, got {obj['chain_id']!r}")
    if not str(obj["default_rpc_url"]).startswith(("http://", "https://")):
        raise ConfigError("network.default_rpc_url: must be an http(s) URL")
    return NetworkConfig(
        name=str(obj["name"]), chain_id=obj["chain_id"], default_rpc_url=str(obj["default_rpc_url"])
    )


def _parse_candidate_block(obj: Any) -> CandidateBlock:
    required = {"number", "hash", "timestamp", "captured_at", "status", "note"}
    _require_keys(obj, required, set(), "candidate_block")
    return CandidateBlock(
        number=int(obj["number"]),
        hash=_validate_hash32(obj["hash"], "candidate_block.hash"),
        timestamp=int(obj["timestamp"]),
        captured_at=str(obj["captured_at"]),
        status=str(obj["status"]),
        note=str(obj["note"]),
    )


def _parse_upstream(obj: Any, where: str) -> UpstreamProvenance:
    _require_keys(obj, {"repo", "ref", "license", "confidence_basis"}, set(), where)
    return UpstreamProvenance(
        repo=str(obj["repo"]),
        ref=str(obj["ref"]),
        license=str(obj["license"]),
        confidence_basis=str(obj["confidence_basis"]),
    )


def _parse_blocker(obj: Any, where: str) -> Blocker:
    _require_keys(obj, {"id", "description"}, {"blocks"}, where)
    blocks = obj.get("blocks", [])
    if not isinstance(blocks, list):
        raise ConfigError(f"{where}.blocks: expected a list")
    return Blocker(id=str(obj["id"]), description=str(obj["description"]), blocks=tuple(blocks))


def _parse_contract_ref(obj: Any, where: str) -> ContractRef:
    if isinstance(obj, str):
        # Bare-address shorthand: no code hash pinned yet for this role.
        return ContractRef(address=_validate_address(obj, where))
    _require_keys(obj, {"address"}, {"code_hash", "verification"}, where)
    address = _validate_address(obj["address"], f"{where}.address")
    code_hash = obj.get("code_hash")
    if code_hash is not None:
        code_hash = _validate_hash32(code_hash, f"{where}.code_hash")
    return ContractRef(
        address=address, code_hash=code_hash, verification=str(obj.get("verification", ""))
    )


def _parse_cl_collection(obj: Any, where: str) -> ClCollectionConfig:
    _require_keys(
        obj,
        {"admitted", "pool_code_normalized_hash", "pool_immutables", "verification"},
        {"factory_deploys_pools"},
        where,
    )
    for flag in ("admitted", "factory_deploys_pools"):
        if not isinstance(obj.get(flag, False), bool):
            raise ConfigError(f"{where}.{flag}: expected a boolean")
    immutables = obj["pool_immutables"]
    if not isinstance(immutables, list) or not immutables:
        raise ConfigError(f"{where}.pool_immutables: expected a non-empty list")
    unknown = [name for name in immutables if name not in KNOWN_POOL_IMMUTABLES]
    if unknown or len(set(immutables)) != len(immutables):
        raise ConfigError(
            f"{where}.pool_immutables: {immutables!r} must be distinct names from "
            f"{list(KNOWN_POOL_IMMUTABLES)}"
        )
    return ClCollectionConfig(
        admitted=obj["admitted"],
        pool_code_normalized_hash=_validate_hash32(
            obj["pool_code_normalized_hash"], f"{where}.pool_code_normalized_hash"
        ),
        pool_immutables=tuple(str(name) for name in immutables),
        verification=str(obj["verification"]),
        factory_deploys_pools=obj.get("factory_deploys_pools", False),
    )


def _parse_classic_collection(obj: Any, where: str) -> ClassicCollectionConfig:
    _require_keys(obj, {"admitted", "swap_fee_bps", "verification"}, set(), where)
    if not isinstance(obj["admitted"], bool):
        raise ConfigError(f"{where}.admitted: expected a boolean")
    fee = obj["swap_fee_bps"]
    if not isinstance(fee, int) or isinstance(fee, bool) or not (0 < fee < 10_000):
        raise ConfigError(f"{where}.swap_fee_bps: expected an int in (0, 10000), got {fee!r}")
    return ClassicCollectionConfig(
        admitted=obj["admitted"], swap_fee_bps=fee, verification=str(obj["verification"])
    )


def _parse_approved_hooks(obj: Any, where: str) -> tuple[ApprovedHook, ...]:
    if not isinstance(obj, list):
        raise ConfigError(f"{where}: expected a list")
    hooks: list[ApprovedHook] = []
    for i, entry in enumerate(obj):
        w = f"{where}[{i}]"
        _require_keys(entry, {"implementation", "code_hash", "contract", "verification"}, set(), w)
        hooks.append(
            ApprovedHook(
                implementation=_validate_address(
                    entry["implementation"], f"{w}.implementation"
                ).lower(),
                code_hash=_validate_hash32(entry["code_hash"], f"{w}.code_hash").lower(),
                contract=str(entry["contract"]),
                verification=str(entry["verification"]),
            )
        )
    if len({h.implementation for h in hooks}) != len(hooks):
        raise ConfigError(f"{where}: duplicate implementation")
    return tuple(hooks)


def _parse_lb_collection(obj: Any, where: str) -> LbCollectionConfig:
    _require_keys(obj, {"admitted", "swap_hooks", "extra_swap_hooks", "verification"}, set(), where)
    if not isinstance(obj["admitted"], bool):
        raise ConfigError(f"{where}.admitted: expected a boolean")
    return LbCollectionConfig(
        admitted=obj["admitted"],
        swap_hooks=_parse_approved_hooks(obj["swap_hooks"], f"{where}.swap_hooks"),
        extra_swap_hooks=_parse_approved_hooks(
            obj["extra_swap_hooks"], f"{where}.extra_swap_hooks"
        ),
        verification=str(obj["verification"]),
    )


def _parse_source(obj: Any) -> SourceConfig:
    required = {"key", "display_name", "protocol_family", "confidence", "contracts"}
    optional = {
        "tokens",
        "pool_fee",
        "expected_fee_tiers",
        "upstream",
        "blockers",
        "notes",
        "cl_collection",
        "classic_collection",
        "lb_collection",
        "sor_protocol",
    }
    where = f"sources[{obj.get('key', '?')}]"
    _require_keys(obj, required, optional, where)

    if obj["protocol_family"] not in KNOWN_PROTOCOL_FAMILIES:
        raise ConfigError(
            f"{where}.protocol_family: {obj['protocol_family']!r} not in "
            f"{sorted(KNOWN_PROTOCOL_FAMILIES)}"
        )
    if obj["confidence"] not in KNOWN_CONFIDENCE_LEVELS:
        raise ConfigError(
            f"{where}.confidence: {obj['confidence']!r} not in {sorted(KNOWN_CONFIDENCE_LEVELS)}"
        )

    contracts_obj = obj["contracts"]
    if not isinstance(contracts_obj, dict) or not contracts_obj:
        raise ConfigError(f"{where}.contracts: expected a non-empty mapping")
    contracts = {
        str(role): _parse_contract_ref(ref, f"{where}.contracts.{role}")
        for role, ref in contracts_obj.items()
    }

    tokens_obj = obj.get("tokens", {})
    tokens = {
        str(role): _validate_address(addr, f"{where}.tokens.{role}")
        for role, addr in tokens_obj.items()
    }

    pool_fee_obj = obj.get("pool_fee")
    pool_fee = int(pool_fee_obj) if pool_fee_obj is not None else None

    fee_tiers_obj = obj.get("expected_fee_tiers")
    fee_tiers = None
    if fee_tiers_obj is not None:
        if not isinstance(fee_tiers_obj, dict) or not fee_tiers_obj:
            raise ConfigError(f"{where}.expected_fee_tiers: expected a non-empty mapping")
        fee_tiers = {int(fee): int(spacing) for fee, spacing in fee_tiers_obj.items()}

    upstream = None
    if "upstream" in obj:
        upstream = _parse_upstream(obj["upstream"], f"{where}.upstream")

    blockers_obj = obj.get("blockers", [])
    if not isinstance(blockers_obj, list):
        raise ConfigError(f"{where}.blockers: expected a list")
    blockers = tuple(
        _parse_blocker(b, f"{where}.blockers[{i}]") for i, b in enumerate(blockers_obj)
    )

    cl_collection = None
    if "cl_collection" in obj:
        if obj["protocol_family"] != "v3_concentrated_liquidity":
            raise ConfigError(f"{where}.cl_collection: only valid for v3_concentrated_liquidity")
        cl_collection = _parse_cl_collection(obj["cl_collection"], f"{where}.cl_collection")
        if cl_collection.factory_deploys_pools and "pool_deployer" in contracts:
            raise ConfigError(
                f"{where}.cl_collection.factory_deploys_pools: contradicts a pinned "
                "contracts.pool_deployer"
            )

    classic_collection = None
    if "classic_collection" in obj:
        if obj["protocol_family"] != "v2_classic":
            raise ConfigError(f"{where}.classic_collection: only valid for v2_classic")
        classic_collection = _parse_classic_collection(
            obj["classic_collection"], f"{where}.classic_collection"
        )

    lb_collection = None
    if "lb_collection" in obj:
        if obj["protocol_family"] != "liquidity_book_v2":
            raise ConfigError(f"{where}.lb_collection: only valid for liquidity_book_v2")
        lb_collection = _parse_lb_collection(obj["lb_collection"], f"{where}.lb_collection")

    sor_protocol = obj.get("sor_protocol")
    if sor_protocol is not None and sor_protocol not in KNOWN_SOR_PROTOCOLS:
        raise ConfigError(
            f"{where}.sor_protocol: {sor_protocol!r} not in {sorted(KNOWN_SOR_PROTOCOLS)}"
        )
    if sor_protocol is not None and _SOR_FAMILY[sor_protocol] != obj["protocol_family"]:
        raise ConfigError(
            f"{where}.sor_protocol: {sor_protocol} routes only "
            f"{_SOR_FAMILY[sor_protocol]} pools, not {obj['protocol_family']}"
        )

    return SourceConfig(
        key=str(obj["key"]),
        display_name=str(obj["display_name"]),
        protocol_family=str(obj["protocol_family"]),
        confidence=str(obj["confidence"]),
        contracts=contracts,
        tokens=tokens,
        pool_fee=pool_fee,
        expected_fee_tiers=fee_tiers,
        upstream=upstream,
        blockers=blockers,
        notes=str(obj.get("notes", "")),
        cl_collection=cl_collection,
        classic_collection=classic_collection,
        lb_collection=lb_collection,
        sor_protocol=sor_protocol,
    )


def parse_catalog(raw: dict[str, Any]) -> ProtocolCatalog:
    _require_keys(raw, {"version", "network", "candidate_block", "sources"}, set(), "<root>")
    if raw["version"] != 1:
        raise ConfigError(f"version: unsupported catalog version {raw['version']!r} (expected 1)")

    sources_obj = raw["sources"]
    if not isinstance(sources_obj, list) or not sources_obj:
        raise ConfigError("sources: expected a non-empty list")
    sources = tuple(_parse_source(s) for s in sources_obj)

    keys = [s.key for s in sources]
    if len(keys) != len(set(keys)):
        raise ConfigError(f"sources: duplicate key(s) in {keys}")

    return ProtocolCatalog(
        version=raw["version"],
        network=_parse_network(raw["network"]),
        candidate_block=_parse_candidate_block(raw["candidate_block"]),
        sources=sources,
    )


def load_catalog(path: str | Path = "config/protocols.yaml") -> ProtocolCatalog:
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML document must be a mapping")
    try:
        return parse_catalog(raw)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
