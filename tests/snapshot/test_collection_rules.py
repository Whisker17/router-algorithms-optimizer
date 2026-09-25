"""WHI-1436 collection mechanics shared by every source: Multicall3 read batching and the
corpus's single, source-agnostic exclusion rule.

Against the scripted CL node (`cl_chain.FakeChain`) serving the WHI-1429 Agni fixture
state:

1. **Multicall3.** With `rpc.use_multicall3`, the collector sends its pinned reads as
   `aggregate3` batches, re-verifies the pinned Multicall3 code hash at the frozen block,
   cross-checks an aggregated sub-call against a direct `eth_call`, and publishes the
   *same* pool state as the direct read path. A wrong Multicall3 code hash, a lying
   aggregator and a reverted sub-call all refuse.
2. **Exclusion rule.** With `collection.exclusion_rule`, a pool whose envelope needs more
   than the read bound is excluded -- recorded with the rule id, direction, amount and
   bound -- instead of refusing the whole source; without it the collector still refuses
   (`incomplete_snapshot`), and a source with every pool excluded is still refused.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from cl_chain import FakeChain

from snapshot import abi
from snapshot.bundle import POOLS_FILE, load_bundle
from snapshot.collectors import PrepareError
from snapshot.collectors.concentrated import ConcentratedCollector, publish
from snapshot.config import Multicall3Config, ProtocolCatalog
from snapshot.models import ConcentratedPoolState
from snapshot.prepare_config import CollectionLimits, load_prepare_config

REPO = Path(__file__).resolve().parents[2]
AGNI_BUNDLE = REPO / "tests" / "fixtures" / "agni" / "bundle"
PREPARE_CONFIG = REPO / "config" / "prepare" / "agni.yaml"
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"
MULTICALL_CODE = b"\x60\x80multicall3"


def _decode_aggregate3_calldata(data: str) -> list[tuple[str, str]]:
    raw = bytes.fromhex(data[2:])
    assert raw[:4].hex() == abi.SEL_AGGREGATE3
    body = raw[4:]

    def word(at: int) -> int:
        return int.from_bytes(body[at : at + 32], "big")

    base = word(0)
    count = word(base)
    calls = []
    for i in range(count):
        elem = base + 32 + word(base + 32 + 32 * i)
        target = "0x" + body[elem + 12 : elem + 32].hex()
        assert word(elem + 32) == 1  # allowFailure
        at = elem + word(elem + 64)
        n = word(at)
        calls.append((target, "0x" + body[at + 32 : at + 32 + n].hex()))
    return calls


def _encode_results(results: list[tuple[bool, str]]) -> str:
    def w(v: int) -> str:
        return f"{v:064x}"

    elems = []
    for ok, data in results:
        payload = data[2:]
        n = len(payload) // 2
        elems.append(w(int(ok)) + w(0x40) + w(n) + payload + "0" * ((-len(payload)) % 64))
    offsets, cursor = [], 32 * len(elems)
    for e in elems:
        offsets.append(w(cursor))
        cursor += len(e) // 2
    return "0x" + w(0x20) + w(len(elems)) + "".join(offsets) + "".join(elems)


class MulticallChain(FakeChain):
    """`FakeChain` plus a Multicall3 at its canonical address that executes each
    aggregated sub-call against the same scripted state."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.aggregate_calls = 0
        self.lie = False
        self.revert_selector: str | None = None

    def code(self, address: str) -> bytes:
        if address == MULTICALL:
            return MULTICALL_CODE
        return super().code(address)

    def _eth_call(self, to: str, data: str) -> str:
        if to != MULTICALL:
            return super()._eth_call(to, data)
        self.aggregate_calls += 1
        results = []
        for target, sub in _decode_aggregate3_calldata(data):
            if self.revert_selector is not None and sub[2:10] == self.revert_selector:
                results.append((False, "0x"))
                continue
            out = super()._eth_call(target, sub)
            if self.lie:
                out = "0x" + "11" * ((len(out) - 2) // 2)
            results.append((True, out))
        return _encode_results(results)


def _cl_pools(path: Path) -> dict[str, ConcentratedPoolState]:
    bundle = load_bundle(path)
    return {k: v for k, v in bundle.pools.items() if isinstance(v, ConcentratedPoolState)}


@pytest.fixture()
def chain() -> MulticallChain:
    bundle = load_bundle(AGNI_BUNDLE)
    fake = MulticallChain(
        _cl_pools(AGNI_BUNDLE),
        block_number=bundle.block.number,
        block_hash=bundle.block.hash,
        timestamp=bundle.block.timestamp,
    )
    fake.extra_pools[
        (
            "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9",
            "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
            100,
        )
    ] = "0x7b3a4b36b0c5c95142afcd1b883ed055aa166f85"
    return fake


def _catalog(chain: FakeChain, code_hash: str | None = None) -> ProtocolCatalog:
    from test_agni import EXAMPLE, _fake_catalog

    catalog = _fake_catalog(chain, EXAMPLE)
    network = dataclasses.replace(
        catalog.network,
        multicall3=Multicall3Config(
            address=MULTICALL,
            code_hash=code_hash or abi.keccak256_hex(MULTICALL_CODE).lower(),
            verification="test",
        ),
    )
    return dataclasses.replace(catalog, network=network)


def _collector(chain: FakeChain, *, multicall: bool = True, **kwargs: Any) -> ConcentratedCollector:
    config = kwargs.pop("config", load_prepare_config(PREPARE_CONFIG))
    config = dataclasses.replace(
        config, rpc=dataclasses.replace(config.rpc, use_multicall3=multicall)
    )
    return ConcentratedCollector(
        catalog=kwargs.pop("catalog", _catalog(chain)),
        source_key="agni_v3",
        config=config,
        transport=chain,
        block_number=chain.block_number,
        **kwargs,
    )


def test_multicall_reads_publish_the_same_state_as_direct_reads(
    chain: MulticallChain, tmp_path: Path
) -> None:
    collected = _collector(chain).collect()
    publish(tmp_path / "bundle", collected)
    assert (tmp_path / "bundle" / POOLS_FILE).read_bytes() == (
        AGNI_BUNDLE / POOLS_FILE
    ).read_bytes()
    assert chain.aggregate_calls > 0
    record = collected.provenance["read_batching"]
    assert record["address"] == MULTICALL and record["direct_cross_check"] is True
    # The Multicall3 code itself was read pinned to the frozen hash (FakeChain asserts it).
    assert any(m == "eth_getCode" and p[0] == MULTICALL for m, p in chain.log)


def test_direct_read_path_is_unchanged_without_the_flag(chain: MulticallChain) -> None:
    collected = _collector(chain, multicall=False).collect()
    assert chain.aggregate_calls == 0
    assert "read_batching" not in collected.provenance


def test_wrong_multicall_code_hash_refuses(chain: MulticallChain) -> None:
    with pytest.raises(PrepareError, match="code_hash_mismatch.*Multicall3"):
        _collector(chain, catalog=_catalog(chain, code_hash="0x" + "ab" * 32)).collect()


def test_aggregated_result_is_cross_checked_against_a_direct_call(chain: MulticallChain) -> None:
    chain.lie = True
    with pytest.raises(PrepareError, match="inconsistent_state.*differs from a direct eth_call"):
        _collector(chain).collect()


def test_a_reverted_sub_call_refuses(chain: MulticallChain) -> None:
    chain.revert_selector = abi.SEL_TICKS
    with pytest.raises(PrepareError, match="call_reverted"):
        _collector(chain).collect()


def test_multicall_without_a_catalog_pin_refuses(chain: MulticallChain) -> None:
    catalog = _catalog(chain)
    unpinned = dataclasses.replace(
        catalog, network=dataclasses.replace(catalog.network, multicall3=None)
    )
    with pytest.raises(PrepareError, match="config_incomplete.*multicall3"):
        _collector(chain, catalog=unpinned).collect()


def _tight(rule: str | None) -> Any:
    config = load_prepare_config(PREPARE_CONFIG)
    return dataclasses.replace(
        config,
        limits=CollectionLimits(1, 1, max_words_per_direction=5, exclusion_rule=rule),
    )


def test_exclusion_rule_excludes_and_records_instead_of_refusing(chain: MulticallChain) -> None:
    collected = _collector(chain, config=_tight("envelope-unprovable-within-read-bound")).collect()
    exclusions = collected.provenance["exclusions"]
    assert exclusions["rule"] == "envelope-unprovable-within-read-bound"
    assert exclusions["pools"], "the 5-word bound must exclude at least one fixture pool"
    kept = {p.pool_id for p in collected.pools}
    for record in exclusions["pools"]:
        assert record["pool_id"] not in kept
        assert record["rule"] == "envelope-unprovable-within-read-bound"
        assert record["bound"] == {"max_bitmap_words_per_direction": 5}
        assert record["direction"] in ("zero_for_one", "one_for_zero")
        assert int(record["envelope_amount_in"]) > 0
        assert record in collected.provenance["discovery"]["omitted"]
    assert kept, "pools that fit the bound are still admitted"


def test_without_the_rule_the_same_bound_still_refuses(chain: MulticallChain) -> None:
    with pytest.raises(PrepareError, match="incomplete_snapshot.*beyond 5 bitmap words"):
        _collector(chain, config=_tight(None)).collect()


def test_a_source_with_every_pool_excluded_is_refused(chain: MulticallChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    config = dataclasses.replace(
        config, limits=CollectionLimits(0, 0, max_words_per_direction=1, exclusion_rule="r")
    )
    with pytest.raises(PrepareError, match="incomplete_snapshot.*every discovered pool"):
        _collector(chain, config=config).collect()


def test_walk_chunk_words_reads_several_words_per_step(chain: MulticallChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    chunked = dataclasses.replace(
        config, limits=dataclasses.replace(config.limits, walk_chunk_words=8)
    )
    collected = _collector(chain, config=chunked).collect()
    # Same admission outcome and every envelope still fits the collected state.
    baseline = _collector(chain).collect()
    assert [a["status"] for a in collected.provenance["admission"]] == [
        a["status"] for a in baseline.provenance["admission"]
    ]
    for pool in collected.pools:
        lo, hi = pool.bitmap_word_range
        base = next(p for p in baseline.pools if p.pool_id == pool.pool_id).bitmap_word_range
        assert lo <= base[0] and hi >= base[1]


def test_aggregate3_codec_round_trips() -> None:
    calls = [("0x" + "12" * 20, "0xdeadbeef"), ("0x" + "34" * 20, "0x" + "00" * 36)]
    assert _decode_aggregate3_calldata(abi.encode_aggregate3(calls)) == calls
    encoded = _encode_results([(True, "0x" + "ab" * 33), (False, "0x")])
    assert abi.decode_aggregate3(encoded, 2) == [(True, "0x" + "ab" * 33), (False, "0x")]
    with pytest.raises(ValueError, match="expected 3"):
        abi.decode_aggregate3(encoded, 3)
