"""JSON validation and SHA-256 checksumming for the offline snapshot bundle format
(docs/DESIGN.md §2.2). A bundle is a directory:

    manifest.json   -- schema version, bundle id/kind, block identity, file list,
                        SHA-256 checksums for every referenced file
    pools.json       -- {"pools": [...]} constant-product pool states
    cases.jsonl       -- one Case JSON object per line

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
from pathlib import Path
from typing import Any, cast

from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

SUPPORTED_SCHEMA_VERSION = 1
KNOWN_KINDS = {"synthetic", "real"}
_HEX_DIGITS = set("0123456789abcdefABCDEF")

MANIFEST_FILE = "manifest.json"
POOLS_FILE = "pools.json"
CASES_FILE = "cases.jsonl"


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
    _require_keys(files_obj, {"pools", "cases"}, set(), f"{where}.files")
    if files_obj["pools"] != POOLS_FILE or files_obj["cases"] != CASES_FILE:
        raise BundleError(
            f"{where}.files: expected {{'pools': {POOLS_FILE!r}, 'cases': {CASES_FILE!r}}}, "
            f"got {files_obj!r}"
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
        if not isinstance(digest, str) or len(digest) != 64 or not all(
            c in _HEX_DIGITS for c in digest
        ):
            raise BundleError(f"{where}.checksums[{name!r}]: not a 64-hex-char SHA-256 digest")

    return cast(dict[str, Any], raw)


def _parse_pool(obj: Any, where: str) -> ConstantProductPoolState:
    _require_keys(
        obj, {"pool_id", "token0", "token1", "reserve0", "reserve1", "fee_bps"}, set(), where
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
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=token0,
        token1=token1,
        reserve0=reserve0,
        reserve1=reserve1,
        fee_bps=fee_bps,
    )


def _parse_pools_file(text: str, where: str) -> dict[str, ConstantProductPoolState]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleError(f"{where}: invalid JSON: {exc}") from exc
    _require_keys(raw, {"pools"}, set(), where)
    if not isinstance(raw["pools"], list):
        raise BundleError(f"{where}.pools: expected a list")
    pools: dict[str, ConstantProductPoolState] = {}
    for i, entry in enumerate(raw["pools"]):
        pool = _parse_pool(entry, f"{where}.pools[{i}]")
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
    pools = _parse_pools_file((bundle_dir / POOLS_FILE).read_text(), str(bundle_dir / POOLS_FILE))
    cases = _parse_cases_file(
        (bundle_dir / CASES_FILE).read_text(), str(bundle_dir / CASES_FILE)
    )

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


def _pools_to_json(pools: list[ConstantProductPoolState]) -> str:
    obj = {
        "pools": [
            {
                "pool_id": p.pool_id,
                "token0": p.token0,
                "token1": p.token1,
                "reserve0": str(p.reserve0),
                "reserve1": str(p.reserve1),
                "fee_bps": p.fee_bps,
            }
            for p in pools
        ]
    }
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _cases_to_jsonl(cases: list[Case]) -> str:
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
    pools: list[ConstantProductPoolState],
    cases: list[Case],
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

    pools_text = _pools_to_json(pools)
    cases_text = _cases_to_jsonl(cases)
    checksums = {
        POOLS_FILE: sha256_bytes(pools_text.encode("utf-8")),
        CASES_FILE: sha256_bytes(cases_text.encode("utf-8")),
    }
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
        "files": {"pools": POOLS_FILE, "cases": CASES_FILE},
        "checksums": checksums,
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        (tmp_dir / MANIFEST_FILE).write_text(manifest_text)
        (tmp_dir / POOLS_FILE).write_text(pools_text)
        (tmp_dir / CASES_FILE).write_text(cases_text)
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
