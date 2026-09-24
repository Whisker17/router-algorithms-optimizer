"""JSON-RPC transport for fixed-block EVM reads.

Two implementations:

- `HttpJsonRpcTransport` — the real transport, stdlib-only (`urllib`), used for the
  explicit online preflight. Bounded retries with exponential backoff; a small
  in-memory cache so a preflight run never issues the same (method, params) call
  twice; no implicit `"latest"` anywhere — callers always pass an explicit block.
- Tests inject their own fake (see `tests/snapshot/test_preflight.py`) implementing
  the same `RpcTransport` protocol, so the offline suite never touches the network.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RetryPolicy:
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
    ) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._cache: dict[tuple[str, str], Any] = {}
        self.call_count = 0
        self.cache_hits = 0

    @property
    def url(self) -> str:
        return self._url

    def call(self, method: str, params: list[Any]) -> Any:
        cache_key = (method, json.dumps(params, sort_keys=True))
        if cache_key in self._cache:
            self.cache_hits += 1
            return self._cache[cache_key]

        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(
            "utf-8"
        )
        last_error: Exception | None = None
        for attempt in range(self._retry_policy.max_attempts):
            self.call_count += 1
            try:
                request = urllib.request.Request(
                    self._url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    payload = json.loads(response.read())
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
                if attempt + 1 < self._retry_policy.max_attempts:
                    self._sleep(self._retry_policy.delay_for(attempt))
                    continue
                raise RpcTransportError(
                    f"{method} to {self._url} failed after "
                    f"{self._retry_policy.max_attempts} attempts: {exc}"
                ) from exc

            if "error" in payload:
                err = payload["error"]
                # JSON-RPC errors (reverts, bad params) are not retried: retrying an
                # already-answered call cannot change a deterministic revert.
                raise RpcError(method, params, err.get("code", -1), err.get("message", ""))

            result = payload["result"]
            self._cache[cache_key] = result
            return result

        # Unreachable: the loop above always returns or raises.
        raise RpcTransportError(f"{method} to {self._url} failed: {last_error}")
