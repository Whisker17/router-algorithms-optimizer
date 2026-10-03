"""Offline tests for `snapshot.rpc.HttpJsonRpcTransport` batching, bounded retries and
caching (WHI-1429). `urllib.request.urlopen` is replaced by a scripted fake, so no
network is touched; every HTTP request the transport makes is recorded."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from snapshot.rpc import (
    HttpJsonRpcTransport,
    RetryPolicy,
    RpcDiskCache,
    RpcError,
    RpcTransportError,
)

PIN = {"blockHash": "0x" + "ab" * 32, "requireCanonical": True}
Responder = Callable[[Any], Any]


class FakeHttp:
    """Scripted `urlopen`: each request is answered by the next responder, which
    receives the decoded JSON payload and returns a JSON-able response (or raises)."""

    def __init__(self, responders: list[Responder]) -> None:
        self.responders = list(responders)
        self.payloads: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> io.BytesIO:
        payload = json.loads(request.data)
        self.payloads.append(payload)
        if not self.responders:
            raise AssertionError(f"unexpected extra HTTP request: {payload}")
        answer = self.responders.pop(0)(payload)
        return io.BytesIO(json.dumps(answer).encode())


def ok(results: Callable[[dict[str, Any]], Any]) -> Responder:
    return lambda payload: [
        {"jsonrpc": "2.0", "id": item["id"], "result": results(item)} for item in payload
    ]


def echo(item: dict[str, Any]) -> Any:
    return "r:" + item["params"][0]["data"]


def _call(n: int) -> tuple[str, list[Any]]:
    return ("eth_call", [{"to": "0x" + "11" * 20, "data": f"0x{n:08x}"}, PIN])


def _transport(
    monkeypatch: pytest.MonkeyPatch, fake: FakeHttp, **kwargs: Any
) -> HttpJsonRpcTransport:
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    sleeps: list[float] = []
    transport = HttpJsonRpcTransport(
        "https://rpc.example.invalid",
        retry_policy=kwargs.pop(
            "retry_policy",
            RetryPolicy(max_attempts=4, base_delay_seconds=1.0, max_delay_seconds=3.0),
        ),
        sleep=sleeps.append,
        **kwargs,
    )
    transport.sleeps = sleeps  # type: ignore[attr-defined]
    return transport


def test_batches_are_chunked_and_answered_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHttp([ok(echo), ok(echo), ok(echo)])
    transport = _transport(monkeypatch, fake, max_batch_size=5)
    calls = [_call(n) for n in range(12)]
    results = transport.call_batch(calls)
    assert results == [f"r:0x{n:08x}" for n in range(12)]
    assert [len(p) for p in fake.payloads] == [5, 5, 2]
    assert transport.call_count == 3


def test_rate_limited_items_are_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    def partly_limited(payload: Any) -> Any:
        out = []
        for item in payload:
            if item["id"] % 2 == 1:
                out.append(
                    {
                        "jsonrpc": "2.0",
                        "id": item["id"],
                        "error": {
                            "code": -32016,
                            "message": "rate limit exceeded, please try it later.",
                        },
                    }
                )
            else:
                out.append({"jsonrpc": "2.0", "id": item["id"], "result": echo(item)})
        return out

    fake = FakeHttp([partly_limited, partly_limited, ok(echo)])
    transport = _transport(monkeypatch, fake, max_batch_size=4)
    results = transport.call_batch([_call(n) for n in range(4)])
    assert results == [f"r:0x{n:08x}" for n in range(4)]
    # attempt 1: 4 items (2 limited); attempt 2: the 2 retried (1 limited); attempt 3: 1.
    assert [len(p) for p in fake.payloads] == [4, 2, 1]
    assert transport.sleeps == [1.0, 2.0]  # type: ignore[attr-defined]
    assert transport.retries == 2


def test_network_failures_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(payload: Any) -> Any:
        raise urllib.error.URLError("connection reset")

    fake = FakeHttp([down, down, down])
    transport = _transport(
        monkeypatch,
        fake,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=0.75),
    )
    with pytest.raises(RpcTransportError, match="after 3 attempts"):
        transport.call("eth_chainId", [])
    assert len(fake.payloads) == 3
    assert transport.sleeps == [0.5, 0.75]  # type: ignore[attr-defined]


def test_deterministic_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHttp(
        [
            lambda payload: [
                {"jsonrpc": "2.0", "id": 0, "error": {"code": 3, "message": "execution reverted"}}
            ]
        ]
    )
    transport = _transport(monkeypatch, fake)
    with pytest.raises(RpcError, match="execution reverted"):
        transport.call(*_call(0))
    assert len(fake.payloads) == 1


def test_lagging_node_header_not_found_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    lagging = lambda payload: [  # noqa: E731
        {"jsonrpc": "2.0", "id": 0, "error": {"code": -32000, "message": "header not found"}}
    ]
    fake = FakeHttp([lagging, ok(echo)])
    transport = _transport(monkeypatch, fake)
    assert transport.call(*_call(7)) == "r:0x00000007"


def test_disk_cache_serves_only_hash_pinned_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    header = {"number": "0x1", "hash": PIN["blockHash"], "timestamp": "0x2"}
    fake = FakeHttp([ok(echo), ok(lambda item: header), ok(lambda item: "0x1388")])
    first = _transport(monkeypatch, fake, disk_cache=RpcDiskCache(tmp_path))
    first.call_batch([_call(1), _call(2)])
    first.call("eth_getBlockByNumber", ["0x1", False])
    first.call("eth_chainId", [])
    assert len(fake.payloads) == 3

    # A fresh transport (new process) re-reads block headers and unpinned calls live
    # but answers the pinned eth_calls from disk without any HTTP request.
    fake2 = FakeHttp([ok(lambda item: header), ok(lambda item: "0x1388")])
    second = _transport(monkeypatch, fake2, disk_cache=RpcDiskCache(tmp_path))
    assert second.call_batch([_call(1), _call(2)]) == ["r:0x00000001", "r:0x00000002"]
    assert second.disk_cache_hits == 2
    assert fake2.payloads == []
    second.call("eth_getBlockByNumber", ["0x1", False])
    second.call("eth_getBlockByNumber", ["0x1", False])  # never cached, even in memory
    assert len(fake2.payloads) == 2
    files = list(tmp_path.iterdir())
    assert [f.name for f in files] == [f"{PIN['blockHash']}.jsonl"]


def test_disk_cache_ignores_a_torn_trailing_line(tmp_path: Path) -> None:
    cache = RpcDiskCache(tmp_path)
    method, params = _call(3)
    cache.put(method, params, "0xabc")
    path = tmp_path / f"{PIN['blockHash']}.jsonl"
    path.write_text(path.read_text() + '{"key": "eth_call [trunc')
    assert RpcDiskCache(tmp_path).get(method, params) == (True, "0xabc")
