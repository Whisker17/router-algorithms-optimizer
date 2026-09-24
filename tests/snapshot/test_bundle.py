"""Validation tests for `snapshot.bundle`: fail-fast schema checks, checksum
verification, and the round-trip `write_bundle` -> `load_bundle` guarantee.
Covers the WHI-1427 acceptance criterion "Bad checksum, negative amount,
unsupported schema and unknown config keys fail clearly."
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from snapshot.bundle import (
    CASES_FILE,
    MANIFEST_FILE,
    POOLS_FILE,
    BundleError,
    load_bundle,
    sha256_bytes,
    write_bundle,
)
from snapshot.models import BlockRef, Case, ConstantProductPoolState

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
POOLS = [
    ConstantProductPoolState(
        pool_id="pool_a", token0="TKA", token1="TKB", reserve0=1000, reserve1=2000, fee_bps=30
    )
]
CASES = [Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)]


def _write_manual_bundle(
    bundle_dir: Path,
    *,
    manifest_overrides: dict[str, Any] | None = None,
    cases_text: str | None = None,
) -> None:
    """Write a bundle directory by hand (bypassing `write_bundle`'s
    self-validation) so tests can construct deliberately invalid content."""
    pools_text = json.dumps(
        {
            "pools": [
                {
                    "pool_id": "pool_a",
                    "token0": "TKA",
                    "token1": "TKB",
                    "reserve0": "1000",
                    "reserve1": "2000",
                    "fee_bps": 30,
                }
            ]
        }
    )
    if cases_text is None:
        cases_text = json.dumps(
            {"case_id": "c1", "token_in": "TKA", "token_out": "TKB", "amount_in": "100"}
        )
    manifest = {
        "schema_version": 1,
        "bundle_id": "b1",
        "kind": "synthetic",
        "block": {"chain_id": 0, "number": 0, "hash": "0x" + "00" * 32, "timestamp": 0},
        "files": {"pools": POOLS_FILE, "cases": CASES_FILE},
        "checksums": {
            POOLS_FILE: sha256_bytes(pools_text.encode()),
            CASES_FILE: sha256_bytes(cases_text.encode()),
        },
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest))
    (bundle_dir / POOLS_FILE).write_text(pools_text)
    (bundle_dir / CASES_FILE).write_text(cases_text)


def test_write_then_load_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    written = write_bundle(
        out, bundle_id="b1", kind="synthetic", block=BLOCK, pools=POOLS, cases=CASES
    )
    loaded = load_bundle(out)
    assert loaded.bundle_id == "b1"
    assert loaded.bundle_hash == written.bundle_hash
    assert loaded.pools["pool_a"].reserve0 == 1000
    assert loaded.cases[0].amount_in == 100


def test_write_bundle_refuses_to_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    write_bundle(out, bundle_id="b1", kind="synthetic", block=BLOCK, pools=POOLS, cases=CASES)
    with pytest.raises(BundleError, match="already exists"):
        write_bundle(out, bundle_id="b1", kind="synthetic", block=BLOCK, pools=POOLS, cases=CASES)


def test_load_bundle_missing_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BundleError, match="no manifest.json"):
        load_bundle(empty)


def test_load_bundle_bad_checksum(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_manual_bundle(bundle_dir)
    # Tamper with pools.json after the manifest's checksum was computed.
    (bundle_dir / POOLS_FILE).write_text(
        (bundle_dir / POOLS_FILE).read_text().replace("1000", "9999")
    )
    with pytest.raises(BundleError, match="bad checksum"):
        load_bundle(bundle_dir)


def test_load_bundle_unsupported_schema(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_manual_bundle(bundle_dir, manifest_overrides={"schema_version": 99})
    with pytest.raises(BundleError, match="unsupported schema"):
        load_bundle(bundle_dir)


def test_load_bundle_unknown_manifest_key(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_manual_bundle(bundle_dir, manifest_overrides={"unexpected_key": True})
    with pytest.raises(BundleError, match="unknown key"):
        load_bundle(bundle_dir)


def test_load_bundle_negative_amount(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bad_case = json.dumps(
        {"case_id": "c1", "token_in": "TKA", "token_out": "TKB", "amount_in": "-100"}
    )
    _write_manual_bundle(bundle_dir, cases_text=bad_case)
    with pytest.raises(BundleError, match="negative amount"):
        load_bundle(bundle_dir)


def test_load_bundle_zero_amount(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bad_case = json.dumps(
        {"case_id": "c1", "token_in": "TKA", "token_out": "TKB", "amount_in": "0"}
    )
    _write_manual_bundle(bundle_dir, cases_text=bad_case)
    with pytest.raises(BundleError, match="must be positive"):
        load_bundle(bundle_dir)


def test_load_bundle_malformed_json(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_manual_bundle(bundle_dir, cases_text="{not json")
    with pytest.raises(BundleError, match="invalid JSON"):
        load_bundle(bundle_dir)
