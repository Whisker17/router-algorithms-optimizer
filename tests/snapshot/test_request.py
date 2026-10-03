"""`snapshot.request` (WHI-1498): exact, unambiguous resolution of one exploratory request
against a frozen corpus bundle, and the derived single-case bundle that carries it."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from snapshot.bundle import load_bundle
from snapshot.request import (
    REQUEST_SCHEMA,
    RequestError,
    TokenInfo,
    derive_request_bundle,
    format_amount,
    parse_amount,
    request_case,
    resolve_token,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
SIX = TokenInfo("0xabc", "SIX", 6)


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("10000", 10_000_000_000),
        ("1.5", 1_500_000),
        ("0.000001", 1),
        ("1.500000", 1_500_000),  # extra digits that are zeros need no rounding
        ("1.5000000000", 1_500_000),
        (" 7 ", 7_000_000),
        ("123456789.123456", 123_456_789_123_456),
    ],
)
def test_parse_amount_is_exact(text: str, raw: int) -> None:
    assert parse_amount(text, SIX) == raw


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1.0000001", "rounded"),
        ("0", "positive"),
        ("0.000000", "positive"),
        ("-1", "plain positive decimal"),
        ("+1", "plain positive decimal"),
        ("1e3", "plain positive decimal"),
        ("nan", "plain positive decimal"),
        ("inf", "plain positive decimal"),
        ("1,000", "plain positive decimal"),
        (".5", "plain positive decimal"),
        ("", "plain positive decimal"),
    ],
)
def test_parse_amount_refuses_rounding_and_non_positive_or_non_finite(
    text: str, message: str
) -> None:
    with pytest.raises(RequestError, match=message):
        parse_amount(text, SIX)


def test_zero_decimal_token_takes_whole_units_only() -> None:
    whole = TokenInfo("0xdef", "WHOLE", 0)
    assert parse_amount("12", whole) == 12
    assert parse_amount("12.000", whole) == 12
    with pytest.raises(RequestError, match="rounded"):
        parse_amount("12.5", whole)


def test_format_amount_round_trips_exactly() -> None:
    for raw in (1, 10, 1_500_000, 10_000_000_000, 123_456_789_123_456):
        assert parse_amount(format_amount(raw, 6), SIX) == raw
    assert format_amount(1_500_000, 6) == "1.5"
    assert format_amount(10**18, 18) == "1"


def test_resolve_symbol_case_insensitively_and_address_exactly() -> None:
    bundle = load_bundle(FIXTURE)
    assert resolve_token(bundle, "usdc", "--token-in") == TokenInfo(USDC, "USDC", 6)
    assert resolve_token(bundle, USDC.upper().replace("0X", "0x"), "--token-in").address == USDC
    assert resolve_token(bundle, "wmnt", "--token-in").decimals == 18


def test_unknown_unsupported_and_native_tokens_fail_clearly() -> None:
    bundle = load_bundle(FIXTURE)
    with pytest.raises(RequestError, match="unknown token 'DOGE'"):
        resolve_token(bundle, "DOGE", "--token-in")
    with pytest.raises(RequestError, match="not in the frozen token universe"):
        resolve_token(bundle, "0x" + "00" * 19 + "01", "--token-in")
    with pytest.raises(RequestError, match=f"use WMNT \\({WMNT}\\) explicitly"):
        resolve_token(bundle, "MNT", "--token-in")


def test_ambiguous_symbol_requires_the_address() -> None:
    bundle = load_bundle(FIXTURE)
    assert bundle.prices is not None
    tokens = dict(bundle.prices.tokens)
    tokens[USDT0] = dataclasses.replace(tokens[USDT0], symbol="usdc")  # clashes by case
    clashing = dataclasses.replace(bundle, prices=dataclasses.replace(bundle.prices, tokens=tokens))
    with pytest.raises(RequestError, match="ambiguous.*pass the token address"):
        resolve_token(clashing, "USDC", "--token-in")
    assert resolve_token(clashing, USDT0, "--token-in").address == USDT0


def test_bundle_without_token_metadata_is_refused() -> None:
    synthetic = load_bundle(REPO / "tests" / "fixtures" / "synthetic")
    with pytest.raises(RequestError, match="no frozen token metadata"):
        resolve_token(synthetic, "TKA", "--token-in")


def test_same_token_request_is_refused() -> None:
    bundle = load_bundle(FIXTURE)
    usdc = resolve_token(bundle, "USDC", "--token-in")
    with pytest.raises(RequestError, match="same token"):
        request_case(bundle, usdc, usdc, 1)


def _digests(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def test_derived_bundle_preserves_state_names_its_parent_and_is_deterministic(
    tmp_path: Path,
) -> None:
    before = _digests(FIXTURE)
    parent = load_bundle(FIXTURE)
    tin = resolve_token(parent, "USDC", "--token-in")
    tout = resolve_token(parent, "USDT0", "--token-out")
    case = request_case(parent, tin, tout, parse_amount("20000", tin))
    first = derive_request_bundle(parent, case, tmp_path / "a")
    second = derive_request_bundle(parent, case, tmp_path / "b")

    assert first.bundle_hash == second.bundle_hash != parent.bundle_hash
    assert first.bundle_id != parent.bundle_id and "exploratory" in first.bundle_id
    assert first.block == parent.block and first.pools == parent.pools
    assert first.cases == (case,) and first.corpus is None and first.prices is None
    assert _digests(FIXTURE) == before  # the parent is only read
    with pytest.raises(Exception, match="refusing to overwrite"):
        derive_request_bundle(parent, case, tmp_path / "a")

    import json

    provenance = json.loads((tmp_path / "a" / "provenance.json").read_text())
    assert provenance["schema"] == REQUEST_SCHEMA and provenance["exploratory"] is True
    assert provenance["derived_from"]["bundle_hash"] == parent.bundle_hash
    assert (
        provenance["derived_from"]["checksums"]
        == json.loads((FIXTURE / "manifest.json").read_text())["checksums"]
    )
    assert provenance["request"]["amount_in_raw"] == "20000000000"
    # 20000 USDC is beyond the fixture corpus envelope: flagged, not refused.
    assert provenance["envelope"]["within"] is False
    assert set(provenance["tokens"]) == set(parent.prices.tokens)  # type: ignore[union-attr]
