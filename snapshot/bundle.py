"""JSON validation and SHA-256 checksumming for the offline snapshot bundle format
(docs/DESIGN.md §2.2). A bundle is a directory:

    manifest.json    -- schema version, bundle id/kind, block identity, file list,
                        SHA-256 checksums for every referenced file
    pools.json       -- {"pools": [...]} pool states: constant-product entries (no
                        `family` key, WHI-1427; a real-source entry adds `source_key`
                        and `read_at`, WHI-1432) and concentrated-liquidity entries
                        (`"family": "concentrated"`, WHI-1429) with their collected
                        tick bitmap/tick data and completeness range
    cases.jsonl      -- one Case JSON object per line
    provenance.json  -- optional (real bundles): how the state was read -- source,
                        catalog pins, discovery and completeness evidence

Real (`kind="real"`) state is block-bound: every concentrated and real-source
constant-product pool record carries a `read_at` block identity, and the loader rejects
any record, or a provenance file, whose block differs from the manifest's
(docs/DESIGN.md §2.2: "Every state read uses that block"; one bundle never mixes blocks).

`load_bundle` is fail-fast and never falls back to a partial/best-effort read: a bad
checksum, an unsupported schema version, a negative/zero amount or an unknown key
kills the load with a clear `BundleError` (docs/DESIGN.md §2.12 fail-fast
convention, mirrored from `snapshot.config`). JSON amount/reserve fields are decimal
strings on disk; this module is the only place that turns them into Python `int`
(docs/DESIGN.md §2.2/§2.12 — large integers are never round-tripped through JSON
numbers).

`write_bundle` is the paired writer used by `snapshot.collectors`: it stages content
in a sibling temporary directory, self-validates by round-tripping through
`load_bundle`, and only then publishes atomically (docs/DESIGN.md §2.2 "writes go to
a temporary directory and become visible only after checksums and schema validation
pass").
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pools.cl_math import MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK
from pools.concentrated import SOURCES as CL_SOURCES
from pools.constant_product import SOURCES as CP_SOURCES
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

SUPPORTED_SCHEMA_VERSION = 1
KNOWN_KINDS = {"synthetic", "real"}
_HEX_DIGITS = set("0123456789abcdefABCDEF")

MANIFEST_FILE = "manifest.json"
POOLS_FILE = "pools.json"
CASES_FILE = "cases.jsonl"
PROVENANCE_FILE = "provenance.json"

CONCENTRATED_FAMILY = "concentrated"
# Concentrated-liquidity source keys the loader accepts: exactly the simulator's
# migrated semantics (collection admission is a separate, per-source catalog switch).
KNOWN_CONCENTRATED_SOURCES = frozenset(CL_SOURCES)

_U128 = (1 << 128) - 1
_U256 = (1 << 256) - 1
_I128_MIN = -(1 << 127)
_I128_MAX = (1 << 127) - 1


class BundleError(ValueError):
    """The bundle is missing, malformed, checksum-mismatched, or fails schema
    validation. Always raised with a specific, actionable message."""


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise BundleError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise BundleError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise BundleError(f"{where}: unknown key(s) {sorted(unknown)}")


def _validate_hash32(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise BundleError(f"{where}: not a 32-byte 0x-hash: {value!r}")
    if not all(c in _HEX_DIGITS for c in value[2:]):
        raise BundleError(f"{where}: not valid hex: {value!r}")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _parse_positive_int_string(value: Any, where: str) -> int:
    if not isinstance(value, str):
        raise BundleError(f"{where}: expected a decimal string, got {type(value).__name__}")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise BundleError(f"{where}: not a base-10 integer string: {value!r}") from exc
    if parsed < 0:
        raise BundleError(f"{where}: negative amount not allowed: {value!r}")
    return parsed


def _parse_nonneg_int_string(value: Any, where: str) -> int:
    return _parse_positive_int_string(value, where)


def _parse_bounded_uint(value: Any, maximum: int, where: str) -> int:
    parsed = _parse_nonneg_int_string(value, where)
    if parsed > maximum:
        raise BundleError(f"{where}: {parsed} exceeds the maximum {maximum}")
    return parsed


def _parse_signed_int_string(value: Any, lo: int, hi: int, where: str) -> int:
    if not isinstance(value, str):
        raise BundleError(f"{where}: expected a decimal string, got {type(value).__name__}")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise BundleError(f"{where}: not a base-10 integer string: {value!r}") from exc
    if not (lo <= parsed <= hi):
        raise BundleError(f"{where}: {parsed} outside [{lo}, {hi}]")
    return parsed


def _parse_json_int(value: Any, lo: int, hi: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BundleError(f"{where}: expected int, got {value!r}")
    if not (lo <= value <= hi):
        raise BundleError(f"{where}: {value} outside [{lo}, {hi}]")
    return value


def _validate_block(obj: Any, where: str) -> BlockRef:
    _require_keys(obj, {"chain_id", "number", "hash", "timestamp"}, set(), where)
    if not isinstance(obj["chain_id"], int) or isinstance(obj["chain_id"], bool):
        raise BundleError(f"{where}.chain_id: expected int, got {obj['chain_id']!r}")
    if not isinstance(obj["number"], int) or isinstance(obj["number"], bool):
        raise BundleError(f"{where}.number: expected int, got {obj['number']!r}")
    if not isinstance(obj["timestamp"], int) or isinstance(obj["timestamp"], bool):
        raise BundleError(f"{where}.timestamp: expected int, got {obj['timestamp']!r}")
    return BlockRef(
        chain_id=obj["chain_id"],
        number=obj["number"],
        hash=_validate_hash32(obj["hash"], f"{where}.hash"),
        timestamp=obj["timestamp"],
    )


def _parse_manifest(raw: Any, where: str) -> dict[str, Any]:
    _require_keys(
        raw,
        {"schema_version", "bundle_id", "kind", "block", "files", "checksums"},
        set(),
        where,
    )
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise BundleError(
            f"{where}.schema_version: unsupported schema {raw['schema_version']!r} "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )
    if not isinstance(raw["bundle_id"], str) or not raw["bundle_id"]:
        raise BundleError(f"{where}.bundle_id: expected a non-empty string")
    if raw["kind"] not in KNOWN_KINDS:
        raise BundleError(f"{where}.kind: {raw['kind']!r} not in {sorted(KNOWN_KINDS)}")

    files_obj = raw["files"]
    _require_keys(files_obj, {"pools", "cases"}, {"provenance"}, f"{where}.files")
    if files_obj["pools"] != POOLS_FILE or files_obj["cases"] != CASES_FILE:
        raise BundleError(
            f"{where}.files: expected {{'pools': {POOLS_FILE!r}, 'cases': {CASES_FILE!r}}}, "
            f"got {files_obj!r}"
        )
    if "provenance" in files_obj and files_obj["provenance"] != PROVENANCE_FILE:
        raise BundleError(
            f"{where}.files.provenance: expected {PROVENANCE_FILE!r}, "
            f"got {files_obj['provenance']!r}"
        )

    checksums_obj = raw["checksums"]
    if not isinstance(checksums_obj, dict):
        raise BundleError(f"{where}.checksums: expected a mapping")
    expected_files = set(files_obj.values())
    if checksums_obj.keys() != expected_files:
        raise BundleError(
            f"{where}.checksums: expected checksums for exactly {sorted(expected_files)}, "
            f"got {sorted(checksums_obj.keys())}"
        )
    for name, digest in checksums_obj.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or not all(c in _HEX_DIGITS for c in digest)
        ):
            raise BundleError(f"{where}.checksums[{name!r}]: not a 64-hex-char SHA-256 digest")

    return cast(dict[str, Any], raw)


def _check_read_at(obj: Any, where: str, block: BlockRef) -> None:
    read_at = obj["read_at"]
    _require_keys(read_at, {"block_number", "block_hash"}, set(), f"{where}.read_at")
    if read_at["block_number"] != block.number or str(read_at["block_hash"]).lower() != (
        block.hash.lower()
    ):
        raise BundleError(
            f"{where}.read_at: state read at block {read_at['block_number']} "
            f"({read_at['block_hash']}) but the bundle is frozen at block {block.number} "
            f"({block.hash}); one bundle never mixes blocks"
        )


def _parse_cp_pool(obj: Any, where: str, block: BlockRef) -> ConstantProductPoolState:
    """A constant-product pool record. A generic (synthetic) record has no
    `source_key`; a real-source record (WHI-1432) carries `source_key` *and* the block it
    was read at, and must satisfy that source's migrated invariants: sorted tokens,
    reserves within the pair's storage width and the source's fixed fee."""
    _require_keys(
        obj,
        {"pool_id", "token0", "token1", "reserve0", "reserve1", "fee_bps"},
        {"source_key", "read_at"},
        where,
    )
    pool_id = obj["pool_id"]
    token0 = obj["token0"]
    token1 = obj["token1"]
    if not isinstance(pool_id, str) or not pool_id:
        raise BundleError(f"{where}.pool_id: expected a non-empty string")
    if not isinstance(token0, str) or not token0 or not isinstance(token1, str) or not token1:
        raise BundleError(f"{where}: token0/token1 must be non-empty strings")
    if token0 == token1:
        raise BundleError(f"{where}: token0 and token1 must differ, both are {token0!r}")
    fee_bps = obj["fee_bps"]
    if not isinstance(fee_bps, int) or isinstance(fee_bps, bool):
        raise BundleError(f"{where}.fee_bps: expected int, got {fee_bps!r}")
    if not (0 <= fee_bps < 10_000):
        raise BundleError(f"{where}.fee_bps: must be in [0, 10000), got {fee_bps}")
    reserve0 = _parse_nonneg_int_string(obj["reserve0"], f"{where}.reserve0")
    reserve1 = _parse_nonneg_int_string(obj["reserve1"], f"{where}.reserve1")
    source_key = obj.get("source_key")
    if ("read_at" in obj) != (source_key is not None):
        raise BundleError(f"{where}: a real-source record needs both `source_key` and `read_at`")
    if source_key is not None:
        source = CP_SOURCES.get(source_key)
        if source is None:
            raise BundleError(f"{where}.source_key: {source_key!r} not in {sorted(CP_SOURCES)}")
        _check_read_at(obj, where, block)
        if not token0 < token1:
            raise BundleError(
                f"{where}: token0 {token0!r} must sort below token1 {token1!r} "
                f"({source_key} pairs order their tokens by address)"
            )
        limit = (1 << source.reserve_bits) - 1
        for key, value in (("reserve0", reserve0), ("reserve1", reserve1)):
            if value > limit:
                raise BundleError(f"{where}.{key}: {value} exceeds uint{source.reserve_bits}")
        if fee_bps != source.fee_bps:
            raise BundleError(
                f"{where}.fee_bps: {fee_bps} is not {source_key}'s fixed {source.fee_bps}"
            )
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=token0,
        token1=token1,
        reserve0=reserve0,
        reserve1=reserve1,
        fee_bps=fee_bps,
        source_key=source_key,
    )


_CL_REQUIRED_KEYS = {
    "family",
    "pool_id",
    "source_key",
    "token0",
    "token1",
    "fee",
    "tick_spacing",
    "sqrt_price_x96",
    "tick",
    "liquidity",
    "fee_protocol",
    "fee_growth_global0_x128",
    "fee_growth_global1_x128",
    "protocol_fees0",
    "protocol_fees1",
    "lm_pool",
    "bitmap_word_range",
    "tick_bitmap",
    "ticks",
    "read_at",
}


def _floor_word(tick: int, spacing: int) -> int:
    return (tick // spacing) >> 8


def _parse_cl_pool(obj: Any, where: str, block: BlockRef) -> ConcentratedPoolState:
    """A concentrated-liquidity pool record. Beyond per-field width checks, the
    collected range must be internally complete: every set bit of every listed
    bitmap word has its tick data, every tick record is an initialized tick of a
    listed word, and nothing lies outside `bitmap_word_range`. A record read at a
    block other than the manifest's is rejected."""
    _require_keys(obj, _CL_REQUIRED_KEYS, set(), where)
    _check_read_at(obj, where, block)
    pool_id, token0, token1 = obj["pool_id"], obj["token0"], obj["token1"]
    for key, value in (("pool_id", pool_id), ("token0", token0), ("token1", token1)):
        if not isinstance(value, str) or not value:
            raise BundleError(f"{where}.{key}: expected a non-empty string")
    if token0 == token1:
        raise BundleError(f"{where}: token0 and token1 must differ, both are {token0!r}")
    source_key = obj["source_key"]
    if source_key not in KNOWN_CONCENTRATED_SOURCES:
        raise BundleError(
            f"{where}.source_key: {source_key!r} not in {sorted(KNOWN_CONCENTRATED_SOURCES)}"
        )
    lm_pool = obj["lm_pool"]
    if lm_pool is not None and not isinstance(lm_pool, str):
        raise BundleError(f"{where}.lm_pool: expected an address string or null")

    fee = _parse_json_int(obj["fee"], 0, 999_999, f"{where}.fee")
    spacing = _parse_json_int(obj["tick_spacing"], 1, 16383, f"{where}.tick_spacing")
    tick = _parse_json_int(obj["tick"], MIN_TICK, MAX_TICK, f"{where}.tick")
    fee_protocol = _parse_json_int(obj["fee_protocol"], 0, (1 << 32) - 1, f"{where}.fee_protocol")
    sqrt_price = _parse_bounded_uint(
        obj["sqrt_price_x96"], MAX_SQRT_RATIO - 1, f"{where}.sqrt_price_x96"
    )
    if sqrt_price < MIN_SQRT_RATIO:
        raise BundleError(f"{where}.sqrt_price_x96: {sqrt_price} below MIN_SQRT_RATIO")

    word_range = obj["bitmap_word_range"]
    if not (isinstance(word_range, list) and len(word_range) == 2):
        raise BundleError(f"{where}.bitmap_word_range: expected [lo, hi]")
    word_lo = _parse_json_int(
        word_range[0], -(1 << 15), (1 << 15) - 1, f"{where}.bitmap_word_range[0]"
    )
    word_hi = _parse_json_int(
        word_range[1], -(1 << 15), (1 << 15) - 1, f"{where}.bitmap_word_range[1]"
    )
    if word_lo > word_hi:
        raise BundleError(f"{where}.bitmap_word_range: lo {word_lo} > hi {word_hi}")
    current_word = _floor_word(tick, spacing)
    if not (word_lo <= current_word <= word_hi):
        raise BundleError(
            f"{where}.bitmap_word_range: [{word_lo}, {word_hi}] does not contain the "
            f"current tick's word {current_word}"
        )

    if not isinstance(obj["tick_bitmap"], list):
        raise BundleError(f"{where}.tick_bitmap: expected a list")
    bitmap: dict[int, int] = {}
    for i, entry in enumerate(obj["tick_bitmap"]):
        w = f"{where}.tick_bitmap[{i}]"
        _require_keys(entry, {"word", "bitmap"}, set(), w)
        word = _parse_json_int(entry["word"], word_lo, word_hi, f"{w}.word")
        value = _parse_bounded_uint(entry["bitmap"], _U256, f"{w}.bitmap")
        if word in bitmap:
            raise BundleError(f"{w}: duplicate word {word}")
        if value != 0:
            bitmap[word] = value

    if not isinstance(obj["ticks"], list):
        raise BundleError(f"{where}.ticks: expected a list")
    ticks: dict[int, TickInfo] = {}
    for i, entry in enumerate(obj["ticks"]):
        w = f"{where}.ticks[{i}]"
        _require_keys(
            entry,
            {
                "tick",
                "liquidity_gross",
                "liquidity_net",
                "fee_growth_outside0_x128",
                "fee_growth_outside1_x128",
            },
            set(),
            w,
        )
        t = _parse_json_int(entry["tick"], MIN_TICK, MAX_TICK, f"{w}.tick")
        if t % spacing != 0:
            raise BundleError(f"{w}.tick: {t} is not a multiple of tick_spacing {spacing}")
        compressed = t // spacing
        if not (bitmap.get(compressed >> 8, 0) >> (compressed & 0xFF)) & 1:
            raise BundleError(f"{w}.tick: {t} is not an initialized tick of a collected word")
        if t in ticks:
            raise BundleError(f"{w}: duplicate tick {t}")
        gross = _parse_bounded_uint(entry["liquidity_gross"], _U128, f"{w}.liquidity_gross")
        if gross == 0:
            raise BundleError(f"{w}.liquidity_gross: an initialized tick has non-zero gross")
        ticks[t] = TickInfo(
            liquidity_gross=gross,
            liquidity_net=_parse_signed_int_string(
                entry["liquidity_net"], _I128_MIN, _I128_MAX, f"{w}.liquidity_net"
            ),
            fee_growth_outside0_x128=_parse_bounded_uint(
                entry["fee_growth_outside0_x128"], _U256, f"{w}.fee_growth_outside0_x128"
            ),
            fee_growth_outside1_x128=_parse_bounded_uint(
                entry["fee_growth_outside1_x128"], _U256, f"{w}.fee_growth_outside1_x128"
            ),
        )
    expected_bits = sum(bin(v).count("1") for v in bitmap.values())
    if expected_bits != len(ticks):
        raise BundleError(
            f"{where}.ticks: {len(ticks)} tick record(s) for {expected_bits} initialized "
            "bit(s) in the collected words; the collected range must be complete"
        )

    return ConcentratedPoolState(
        pool_id=pool_id,
        source_key=source_key,
        token0=token0,
        token1=token1,
        fee=fee,
        tick_spacing=spacing,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        liquidity=_parse_bounded_uint(obj["liquidity"], _U128, f"{where}.liquidity"),
        fee_protocol=fee_protocol,
        fee_growth_global0_x128=_parse_bounded_uint(
            obj["fee_growth_global0_x128"], _U256, f"{where}.fee_growth_global0_x128"
        ),
        fee_growth_global1_x128=_parse_bounded_uint(
            obj["fee_growth_global1_x128"], _U256, f"{where}.fee_growth_global1_x128"
        ),
        protocol_fees0=_parse_bounded_uint(obj["protocol_fees0"], _U128, f"{where}.protocol_fees0"),
        protocol_fees1=_parse_bounded_uint(obj["protocol_fees1"], _U128, f"{where}.protocol_fees1"),
        bitmap_word_range=(word_lo, word_hi),
        tick_bitmap=bitmap,
        ticks=ticks,
        lm_pool=lm_pool,
    )


def _parse_pool(obj: Any, where: str, block: BlockRef) -> PoolState:
    if isinstance(obj, dict) and "family" in obj:
        if obj["family"] != CONCENTRATED_FAMILY:
            raise BundleError(f"{where}.family: unknown pool family {obj['family']!r}")
        return _parse_cl_pool(obj, where, block)
    return _parse_cp_pool(obj, where, block)


def _parse_pools_file(text: str, where: str, block: BlockRef) -> dict[str, PoolState]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleError(f"{where}: invalid JSON: {exc}") from exc
    _require_keys(raw, {"pools"}, set(), where)
    if not isinstance(raw["pools"], list):
        raise BundleError(f"{where}.pools: expected a list")
    pools: dict[str, PoolState] = {}
    for i, entry in enumerate(raw["pools"]):
        pool = _parse_pool(entry, f"{where}.pools[{i}]", block)
        if pool.pool_id in pools:
            raise BundleError(f"{where}.pools: duplicate pool_id {pool.pool_id!r}")
        pools[pool.pool_id] = pool
    return pools


def _parse_case(obj: Any, where: str) -> Case:
    _require_keys(obj, {"case_id", "token_in", "token_out", "amount_in"}, set(), where)
    case_id = obj["case_id"]
    token_in = obj["token_in"]
    token_out = obj["token_out"]
    if not isinstance(case_id, str) or not case_id:
        raise BundleError(f"{where}.case_id: expected a non-empty string")
    if not isinstance(token_in, str) or not token_in:
        raise BundleError(f"{where}.token_in: expected a non-empty string")
    if not isinstance(token_out, str) or not token_out:
        raise BundleError(f"{where}.token_out: expected a non-empty string")
    if token_in == token_out:
        raise BundleError(f"{where}: token_in and token_out must differ, both are {token_in!r}")
    amount_in = _parse_positive_int_string(obj["amount_in"], f"{where}.amount_in")
    if amount_in <= 0:
        raise BundleError(f"{where}.amount_in: must be positive, got {amount_in}")
    return Case(case_id=case_id, token_in=token_in, token_out=token_out, amount_in=amount_in)


def _parse_cases_file(text: str, where: str) -> tuple[Case, ...]:
    cases: list[Case] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"{where}:{lineno}: invalid JSON: {exc}") from exc
        case = _parse_case(obj, f"{where}:{lineno}")
        if case.case_id in seen:
            raise BundleError(f"{where}: duplicate case_id {case.case_id!r}")
        seen.add(case.case_id)
        cases.append(case)
    return tuple(cases)


def load_bundle(path: str | Path) -> SnapshotBundle:
    bundle_dir = Path(path)
    manifest_path = bundle_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise BundleError(f"{bundle_dir}: no {MANIFEST_FILE} found")

    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest_raw = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise BundleError(f"{manifest_path}: invalid JSON: {exc}") from exc
    manifest = _parse_manifest(manifest_raw, str(manifest_path))

    for filename, expected_digest in manifest["checksums"].items():
        file_path = bundle_dir / filename
        if not file_path.is_file():
            raise BundleError(f"{bundle_dir}: {filename} referenced by manifest but missing")
        observed_digest = sha256_file(file_path)
        if observed_digest != expected_digest:
            raise BundleError(
                f"{filename}: bad checksum, expected {expected_digest}, got {observed_digest}"
            )

    block = _validate_block(manifest["block"], f"{manifest_path}.block")
    pools = _parse_pools_file(
        (bundle_dir / POOLS_FILE).read_text(), str(bundle_dir / POOLS_FILE), block
    )
    if "provenance" in manifest["files"]:
        _check_provenance(bundle_dir / PROVENANCE_FILE, block)
    cases = _parse_cases_file((bundle_dir / CASES_FILE).read_text(), str(bundle_dir / CASES_FILE))

    return SnapshotBundle(
        bundle_id=manifest["bundle_id"],
        kind=manifest["kind"],
        schema_version=manifest["schema_version"],
        block=block,
        pools=pools,
        cases=cases,
        bundle_hash=sha256_bytes(manifest_bytes),
        source_path=str(bundle_dir),
    )


def _check_provenance(path: Path, block: BlockRef) -> None:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BundleError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or "block" not in raw:
        raise BundleError(f"{path}: expected a mapping with a 'block' identity")
    prov_block = _validate_block(raw["block"], f"{path}.block")
    if prov_block != block:
        raise BundleError(
            f"{path}.block: provenance block {prov_block} differs from the manifest's {block}"
        )


def _cp_pool_to_obj(p: ConstantProductPoolState, block: BlockRef) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "pool_id": p.pool_id,
        "token0": p.token0,
        "token1": p.token1,
        "reserve0": str(p.reserve0),
        "reserve1": str(p.reserve1),
        "fee_bps": p.fee_bps,
    }
    if p.source_key is not None:
        obj["source_key"] = p.source_key
        obj["read_at"] = {"block_number": block.number, "block_hash": block.hash}
    return obj


def _cl_pool_to_obj(p: ConcentratedPoolState, block: BlockRef) -> dict[str, Any]:
    lo, hi = p.bitmap_word_range
    return {
        "family": CONCENTRATED_FAMILY,
        "pool_id": p.pool_id,
        "source_key": p.source_key,
        "token0": p.token0,
        "token1": p.token1,
        "fee": p.fee,
        "tick_spacing": p.tick_spacing,
        "sqrt_price_x96": str(p.sqrt_price_x96),
        "tick": p.tick,
        "liquidity": str(p.liquidity),
        "fee_protocol": p.fee_protocol,
        "fee_growth_global0_x128": str(p.fee_growth_global0_x128),
        "fee_growth_global1_x128": str(p.fee_growth_global1_x128),
        "protocol_fees0": str(p.protocol_fees0),
        "protocol_fees1": str(p.protocol_fees1),
        "lm_pool": p.lm_pool,
        "bitmap_word_range": [lo, hi],
        "tick_bitmap": [
            {"word": w, "bitmap": str(p.tick_bitmap[w])}
            for w in sorted(p.tick_bitmap)
            if p.tick_bitmap[w] != 0
        ],
        "ticks": [
            {
                "tick": t,
                "liquidity_gross": str(info.liquidity_gross),
                "liquidity_net": str(info.liquidity_net),
                "fee_growth_outside0_x128": str(info.fee_growth_outside0_x128),
                "fee_growth_outside1_x128": str(info.fee_growth_outside1_x128),
            }
            for t, info in sorted(p.ticks.items())
        ],
        "read_at": {"block_number": block.number, "block_hash": block.hash},
    }


def _pools_to_json(pools: Sequence[PoolState], block: BlockRef) -> str:
    entries: list[dict[str, Any]] = []
    for p in pools:
        if isinstance(p, ConcentratedPoolState):
            entries.append(_cl_pool_to_obj(p, block))
        else:
            entries.append(_cp_pool_to_obj(p, block))
    return json.dumps({"pools": entries}, indent=2, sort_keys=True) + "\n"


def _cases_to_jsonl(cases: Sequence[Case]) -> str:
    lines = [
        json.dumps(
            {
                "case_id": c.case_id,
                "token_in": c.token_in,
                "token_out": c.token_out,
                "amount_in": str(c.amount_in),
            },
            sort_keys=True,
        )
        for c in cases
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def write_bundle(
    output_dir: str | Path,
    *,
    bundle_id: str,
    kind: str,
    block: BlockRef,
    pools: Sequence[PoolState],
    cases: Sequence[Case],
    provenance: dict[str, Any] | None = None,
) -> SnapshotBundle:
    """Write, self-validate and atomically publish a bundle directory.

    Refuses to overwrite an existing `output_dir` (docs/DESIGN.md §4.5: input
    bundles never change in place). Validation failures leave no partial output at
    `output_dir` -- staging happens in a sibling temp directory that is only
    renamed into place after a full `load_bundle` round-trip succeeds.
    """
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise BundleError(f"{output_dir}: already exists, refusing to overwrite a bundle")

    pools_text = _pools_to_json(pools, block)
    cases_text = _cases_to_jsonl(cases)
    checksums = {
        POOLS_FILE: sha256_bytes(pools_text.encode("utf-8")),
        CASES_FILE: sha256_bytes(cases_text.encode("utf-8")),
    }
    files = {"pools": POOLS_FILE, "cases": CASES_FILE}
    provenance_text: str | None = None
    if provenance is not None:
        provenance_text = json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        checksums[PROVENANCE_FILE] = sha256_bytes(provenance_text.encode("utf-8"))
        files["provenance"] = PROVENANCE_FILE
    manifest = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "kind": kind,
        "block": {
            "chain_id": block.chain_id,
            "number": block.number,
            "hash": block.hash,
            "timestamp": block.timestamp,
        },
        "files": files,
        "checksums": checksums,
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        (tmp_dir / MANIFEST_FILE).write_text(manifest_text)
        (tmp_dir / POOLS_FILE).write_text(pools_text)
        (tmp_dir / CASES_FILE).write_text(cases_text)
        if provenance_text is not None:
            (tmp_dir / PROVENANCE_FILE).write_text(provenance_text)
        # Self-validate before publishing: a bundle only becomes visible at
        # `output_dir` once it round-trips through the same loader every reader uses.
        bundle = load_bundle(tmp_dir)
        os.replace(tmp_dir, output_dir)
    except BaseException:
        _rmtree(tmp_dir)
        raise
    return SnapshotBundle(
        bundle_id=bundle.bundle_id,
        kind=bundle.kind,
        schema_version=bundle.schema_version,
        block=bundle.block,
        pools=bundle.pools,
        cases=bundle.cases,
        bundle_hash=bundle.bundle_hash,
        source_path=str(output_dir),
    )


def _rmtree(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
