"""Unit tests for `snapshot.abi`: every hardcoded selector is cross-checked against
an independently computed Keccak-256 (not just trusted as typed), plus the
encode/decode helpers used throughout the preflight.
"""

from __future__ import annotations

import pytest

from snapshot import abi

SIGNATURES = {
    abi.SEL_FACTORY: "factory()",
    abi.SEL_TOKEN0: "token0()",
    abi.SEL_TOKEN1: "token1()",
    abi.SEL_FEE: "fee()",
    abi.SEL_TICK_SPACING: "tickSpacing()",
    abi.SEL_LIQUIDITY: "liquidity()",
    abi.SEL_SLOT0: "slot0()",
    abi.SEL_TICK_BITMAP: "tickBitmap(int16)",
    abi.SEL_FEE_AMOUNT_TICK_SPACING: "feeAmountTickSpacing(uint24)",
    abi.SEL_GET_POOL: "getPool(address,address,uint24)",
    abi.SEL_POOL_DEPLOYER: "poolDeployer()",
    abi.SEL_OWNER: "owner()",
    abi.SEL_GET_RESERVES: "getReserves()",
    abi.SEL_GET_PAIR: "getPair(address,address)",
    abi.SEL_ALL_PAIRS_LENGTH: "allPairsLength()",
    abi.SEL_GET_TOKEN_X: "getTokenX()",
    abi.SEL_GET_TOKEN_Y: "getTokenY()",
    abi.SEL_GET_ACTIVE_ID: "getActiveId()",
    abi.SEL_GET_BIN_STEP: "getBinStep()",
    abi.SEL_GET_FACTORY: "getFactory()",
    abi.SEL_GET_NUMBER_OF_LB_PAIRS: "getNumberOfLBPairs()",
    abi.SEL_GET_LB_PAIR_AT_INDEX: "getLBPairAtIndex(uint256)",
    abi.SEL_SYMBOL: "symbol()",
    abi.SEL_DECIMALS: "decimals()",
}


@pytest.mark.parametrize("selector,signature", sorted(SIGNATURES.items()))
def test_selector_matches_keccak256_of_signature(selector: str, signature: str) -> None:
    computed = abi.keccak256_hex(signature.encode())[2:10]
    assert selector == computed, f"{signature}: hardcoded {selector} != computed {computed}"


def test_keccak256_hex_matches_known_test_vector() -> None:
    # Keccak-256 (original padding, not NIST SHA3-256) of the empty string.
    assert (
        abi.keccak256_hex(b"")
        == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_code_hash_none_for_empty_code() -> None:
    assert abi.code_hash(None) is None
    assert abi.code_hash("0x") is None


def test_code_hash_matches_keccak256_of_raw_bytes() -> None:
    code = "0x600160026003"
    expected = abi.keccak256_hex(bytes.fromhex(code[2:]))
    assert abi.code_hash(code) == expected


def test_pad_and_decode_address_round_trip() -> None:
    addr = "0x" + "ab" * 20
    encoded = abi.pad_address(addr)
    assert len(encoded) == 64
    assert abi.decode_address("0x" + encoded) == addr


def test_pad_address_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        abi.pad_address("0x1234")


def test_pad_uint_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        abi.pad_uint(-1)
    with pytest.raises(ValueError):
        abi.pad_uint(1 << 256)


def test_decode_int24_sign_extends() -> None:
    # -1 as a sign-extended 256-bit two's complement ABI word.
    raw = "0x" + "f" * 64
    assert abi.decode_int24(raw) == -1


def test_byte_length_tolerates_missing_prefix() -> None:
    assert abi.byte_length("0x60") == 1
    assert abi.byte_length("60") == 1
    assert abi.byte_length(None) == 0
    assert abi.byte_length("0x") == 0


def test_is_zero_address() -> None:
    assert abi.is_zero_address(None)
    assert abi.is_zero_address("0x" + "00" * 20)
    assert not abi.is_zero_address("0x" + "11" * 20)
