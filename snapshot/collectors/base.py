"""Shared prepare-source request/error types (docs/DESIGN.md §4.4 Prepare flow).

Every registered collector is `collect(request: PrepareRequest) -> SnapshotBundle`.
Offline sources (`synthetic`) ignore the network fields and refuse a block; online
sources require an explicit `block_number` and never read `latest` (docs/DESIGN.md
§2.2: "RPC failure must never fall back to `latest` or mix blocks").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_CATALOG_PATH = Path("config/protocols.yaml")
DEFAULT_RPC_CACHE_DIR = Path("data/cache/rpc")


@dataclass(frozen=True)
class PrepareRequest:
    output_dir: Path
    block_number: int | None = None
    expected_block_hash: str | None = None
    rpc_url: str | None = None  # None -> the catalog's public default
    prepare_config: Path | None = None  # None -> the source's default config
    catalog_path: Path = DEFAULT_CATALOG_PATH
    cache_dir: Path | None = DEFAULT_RPC_CACHE_DIR  # None disables the disk cache


class PrepareError(RuntimeError):
    """Preparation refused to publish. `code` is a stable category, e.g.
    `block_hash_mismatch`, `block_not_finalized`, `code_hash_mismatch`,
    `identity_mismatch`, `incomplete_snapshot`, `inconsistent_state`,
    `admission_failed`, `rpc_unavailable`, `invalid_request`."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
