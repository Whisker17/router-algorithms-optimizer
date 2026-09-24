// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity 0.8.26;

// Independent evidence generator for pools/concentrated.py (WHI-1428).
//
// Runs inside `forge test` against a Mantle mainnet fork pinned at the catalog's
// candidate block (see README.md / regen.sh). Every expected value it records is
// produced by the *deployed* pool bytecode executing in the EVM (real pools) or by
// pools freshly deployed through the *deployed* verified factories on that fork
// (controlled pools). Nothing here imports or reimplements the Python under test.
// The only math copied into this file is `TickMath.getSqrtRatioAtTick`, used solely
// to choose `sqrtPriceLimitX96` inputs for the pool; the pool itself decides every
// recorded output.
//
// Output: one JSON-Lines file per (source, scenario) under
// tests/fixtures/concentrated/. Integers are decimal strings.

interface Vm {
    function prank(address) external;
    function store(address, bytes32, bytes32) external;
    function load(address, bytes32) external view returns (bytes32);
    function record() external;
    function accesses(address) external returns (bytes32[] memory reads, bytes32[] memory writes);
    function writeFile(string calldata, string calldata) external;
    function writeLine(string calldata, string calldata) external;
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

interface IPool {
    // feeProtocol is uint8 on Uniswap v3 and uint32 on Agni/FusionX; both ABI-decode
    // into uint32 losslessly.
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint32, bool);
    function liquidity() external view returns (uint128);
    function feeGrowthGlobal0X128() external view returns (uint256);
    function feeGrowthGlobal1X128() external view returns (uint256);
    function protocolFees() external view returns (uint128, uint128);
    function tickSpacing() external view returns (int24);
    function fee() external view returns (uint24);
    function token0() external view returns (address);
    function token1() external view returns (address);
    function factory() external view returns (address);
    function maxLiquidityPerTick() external view returns (uint128);
    function tickBitmap(int16) external view returns (uint256);
    function ticks(int24)
        external
        view
        returns (uint128, int128, uint256, uint256, int56, uint160, uint32, bool);
    function swap(address, bool, int256, uint160, bytes calldata) external returns (int256, int256);
    function mint(address, int24, int24, uint128, bytes calldata) external returns (uint256, uint256);
    function initialize(uint160) external;
}

interface IPancakeFamilyPool {
    function lmPool() external view returns (address);
    function setFeeProtocol(uint32, uint32) external;
}

interface IUniswapPool {
    function setFeeProtocol(uint8, uint8) external;
}

interface IFactory {
    function createPool(address, address, uint24) external returns (address);
    function owner() external view returns (address);
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

contract MockToken {
    mapping(address => uint256) public balanceOf;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

// Copy of Uniswap/v3-core v1.0.0 TickMath.getSqrtRatioAtTick (0.8 `unchecked`
// port), used only to construct price-limit *inputs* for the pools.
library LimitTickMath {
    int24 internal constant MAX_TICK = 887272;

    function getSqrtRatioAtTick(int24 tick) internal pure returns (uint160 sqrtPriceX96) {
        unchecked {
            uint256 absTick = tick < 0 ? uint256(-int256(tick)) : uint256(int256(tick));
            require(absTick <= uint256(int256(MAX_TICK)), "T");
            uint256 ratio = absTick & 0x1 != 0
                ? 0xfffcb933bd6fad37aa2d162d1a594001
                : 0x100000000000000000000000000000000;
            if (absTick & 0x2 != 0) ratio = (ratio * 0xfff97272373d413259a46990580e213a) >> 128;
            if (absTick & 0x4 != 0) ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdcc) >> 128;
            if (absTick & 0x8 != 0) ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0) >> 128;
            if (absTick & 0x10 != 0) ratio = (ratio * 0xffcb9843d60f6159c9db58835c926644) >> 128;
            if (absTick & 0x20 != 0) ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0) >> 128;
            if (absTick & 0x40 != 0) ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861) >> 128;
            if (absTick & 0x80 != 0) ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053) >> 128;
            if (absTick & 0x100 != 0) ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4) >> 128;
            if (absTick & 0x200 != 0) ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54) >> 128;
            if (absTick & 0x400 != 0) ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3) >> 128;
            if (absTick & 0x800 != 0) ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9) >> 128;
            if (absTick & 0x1000 != 0) ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825) >> 128;
            if (absTick & 0x2000 != 0) ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5) >> 128;
            if (absTick & 0x4000 != 0) ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7) >> 128;
            if (absTick & 0x8000 != 0) ratio = (ratio * 0x31be135f97d08fd981231505542fcfa6) >> 128;
            if (absTick & 0x10000 != 0) ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9) >> 128;
            if (absTick & 0x20000 != 0) ratio = (ratio * 0x5d6af8dedb81196699c329225ee604) >> 128;
            if (absTick & 0x40000 != 0) ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98) >> 128;
            if (absTick & 0x80000 != 0) ratio = (ratio * 0x48a170391f7dc42444e8fa2) >> 128;
            if (tick > 0) ratio = type(uint256).max / ratio;
            sqrtPriceX96 = uint160((ratio >> 32) + (ratio % (1 << 32) == 0 ? 0 : 1));
        }
    }
}

contract CaptureCL {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;
    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK = 887272;
    string internal constant DIR = "../../tests/fixtures/concentrated/";

    // Mantle mainnet deployments (config/protocols.yaml, WHI-1426).
    address internal constant UNI_FACTORY = 0x0d922Fb1Bc191F64970ac40376643808b4B74Df9;
    address internal constant UNI_POOL = 0x4cdFc22bF05209de87Ee564746Dc7E5174631d2b;
    address internal constant UNI_QUOTER = 0xdD489C75be1039ec7d843A6aC2Fd658350B067Cf;
    address internal constant AGNI_FACTORY = 0x25780dc8Fc3cfBD75F33bFDAB65e969b603b2035;
    address internal constant AGNI_POOL = 0x1858d52cf57c07A018171D7a1E68DC081F17144f;
    address internal constant FX_FACTORY = 0x530d2766D1988CC1c000C8b7d00334c14B69AD71;
    address internal constant FX_POOL = 0x262255F4770aEbE2D0C8b97a46287dCeCc2a0AfF;
    address internal constant FX_QUOTER = 0x90f72244294E7c5028aFd6a96E18CC2c1E913995;

    // ---- per-run context (storage; forge runs each test in its own EVM) ----
    IPool internal pool;
    address internal quoter;
    string internal outPath;
    string internal seqName;
    uint256 internal swapIndex;
    int24[] internal tracked; // every initialized tick inside the captured word range
    mapping(int24 => bytes32) internal tickDigest; // last recorded tick state digest

    // =====================================================================
    // Entry points
    // =====================================================================

    function test_real_uniswap_v3() external {
        _runReal("uniswap_v3", UNI_POOL, UNI_QUOTER, false);
    }

    function test_real_agni_v3() external {
        _runReal("agni_v3", AGNI_POOL, address(0), true);
    }

    function test_real_fusionx_v3() external {
        _runReal("fusionx_v3", FX_POOL, FX_QUOTER, true);
    }

    function test_controlled_uniswap_v3() external {
        _runControlled("uniswap_v3", UNI_FACTORY, false);
    }

    function test_controlled_agni_v3() external {
        _runControlled("agni_v3", AGNI_FACTORY, true);
    }

    function test_controlled_fusionx_v3() external {
        _runControlled("fusionx_v3", FX_FACTORY, true);
    }

    // =====================================================================
    // Real pools at the fork block: bounded word window around the current tick
    // =====================================================================

    function _runReal(string memory source, address poolAddr, address quoterAddr, bool pancake) internal {
        pool = IPool(poolAddr);
        quoter = quoterAddr;
        outPath = string.concat(DIR, source, "_real.jsonl");
        vm.writeFile(outPath, "");

        int24 spacing = pool.tickSpacing();
        (, int24 tick0,,,,,) = pool.slot0();
        int16 w = int16(_floorDiv(tick0, spacing) >> 8);
        int16 wLo = w - 1;
        int16 wHi = w + 1;

        _deal(pool.token0(), address(this), 1e36);
        _deal(pool.token1(), address(this), 1e36);

        _writeMeta(source, "real_pool", pancake, wLo, wHi);
        _writePreState(wLo, wHi);

        int24[] memory below = _initializedBelowOrAt(tick0);
        int24[] memory above = _initializedAbove(tick0);
        int24 windowLo = int24(int256(wLo) * 256) * spacing;
        int24 windowHi = int24((int256(wHi) + 1) * 256) * spacing - 1;
        require(below.length >= 2 && above.length >= 1, "window too sparse");

        uint256 snap = vm.snapshotState();

        // R1: small swap each direction, sequentially.
        _beginSequence("small_both_directions", "small exact-in swap token0->token1 then token1->token0");
        _swapExactIn(true, _amountToReach(true, below[0]) / 3, true);
        (, int24 t1,,,,,) = pool.slot0();
        _swapExactIn(false, _amountToReach(false, _firstAbove(t1)) / 3, true);
        vm.revertToState(snap);

        // R2: multi-tick move down, then back up past the starting tick.
        _beginSequence("multi_tick_round_trip", "cross several initialized ticks down, then cross them back up");
        uint256 k = below.length < 5 ? below.length : 5;
        int24 downTarget = _clamp(below[k - 1] - 1, windowLo + spacing, windowHi);
        _swapExactIn(true, _amountToReach(true, downTarget), true);
        uint256 j = above.length < 3 ? above.length : 3;
        int24 upTarget = _clamp(above[j - 1] + 1, windowLo, windowHi - spacing);
        _swapExactIn(false, _amountToReach(false, upTarget), false);
        vm.revertToState(snap);

        // R3: land exactly on an initialized tick boundary, then continue past it.
        _beginSequence("exact_boundary_then_continue", "exact-in amount that reaches the next initialized tick below, then a follow-up swap");
        _swapExactIn(true, _amountToReach(true, below[0]), false);
        (, int24 t3,,,,,) = pool.slot0();
        _swapExactIn(true, _amountToReach(true, _clamp(_firstBelowOrAt(t3) - 1, windowLo + spacing, windowHi)) / 4 + 1, false);
        vm.revertToState(snap);

        // R4: move up across the bitmap word boundary, then swap back.
        _beginSequence("word_boundary_crossing", "token1->token0 into the next bitmap word, then token0->token1 back");
        int24 nextWordTarget = int24((int256(w) + 1) * 256 + 20) * spacing;
        (int256 a0,) = _swapExactIn(false, _amountToReach(false, nextWordTarget), false);
        _swapExactIn(true, uint256(-a0) / 2, false);
        vm.revertToState(snap);

        // R5: explicit price limit -> partial fill (pool semantics, not Exact Input full fill).
        _beginSequence("price_limit_partial_fill", "token0->token1 with a sqrtPriceLimit that stops the swap early, then token1->token0");
        int24 limitTick = _clamp(below[1] - 1, windowLo + spacing, windowHi);
        uint256 need = _amountToReach(true, limitTick);
        _swap(true, int256(need * 2), LimitTickMath.getSqrtRatioAtTick(limitTick), false);
        (, int24 t5,,,,,) = pool.slot0();
        _swapExactIn(false, _amountToReach(false, _firstAbove(t5)) / 2 + 1, false);
        vm.revertToState(snap);

        _line('{"kind":"end"}');
    }

    // =====================================================================
    // Controlled pools: deployed through the real factory on the fork, with
    // known positions and fully captured bitmap (MIN..MAX words).
    // =====================================================================

    function _runControlled(string memory source, address factory, bool pancake) internal {
        outPath = string.concat(DIR, source, "_controlled.jsonl");
        vm.writeFile(outPath, "");
        quoter = address(0);

        MockToken ta = new MockToken();
        MockToken tb = new MockToken();
        pool = IPool(IFactory(factory).createPool(address(ta), address(tb), 500));
        pool.initialize(uint160(1) << 96); // price 1.0, tick 0
        ta.mint(address(this), 1e40);
        tb.mint(address(this), 1e40);

        address owner = IFactory(factory).owner();
        vm.prank(owner);
        if (pancake) {
            IPancakeFamilyPool(address(pool)).setFeeProtocol(1000, 4000); // 10% / 40%
        } else {
            IUniswapPool(address(pool)).setFeeProtocol(4, 7); // 1/4 and 1/7
        }

        int24 spacing = pool.tickSpacing();
        int16 wLo = int16(_floorDiv(MIN_TICK, spacing) >> 8);
        int16 wHi = int16(_floorDiv(MAX_TICK, spacing) >> 8);
        _writeMeta(source, "controlled_pool", pancake, wLo, wHi);

        _mint(-600, 600, 1e21);
        _mint(-200, 100, 5e20);
        _mint(300, 900, 2e20);
        _mint(-3000, -2600, 3e20);
        _mint(2560, 2600, 4e20);

        _writePreState(wLo, wHi);

        uint256 snap = vm.snapshotState();

        _beginSequence("small_both_directions", "small exact-in swaps in the central range, both directions");
        _swapExactIn(true, 1e15, false);
        _swapExactIn(false, 3e15, false);
        vm.revertToState(snap);

        _beginSequence("multi_tick_through_gap", "down across -200/-600, a zero-liquidity gap and a word boundary into [-3000,-2600], then back up to 400");
        _swapExactIn(true, _amountToReach(true, -2800), false);
        _swapExactIn(false, _amountToReach(false, 400), false);
        vm.revertToState(snap);

        _beginSequence("exact_boundary_then_continue", "exact-in amount reaching initialized tick -200, then continue down");
        _swapExactIn(true, _amountToReach(true, -200), false);
        _swapExactIn(true, 2e17, false);
        vm.revertToState(snap);

        _beginSequence("insufficient_liquidity_zero_for_one", "token0 input larger than all liquidity below: pool stops at MIN_SQRT_RATIO+1 with a partial fill");
        _swapExactIn(true, 1e30, false);
        _swapExactIn(false, 5e19, false);
        vm.revertToState(snap);

        _beginSequence("insufficient_liquidity_one_for_zero", "token1 input larger than all liquidity above: pool stops at MAX_SQRT_RATIO-1 with a partial fill");
        _swapExactIn(false, 1e30, false);
        vm.revertToState(snap);

        _beginSequence("dust", "1-wei and 2-wei inputs: fee-only steps with zero output");
        _swapExactIn(true, 1, false);
        _swapExactIn(false, 2, false);
        vm.revertToState(snap);

        _line('{"kind":"end"}');
    }

    // =====================================================================
    // Swap / capture helpers
    // =====================================================================

    function _swapExactIn(bool zeroForOne, uint256 amount, bool withQuote) internal returns (int256, int256) {
        uint160 limit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        return _swap(zeroForOne, int256(amount), limit, withQuote);
    }

    function _swap(bool zeroForOne, int256 amountSpecified, uint160 limit, bool withQuote)
        internal
        returns (int256 amount0, int256 amount1)
    {
        require(amountSpecified > 0, "non-positive amount");
        uint256 idx = swapIndex++;
        if (withQuote && quoter != address(0)) {
            (address tIn, address tOut) = zeroForOne ? (pool.token0(), pool.token1()) : (pool.token1(), pool.token0());
            (uint256 qOut, uint160 qPrice, uint32 qTicks,) = IQuoterV2(quoter).quoteExactInputSingle(
                IQuoterV2.QuoteExactInputSingleParams(tIn, tOut, uint256(amountSpecified), pool.fee(), 0)
            );
            _line(string.concat(
                '{"kind":"quote",', _seqIdx(idx),
                ',"quoter":"', vm.toString(quoter),
                '","amount_in":', _u(uint256(amountSpecified)),
                ',"amount_out":', _u(qOut),
                ',"sqrt_price_x96_after":', _u(qPrice),
                ',"initialized_ticks_crossed":', _u(qTicks), "}"
            ));
        }
        (amount0, amount1) = pool.swap(address(this), zeroForOne, amountSpecified, limit, "");
        _line(string.concat(
            '{"kind":"swap",', _seqIdx(idx),
            ',"zero_for_one":', zeroForOne ? "true" : "false",
            ',"amount_specified":', _i(amountSpecified),
            ',"sqrt_price_limit_x96":', _u(limit),
            ',"amount0":', _i(amount0),
            ',"amount1":', _i(amount1), "}"
        ));
        _writePost(idx);
    }

    /// Input amount the pool itself consumes to move the price exactly to
    /// `targetTick` (a price-limited probe swap that is then reverted).
    function _amountToReach(bool zeroForOne, int24 targetTick) internal returns (uint256) {
        uint256 s = vm.snapshotState();
        (int256 a0, int256 a1) = pool.swap(
            address(this), zeroForOne, int256(uint256(1) << 200), LimitTickMath.getSqrtRatioAtTick(targetTick), ""
        );
        vm.revertToState(s);
        int256 consumed = zeroForOne ? a0 : a1;
        require(consumed > 0, "probe consumed nothing");
        return uint256(consumed);
    }

    function _mint(int24 lower, int24 upper, uint128 amount) internal {
        pool.mint(address(this), lower, upper, amount, "");
        _line(string.concat(
            '{"kind":"setup_position","tick_lower":', _i(lower),
            ',"tick_upper":', _i(upper),
            ',"liquidity":', _u(amount), "}"
        ));
    }

    function _beginSequence(string memory name, string memory description) internal {
        seqName = name;
        swapIndex = 0;
        _line(string.concat('{"kind":"sequence","sequence":"', name, '","description":"', description, '"}'));
    }

    function _writeMeta(string memory source, string memory scenario, bool pancake, int16 wLo, int16 wHi) internal {
        address lm = pancake ? IPancakeFamilyPool(address(pool)).lmPool() : address(0);
        string memory head = string.concat(
            '{"kind":"meta","schema":"cl-evidence/1","source_key":"', source,
            '","scenario":"', scenario,
            '","generator":"tools/cl_evidence/test/CaptureCL.t.sol (forge test on a Mantle fork)"',
            ',"chain_id":', vm.toString(block.chainid),
            ',"block_number":', vm.toString(block.number),
            ',"block_timestamp":', vm.toString(block.timestamp),
            ',"block_hash":"', vm.envOr("CL_FORK_BLOCK_HASH", string("")), '"'
        );
        string memory body = string.concat(
            ',"pool":"', vm.toString(address(pool)),
            '","pool_code_hash":"', vm.toString(address(pool).codehash),
            '","factory":"', vm.toString(pool.factory()),
            '","token0":"', vm.toString(pool.token0()),
            '","token1":"', vm.toString(pool.token1()),
            '","fee":', vm.toString(uint256(pool.fee())),
            ',"tick_spacing":', vm.toString(int256(pool.tickSpacing())),
            ',"max_liquidity_per_tick":', _u(pool.maxLiquidityPerTick()),
            ',"lm_pool":"', vm.toString(lm), '"'
        );
        _line(string.concat(
            head, body,
            ',"bitmap_word_lo":', vm.toString(int256(wLo)),
            ',"bitmap_word_hi":', vm.toString(int256(wHi)), "}"
        ));
    }

    function _writePreState(int16 wLo, int16 wHi) internal {
        _line(string.concat('{"kind":"pre_state",', _poolScalars(), "}"));
        int24 spacing = pool.tickSpacing();
        for (int256 wp = wLo; wp <= wHi; wp++) {
            uint256 word = pool.tickBitmap(int16(wp));
            if (word == 0) continue;
            _line(string.concat('{"kind":"pre_word","word":', vm.toString(wp), ',"bitmap":', _u(word), "}"));
            for (uint256 bit = 0; bit < 256; bit++) {
                if (word & (uint256(1) << bit) == 0) continue;
                int24 t = int24((wp * 256 + int256(bit)) * spacing);
                tracked.push(t);
                (string memory fields, bytes32 digest) = _tickFields(t);
                tickDigest[t] = digest;
                _line(string.concat('{"kind":"pre_tick",', fields, "}"));
            }
        }
    }

    function _writePost(uint256 idx) internal {
        _line(string.concat('{"kind":"post_state",', _seqIdx(idx), ",", _poolScalars(), "}"));
        for (uint256 n = 0; n < tracked.length; n++) {
            int24 t = tracked[n];
            (string memory fields, bytes32 digest) = _tickFields(t);
            if (digest == tickDigest[t]) continue;
            tickDigest[t] = digest;
            _line(string.concat('{"kind":"post_tick",', _seqIdx(idx), ",", fields, "}"));
        }
    }

    function _poolScalars() internal view returns (string memory) {
        (uint160 sqrtP, int24 tick,,,, uint32 feeProtocol, bool unlocked) = pool.slot0();
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

    function _tickFields(int24 t) internal view returns (string memory, bytes32) {
        (uint128 gross, int128 net, uint256 fgo0, uint256 fgo1,,,, bool init) = pool.ticks(t);
        string memory s = string.concat(
            '"tick":', _i(t),
            ',"liquidity_gross":', _u(gross),
            ',"liquidity_net":', _i(net),
            ',"fee_growth_outside0_x128":', _u(fgo0),
            ',"fee_growth_outside1_x128":', _u(fgo1),
            ',"initialized":', init ? "true" : "false"
        );
        return (s, keccak256(abi.encode(gross, net, fgo0, fgo1, init)));
    }

    // ---- initialized-tick lookups over the captured (tracked) set ----

    function _initializedBelowOrAt(int24 t) internal view returns (int24[] memory out) {
        uint256 c;
        for (uint256 n = 0; n < tracked.length; n++) if (tracked[n] <= t) c++;
        out = new int24[](c);
        uint256 m;
        for (uint256 n = tracked.length; n > 0; n--) if (tracked[n - 1] <= t) out[m++] = tracked[n - 1];
    }

    function _initializedAbove(int24 t) internal view returns (int24[] memory out) {
        uint256 c;
        for (uint256 n = 0; n < tracked.length; n++) if (tracked[n] > t) c++;
        out = new int24[](c);
        uint256 m;
        for (uint256 n = 0; n < tracked.length; n++) if (tracked[n] > t) out[m++] = tracked[n];
    }

    function _firstAbove(int24 t) internal view returns (int24) {
        int24[] memory a = _initializedAbove(t);
        require(a.length > 0, "no initialized tick above");
        return a[0];
    }

    function _firstBelowOrAt(int24 t) internal view returns (int24) {
        int24[] memory b = _initializedBelowOrAt(t);
        require(b.length > 0, "no initialized tick below");
        return b[0];
    }

    // ---- callbacks: pay whatever the pool asks for ----

    function _pay(int256 amount0, int256 amount1) internal {
        if (amount0 > 0) IERC20(IPool(msg.sender).token0()).transfer(msg.sender, uint256(amount0));
        if (amount1 > 0) IERC20(IPool(msg.sender).token1()).transfer(msg.sender, uint256(amount1));
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external { _pay(a0, a1); }
    function agniSwapCallback(int256 a0, int256 a1, bytes calldata) external { _pay(a0, a1); }
    function fusionXV3SwapCallback(int256 a0, int256 a1, bytes calldata) external { _pay(a0, a1); }
    function uniswapV3MintCallback(uint256 a0, uint256 a1, bytes calldata) external { _pay(int256(a0), int256(a1)); }
    function agniMintCallback(uint256 a0, uint256 a1, bytes calldata) external { _pay(int256(a0), int256(a1)); }
    function fusionXV3MintCallback(uint256 a0, uint256 a1, bytes calldata) external { _pay(int256(a0), int256(a1)); }

    // ---- misc ----

    /// forge-std-style `deal` for an ERC20 balance: find the balance slot by
    /// recording storage reads of `balanceOf`, then overwrite it.
    function _deal(address token, address who, uint256 amount) internal {
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

    function _clamp(int24 x, int24 lo, int24 hi) internal pure returns (int24) {
        return x < lo ? lo : (x > hi ? hi : x);
    }

    function _line(string memory s) internal {
        vm.writeLine(outPath, s);
    }

    function _seqIdx(uint256 idx) internal view returns (string memory) {
        return string.concat('"sequence":"', seqName, '","index":', vm.toString(idx));
    }

    function _u(uint256 x) internal pure returns (string memory) {
        return string.concat('"', vm.toString(x), '"');
    }

    function _i(int256 x) internal pure returns (string memory) {
        return string.concat('"', vm.toString(x), '"');
    }
}
