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

from Crypto.Hash import keccak

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
# Uniswap-v3-family pool state used by the fixed-block CL collector (WHI-1429)
SEL_TICKS = "f30dba93"  # ticks(int24)
SEL_FEE_GROWTH_GLOBAL0_X128 = "f3058399"  # feeGrowthGlobal0X128()
SEL_FEE_GROWTH_GLOBAL1_X128 = "46141319"  # feeGrowthGlobal1X128()
SEL_PROTOCOL_FEES = "1ad8b03b"  # protocolFees()
SEL_MAX_LIQUIDITY_PER_TICK = "70cf754a"  # maxLiquidityPerTick()
SEL_LM_POOL = "540d4918"  # lmPool()  (PancakeSwap-v3 family: Agni, FusionX)
SEL_POOL = "16f0115b"  # pool()  (an LM pool's back-reference to its CL pool)

# Uniswap-v2-family / Merchant Moe Classic v1
SEL_GET_RESERVES = "0902f1ac"  # getReserves()
SEL_GET_PAIR = "e6a43905"  # getPair(address,address)
SEL_ALL_PAIRS_LENGTH = "574f2ba3"  # allPairsLength()
SEL_MOE_PAIR_IMPLEMENTATION = "73f9936d"  # moePairImplementation()  (MoeFactory)
SEL_IMPLEMENTATION = "5c60da1b"  # implementation()  (MoePair immutable)

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
SEL_BALANCE_OF = "70a08231"  # balanceOf(address)


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


def pad_int24(value: int) -> str:
    """ABI-encode an `int24` argument (sign-extended 256-bit two's complement)."""
    if not (-(1 << 23) <= value < (1 << 23)):
        raise ValueError(f"int24 out of range: {value}")
    return format(value & ((1 << 256) - 1), "064x")


def decode_words(result_hex: str | None) -> list[int]:
    """Split a static-tuple return value into its 32-byte words (unsigned). Raises
    `ValueError` on an empty or non-word-aligned result: a caller decoding a known
    static tuple must never silently read a short answer as zeros."""
    if not result_hex or result_hex == "0x":
        raise ValueError("empty eth_call result")
    body = result_hex.removeprefix("0x")
    if len(body) % 64 != 0:
        raise ValueError(f"eth_call result is not 32-byte aligned ({len(body)} hex chars)")
    return [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]


def to_signed(word: int, bits: int) -> int:
    """Interpret an ABI word as a sign-extended `int<bits>`; raises if the word is
    not a valid sign extension (i.e. the value does not fit the declared width)."""
    value = word - (1 << 256) if word >= 1 << 255 else word
    if not (-(1 << (bits - 1)) <= value < (1 << (bits - 1))):
        raise ValueError(f"word {word:#x} is not a valid int{bits}")
    return value


def to_unsigned(word: int, bits: int) -> int:
    """Check an ABI word fits `uint<bits>` (ABI zero-pads; a dirty high part means
    the decoder is reading the wrong type)."""
    if word >= 1 << bits:
        raise ValueError(f"word {word:#x} does not fit uint{bits}")
    return word


def word_to_address(word: int) -> str:
    """A 32-byte ABI word holding an `address`, as a lowercase `0x` string."""
    if word >= 1 << 160:
        raise ValueError(f"word {word:#x} is not an address")
    return "0x" + format(word, "040x")


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
    """Decode a signed integer (e.g. `int24` tickSpacing/tick) ABI-encoded as a
    sign-extended 256-bit word -- the general rule for any signed fixed-size
    Solidity integer, not specific to 24 bits."""
    value = decode_uint(result_hex)
    if value is None:
        return None
    if value >= 1 << 255:
        value -= 1 << 256
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


def keccak256_hex(data: bytes) -> str:
    """`0x`-prefixed Keccak-256 (the EVM's own hash/CODEHASH function -- *not*
    NIST SHA3-256, which uses different padding). Used to fingerprint runtime
    bytecode so the preflight can detect any drift at a configured address
    without re-deriving every identity call."""
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return "0x" + digest.hexdigest()


def code_hash(result_hex: str | None) -> str | None:
    """Keccak-256 of an `eth_getCode` result's raw bytes, or `None` for empty code
    (mirrors the EVM convention that an account with no code has no CODEHASH)."""
    if not result_hex or result_hex == "0x":
        return None
    return keccak256_hex(bytes.fromhex(result_hex.removeprefix("0x")))


def push32_immediates(code: bytes) -> list[tuple[int, int]]:
    """`(offset, value)` of every `PUSH32` immediate in EVM runtime code, found by a
    proper opcode walk (PUSH1..PUSH32 immediates are skipped, so data bytes are never
    misread as opcodes). Solidity 0.7.x embeds every `immutable` as a `PUSH32`."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(code):
        op = code[i]
        if 0x60 <= op <= 0x7F:
            width = op - 0x5F
            if width == 32:
                out.append((i + 1, int.from_bytes(code[i + 1 : i + 33], "big")))
            i += 1 + width
        else:
            i += 1
    return out


def immutable_normalized_code_hash(
    code: bytes, immutables: dict[str, int]
) -> tuple[str, dict[str, int]]:
    """Keccak-256 of runtime `code` after zeroing every `PUSH32` immediate equal to one
    of the contract's own immutable values (`immutables`, e.g. a pool's
    factory/token0/token1/fee/tickSpacing/maxLiquidityPerTick read from its getters).

    Every pool a factory deploys runs the same creation code but embeds its own
    immutables, so its exact code hash is pool-specific; the normalized hash is the
    same for all of them and differs for any other code. Returns the hash and the
    number of zeroed sites per immutable name (a name with no site means the value is
    not embedded where expected, and the normalized hash will not match the pin).
    Fail-closed by construction: a coincidental match changes the normalized bytes
    and can only cause a rejection, never an acceptance of different code."""
    normalized = bytearray(code)
    hits = {name: 0 for name in immutables}
    for offset, value in push32_immediates(code):
        for name, expected in immutables.items():
            if value == expected:
                normalized[offset : offset + 32] = bytes(32)
                hits[name] += 1
                break
    return keccak256_hex(bytes(normalized)), hits
