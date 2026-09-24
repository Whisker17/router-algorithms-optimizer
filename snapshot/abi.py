"""Minimal, dependency-free ABI helpers for the fixed-block preflight.

Only the handful of read-only function selectors the preflight needs are supported.
Selectors are the standard `keccak256(signature)[:4]` values published by every EVM
ABI/verification tool; they are hardcoded here (rather than computed at runtime) so
this module needs no keccak/crypto dependency. Each constant is annotated with the
human-readable signature it was derived from so it stays independently checkable
(e.g. against https://www.4byte.directory or `cast sig <signature>`).

This is intentionally not a general ABI encoder/decoder: it only understands the
fixed-size types (`address`, `uintN`, `intN`, `bool`) needed by the contracts in
`config/protocols.yaml`. Dynamic types (e.g. `string`) are not needed by the
preflight and are not implemented.
"""

from __future__ import annotations

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# --- Selectors (4-byte function selectors = keccak256(signature)[:4]) -------------
# Uniswap-v3-family (Agni v3, FusionX v3, Uniswap v3)
SEL_FACTORY = "c45a0155"  # factory()
SEL_TOKEN0 = "0dfe1681"  # token0()
SEL_TOKEN1 = "d21220a7"  # token1()
SEL_FEE = "ddca3f43"  # fee()
SEL_TICK_SPACING = "d0c93a7c"  # tickSpacing()
SEL_LIQUIDITY = "1a686502"  # liquidity()
SEL_SLOT0 = "3850c7bd"  # slot0()
SEL_TICK_BITMAP = "5339c296"  # tickBitmap(int16)
SEL_FEE_AMOUNT_TICK_SPACING = "22afcccb"  # feeAmountTickSpacing(uint24)
SEL_GET_POOL = "1698ee82"  # getPool(address,address,uint24)
SEL_POOL_DEPLOYER = "3119049a"  # poolDeployer()
SEL_OWNER = "8da5cb5b"  # owner()

# Uniswap-v2-family / Merchant Moe Classic v1
SEL_GET_RESERVES = "0902f1ac"  # getReserves()
SEL_GET_PAIR = "e6a43905"  # getPair(address,address)
SEL_ALL_PAIRS_LENGTH = "574f2ba3"  # allPairsLength()

# Merchant Moe Liquidity Book v2.2 (LFJ joe-v2)
SEL_GET_TOKEN_X = "05e8746d"  # getTokenX()
SEL_GET_TOKEN_Y = "da10610c"  # getTokenY()
SEL_GET_ACTIVE_ID = "dbe65edc"  # getActiveId()
SEL_GET_BIN_STEP = "17f11ecc"  # getBinStep()
SEL_GET_FACTORY = "88cc58e4"  # getFactory()
SEL_GET_NUMBER_OF_LB_PAIRS = "4e937c3a"  # getNumberOfLBPairs()
SEL_GET_LB_PAIR_AT_INDEX = "7daf5d66"  # getLBPairAtIndex(uint256)

# ERC-20 (best-effort; only used for informational token identification)
SEL_SYMBOL = "95d89b41"  # symbol()
SEL_DECIMALS = "313ce567"  # decimals()


def encode_call(selector_hex: str, *args_hex32: str) -> str:
    """Build `data` for eth_call from a 4-byte selector and pre-padded 32-byte args."""
    return "0x" + selector_hex + "".join(args_hex32)


def pad_address(addr: str) -> str:
    a = addr.lower().removeprefix("0x")
    if len(a) != 40:
        raise ValueError(f"not a 20-byte address: {addr!r}")
    return a.rjust(64, "0")


def pad_uint(value: int, bits: int = 256) -> str:
    if value < 0 or value >= (1 << bits):
        raise ValueError(f"uint{bits} out of range: {value}")
    return format(value, "064x")


def pad_int16(value: int) -> str:
    if not (-(1 << 15) <= value < (1 << 15)):
        raise ValueError(f"int16 out of range: {value}")
    return format(value & ((1 << 256) - 1), "064x")


def decode_address(result_hex: str | None) -> str | None:
    """Decode a single `address` return value. `None`/empty input means no data."""
    if not result_hex or result_hex == "0x":
        return None
    body = result_hex.removeprefix("0x")
    if len(body) < 64:
        return None
    return "0x" + body[-40:]


def decode_uint(result_hex: str | None) -> int | None:
    """Decode a single `uintN`/`intN` (unsigned interpretation) return value."""
    if not result_hex or result_hex == "0x":
        return None
    body = result_hex.removeprefix("0x")
    if len(body) < 64:
        return None
    return int(body[:64], 16)


def decode_int24(result_hex: str | None) -> int | None:
    """Decode a single `int24` return value (e.g. tickSpacing, tick), sign-extended."""
    value = decode_uint(result_hex)
    if value is None:
        return None
    if value >= 1 << 23:
        value -= 1 << 24
    return value


def is_zero_address(addr: str | None) -> bool:
    return addr is None or addr.lower() == ZERO_ADDRESS


def byte_length(result_hex: str | None) -> int:
    """Number of bytes in an eth_call/eth_getCode result, tolerating either a
    `0x`-prefixed or bare hex string (empty/`None` -> 0)."""
    if not result_hex:
        return 0
    body = result_hex.removeprefix("0x")
    return len(body) // 2
