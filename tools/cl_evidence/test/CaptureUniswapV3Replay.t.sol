// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.26;

// Fixed-block Uniswap v3 replay evidence for WHI-1431 (`tests/snapshot/test_uniswap_v3.py`).
// Same capture as CaptureFusionXReplay.t.sol (WHI-1430) against the deployed
// UniswapV3Pool bytecode (Uniswap/v3-core v1.0.0 on Mantle), with Uniswap's own deployed
// QuoterV2 as a second, independent on-chain quote path. Uniswap v3 pools have no LM
// hook and a uint8 `feeProtocol`.
//
// Runs inside `forge test` on a Mantle mainnet fork pinned at the *bundle's* block
// (see replay_uniswap_v3.sh). For every (reference case x pool of its pair) request in
// the input file it records, from the deployed pool bytecode only:
//   - the pool's pre-state scalars, every tickBitmap word of the bundle's collected
//     range and every initialized tick in it (an independent storage read path for the
//     collector's whole state);
//   - Uniswap QuoterV2.quoteExactInputSingle for the same input (priceLimit 0), or
//     `quoter_reverted` (QuoterV2 refuses a swap that moves no token at all:
//     "swaps entirely within 0-liquidity regions are not supported");
//   - the exact-input swap's (amount0, amount1) at the widest price limit -- a full
//     fill or, where the pool runs out of liquidity, the partial fill the pool really
//     executes;
//   - post-state scalars and every tick whose storage the swap changed (the crossed
//     ticks' fee-growth-outside flips);
//   - a follow-up reverse swap of half the output from that post-state (when the output
//     is non-zero), so the next state is checked by a second swap through the same pool.
// Nothing here imports or reimplements the Python under test. replay_uniswap_v3.sh gzips
// the output to tests/fixtures/uniswap_v3/evidence.jsonl.gz. Integers are decimal
// strings; the input is produced by tools/cl_evidence/make_replay_requests.py.
// To keep the checked-in evidence small, every initialized tick of the collected range
// is written once per pool (`pool_tick`), and each swap writes only the ticks whose
// storage it changed (`post_tick`). Crossed-tick discovery walks tickBitmap word by word
// (a partial fill can traverse the whole int24 tick range).

interface Vm {
    function store(address, bytes32, bytes32) external;
    function load(address, bytes32) external view returns (bytes32);
    function record() external;
    function accesses(address) external returns (bytes32[] memory reads, bytes32[] memory writes);
    function writeFile(string calldata, string calldata) external;
    function writeLine(string calldata, string calldata) external;
    function readFile(string calldata) external view returns (string memory);
    function parseJsonUint(string calldata, string calldata) external pure returns (uint256);
    function parseJsonString(string calldata, string calldata) external pure returns (string memory);
    function parseJsonAddressArray(string calldata, string calldata) external pure returns (address[] memory);
    function parseJsonUintArray(string calldata, string calldata) external pure returns (uint256[] memory);
    function parseJsonIntArray(string calldata, string calldata) external pure returns (int256[] memory);
    function parseJsonBoolArray(string calldata, string calldata) external pure returns (bool[] memory);
    function parseJsonStringArray(string calldata, string calldata) external pure returns (string[] memory);
    function toString(uint256) external pure returns (string memory);
    function toString(int256) external pure returns (string memory);
    function toString(address) external pure returns (string memory);
    function toString(bytes32) external pure returns (string memory);
    function snapshotState() external returns (uint256);
    function revertToState(uint256) external returns (bool);
    function envOr(string calldata, string calldata) external view returns (string memory);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
}

interface IQuoterV2 {
    struct QuoteExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint24 fee;
        uint160 sqrtPriceLimitX96;
    }

    function quoteExactInputSingle(QuoteExactInputSingleParams memory)
        external
        returns (uint256, uint160, uint32, uint256);
}

interface IUniswapV3Pool {
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool);
    function liquidity() external view returns (uint128);
    function feeGrowthGlobal0X128() external view returns (uint256);
    function feeGrowthGlobal1X128() external view returns (uint256);
    function protocolFees() external view returns (uint128, uint128);
    function tickSpacing() external view returns (int24);
    function fee() external view returns (uint24);
    function token0() external view returns (address);
    function token1() external view returns (address);
    function tickBitmap(int16) external view returns (uint256);
    function ticks(int24)
        external
        view
        returns (uint128, int128, uint256, uint256, int56, uint160, uint32, bool);
    function swap(address, bool, int256, uint160, bytes calldata) external returns (int256, int256);
}

contract CaptureUniswapV3Replay {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;
    string internal constant IN_PATH = "requests/uniswap_v3_replay.json";
    string internal constant OUT_PATH = "../../tests/fixtures/uniswap_v3/evidence.jsonl";
    // config/protocols.yaml uniswap_v3.contracts.quoter_v2 (checked by the Python test).
    address internal constant QUOTER = 0xdD489C75be1039ec7d843A6aC2Fd658350B067Cf;

    IUniswapV3Pool internal pool;

    function test_uniswap_v3_replay() external {
        string memory json = vm.readFile(IN_PATH);
        require(vm.parseJsonUint(json, ".chain_id") == block.chainid, "fork chain id != request chain id");
        require(vm.parseJsonUint(json, ".block_number") == block.number, "fork block != bundle block");
        vm.writeFile(OUT_PATH, "");
        _line(string.concat(
            '{"kind":"meta","schema":"uniswap-v3-replay-evidence/1"',
            ',"generator":"tools/cl_evidence/test/CaptureUniswapV3Replay.t.sol (forge test on a Mantle fork)"',
            ',"quoter":"', vm.toString(QUOTER), '"',
            ',"bundle_id":"', vm.parseJsonString(json, ".bundle_id"),
            '","chain_id":', vm.toString(block.chainid),
            ',"block_number":', vm.toString(block.number),
            ',"block_timestamp":', vm.toString(block.timestamp),
            ',"block_hash":"', vm.envOr("CL_FORK_BLOCK_HASH", string("")), '"}'
        ));

        address[] memory pools = vm.parseJsonAddressArray(json, ".pools");
        int256[] memory wordLo = vm.parseJsonIntArray(json, ".word_lo");
        int256[] memory wordHi = vm.parseJsonIntArray(json, ".word_hi");
        for (uint256 i = 0; i < pools.length; i++) {
            pool = IUniswapV3Pool(pools[i]);
            _deal(pool.token0(), address(this), 1e40);
            _deal(pool.token1(), address(this), 1e40);
            _writePoolState(int16(wordLo[i]), int16(wordHi[i]));
        }

        string[] memory caseIds = vm.parseJsonStringArray(json, ".case_ids");
        address[] memory reqPools = vm.parseJsonAddressArray(json, ".request_pools");
        bool[] memory zfo = vm.parseJsonBoolArray(json, ".zero_for_one");
        uint256[] memory amounts = vm.parseJsonUintArray(json, ".amounts");
        for (uint256 i = 0; i < caseIds.length; i++) {
            pool = IUniswapV3Pool(reqPools[i]);
            uint256 snap = vm.snapshotState();
            _request(caseIds[i], zfo[i], amounts[i]);
            vm.revertToState(snap);
        }
        _line('{"kind":"end"}');
    }

    function _request(string memory caseId, bool zeroForOne, uint256 amount) internal {
        uint160 limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        string memory head = string.concat(
            '"case_id":"', caseId, '","pool":"', vm.toString(address(pool)), '"'
        );
        (, int24 tickBefore,,,,,) = pool.slot0();
        // Uniswap's own QuoterV2 (reverts internally; state is untouched afterwards).
        (address tIn, address tOut) = zeroForOne ? (pool.token0(), pool.token1()) : (pool.token1(), pool.token0());
        string memory quote;
        try IQuoterV2(QUOTER).quoteExactInputSingle(
            IQuoterV2.QuoteExactInputSingleParams(tIn, tOut, amount, pool.fee(), 0)
        ) returns (uint256 qOut, uint160 qPrice, uint32 qTicks, uint256) {
            quote = string.concat(
                '"quoter_reverted":false,"quoter_amount_out":', _u(qOut),
                ',"quoter_sqrt_price_x96_after":', _u(qPrice),
                ',"quoter_initialized_ticks_crossed":', _u(qTicks)
            );
        } catch {
            quote = '"quoter_reverted":true';
        }
        // Probe the swap once (reverted) to learn the traversed interval, then digest
        // every initialized tick in it before executing the swap for real.
        uint256 probe = vm.snapshotState();
        pool.swap(address(this), zeroForOne, int256(amount), limit, "");
        (, int24 tickProbe,,,,,) = pool.slot0();
        vm.revertToState(probe);
        (int24[] memory ticks, bytes32[] memory before) = _digestsInRange(tickBefore, tickProbe);

        (int256 a0, int256 a1) = pool.swap(address(this), zeroForOne, int256(amount), limit, "");
        (, int24 tickAfter,,,,,) = pool.slot0();
        require(tickAfter == tickProbe, "swap is not deterministic");
        _line(string.concat(
            '{"kind":"swap",', head,
            ',"zero_for_one":', zeroForOne ? "true" : "false",
            ',"amount_specified":', _u(amount),
            ',"amount0":', _i(a0), ',"amount1":', _i(a1),
            ',"traversed_initialized_ticks":', vm.toString(ticks.length),
            ',', quote,
            ',', _scalars(), "}"
        ));
        // Only ticks whose storage changed (the crossed ones: fee-growth-outside flips).
        for (uint256 n = 0; n < ticks.length; n++) {
            if (_digest(ticks[n]) == before[n]) continue;
            _line(string.concat('{"kind":"post_tick",', head, ",", _tickFields(ticks[n]), "}"));
        }

        // Follow-up: reverse swap of half the output through the same (updated) pool.
        uint256 back = uint256(-(zeroForOne ? a1 : a0)) / 2;
        if (back > 0) {
            (int256 b0, int256 b1) = pool.swap(
                address(this), !zeroForOne, int256(back), zeroForOne ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1, ""
            );
            _line(string.concat(
                '{"kind":"followup",', head,
                ',"zero_for_one":', zeroForOne ? "false" : "true",
                ',"amount_specified":', _u(back),
                ',"amount0":', _i(b0), ',"amount1":', _i(b1),
                ',', _scalars(), "}"
            ));
        }
    }

    /// Every initialized tick in (min(a,b), max(a,b)] -- the only ticks a swap moving the
    /// current tick between `a` and `b` can cross -- with a digest of its storage.
    /// Walks tickBitmap word by word, so a whole-range partial fill costs ~7k word reads
    /// at worst (tickSpacing 1), not one read per compressed tick.
    function _digestsInRange(int24 a, int24 b) internal view returns (int24[] memory ticks, bytes32[] memory digests) {
        int24 lo = a < b ? a : b;
        int24 hi = a < b ? b : a;
        int24 spacing = pool.tickSpacing();
        int256 cLo = int256(_floorDiv(lo, spacing));
        int256 cHi = int256(_floorDiv(hi, spacing));
        uint256 count;
        int24[] memory buf = new int24[](512);
        for (int256 wp = cLo >> 8; wp <= cHi >> 8; wp++) {
            uint256 word = pool.tickBitmap(int16(wp));
            if (word == 0) continue;
            for (uint256 bit = 0; bit < 256; bit++) {
                if (word & (uint256(1) << bit) == 0) continue;
                int256 c = wp * 256 + int256(bit);
                if (c < cLo || c > cHi) continue;
                int24 t = int24(c * spacing);
                if (t <= lo || t > hi) continue;
                require(count < buf.length, "too many initialized ticks in range");
                buf[count++] = t;
            }
        }
        ticks = new int24[](count);
        digests = new bytes32[](count);
        for (uint256 n = 0; n < count; n++) {
            ticks[n] = buf[n];
            digests[n] = _digest(buf[n]);
        }
    }

    function _digest(int24 t) internal view returns (bytes32) {
        (uint128 gross, int128 net, uint256 fgo0, uint256 fgo1,,,, bool init) = pool.ticks(t);
        return keccak256(abi.encode(gross, net, fgo0, fgo1, init));
    }

    function _writePoolState(int16 wLo, int16 wHi) internal {
        _line(string.concat(
            '{"kind":"pool_state","pool":"', vm.toString(address(pool)),
            '","code_hash":"', vm.toString(address(pool).codehash),
            '","token0":"', vm.toString(pool.token0()),
            '","token1":"', vm.toString(pool.token1()),
            '","fee":', vm.toString(uint256(pool.fee())),
            ',"tick_spacing":', vm.toString(int256(pool.tickSpacing())),
            ',', _scalars(),
            ',"bitmap_word_lo":', vm.toString(int256(wLo)),
            ',"bitmap_word_hi":', vm.toString(int256(wHi)), "}"
        ));
        int24 spacing = pool.tickSpacing();
        for (int256 wp = wLo; wp <= wHi; wp++) {
            uint256 word = pool.tickBitmap(int16(wp));
            _line(string.concat(
                '{"kind":"pool_word","pool":"', vm.toString(address(pool)),
                '","word":', vm.toString(wp), ',"bitmap":', _u(word), "}"
            ));
            for (uint256 bit = 0; bit < 256; bit++) {
                if (word & (uint256(1) << bit) == 0) continue;
                int24 t = int24((wp * 256 + int256(bit)) * spacing);
                _line(string.concat(
                    '{"kind":"pool_tick","pool":"', vm.toString(address(pool)), '",', _tickFields(t), "}"
                ));
            }
        }
    }

    function _scalars() internal view returns (string memory) {
        (uint160 sqrtP, int24 tick,,,, uint8 feeProtocol, bool unlocked) = pool.slot0();
        (uint128 pf0, uint128 pf1) = pool.protocolFees();
        return string.concat(
            '"sqrt_price_x96":', _u(sqrtP),
            ',"tick":', _i(tick),
            ',"fee_protocol":', _u(feeProtocol),
            ',"unlocked":', unlocked ? "true" : "false",
            ',"liquidity":', _u(pool.liquidity()),
            ',"fee_growth_global0_x128":', _u(pool.feeGrowthGlobal0X128()),
            ',"fee_growth_global1_x128":', _u(pool.feeGrowthGlobal1X128()),
            ',"protocol_fees0":', _u(pf0),
            ',"protocol_fees1":', _u(pf1)
        );
    }

    function _tickFields(int24 t) internal view returns (string memory) {
        (uint128 gross, int128 net, uint256 fgo0, uint256 fgo1,,,, bool init) = pool.ticks(t);
        return string.concat(
            '"tick":', _i(t),
            ',"liquidity_gross":', _u(gross),
            ',"liquidity_net":', _i(net),
            ',"fee_growth_outside0_x128":', _u(fgo0),
            ',"fee_growth_outside1_x128":', _u(fgo1),
            ',"initialized":', init ? "true" : "false"
        );
    }

    // ---- callbacks: pay whatever the pool asks for ----

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        if (a0 > 0) IERC20(IUniswapV3Pool(msg.sender).token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20(IUniswapV3Pool(msg.sender).token1()).transfer(msg.sender, uint256(a1));
    }

    // ---- misc (same helpers as CaptureCL.t.sol) ----

    function _deal(address token, address who, uint256 amount) internal {
        if (IERC20(token).balanceOf(who) >= amount) return;
        vm.record();
        IERC20(token).balanceOf(who);
        (bytes32[] memory reads,) = vm.accesses(token);
        for (uint256 n = reads.length; n > 0; n--) {
            bytes32 slot = reads[n - 1];
            bytes32 prev = vm.load(token, slot);
            vm.store(token, slot, bytes32(amount));
            try IERC20(token).balanceOf(who) returns (uint256 b) {
                if (b == amount) return;
            } catch {}
            vm.store(token, slot, prev);
        }
        revert("deal: balance slot not found");
    }

    function _floorDiv(int24 a, int24 b) internal pure returns (int24) {
        int24 q = a / b;
        if (a % b != 0 && ((a < 0) != (b < 0))) q--;
        return q;
    }

    function _line(string memory s) internal {
        vm.writeLine(OUT_PATH, s);
    }

    function _u(uint256 x) internal pure returns (string memory) {
        return string.concat('"', vm.toString(x), '"');
    }

    function _i(int256 x) internal pure returns (string memory) {
        return string.concat('"', vm.toString(x), '"');
    }
}
