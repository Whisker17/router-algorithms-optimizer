"""WHI-1432: Merchant Moe Classic v1 replayed from verified, fixed-block reserves.

1. **Migrated semantics, not assumed.** `pools.constant_product.SOURCES["moe_classic_v1"]`
   carries the pair's own fixed 0.3% fee (`MoePair.swap`'s `balance * 1000 - amountIn *
   3`) and uint112 reserve width, and the catalog's `classic_collection.swap_fee_bps` must
   agree with it.
2. **Fixed-block fork evidence.** `tests/fixtures/moe_classic/bundle/` was published by
   `main.py prepare --source moe_classic --block 101057678`;
   `tests/fixtures/moe_classic/evidence.jsonl.gz` was produced independently by
   `tools/cl_evidence/test/CaptureMoeClassicReplay.t.sol` with the *deployed* MoePair
   clones and MoeRouter on a Mantle fork at that block. For every (case x pair) request,
   in both directions, and for a sequential second swap from the post-state: the Python
   output must equal the router quote and the tokens the recipient received, the pair
   must reject one more wei (`Moe: K`, so the quote is the pair's exact maximum), the
   next state must equal the pair's reserves after the swap, and dust / uint112 overflow
   must fail exactly where the pair reverts.
3. **Rejections**: a wrong source (a non-Classic or unadmitted catalog source, another
   implementation's or an LB clone's code, a pair not at its CREATE2 address, a foreign
   factory), a wrong token order (swapped clone args or getters, an unsorted bundle
   record) and unsupported token behaviour (balance != reserve, wrong decimals, an
   undeclared token, a token the pair does not hold).
4. **Cohorts**: every captured pair is in the full cohort and, via the catalog's
   `sor_protocol: V2`, in the SOR-compatible V2 cohort.
5. **Offline replay**: `validate`/`run` over the saved bundle with sockets disabled.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

import main
from pools import constant_product
from pools.constant_product import SOURCES, quote_exact_in
from pools.result import QuoteStatus
from snapshot import abi
from snapshot.bundle import (
    MANIFEST_FILE,
    POOLS_FILE,
    PROVENANCE_FILE,
    BundleError,
    load_bundle,
    sha256_bytes,
)
from snapshot.collectors import COLLECTORS, PrepareError, PrepareRequest
from snapshot.collectors.classic import (
    ClassicCollector,
    clone_address,
    clone_runtime_code,
    publish,
)
from snapshot.config import ConfigError, ContractRef, ProtocolCatalog, load_catalog, parse_catalog
from snapshot.models import ConstantProductPoolState, SnapshotBundle
from snapshot.prepare_config import (
    ClassicPairSpec,
    ClassicPrepareConfig,
    PrepareConfigError,
    load_classic_prepare_config,
    parse_classic_prepare_config,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "moe_classic"
FIXTURE_BUNDLE = FIXTURES / "bundle"
EVIDENCE = FIXTURES / "evidence.jsonl.gz"
PREPARE_CONFIG = REPO / "config" / "prepare" / "moe_classic.yaml"
CATALOG_PATH = REPO / "config" / "protocols.yaml"
CATALOG = load_catalog(CATALOG_PATH)
MOE = CATALOG.source("moe_classic_v1")
SMOKE_PROFILE = REPO / "config" / "smoke.yaml"
FACTORY = MOE.contracts["factory"].address.lower()
IMPLEMENTATION = MOE.contracts["pair_implementation"].address.lower()
EXAMPLE = MOE.contracts["example_pool"].address.lower()  # USDT/WMNT
USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
WETH = "0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111"
METH = "0xcda86a272531e8640cd7f1a92c01839911b90bb0"
DECIMALS = {USDT: 6, USDC: 6, WMNT: 18, WETH: 18, METH: 18}
# MoeFactory.getPair for all ten pairs of the five tokens at block 101057678.
PAIRS = {
    "0x4e7685df06201521f35a182467feefe02c53d847": (USDT, WMNT),
    "0x1a4d4aa3bd8587f6e05cc98cf87954f7d95c11c6": (USDC, WMNT),
    "0x4a18891de69124d2853a4e27543edb7e2e001179": (WMNT, WETH),
    "0xa375ea3e1f92d62e3a71b668bab09f7155267fa3": (WMNT, METH),
    "0x86e3a987187fed135d6d9c114f1857d8144f01e1": (METH, WETH),
    "0xaf50cd8c03096a416fc1b88e328bab3e09e0f175": (USDC, METH),
    "0x19a414a6b1743315c731492cb9b7b559d7db9ab7": (USDT, WETH),
    "0x5c819961990c9f4f9fbfd4101f1d4e565b8aa0a6": (USDT, METH),
    "0x33b1d7cfff71bba9dd987f96ad57e0a5f7db9ac5": (USDC, WETH),
    "0x8e3a13418743ab1a98434551937ea687e451b589": (USDC, USDT),
}
DUST_CASES = {"weth_usdt_dust", "usdc_usdt_dust"}


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during an offline command")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


@pytest.fixture(scope="module")
def bundle() -> SnapshotBundle:
    return load_bundle(FIXTURE_BUNDLE)


@pytest.fixture(scope="module")
def provenance() -> dict[str, Any]:
    obj = json.loads((FIXTURE_BUNDLE / PROVENANCE_FILE).read_text())
    assert isinstance(obj, dict)
    return obj


@pytest.fixture(scope="module")
def evidence() -> list[dict[str, Any]]:
    lines = gzip.decompress(EVIDENCE.read_bytes()).decode().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    assert records[0]["kind"] == "meta" and records[-1]["kind"] == "end"
    return records


def _pools(b: SnapshotBundle) -> dict[str, ConstantProductPoolState]:
    out = {k: v for k, v in b.pools.items() if isinstance(v, ConstantProductPoolState)}
    assert len(out) == len(b.pools)
    return out


def _lower(rec: dict[str, Any], *keys: str) -> tuple[str, ...]:
    return tuple(str(rec[k]).lower() for k in keys)


# ---------------------------------------------------------------------------
# 1. Migrated semantics and the admission record
# ---------------------------------------------------------------------------


def test_moe_fee_is_the_pairs_own_constant_not_an_assumed_v2_default() -> None:
    source = SOURCES["moe_classic_v1"]
    assert (source.fee_bps, source.reserve_bits) == (30, 112)
    assert source.overflow_revert == "Moe: OVERFLOW"
    assert MOE.classic_collection is not None and MOE.classic_collection.admitted
    assert MOE.classic_collection.swap_fee_bps == source.fee_bps
    # 30/10_000 reproduces MoePair's 3/1000 exactly: hand-computed vector,
    # 997 * 1000 * 5000 // (10000 * 1000 + 997 * 1000) = 4985000000 // 10997000 = 453
    assert constant_product.get_amount_out(1000, 10_000, 5_000, 30) == 453
    assert "moe_classic" in COLLECTORS
    assert load_classic_prepare_config(PREPARE_CONFIG).source_key == "moe_classic_v1"
    assert (MOE.tokens["token0"].lower(), MOE.tokens["token1"].lower()) == (USDT, WMNT)


def test_clone_layout_reproduces_the_pinned_example_pair() -> None:
    """The ImmutableClone runtime/CREATE2 derivation, checked against the catalog's
    independently pinned example-pair code hash and the real pair address."""
    data = bytes.fromhex(USDT[2:] + WMNT[2:])
    code = clone_runtime_code(IMPLEMENTATION, data)
    assert len(code) == 95
    assert abi.keccak256_hex(code) == MOE.contracts["example_pool"].code_hash
    assert clone_address(FACTORY, IMPLEMENTATION, USDT, WMNT) == EXAMPLE
    # a different token pair has different code (the tokens are clone immutables)
    assert clone_runtime_code(IMPLEMENTATION, bytes.fromhex(USDC[2:] + WMNT[2:])) != code


def test_fixture_is_the_published_classic_bundle_at_the_catalog_block(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    cb = CATALOG.candidate_block
    assert bundle.kind == "real"
    assert (bundle.block.chain_id, bundle.block.number, bundle.block.hash) == (
        CATALOG.network.chain_id,
        cb.number,
        cb.hash,
    )
    assert provenance["source_key"] == "moe_classic_v1"
    catalog = provenance["catalog"]
    assert catalog["factory"] == {
        "address": FACTORY,
        "code_hash": MOE.contracts["factory"].code_hash,
    }
    assert catalog["pair_implementation"] == {
        "address": IMPLEMENTATION,
        "code_hash": MOE.contracts["pair_implementation"].code_hash,
    }
    assert catalog["swap_fee_bps"] == 30
    assert MOE.upstream is not None and catalog["upstream"]["ref"] == MOE.upstream.ref
    assert provenance["prepare_config"] == {
        "path": "config/prepare/moe_classic.yaml",
        "sha256": sha256_bytes(PREPARE_CONFIG.read_bytes()),  # the committed selection
    }
    assert provenance["tokens"] == {
        t: {"label": label, "decimals": DECIMALS[t]}
        for t, label in (
            (USDT, "USDT"),
            (USDC, "USDC"),
            (WMNT, "WMNT"),
            (WETH, "WETH"),
            (METH, "METH"),
        )
    }

    pools = _pools(bundle)
    assert {pid: (p.token0, p.token1) for pid, p in pools.items()} == PAIRS
    for pool_id, pool in pools.items():
        assert pool.source_key == "moe_classic_v1" and pool.fee_bps == 30
        assert pool.token0 < pool.token1
        assert pool.reserve0 > 0 and pool.reserve1 > 0
        record = provenance["pools"][pool_id]
        assert record["balances_equal_reserves"] is True
        assert record["code_hash"] == abi.keccak256_hex(
            clone_runtime_code(IMPLEMENTATION, bytes.fromhex(pool.token0[2:] + pool.token1[2:]))
        )
    assert provenance["pools"][EXAMPLE]["code_hash"] == MOE.contracts["example_pool"].code_hash
    assert provenance["discovery"]["omitted"] == []
    expected = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    assert {(a["case_id"], a["pool_id"]) for a in provenance["admission"]} == expected
    statuses = {a["case_id"]: a["status"] for a in provenance["admission"]}
    assert {c for c, s in statuses.items() if s == "insufficient_output_amount"} == DUST_CASES
    assert set(statuses.values()) == {"ok", "insufficient_output_amount"}
    assert len(bundle.cases) == 62


def test_every_state_component_records_the_bundle_block(provenance: dict[str, Any]) -> None:
    manifest = json.loads((FIXTURE_BUNDLE / MANIFEST_FILE).read_text())
    raw = json.loads((FIXTURE_BUNDLE / POOLS_FILE).read_text())
    assert len(raw["pools"]) == len(PAIRS)
    for rec in raw["pools"]:
        assert rec["source_key"] == "moe_classic_v1"
        assert rec["read_at"] == {
            "block_number": manifest["block"]["number"],
            "block_hash": manifest["block"]["hash"],
        }
    assert provenance["block"] == manifest["block"]


# ---------------------------------------------------------------------------
# 2. Independent fork evidence at the same block
# ---------------------------------------------------------------------------


def test_evidence_was_captured_at_the_bundle_block_with_the_catalog_router(
    bundle: SnapshotBundle, evidence: list[dict[str, Any]]
) -> None:
    meta = evidence[0]
    assert meta["schema"] == "moe-classic-replay-evidence/1"
    assert meta["bundle_id"] == bundle.bundle_id
    assert (meta["chain_id"], meta["block_number"], meta["block_hash"]) == (
        bundle.block.chain_id,
        bundle.block.number,
        bundle.block.hash,
    )
    assert meta["block_timestamp"] == bundle.block.timestamp
    router = MOE.contracts["router"]
    assert meta["router"].lower() == router.address.lower()
    assert meta["router_code_hash"] == router.code_hash
    assert meta["router_factory"].lower() == FACTORY


def test_forked_pair_state_equals_the_collected_state(
    bundle: SnapshotBundle, provenance: dict[str, Any], evidence: list[dict[str, Any]]
) -> None:
    """An independent read path (the fork's own view of each deployed pair) of the whole
    collected state: tokens, order, decimals, clone target, reserves == balances."""
    states = [r for r in evidence if r["kind"] == "pool_state"]
    pools = _pools(bundle)
    assert {r["pool"].lower() for r in states} == set(PAIRS)
    for rec in states:
        pool = pools[rec["pool"].lower()]
        assert _lower(rec, "token0", "token1") == (pool.token0, pool.token1)
        assert (rec["decimals0"], rec["decimals1"]) == (
            DECIMALS[pool.token0],
            DECIMALS[pool.token1],
        )
        assert _lower(rec, "factory", "implementation") == (FACTORY, IMPLEMENTATION)
        assert rec["code_hash"] == provenance["pools"][pool.pool_id]["code_hash"]
        assert (int(rec["reserve0"]), int(rec["reserve1"])) == (pool.reserve0, pool.reserve1)
        assert (int(rec["balance0"]), int(rec["balance1"])) == (pool.reserve0, pool.reserve1)
        assert (
            int(rec["block_timestamp_last"])
            == provenance["pools"][pool.pool_id]["block_timestamp_last"]
        )


def _check_swap(state: ConstantProductPoolState, rec: dict[str, Any]) -> ConstantProductPoolState:
    """One executed (or refused) swap in the evidence against the migrated Python."""
    token_in = state.token0 if rec["zero_for_one"] else state.token1
    amount = int(rec["amount_in"])
    assert int(rec["credited"]) == amount  # the transfer credited exactly: no fee-on-transfer
    result = quote_exact_in(state, token_in, amount)
    router_out = int(rec["router_amount_out"])
    if not rec["executed"]:
        assert router_out == 0 and rec["revert"] == "Moe: INSUFFICIENT_OUTPUT_AMOUNT"
        assert result.status is QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT
        assert result.new_state is None
        return state
    assert result.status is QuoteStatus.OK, result.detail
    assert result.amount_out == router_out == int(rec["received"])
    assert rec["plus_one"] == "Moe: K"  # one more wei breaks the pair's own invariant
    assert result.new_state is not None
    assert (result.new_state.reserve0, result.new_state.reserve1) == (
        int(rec["reserve0"]),
        int(rec["reserve1"]),
    )
    assert (int(rec["balance0"]), int(rec["balance1"])) == (
        int(rec["reserve0"]),
        int(rec["reserve1"]),
    )
    return result.new_state


def test_every_swap_and_sequential_followup_matches_the_deployed_pair(
    bundle: SnapshotBundle, evidence: list[dict[str, Any]]
) -> None:
    pools = _pools(bundle)
    swaps = [r for r in evidence if r["kind"] == "swap"]
    followups = {(r["case_id"], r["pool"].lower()): r for r in evidence if r["kind"] == "followup"}
    assert {(r["case_id"], r["pool"].lower()) for r in swaps} == {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    directions: dict[str, set[bool]] = {pid: set() for pid in PAIRS}
    for rec in swaps:
        key = (rec["case_id"], rec["pool"].lower())
        case = bundle.case(rec["case_id"])
        state = pools[key[1]]
        assert rec["zero_for_one"] is (case.token_in == state.token0)
        assert int(rec["amount_in"]) == case.amount_in
        directions[key[1]].add(rec["zero_for_one"])
        after = _check_swap(state, rec)
        if rec["executed"]:
            # the second, reverse swap starts from the Python next state
            follow = followups.pop(key)
            assert follow["zero_for_one"] is not rec["zero_for_one"]
            assert int(follow["amount_in"]) == int(rec["received"]) // 2
            assert follow["executed"]
            _check_swap(after, follow)
        else:
            assert key not in followups
    assert followups == {}
    assert all(d == {True, False} for d in directions.values())  # both directions per pair


def test_uint112_overflow_is_reverted_exactly_where_the_pair_reverts(
    bundle: SnapshotBundle, evidence: list[dict[str, Any]]
) -> None:
    state = _pools(bundle)[EXAMPLE]
    probes = [r for r in evidence if r["kind"] == "overflow"]
    assert len(probes) == 4
    for rec in probes:
        assert rec["pool"].lower() == EXAMPLE
        token_in = state.token0 if rec["zero_for_one"] else state.token1
        amount = int(rec["amount_in"])
        reserve_in = state.reserves_for(token_in)[0]
        result = quote_exact_in(state, token_in, amount)
        if rec["executed"]:
            assert reserve_in + amount == (1 << 112) - 1
            assert result.status is QuoteStatus.OK
            assert result.amount_out == int(rec["received"]) == int(rec["router_amount_out"])
            assert result.new_state is not None
            assert (result.new_state.reserve0, result.new_state.reserve1) == (
                int(rec["reserve0"]),
                int(rec["reserve1"]),
            )
        else:
            assert reserve_in + amount == 1 << 112
            assert rec["revert"] == "Moe: OVERFLOW"
            assert result.status is QuoteStatus.REVERTED
            assert "Moe: OVERFLOW" in result.detail


# ---------------------------------------------------------------------------
# 3a. Quote-, bundle- and catalog-level rejections
# ---------------------------------------------------------------------------


def test_quote_rejects_unsupported_token_and_foreign_semantics(bundle: SnapshotBundle) -> None:
    state = _pools(bundle)[EXAMPLE]
    assert quote_exact_in(state, WETH, 10**18).status is QuoteStatus.UNSUPPORTED_TOKEN
    wrong_fee = dataclasses.replace(state, fee_bps=25)  # e.g. a 0.25% fork's fee
    result = quote_exact_in(wrong_fee, USDT, 10**6)
    assert result.status is QuoteStatus.UNSUPPORTED and "fixed 30" in result.detail
    unknown = dataclasses.replace(state, source_key="uniswap_v2")
    result = quote_exact_in(unknown, USDT, 10**6)
    assert result.status is QuoteStatus.UNSUPPORTED and "no migrated" in result.detail
    # the source-free synthetic formula has no uint112 bound; the Moe pair does
    huge = (1 << 112) - state.reserve0
    assert quote_exact_in(state, USDT, huge).status is QuoteStatus.REVERTED
    generic = dataclasses.replace(state, source_key=None)
    assert quote_exact_in(generic, USDT, huge).status is QuoteStatus.OK


def _rewrite(bundle_dir: Path, name: str, mutate: Any) -> None:
    path = bundle_dir / name
    obj = json.loads(path.read_text())
    mutate(obj)
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    manifest = json.loads((bundle_dir / MANIFEST_FILE).read_text())
    manifest["checksums"][name] = sha256_bytes(text.encode())
    (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True))


def _example_record(obj: dict[str, Any]) -> dict[str, Any]:
    rec = next(p for p in obj["pools"] if p["pool_id"] == EXAMPLE)
    assert isinstance(rec, dict)
    return rec


def _swap_tokens(rec: dict[str, Any]) -> None:
    rec["token0"], rec["token1"] = rec["token1"], rec["token0"]
    rec["reserve0"], rec["reserve1"] = rec["reserve1"], rec["reserve0"]


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda o: _swap_tokens(_example_record(o)), "must sort below token1"),
        (lambda o: _example_record(o).update(source_key="moe_lb_v2_2"), "source_key"),
        (lambda o: _example_record(o).update(fee_bps=25), "fixed 30"),
        (lambda o: _example_record(o).update(reserve0=str(1 << 112)), "exceeds uint112"),
        (lambda o: _example_record(o).pop("read_at"), "needs both `source_key` and `read_at`"),
        (
            lambda o: _example_record(o)["read_at"].update(block_number=1),
            "never mixes blocks",
        ),
    ],
)
def test_loader_rejects_malformed_classic_state(tmp_path: Path, mutate: Any, match: str) -> None:
    bundle_dir = tmp_path / "b"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    _rewrite(bundle_dir, POOLS_FILE, mutate)
    with pytest.raises(BundleError, match=match):
        load_bundle(bundle_dir)


def _raw_catalog() -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(CATALOG_PATH.read_text())
    assert isinstance(raw, dict)
    return raw


def _source_raw(raw: dict[str, Any], key: str) -> dict[str, Any]:
    entry = next(s for s in raw["sources"] if s["key"] == key)
    assert isinstance(entry, dict)
    return entry


@pytest.mark.parametrize(
    "key,mutate,match",
    [
        ("moe_classic_v1", lambda s: s.update(sor_protocol="V3"), "routes only"),
        ("moe_lb_v2_2", lambda s: s.update(sor_protocol="V2"), "routes only"),
        ("uniswap_v3", lambda s: s.update(sor_protocol="V2"), "routes only"),
        (
            "uniswap_v3",
            lambda s: s.update(classic_collection={"admitted": True}),
            "only valid for v2_classic",
        ),
        (
            "moe_classic_v1",
            lambda s: s["classic_collection"].update(swap_fee_bps=0),
            "swap_fee_bps",
        ),
        ("moe_classic_v1", lambda s: s["classic_collection"].pop("verification"), "missing"),
    ],
)
def test_catalog_rejects_malformed_classic_admission(key: str, mutate: Any, match: str) -> None:
    raw = _raw_catalog()
    mutate(_source_raw(raw, key))
    with pytest.raises(ConfigError, match=match):
        parse_catalog(raw)


def test_prepare_config_rejects_undeclared_tokens_and_missing_decimals() -> None:
    import yaml

    raw = yaml.safe_load(PREPARE_CONFIG.read_text())
    bad = json.loads(json.dumps(raw))
    bad["pairs"].append({"tokens": ["USDT", "0x" + "11" * 20]})
    with pytest.raises(PrepareConfigError, match="not a declared token"):
        parse_classic_prepare_config(bad, source_path="x", sha256="0")
    bad = json.loads(json.dumps(raw))
    del bad["tokens"]["USDT"]["decimals"]
    with pytest.raises(PrepareConfigError, match="decimals"):
        parse_classic_prepare_config(bad, source_path="x", sha256="0")


# ---------------------------------------------------------------------------
# 3b. Collector mechanics against a scripted node
# ---------------------------------------------------------------------------


def _word(value: int) -> str:
    return format(value, "064x")


def _ret(*values: int) -> str:
    return "0x" + "".join(_word(v) for v in values)


class FakeClassicChain:
    """A JSON-RPC node serving Moe Classic pairs at one block. Every state read must be
    EIP-1898 pinned to that block's hash with `requireCanonical`."""

    def __init__(self, bundle: SnapshotBundle, provenance: dict[str, Any]) -> None:
        self.pools = dict(_pools(bundle))
        self.block_number = bundle.block.number
        self.block_hash = bundle.block.hash
        self.hashes_by_number = [bundle.block.hash]
        self.timestamp = bundle.block.timestamp
        self.ts_last = {p: provenance["pools"][p]["block_timestamp_last"] for p in self.pools}
        self.factory_code = b"\x60\x01moefactory"
        self.impl_code = b"\x60\x02moepair"
        self.impl_answer = IMPLEMENTATION
        self.decimals = dict(DECIMALS)
        self.balance_delta: dict[tuple[str, str], int] = {}
        self.code_patch: dict[str, bytes] = {}
        self.getter_patch: dict[tuple[str, str], str] = {}
        self.pair_address: dict[tuple[str, str], str] = {}  # getPair override
        self.log: list[tuple[str, list[Any]]] = []

    def _check_pin(self, pin: Any) -> None:
        assert pin == {"blockHash": self.block_hash, "requireCanonical": True}, pin

    def _header(self, block_hash: str) -> dict[str, str]:
        return {
            "number": hex(self.block_number),
            "hash": block_hash,
            "timestamp": hex(self.timestamp),
        }

    def code(self, address: str) -> bytes:
        if address == FACTORY:
            return self.factory_code
        if address == IMPLEMENTATION:
            return self.impl_code
        if address in self.code_patch:
            return self.code_patch[address]
        p = self._pool_at(address)
        return clone_runtime_code(IMPLEMENTATION, bytes.fromhex(p.token0[2:] + p.token1[2:]))

    def _pool_at(self, address: str) -> ConstantProductPoolState:
        for (t0, t1), alias in self.pair_address.items():
            if alias == address and t0 < t1:  # a relocated pair (reverse keys only lie)
                return next(p for p in self.pools.values() if (p.token0, p.token1) == (t0, t1))
        return self.pools[address]

    def call(self, method: str, params: list[Any]) -> Any:
        self.log.append((method, params))
        if method == "eth_chainId":
            return hex(5000)
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(self.block_number + 100), "hash": "0x" + "00" * 32}
            assert params[0] == hex(self.block_number), params
            h = (
                self.hashes_by_number.pop(0)
                if len(self.hashes_by_number) > 1
                else self.hashes_by_number[0]
            )
            return self._header(h)
        if method == "eth_getBlockByHash":
            return self._header(params[0])
        if method == "eth_getCode":
            self._check_pin(params[1])
            return "0x" + self.code(params[0].lower()).hex()
        if method == "eth_call":
            self._check_pin(params[1])
            return self._eth_call(params[0]["to"].lower(), params[0]["data"])
        raise AssertionError(f"unscripted RPC method {method}")

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(m, p) for m, p in calls]

    def _eth_call(self, to: str, data: str) -> str:
        sel, args = data[2:10], data[10:]
        argv = [int(args[i : i + 64], 16) for i in range(0, len(args), 64)]
        if to == FACTORY:
            if sel == abi.SEL_MOE_PAIR_IMPLEMENTATION:
                return _ret(int(self.impl_answer, 16))
            assert sel == abi.SEL_GET_PAIR, sel
            a, b = f"0x{argv[0]:040x}", f"0x{argv[1]:040x}"
            t0, t1 = sorted((a, b))
            if (a, b) in self.pair_address:
                return _ret(int(self.pair_address[(a, b)], 16))
            if (t0, t1) in self.pair_address:
                return _ret(int(self.pair_address[(t0, t1)], 16))
            for p in self.pools.values():
                if (p.token0, p.token1) == (t0, t1):
                    return _ret(int(p.pool_id, 16))
            return _ret(0)
        if to in DECIMALS:
            if sel == abi.SEL_DECIMALS:
                return _ret(self.decimals[to])
            assert sel == abi.SEL_BALANCE_OF, sel
            holder = f"0x{argv[0]:040x}"
            p = self._pool_at(holder)
            reserve = p.reserve0 if to == p.token0 else p.reserve1
            return _ret(reserve + self.balance_delta.get((holder, to), 0))
        p = self._pool_at(to)
        answers = {
            abi.SEL_TOKEN0: p.token0,
            abi.SEL_TOKEN1: p.token1,
            abi.SEL_FACTORY: FACTORY,
            abi.SEL_IMPLEMENTATION: IMPLEMENTATION,
        }
        if sel in answers:
            return _ret(int(self.getter_patch.get((to, sel), answers[sel]), 16))
        assert sel == abi.SEL_GET_RESERVES, sel
        return _ret(p.reserve0, p.reserve1, self.ts_last[p.pool_id])


def _fake_catalog(chain: FakeClassicChain) -> ProtocolCatalog:
    contracts = dict(MOE.contracts)
    contracts["factory"] = ContractRef(FACTORY, abi.keccak256_hex(chain.factory_code))
    contracts["pair_implementation"] = ContractRef(
        IMPLEMENTATION, abi.keccak256_hex(chain.impl_code)
    )
    source = dataclasses.replace(MOE, contracts=contracts)
    return dataclasses.replace(
        CATALOG,
        sources=tuple(source if s.key == "moe_classic_v1" else s for s in CATALOG.sources),
    )


@pytest.fixture()
def chain(bundle: SnapshotBundle, provenance: dict[str, Any]) -> FakeClassicChain:
    return FakeClassicChain(bundle, provenance)


def _collector(
    chain: FakeClassicChain, config: ClassicPrepareConfig | None = None, **kwargs: Any
) -> ClassicCollector:
    return ClassicCollector(
        catalog=kwargs.pop("catalog", _fake_catalog(chain)),
        source_key=kwargs.pop("source_key", "moe_classic_v1"),
        config=config or load_classic_prepare_config(PREPARE_CONFIG),
        transport=chain,
        block_number=chain.block_number,
        **kwargs,
    )


def test_collector_reproduces_the_fixture_state_from_pinned_reads(
    chain: FakeClassicChain, tmp_path: Path
) -> None:
    collected = _collector(chain, expected_block_hash=chain.block_hash).collect()
    out = publish(tmp_path / "bundle", collected)
    for name in (POOLS_FILE, "cases.jsonl"):
        assert (tmp_path / "bundle" / name).read_bytes() == (FIXTURE_BUNDLE / name).read_bytes()
    assert out.bundle_id == load_bundle(FIXTURE_BUNDLE).bundle_id
    assert all(json.dumps(p).count("latest") == 0 for _, p in chain.log)
    assert [m for m, _ in chain.log][-2:] == ["eth_getBlockByHash", "eth_getBlockByNumber"]
    assert collected.provenance["source_capability"] == {
        "protocol_family": "v2_classic",
        "sor_protocol": "V2",
    }


def test_captured_pairs_are_in_the_full_and_the_sor_v2_cohorts(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    sor = CATALOG.sor_protocols()
    assert sor["moe_classic_v1"] == "V2"
    assert "moe_lb_v2_2" not in sor
    assert provenance["source_capability"]["sor_protocol"] == "V2"
    full = set(_pools(bundle))
    v2 = {pid for pid, p in _pools(bundle).items() if sor.get(p.source_key or "") == "V2"}
    assert full == v2 == set(PAIRS)
    # one pair per token pair: the SOR V2 pool identity (factory, token0, token1) is unique
    assert len({(p.token0, p.token1) for p in _pools(bundle).values()}) == len(PAIRS)


@pytest.mark.parametrize(
    "source_key,code",
    [
        ("uniswap_v3", "invalid_request"),  # not a v2_classic source
        ("moe_lb_v2_2", "invalid_request"),
        ("moe_classic_v2", "invalid_request"),  # not in the catalog at all
    ],
)
def test_wrong_source_is_refused(chain: FakeClassicChain, source_key: str, code: str) -> None:
    with pytest.raises(PrepareError, match=code):
        _collector(chain, source_key=source_key)


def test_unadmitted_or_fee_mismatched_catalog_entry_is_refused(chain: FakeClassicChain) -> None:
    catalog = _fake_catalog(chain)
    moe = catalog.source("moe_classic_v1")
    assert moe.classic_collection is not None
    for classic, match in (
        (dataclasses.replace(moe.classic_collection, admitted=False), "source_not_admitted"),
        (dataclasses.replace(moe.classic_collection, swap_fee_bps=25), "catalog swap_fee_bps 25"),
        (None, "source_not_admitted"),
    ):
        bad = dataclasses.replace(
            catalog,
            sources=tuple(
                dataclasses.replace(s, classic_collection=classic)
                if s.key == "moe_classic_v1"
                else s
                for s in catalog.sources
            ),
        )
        with pytest.raises(PrepareError, match=match):
            _collector(chain, catalog=bad)


def test_foreign_factory_or_implementation_is_rejected(chain: FakeClassicChain) -> None:
    catalog = _fake_catalog(chain)
    chain.factory_code = b"\x60\x09otherfactory"
    with pytest.raises(PrepareError, match="code_hash_mismatch: factory"):
        _collector(chain, catalog=catalog).collect()
    chain.factory_code = b"\x60\x01moefactory"
    chain.impl_answer = "0x" + "ab" * 20
    with pytest.raises(PrepareError, match="identity_mismatch.*moePairImplementation"):
        _collector(chain).collect()


def _victim(chain: FakeClassicChain) -> ConstantProductPoolState:
    return next(p for pid, p in chain.pools.items() if pid != EXAMPLE)


@pytest.mark.parametrize(
    "patch",
    [
        "other_implementation",  # a clone of some other contract, same tokens
        "swapped_args",  # token order reversed inside the clone immutables
        "lb_clone",  # a 97-byte LB-pair-style clone
        "full_contract",  # real bytecode instead of a clone
    ],
)
def test_pair_code_that_is_not_the_verified_clone_is_rejected(
    chain: FakeClassicChain, patch: str
) -> None:
    victim = _victim(chain)
    args = bytes.fromhex(victim.token0[2:] + victim.token1[2:])
    code = {
        "other_implementation": clone_runtime_code("0x" + "cd" * 20, args),
        "swapped_args": clone_runtime_code(
            IMPLEMENTATION, bytes.fromhex(victim.token1[2:] + victim.token0[2:])
        ),
        "lb_clone": clone_runtime_code(IMPLEMENTATION, args + b"\x00\x0f"),
        "full_contract": b"\x60\x80\x60\x40" + bytes(9591),
    }[patch]
    chain.code_patch[victim.pool_id] = code
    with pytest.raises(PrepareError, match="code_hash_mismatch.*not the ImmutableClone"):
        _collector(chain).collect()
    # refused before any of the victim's state is read
    assert not any(m == "eth_call" and p[0]["to"].lower() == victim.pool_id for m, p in chain.log)


def test_pair_not_at_its_create2_address_is_rejected(chain: FakeClassicChain) -> None:
    """Correct clone code at an address MoeFactory would never have deployed it to."""
    victim = _victim(chain)
    chain.pair_address[(victim.token0, victim.token1)] = "0x" + "77" * 20
    with pytest.raises(PrepareError, match="identity_mismatch.*CREATE2"):
        _collector(chain).collect()


def test_asymmetric_get_pair_is_rejected(chain: FakeClassicChain) -> None:
    victim = _victim(chain)
    chain.pair_address[(victim.token1, victim.token0)] = EXAMPLE
    with pytest.raises(PrepareError, match="identity_mismatch.*disagree"):
        _collector(chain).collect()


@pytest.mark.parametrize("selector", [abi.SEL_TOKEN0, abi.SEL_FACTORY, abi.SEL_IMPLEMENTATION])
def test_pair_getters_disagreeing_with_the_clone_are_rejected(
    chain: FakeClassicChain, selector: str
) -> None:
    victim = _victim(chain)
    chain.getter_patch[(victim.pool_id, selector)] = victim.token1
    with pytest.raises(PrepareError, match="identity_mismatch.*token0, token1, factory"):
        _collector(chain).collect()


def test_balance_differing_from_reserve_is_unsupported_token_behavior(
    chain: FakeClassicChain,
) -> None:
    victim = _victim(chain)
    chain.balance_delta[(victim.pool_id, victim.token1)] = 1  # a donation / rebase
    with pytest.raises(PrepareError, match="unsupported_token_behavior"):
        _collector(chain).collect()


def test_token_decimals_disagreeing_with_the_declaration_are_rejected(
    chain: FakeClassicChain,
) -> None:
    chain.decimals[USDC] = 18
    with pytest.raises(PrepareError, match="identity_mismatch.*decimals"):
        _collector(chain).collect()


def test_missing_pair_is_a_recorded_omission_and_exclusions_are_reasoned(
    chain: FakeClassicChain,
) -> None:
    config = load_classic_prepare_config(PREPARE_CONFIG)
    usdc_usdt = next(p for p in config.pairs if (p.token0, p.token1) == (USDC, USDT))
    del chain.pools[
        next(pid for pid, p in chain.pools.items() if (p.token0, p.token1) == (USDC, USDT))
    ]
    others = tuple(p for p in config.pairs if p is not usdc_usdt)
    no_cases = tuple(c for c in config.cases if {c.token_in, c.token_out} != {USDC, USDT})
    trimmed = dataclasses.replace(
        config,
        pairs=(
            *others,
            ClassicPairSpec(USDC, USDT),
            ClassicPairSpec(METH, "0x" + "ff" * 20, excluded="illustrative exclusion"),
        ),
        cases=no_cases,
    )
    collected = _collector(chain, trimmed).collect()
    omitted = collected.provenance["discovery"]["omitted"]
    assert omitted[0] == {
        "token0": USDC,
        "token1": USDT,
        "pool_id": None,
        "reason": "factory.getPair returned the zero address",
    }
    assert omitted[1]["reason"] == "excluded by prepare config: illustrative exclusion"
    assert len(collected.pools) == len(PAIRS) - 1


def test_changing_block_hash_during_collection_aborts_publication(
    chain: FakeClassicChain, tmp_path: Path
) -> None:
    chain.hashes_by_number = [chain.block_hash, "0x" + "ee" * 32]
    with pytest.raises(PrepareError, match="block_hash_mismatch.*not published"):
        publish(tmp_path / "bundle", _collector(chain).collect())
    assert list(tmp_path.iterdir()) == []


def test_prepare_requires_an_explicit_block(tmp_path: Path) -> None:
    with pytest.raises(PrepareError, match="explicit --block"):
        COLLECTORS["moe_classic"](PrepareRequest(output_dir=tmp_path / "b"))


# ---------------------------------------------------------------------------
# 4. Offline replay through the CLI
# ---------------------------------------------------------------------------


def test_validate_and_run_replay_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: list[dict[str, Any]]
) -> None:
    _block_network(monkeypatch)
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    assert main.main(["validate", "--bundle", str(bundle_dir)]) == 0
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(bundle_dir), "--profile", str(SMOKE_PROFILE)]
    assert main.main([*argv, "--results-dir", str(results)]) == 0
    (run_dir,) = results.iterdir()
    records = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    replayed = load_bundle(bundle_dir)
    received = {
        r["case_id"]: int(r["received"]) if r["executed"] else 0
        for r in evidence
        if r["kind"] == "swap"
    }
    assert len(records) == len(replayed.cases) == len(received)
    for rec in records:
        if rec["case_id"] in DUST_CASES:
            assert rec["status"] == "no_route"
            continue
        assert rec["status"] == "ok"
        assert int(rec["evaluation"]["gross_output"]) == received[rec["case_id"]]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["bundle_hash"] == replayed.bundle_hash
