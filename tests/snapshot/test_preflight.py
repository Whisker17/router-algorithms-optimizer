"""Offline tests for the admission preflight. No network access: every RPC call is
served by `ScriptedTransport` below, which implements the same `RpcTransport`
protocol (`snapshot.rpc`) that the real `HttpJsonRpcTransport` does. Any call the
test does not explicitly script raises, so a test can never pass by accident on an
unscripted, silently-defaulted response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from snapshot import abi
from snapshot.config import (
    Blocker,
    CandidateBlock,
    ContractRef,
    NetworkConfig,
    ProtocolCatalog,
    SourceConfig,
    UpstreamProvenance,
    load_catalog,
)
from snapshot.preflight import (
    PreflightFailed,
    redact_url,
    run_preflight,
    run_preflight_or_raise,
)
from snapshot.rpc import RpcError

FACTORY = "0x" + "11" * 20
POOL = "0x" + "22" * 20
TOKEN0 = "0x" + "33" * 20
TOKEN1 = "0x" + "44" * 20
POOL_DEPLOYER = "0x" + "55" * 20
BLOCK_HASH = "0x" + "aa" * 32
BLOCK_NUMBER = 100
BLOCK_TIMESTAMP = 1_700_000_000


@dataclass
class ScriptedTransport:
    """A fully explicit fake `RpcTransport`: every call must be scripted or it
    raises, so tests cannot pass on an accidental default."""

    chain_id: int = 5000
    block: dict[str, Any] | None = None
    block_error: Exception | None = None
    code: dict[str, str] = field(default_factory=dict)
    calls: dict[tuple[str, str], Any] = field(default_factory=dict)
    chain_id_error: Exception | None = None

    def call(self, method: str, params: list[Any]) -> Any:
        if method == "eth_chainId":
            if self.chain_id_error:
                raise self.chain_id_error
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            if self.block_error:
                raise self.block_error
            return self.block
        if method == "eth_getCode":
            address = params[0].lower()
            return self.code.get(address, "0x")
        if method == "eth_call":
            to = params[0]["to"].lower()
            data = params[0]["data"]
            key = (to, data)
            if key not in self.calls:
                raise RpcError(method, params, -32000, f"unscripted call to {to} {data[:10]}...")
            result = self.calls[key]
            if isinstance(result, Exception):
                raise result
            return result
        raise AssertionError(f"unscripted RPC method: {method}")


def default_block() -> dict[str, Any]:
    return {
        "number": hex(BLOCK_NUMBER),
        "hash": BLOCK_HASH,
        "timestamp": hex(BLOCK_TIMESTAMP),
    }


def build_v3_source(
    *,
    key: str = "test_v3",
    fee_tiers: dict[int, int] | None = None,
    pool_deployer: str | None = None,
    factory_code_hash: str | None = None,
) -> SourceConfig:
    contracts = {
        "factory": ContractRef(address=FACTORY, code_hash=factory_code_hash),
        "example_pool": ContractRef(address=POOL),
    }
    if pool_deployer:
        contracts["pool_deployer"] = ContractRef(address=pool_deployer)
    return SourceConfig(
        key=key,
        display_name="Test V3",
        protocol_family="v3_concentrated_liquidity",
        confidence="high",
        contracts=contracts,
        tokens={"token0": TOKEN0, "token1": TOKEN1},
        pool_fee=500,
        expected_fee_tiers=fee_tiers or {500: 10},
        upstream=UpstreamProvenance(
            repo="https://example.invalid/repo",
            ref="deadbeef",
            license="MIT",
            confidence_basis="test",
        ),
        blockers=(Blocker(id="test_blocker", description="known gap", blocks=("I99",)),),
    )


def build_catalog(sources: tuple[SourceConfig, ...]) -> ProtocolCatalog:
    return ProtocolCatalog(
        version=1,
        network=NetworkConfig(name="mantle", chain_id=5000, default_rpc_url="https://rpc.mantle.xyz"),
        candidate_block=CandidateBlock(
            number=BLOCK_NUMBER,
            hash=BLOCK_HASH,
            timestamp=BLOCK_TIMESTAMP,
            captured_at="2026-01-01T00:00:00Z",
            status="candidate_only",
            note="test fixture",
        ),
        sources=sources,
    )


def script_healthy_v3(transport: ScriptedTransport, source: SourceConfig) -> None:
    """Script every call `_check_v3_family` will make for a fully-healthy source."""
    factory = source.contracts["factory"].address
    pool = source.contracts["example_pool"].address
    for ref in source.contracts.values():
        transport.code[ref.address.lower()] = "0x60"  # any non-empty bytecode

    transport.calls[(pool.lower(), abi.encode_call(abi.SEL_FACTORY))] = (
        "0x" + factory[2:].rjust(64, "0")
    )
    args = abi.pad_address(TOKEN0) + abi.pad_address(TOKEN1) + abi.pad_uint(source.pool_fee)  # type: ignore[arg-type]
    transport.calls[(factory.lower(), abi.encode_call(abi.SEL_GET_POOL, args))] = (
        "0x" + pool[2:].rjust(64, "0")
    )
    for fee_amount, spacing in (source.expected_fee_tiers or {}).items():
        selector_data = abi.encode_call(
            abi.SEL_FEE_AMOUNT_TICK_SPACING, abi.pad_uint(fee_amount)
        )
        transport.calls[(factory.lower(), selector_data)] = abi.pad_uint(spacing)


class TestChainAndBlockRejection:
    def test_rejects_wrong_chain(self) -> None:
        catalog = build_catalog((build_v3_source(),))
        transport = ScriptedTransport(chain_id=1)  # Ethereum mainnet, not Mantle
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert report.observed_chain_id == 1
        codes = [i.code for i in report.issues]
        assert "wrong_chain" in codes
        # A wrong chain must short-circuit: no contract was ever checked.
        assert report.checked_sources == []

    def test_rejects_block_hash_mismatch(self) -> None:
        catalog = build_catalog((build_v3_source(),))
        transport = ScriptedTransport(
            chain_id=5000, block={**default_block(), "hash": "0x" + "bb" * 32}
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert [i.code for i in report.issues] == ["block_hash_mismatch"]
        assert report.checked_sources == []

    def test_rejects_missing_block(self) -> None:
        catalog = build_catalog((build_v3_source(),))
        transport = ScriptedTransport(chain_id=5000, block=None)
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert [i.code for i in report.issues] == ["block_unavailable"]

    def test_rejects_block_timestamp_mismatch(self) -> None:
        catalog = build_catalog((build_v3_source(),))
        transport = ScriptedTransport(
            chain_id=5000, block={**default_block(), "timestamp": hex(BLOCK_TIMESTAMP + 1)}
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert "block_timestamp_mismatch" in [i.code for i in report.issues]

    def test_accepts_matching_chain_and_block(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        report = run_preflight(catalog, transport)
        assert report.ok, report.issues
        assert report.observed_chain_id == 5000
        assert report.checked_sources == ["test_v3"]


class TestContractStateRejection:
    def test_rejects_code_hash_mismatch(self) -> None:
        pinned_hash = "0x" + "cc" * 32
        source = build_v3_source(factory_code_hash=pinned_hash)
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        # The scripted factory bytecode ("0x60") hashes to something other than
        # the pinned hash -- simulating bytecode drift since the catalog was written.
        report = run_preflight(catalog, transport)
        assert not report.ok
        mismatches = [i for i in report.issues if i.code == "code_hash_mismatch"]
        assert len(mismatches) == 1
        assert mismatches[0].contract_role == "factory"

    def test_accepts_matching_code_hash(self) -> None:
        pinned_hash = abi.code_hash("0x60")
        source = build_v3_source(factory_code_hash=pinned_hash)
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        report = run_preflight(catalog, transport)
        assert report.ok, report.issues

    def test_rejects_missing_code(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        del transport.code[
            source.contracts["example_pool"].address.lower()
        ]  # simulate wrong address/chain
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "missing_code" for i in report.issues)

    def test_rejects_factory_identity_mismatch(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        # pool.factory() answers with a different factory than configured.
        wrong_factory = "0x" + "99" * 20
        transport.calls[(POOL.lower(), abi.encode_call(abi.SEL_FACTORY))] = (
            "0x" + wrong_factory[2:].rjust(64, "0")
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "identity_mismatch" for i in report.issues)

    def test_rejects_getpool_roundtrip_mismatch(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        args = abi.pad_address(TOKEN0) + abi.pad_address(TOKEN1) + abi.pad_uint(500)
        other_pool = "0x" + "88" * 20
        transport.calls[(FACTORY.lower(), abi.encode_call(abi.SEL_GET_POOL, args))] = (
            "0x" + other_pool[2:].rjust(64, "0")
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "identity_mismatch" for i in report.issues)

    def test_rejects_fee_tier_fingerprint_mismatch(self) -> None:
        # Simulates exactly the real finding: a source claims the canonical Uniswap
        # tier set but the live factory answers with a different (e.g. Pancake-style)
        # spacing for one tier.
        source = build_v3_source(fee_tiers={500: 10, 3000: 60})
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        transport.calls[
            (FACTORY.lower(), abi.encode_call(abi.SEL_FEE_AMOUNT_TICK_SPACING, abi.pad_uint(3000)))
        ] = abi.pad_uint(0)  # tier not enabled, unlike canonical Uniswap
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "fee_tier_mismatch" for i in report.issues)

    def test_rejects_reverting_call_as_unavailable_state(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        transport.calls[(POOL.lower(), abi.encode_call(abi.SEL_FACTORY))] = RpcError(
            "eth_call", [], 3, "execution reverted"
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "call_reverted" for i in report.issues)

    def test_pool_deployer_missing_code_is_reported_per_contract(self) -> None:
        source = build_v3_source(pool_deployer=POOL_DEPLOYER)
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        del transport.code[POOL_DEPLOYER.lower()]
        report = run_preflight(catalog, transport)
        assert not report.ok
        missing = [i for i in report.issues if i.code == "missing_code"]
        assert any(i.contract_role == "pool_deployer" for i in missing)

    def test_known_blockers_are_surfaced_but_do_not_fail_a_healthy_run(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        report = run_preflight(catalog, transport)
        assert report.ok
        assert report.known_blockers == [
            {
                "source_key": "test_v3",
                "id": "test_blocker",
                "description": "known gap",
                "blocks": ["I99"],
            }
        ]


class TestClassicAndLiquidityBookFamilies:
    def test_v2_classic_healthy_round_trip(self) -> None:
        factory, pool = FACTORY, POOL
        source = SourceConfig(
            key="test_classic",
            display_name="Test Classic",
            protocol_family="v2_classic",
            confidence="high",
            contracts={
                "factory": ContractRef(address=factory),
                "example_pool": ContractRef(address=pool),
            },
            tokens={"token0": TOKEN0, "token1": TOKEN1},
        )
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        transport.code[factory.lower()] = "0x60"
        transport.code[pool.lower()] = "0x60"
        transport.calls[(pool.lower(), abi.encode_call(abi.SEL_FACTORY))] = (
            "0x" + factory[2:].rjust(64, "0")
        )
        transport.calls[(pool.lower(), abi.encode_call(abi.SEL_GET_RESERVES))] = (
            "0x" + abi.pad_uint(123) + abi.pad_uint(456) + abi.pad_uint(1_700_000_000)
        )
        args = abi.pad_address(TOKEN0) + abi.pad_address(TOKEN1)
        transport.calls[(factory.lower(), abi.encode_call(abi.SEL_GET_PAIR, args))] = (
            "0x" + pool[2:].rjust(64, "0")
        )
        report = run_preflight(catalog, transport)
        assert report.ok, report.issues

    def test_v2_classic_rejects_truncated_reserves(self) -> None:
        factory, pool = FACTORY, POOL
        source = SourceConfig(
            key="test_classic",
            display_name="Test Classic",
            protocol_family="v2_classic",
            confidence="high",
            contracts={
                "factory": ContractRef(address=factory),
                "example_pool": ContractRef(address=pool),
            },
            tokens={"token0": TOKEN0, "token1": TOKEN1},
        )
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        transport.code[factory.lower()] = "0x60"
        transport.code[pool.lower()] = "0x60"
        transport.calls[(pool.lower(), abi.encode_call(abi.SEL_FACTORY))] = (
            "0x" + factory[2:].rjust(64, "0")
        )
        transport.calls[(pool.lower(), abi.encode_call(abi.SEL_GET_RESERVES))] = (
            "0x" + abi.pad_uint(123)
        )
        args = abi.pad_address(TOKEN0) + abi.pad_address(TOKEN1)
        transport.calls[(factory.lower(), abi.encode_call(abi.SEL_GET_PAIR, args))] = (
            "0x" + pool[2:].rjust(64, "0")
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "missing_state" for i in report.issues)

    def test_liquidity_book_healthy_round_trip(self) -> None:
        factory, pair = FACTORY, POOL
        source = SourceConfig(
            key="test_lb",
            display_name="Test LB",
            protocol_family="liquidity_book_v2",
            confidence="high",
            contracts={
                "factory": ContractRef(address=factory),
                "example_pair": ContractRef(address=pair),
            },
        )
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        transport.code[factory.lower()] = "0x60"
        transport.code[pair.lower()] = "0x60"
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_FACTORY))] = (
            "0x" + factory[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_TOKEN_X))] = (
            "0x" + TOKEN0[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_TOKEN_Y))] = (
            "0x" + TOKEN1[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_BIN_STEP))] = abi.pad_uint(15)
        transport.calls[(factory.lower(), abi.encode_call(abi.SEL_GET_NUMBER_OF_LB_PAIRS))] = (
            abi.pad_uint(195)
        )
        report = run_preflight(catalog, transport)
        assert report.ok, report.issues

    def test_liquidity_book_rejects_zero_bin_step(self) -> None:
        factory, pair = FACTORY, POOL
        source = SourceConfig(
            key="test_lb",
            display_name="Test LB",
            protocol_family="liquidity_book_v2",
            confidence="high",
            contracts={
                "factory": ContractRef(address=factory),
                "example_pair": ContractRef(address=pair),
            },
        )
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        transport.code[factory.lower()] = "0x60"
        transport.code[pair.lower()] = "0x60"
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_FACTORY))] = (
            "0x" + factory[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_TOKEN_X))] = (
            "0x" + TOKEN0[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_TOKEN_Y))] = (
            "0x" + TOKEN1[2:].rjust(64, "0")
        )
        transport.calls[(pair.lower(), abi.encode_call(abi.SEL_GET_BIN_STEP))] = abi.pad_uint(0)
        transport.calls[(factory.lower(), abi.encode_call(abi.SEL_GET_NUMBER_OF_LB_PAIRS))] = (
            abi.pad_uint(195)
        )
        report = run_preflight(catalog, transport)
        assert not report.ok
        assert any(i.code == "missing_state" for i in report.issues)


class TestRunPreflightOrRaise:
    def test_raises_on_rejection(self) -> None:
        catalog = build_catalog((build_v3_source(),))
        transport = ScriptedTransport(chain_id=1)
        with pytest.raises(PreflightFailed) as excinfo:
            run_preflight_or_raise(catalog, transport)
        assert "wrong_chain" in str(excinfo.value)

    def test_returns_report_when_ok(self) -> None:
        source = build_v3_source()
        catalog = build_catalog((source,))
        transport = ScriptedTransport(chain_id=5000, block=default_block())
        script_healthy_v3(transport, source)
        report = run_preflight_or_raise(catalog, transport)
        assert report.ok


class TestRedactUrl:
    def test_strips_credentials_and_query(self) -> None:
        assert (
            redact_url("https://user:secret@rpc.example.com:8545/v3/abcdef?key=shh")
            == "https://rpc.example.com:8545/v3/abcdef"
        )

    def test_public_default_round_trips(self) -> None:
        assert redact_url("https://rpc.mantle.xyz") == "https://rpc.mantle.xyz"


class TestRealCatalogSchema:
    """The shipped config/protocols.yaml must always parse and stay internally
    consistent. This does not touch the network — only YAML/schema validation."""

    def test_loads_and_covers_five_sources(self) -> None:
        catalog = load_catalog("config/protocols.yaml")
        assert catalog.network.chain_id == 5000
        keys = {s.key for s in catalog.sources}
        assert keys == {
            "uniswap_v3",
            "agni_v3",
            "fusionx_v3",
            "moe_classic_v1",
            "moe_lb_v2_2",
        }

    def test_every_source_has_an_upstream_or_is_explicitly_unmatched(self) -> None:
        catalog = load_catalog("config/protocols.yaml")
        for source in catalog.sources:
            if source.confidence == "unmatched":
                assert source.blockers, f"{source.key} is unmatched but declares no blocker"
            else:
                assert source.upstream is not None, f"{source.key} has no upstream provenance"

    def test_most_contracts_have_a_pinned_code_hash(self) -> None:
        catalog = load_catalog("config/protocols.yaml")
        total = 0
        hashed = 0
        for source in catalog.sources:
            for ref in source.contracts.values():
                total += 1
                if ref.code_hash is not None:
                    hashed += 1
        assert total >= 15
        assert hashed == total, "every configured contract should carry a pinned code hash"
