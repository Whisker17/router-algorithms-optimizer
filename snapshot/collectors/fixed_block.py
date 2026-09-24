"""Fixed-block reading shared by the online collectors (docs/DESIGN.md §2.2, §4.4
Prepare): the concentrated-liquidity collector (WHI-1429) and the constant-product
Classic collector (WHI-1432).

`FixedBlockReader` owns block identity and every chain read:

- the requested block number is resolved once to its hash/timestamp (an expected hash,
  if given, must match), the chain id must equal the catalog's, and the block must be at
  or below the node's `finalized` head;
- every later state read is an EIP-1898 `{"blockHash": h, "requireCanonical": true}`
  call, so a node can only answer from exactly that block -- nothing ever reads
  `latest`, and there is no fallback block;
- after collection the header is re-read by number and by hash; a different hash
  (reorg) aborts publication.

`http_transport` builds the public-RPC transport (small batches, bounded retries,
on-disk cache of hash-pinned results) from a prepare config's RPC settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snapshot import abi
from snapshot.collectors.base import PrepareError
from snapshot.config import ProtocolCatalog
from snapshot.models import BlockRef
from snapshot.prepare_config import RpcSettings
from snapshot.rpc import (
    BatchRpcTransport,
    HttpJsonRpcTransport,
    RetryPolicy,
    RpcDiskCache,
    RpcError,
    RpcTransportError,
)


class FixedBlockReader:
    """Block identity plus block-hash-pinned reads for one collection run."""

    def __init__(
        self,
        *,
        catalog: ProtocolCatalog,
        transport: BatchRpcTransport,
        block_number: int,
        expected_block_hash: str | None = None,
        rpc_label: str = "<injected transport>",
    ) -> None:
        self.catalog = catalog
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


def http_transport(
    rpc_url: str, settings: RpcSettings, cache_dir: Path | None
) -> HttpJsonRpcTransport:
    return HttpJsonRpcTransport(
        rpc_url,
        timeout_seconds=settings.timeout_seconds,
        retry_policy=RetryPolicy(
            max_attempts=settings.max_attempts,
            base_delay_seconds=settings.base_delay_seconds,
            max_delay_seconds=settings.max_delay_seconds,
        ),
        disk_cache=RpcDiskCache(cache_dir) if cache_dir is not None else None,
        max_batch_size=settings.max_batch_size,
    )
