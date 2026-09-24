"""JSON-RPC transport for fixed-block EVM reads.

Two implementations:

- `HttpJsonRpcTransport` — the real transport, stdlib-only (`urllib`), used for the
  explicit online preflight and fixed-block collectors. Bounded retries with
  exponential backoff (network failures and the public endpoint's transient
  rate-limit/internal JSON-RPC errors only -- a deterministic error such as a revert is
  never retried); JSON-RPC batching (`call_batch`) in small chunks; an in-memory cache
  so one run never issues the same (method, params) call twice; and an optional
  on-disk `RpcDiskCache` for block-hash-pinned reads. No implicit `"latest"` anywhere
  — callers always pass an explicit block.
- Tests inject their own fake (see `tests/snapshot/test_preflight.py`) implementing
  the same `RpcTransport` protocol, so the offline suite never touches the network.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class RpcError(RuntimeError):
    """A JSON-RPC call reached the node but returned a JSON-RPC error object."""

    def __init__(self, method: str, params: list[Any], code: int, message: str) -> None:
        self.method = method
        self.params = params
        self.code = code
        self.message = message
        super().__init__(f"{method}({params!r}) -> JSON-RPC error {code}: {message}")


class RpcTransportError(RuntimeError):
    """The call could not be completed at all (network/timeout/decode failure)."""


class RpcTransport(Protocol):
    """The subset of JSON-RPC the preflight needs. Implementations must never
    silently substitute a different chain/block/endpoint than what was requested."""

    def call(self, method: str, params: list[Any]) -> Any:
        """Return the JSON-RPC `result`, or raise `RpcError`/`RpcTransportError`."""
        ...


class BatchRpcTransport(RpcTransport, Protocol):
    """An `RpcTransport` that can also answer many calls at once (collectors)."""

    def call_batch(self, calls: Sequence[tuple[str, list[Any]]]) -> list[Any]:
        """Results in input order; raise `RpcError`/`RpcTransportError` like `call`."""
        ...


# JSON-RPC error codes the public Mantle endpoint returns for *transient* conditions
# (observed 2026-09-24: `-32016 rate limit exceeded` inside batches). `-32005` is the
# common "limit exceeded" code, `-32603` a generic internal error. Anything else --
# notably reverts (`3`, `-32000 execution reverted`) -- is deterministic and raised.
RETRYABLE_RPC_ERROR_CODES = frozenset({-32005, -32016, -32603})
# Load-balanced public nodes can briefly lag: a hash-pinned read on a node that has not
# seen the block yet answers `header not found`. Retried (bounded); a truly unknown
# block still fails once the attempts are exhausted.
RETRYABLE_RPC_ERROR_MESSAGES = ("header not found", "rate limit")


def is_retryable_rpc_error(code: int, message: str) -> bool:
    lowered = message.lower()
    return code in RETRYABLE_RPC_ERROR_CODES or any(
        m in lowered for m in RETRYABLE_RPC_ERROR_MESSAGES
    )


# Block-identity queries are answered live every time: a reorg-check that re-reads a
# block header must never be satisfied from a cache filled earlier in the same run.
UNCACHED_METHODS = frozenset({"eth_blockNumber", "eth_getBlockByNumber", "eth_getBlockByHash"})


def _pinned_block_hash(params: list[Any]) -> str | None:
    """The EIP-1898 `blockHash` a call is pinned to, if any. Only such calls are
    immutable (state at a given block hash never changes), hence disk-cacheable."""
    for p in params:
        if isinstance(p, dict) and isinstance(p.get("blockHash"), str):
            return str(p["blockHash"]).lower()
    return None


class RpcDiskCache:
    """Append-only on-disk cache of block-hash-pinned read results.

    One JSON-Lines file per block hash under `root`; a line is
    `{"key": <method + canonical params>, "result": <raw result>}`. Only calls whose
    params carry an EIP-1898 `{"blockHash": ...}` are cached: their answer is fixed
    by the hash, so a cached value can never be stale. Calls by block number, tags or
    no block are never cached (a number can be reorged; block-identity checks must
    always be re-read live). A torn trailing line from an interrupted write is
    ignored rather than trusted. Block-header queries (`UNCACHED_METHODS`) are never
    cached, even when pinned by hash."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._loaded: dict[str, dict[str, Any]] = {}

    def _file(self, block_hash: str) -> Path:
        return self._root / f"{block_hash}.jsonl"

    def _entries(self, block_hash: str) -> dict[str, Any]:
        if block_hash not in self._loaded:
            entries: dict[str, Any] = {}
            path = self._file(block_hash)
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("key"), str):
                        entries[rec["key"]] = rec.get("result")
            self._loaded[block_hash] = entries
        return self._loaded[block_hash]

    @staticmethod
    def key(method: str, params: list[Any]) -> str:
        return method + " " + json.dumps(params, sort_keys=True)

    def get(self, method: str, params: list[Any]) -> tuple[bool, Any]:
        block_hash = _pinned_block_hash(params)
        if block_hash is None or method in UNCACHED_METHODS:
            return False, None
        entries = self._entries(block_hash)
        k = self.key(method, params)
        if k in entries:
            return True, entries[k]
        return False, None

    def put(self, method: str, params: list[Any], result: Any) -> None:
        block_hash = _pinned_block_hash(params)
        if block_hash is None or method in UNCACHED_METHODS:
            return
        entries = self._entries(block_hash)
        k = self.key(method, params)
        if k in entries:
            return
        entries[k] = result
        self._root.mkdir(parents=True, exist_ok=True)
        with self._file(block_hash).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": k, "result": result}, sort_keys=True) + "\n")


@dataclass(frozen=True)
class RetryPolicy:
    """`max_attempts` counts the initial try. The delay doubles per retry, capped."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """`attempt` is 0-indexed (0 = first retry after the initial failed try)."""
        return min(self.base_delay_seconds * float(2**attempt), self.max_delay_seconds)


class HttpJsonRpcTransport:
    """Bounded-retry JSON-RPC client over a single fixed HTTP(S) endpoint.

    No credential is ever accepted or logged here: the URL is expected to be the
    non-secret public default (or an operator-provided override), never a
    credential-bearing string baked into an artifact (docs/DESIGN.md §2.12).
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 20.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Any = time.sleep,
        disk_cache: RpcDiskCache | None = None,
        max_batch_size: int = 5,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be >= 1")
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._cache: dict[tuple[str, str], Any] = {}
        self._disk_cache = disk_cache
        self._max_batch_size = max_batch_size
        self.call_count = 0  # HTTP requests sent (a batch counts once)
        self.cache_hits = 0
        self.disk_cache_hits = 0
        self.retries = 0

    @property
    def url(self) -> str:
        return self._url

    def _post(self, payload: Any) -> Any:
        """One HTTP POST; network/decode failures raise `RpcTransportError`."""
        self.call_count += 1
        body = json.dumps(payload).encode("utf-8")
        try:
            request = urllib.request.Request(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return json.loads(response.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise RpcTransportError(f"POST to {self._url} failed: {exc}") from exc

    def _cached(self, method: str, params: list[Any]) -> tuple[bool, Any]:
        if method in UNCACHED_METHODS:
            return False, None
        cache_key = (method, json.dumps(params, sort_keys=True))
        if cache_key in self._cache:
            self.cache_hits += 1
            return True, self._cache[cache_key]
        if self._disk_cache is not None:
            hit, result = self._disk_cache.get(method, params)
            if hit:
                self.disk_cache_hits += 1
                self._cache[cache_key] = result
                return True, result
        return False, None

    def _store(self, method: str, params: list[Any], result: Any) -> None:
        if method in UNCACHED_METHODS:
            return
        self._cache[(method, json.dumps(params, sort_keys=True))] = result
        if self._disk_cache is not None:
            self._disk_cache.put(method, params, result)

    def call(self, method: str, params: list[Any]) -> Any:
        return self.call_batch([(method, params)])[0]

    def call_batch(self, calls: Sequence[tuple[str, list[Any]]]) -> list[Any]:
        """Answer `calls` in order, sending uncached ones as JSON-RPC batches of at
        most `max_batch_size`. Items that fail transiently (whole-request network
        failure, or a retryable per-item error) are re-sent after a backoff delay
        until `retry_policy.max_attempts` is exhausted; a deterministic per-item
        JSON-RPC error raises `RpcError` immediately."""
        results: list[Any] = [None] * len(calls)
        pending: list[int] = []
        for i, (method, params) in enumerate(calls):
            hit, result = self._cached(method, params)
            if hit:
                results[i] = result
            else:
                pending.append(i)

        attempt = 0
        last_error: str = ""
        while pending:
            if attempt >= self._retry_policy.max_attempts:
                method, params = calls[pending[0]]
                raise RpcTransportError(
                    f"{method} to {self._url} failed after "
                    f"{self._retry_policy.max_attempts} attempts ({len(pending)} call(s) "
                    f"unanswered): {last_error}"
                )
            if attempt > 0:
                self.retries += 1
                self._sleep(self._retry_policy.delay_for(attempt - 1))
            attempt += 1
            still_pending: list[int] = []
            for chunk_start in range(0, len(pending), self._max_batch_size):
                chunk = pending[chunk_start : chunk_start + self._max_batch_size]
                payload = [
                    {"jsonrpc": "2.0", "id": n, "method": calls[i][0], "params": calls[i][1]}
                    for n, i in enumerate(chunk)
                ]
                try:
                    response = self._post(payload)
                except RpcTransportError as exc:
                    last_error = str(exc)
                    still_pending.extend(chunk)
                    continue
                by_id = _index_batch_response(response)
                for n, i in enumerate(chunk):
                    method, params = calls[i]
                    item = by_id.get(n)
                    if item is None:
                        last_error = f"batch response missing id {n}"
                        still_pending.append(i)
                        continue
                    if "error" in item:
                        err = item["error"] if isinstance(item["error"], dict) else {}
                        code = int(err.get("code", -1))
                        message = str(err.get("message", ""))
                        if is_retryable_rpc_error(code, message):
                            last_error = f"JSON-RPC error {code}: {message}"
                            still_pending.append(i)
                            continue
                        # Deterministic (e.g. a revert): retrying cannot change it.
                        raise RpcError(method, params, code, message)
                    if "result" not in item:
                        last_error = f"batch item {n} has neither result nor error"
                        still_pending.append(i)
                        continue
                    results[i] = item["result"]
                    self._store(method, params, item["result"])
            pending = still_pending
        return results


def _index_batch_response(response: Any) -> dict[int, dict[str, Any]]:
    """Map a JSON-RPC batch response by id. A single error object (some nodes answer a
    whole rejected batch with one) maps to nothing, so every item is retried."""
    if not isinstance(response, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for item in response:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            out[item["id"]] = item
    return out
