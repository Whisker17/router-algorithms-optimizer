// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Independent evidence generator for pools/liquidity_book.py (WHI-1433).
//
// Runs inside `forge test` on a Mantle mainnet fork pinned at the catalog candidate block
// (capture_lb.sh). Every recorded output is produced by the *deployed* Merchant Moe LB
// bytecode executing in the EVM: real pairs (clones of LBPair implementation
// 0xf6863Db7...DDB3B, with their live LBHooksRewarder swap hooks) and controlled pairs
// created through the deployed LBFactory on the fork with mock tokens. Nothing here
// imports or reimplements the Python under test: no LB swap/fee/price formula appears
// below. Swap inputs are sized with the pair's own views (`getSwapIn`, `getReserves`,
// `getBin`) and fixed constants.
//
// For each scenario it records the pair identity, the complete bin tree (walked with
// `getNextNonEmptyBin` to both sentinels) and every bin's `getBin`, the packed fee state
// and reserves; then, for each swap sequence (each starting from the pristine pair
// state), every swap's `getSwapOut` view quote, the executed result (recipient-received
// output and the `swap` return value, or the revert data), the per-bin `Swap` events, the
// hook's storage writes, and the post-swap state: scalars plus every tree bin whose
// reserves changed (the tree membership itself is re-walked and must be unchanged).
//
// Output: tests/fixtures/liquidity_book/{real_<pair>,controlled}.jsonl (gzipped by the script).
// Integers are decimal strings.

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function prank(address) external;
    function warp(uint256) external;
    function store(address, bytes32, bytes32) external;
    function load(address, bytes32) external view returns (bytes32);
    function record() external;
    function accesses(address) external returns (bytes32[] memory reads, bytes32[] memory writes);
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
    function writeFile(string calldata, string calldata) external;
    function writeLine(string calldata, string calldata) external;
    function toString(uint256) external pure returns (string memory);
    function toString(address) external pure returns (string memory);
    function toString(bytes32) external pure returns (string memory);
    function toString(bytes calldata) external pure returns (string memory);
    function snapshotState() external returns (uint256);
    function revertToState(uint256) external returns (bool);
    function envOr(string calldata, string calldata) external view returns (string memory);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
}

interface ILBPair {
    function getFactory() external view returns (address);
    function implementation() external view returns (address);
    function getTokenX() external view returns (address);
    function getTokenY() external view returns (address);
    function getBinStep() external view returns (uint16);
    function getReserves() external view returns (uint128, uint128);
    function getActiveId() external view returns (uint24);
    function getBin(uint24) external view returns (uint128, uint128);
    function getNextNonEmptyBin(bool, uint24) external view returns (uint24);
    function getProtocolFees() external view returns (uint128, uint128);
    function getStaticFeeParameters()
        external
        view
        returns (uint16, uint16, uint16, uint16, uint24, uint16, uint24);
    function getVariableFeeParameters() external view returns (uint24, uint24, uint24, uint40);
    function getLBHooksParameters() external view returns (bytes32);
    function getSwapIn(uint128, bool) external view returns (uint128, uint128, uint128);
    function getSwapOut(uint128, bool) external view returns (uint128, uint128, uint128);
    function swap(bool, address) external returns (bytes32);
    function mint(address, bytes32[] calldata, address) external returns (bytes32, bytes32, uint256[] memory);
}

interface ILBFactory {
    function owner() external view returns (address);
    function getLBPairImplementation() external view returns (address);
    function addQuoteAsset(address) external;
    function createLBPair(address, address, uint24, uint16) external returns (address);
    function setFeesParametersOnPair(address, address, uint16, uint16, uint16, uint16, uint16, uint24, uint16, uint24)
        external;
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

contract CaptureLB {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    string internal constant DIR = "../../tests/fixtures/liquidity_book/";
    bytes32 internal constant SWAP_TOPIC =
        keccak256("Swap(address,address,uint24,bytes32,bytes32,uint24,bytes32,bytes32)");
    uint256 internal constant MAX_LIQUIDITY_PER_BIN =
        65251743116719673010965625540244653191619923014385985379600384103134737; // Constants.sol
    uint24 internal constant CENTER = 1 << 23;

    // Mantle mainnet deployments (config/protocols.yaml `moe_lb_v2_2`, WHI-1426).
    ILBFactory internal constant FACTORY = ILBFactory(0xa6630671775c4EA2743840F9A5016dCf2A104054);
    address internal constant PAIR_IMPL = 0xf6863Db7323aaC43fE8AEF0B3eF63AA6b32DDB3B;
    address internal constant PAIR_WMNT_USDT_15 = 0xf6C9020c9E915808481757779EDB53DACEaE2415;
    address internal constant PAIR_WMNT_USDT_25 = 0x365722f12ceb2063286A268B03c654Df81B7C00F;
    address internal constant PAIR_USDC_USDT_1 = 0x48C1A89af1102Cad358549e9Bb16aE5f96CddFEc;
    address internal constant PAIR_WETH_WMNT_10 = 0x1606C79bE3EBD70D8d40bAc6287e23005CfBefA2;

    // ---- per-scenario context ----
    string internal outPath;
    string internal scenario;
    string internal sequence;
    ILBPair internal pair;
    address internal hook;
    uint256 internal step;
    uint256 internal pristineTime;
    uint24[] internal treeIds; // the complete bin tree, ascending
    uint24[] internal scratch;
    mapping(uint24 => bytes32) internal lastBin; // last recorded (x | y << 128) per tree id

    // =====================================================================================
    // Entry points
    // =====================================================================================

    function test_real_wmnt_usdt_15() external {
        _realFile("wmnt_usdt_15", PAIR_WMNT_USDT_15);
    }

    function test_real_wmnt_usdt_25() external {
        _realFile("wmnt_usdt_25", PAIR_WMNT_USDT_25);
    }

    function test_real_usdc_usdt_1() external {
        _realFile("usdc_usdt_1", PAIR_USDC_USDT_1);
    }

    function test_real_weth_wmnt_10() external {
        _realFile("weth_wmnt_10", PAIR_WETH_WMNT_10);
    }

    function test_controlled() external {
        outPath = string.concat(DIR, "controlled.jsonl");
        vm.writeFile(outPath, "");
        _meta();
        _controlledGaps();
        _controlledDecay();
        _controlledRounding();
        _controlledMaxLiquidity();
        _line('{"kind":"end"}');
    }

    function _realFile(string memory name, address pairAddress) internal {
        outPath = string.concat(DIR, "real_", name, ".jsonl");
        vm.writeFile(outPath, "");
        _meta();
        _real(name, pairAddress);
        _line('{"kind":"end"}');
    }

    // =====================================================================================
    // Real pairs at the fork block
    // =====================================================================================

    function _real(string memory name, address pairAddress) internal {
        _begin(name, ILBPair(pairAddress));
        uint256 pristine = vm.snapshotState();
        (uint128 rx, uint128 ry) = pair.getReserves();
        (uint128 ax, uint128 ay) = pair.getBin(pair.getActiveId());

        // Two directions, many bins, then repeated use of the same pair at the same time.
        _seqStart("x_then_y", pristine);
        uint256 got = _swap(true, _inFor(ry / 5, true));
        got = _swap(false, got / 2);
        _swap(true, _inFor(ry / 5, true));
        _swap(true, 1); // dust

        _seqStart("y_then_x", pristine);
        got = _swap(false, _inFor(rx / 5, false));
        got = _swap(true, got / 2);
        _swap(false, _inFor(rx / 5, false));
        _swap(false, 1); // dust

        // The same input twice in a row: the second swap must see the first one's bins
        // and fee state (repeated use of one physical pair within a plan).
        _seqStart("repeat_x_to_y", pristine);
        uint256 repeatIn = _inFor(ry / 10, true);
        _swap(true, repeatIn);
        _swap(true, repeatIn);

        _seqStart("small_both", pristine);
        uint256 smallX = _inFor(ay / 3 + 1, true);
        uint256 smallY = _inFor(ax / 3 + 1, false);
        _swap(true, smallX);
        _swap(false, smallY);

        // Exact active-bin drain (the `amountIn >= maxAmountIn` branch at equality), the
        // same minus one, then a swap starting from the drained active bin.
        // (Inputs are sized with the pair's own getSwapIn on the pristine state.)
        if (ay > 0) {
            _seqStart("drain_active_y", pristine);
            uint256 drain = _inFor(ay, true);
            _swap(true, drain);
            _swap(true, _inFor(ry / 50 + 1, true));
            _seqStart("drain_active_y_minus_one", pristine);
            _swap(true, drain - 1);
        }
        if (ax > 0) {
            _seqStart("drain_active_x", pristine);
            _swap(false, _inFor(ax, false));
            _swap(false, _inFor(rx / 50 + 1, false));
        }

        // Book exhaustion: more input than every bin of the other token can absorb.
        _seqStart("exhaust_x_to_y", pristine);
        _swap(true, 2 * _inFor(ry, true) + 1e6);
        _seqStart("exhaust_y_to_x", pristine);
        _swap(false, 2 * _inFor(rx, false) + 1e6);
        // Just enough / one wei short of the whole book (getSwapIn over every bin).
        _seqStart("whole_book_x_to_y", pristine);
        _swap(true, _inFor(ry, true));

        vm.revertToState(pristine);
    }

    // =====================================================================================
    // Controlled pairs created through the deployed factory
    // =====================================================================================

    /// Sparse bins across 256-id tree words and a 65536-id level-1 boundary (binStep 1),
    /// custom fees with a protocol share.
    function _controlledGaps() internal {
        ILBPair p = _createPair(1, 5000, 10, 120, 5000, 40000, 1000, 350000);
        uint24[4] memory offs = [uint24(1), 3, 300, 70000];
        for (uint256 i; i < offs.length; i++) {
            _mintBin(p, CENTER - offs[i], 0, 3e18 + i * 1e17);
            _mintBin(p, CENTER + offs[i], 2e18 + i * 1e17, 0);
        }
        _mintBin(p, CENTER, 1e18, 1e18);
        _begin("gaps", p);
        uint256 pristine = vm.snapshotState();

        _seqStart("x_across_gaps", pristine);
        _swap(true, 5e18);
        _swap(true, 5e18);
        _swap(false, 4e18);
        _seqStart("y_across_gaps", pristine);
        _swap(false, 7e18);
        _swap(false, 1e18);
        _swap(true, 3e18);
        _seqStart("exhaust_x_to_y", pristine);
        _swap(true, 1e23);
        _seqStart("exhaust_y_to_x", pristine);
        _swap(false, 1e23);
        vm.revertToState(pristine);
    }

    /// Dense contiguous bins (binStep 10) with a low volatility cap and the maximum
    /// protocol share; the time-dependent reference update is exercised with vm.warp:
    /// dt < filter, filter <= dt < decay and dt >= decay.
    function _controlledDecay() internal {
        ILBPair p = _createPair(10, 8000, 30, 600, 5000, 120000, 2500, 90000);
        for (uint24 i = 1; i <= 40; i++) {
            _mintBin(p, CENTER - i, 0, 1e18);
            _mintBin(p, CENTER + i, 1e18, 0);
        }
        _mintBin(p, CENTER, 5e17, 5e17);
        _begin("decay", p);
        uint256 t0 = block.timestamp;
        uint256 pristine = vm.snapshotState();

        _seqStart("decay_branches", pristine);
        _swap(true, 12e18); // crosses ~12 bins: accumulator climbs to the cap
        _swap(false, 3e18); // same timestamp: references frozen
        vm.warp(t0 + 45); // filter <= dt < decay: idRef moves, volRef = volAcc * R
        _swap(true, 4e18);
        vm.warp(t0 + 45 + 29); // dt < filter: references untouched
        _swap(false, 6e18);
        vm.warp(t0 + 45 + 29 + 600); // dt >= decay: volRef resets
        _swap(true, 2e18);
        _swap(true, 1); // dust

        // Reference-update edges: dt == filterPeriod and dt == decayPeriod exactly.
        _seqStart("time_edges", pristine);
        _swap(true, 9e18);
        vm.warp(t0 + 30); // dt == filter: references update
        _swap(false, 5e18);
        vm.warp(t0 + 30 + 600); // dt == decay: volRef resets
        _swap(true, 5e18);
        vm.warp(t0 + 30 + 600 + 29); // dt == filter - 1: untouched
        _swap(false, 5e18);

        _seqStart("bin_boundaries", pristine);
        (, uint128 ay) = pair.getBin(pair.getActiveId());
        uint256 drain = _inFor(ay, true);
        _swap(true, drain);
        (, uint128 ny) = pair.getBin(pair.getActiveId() - 1);
        _swap(true, _inFor(ny, true) - 1); // one wei short of draining the next bin
        _swap(true, 3);
        _swap(false, 1e12);
        vm.revertToState(pristine);
    }

    /// Irregular bin reserves (binStep 25): every bin drained exactly in turn with the
    /// input the pair's own getSwapIn reports (the `amountIn >= maxAmountIn` branch at
    /// equality), one wei short of it, and a grid of dust inputs (integer rounding).
    function _controlledRounding() internal {
        ILBPair p = _createPair(25, 7777, 20, 300, 6000, 70000, 1337, 400000);
        for (uint24 i = 1; i <= 16; i++) {
            _mintBin(p, CENTER - i, 0, 1e15 + uint256(i) ** 7 * 7919);
            _mintBin(p, CENTER + i, 1e15 + uint256(i) ** 7 * 104729, 0);
        }
        _mintBin(p, CENTER, 123456789012345, 987654321098765);
        _begin("rounding", p);
        uint256 pristine = vm.snapshotState();

        _seqStart("drain_each_y", pristine);
        for (uint256 k; k < 12; k++) {
            (, uint128 by) = pair.getBin(pair.getActiveId());
            uint24 id = by > 0 ? pair.getActiveId() : pair.getNextNonEmptyBin(true, pair.getActiveId());
            (, uint128 ny) = pair.getBin(id);
            _swap(true, _inFor(ny, true));
        }
        _seqStart("drain_each_x", pristine);
        for (uint256 k; k < 12; k++) {
            (uint128 bx,) = pair.getBin(pair.getActiveId());
            uint24 id = bx > 0 ? pair.getActiveId() : pair.getNextNonEmptyBin(false, pair.getActiveId());
            (uint128 nx,) = pair.getBin(id);
            _swap(false, _inFor(nx, false));
        }
        _seqStart("short_of_drain", pristine);
        for (uint256 k; k < 8; k++) {
            bool forY = k % 2 == 0;
            (uint128 bx, uint128 by) = pair.getBin(pair.getActiveId());
            uint256 want = forY ? by : bx;
            if (want == 0) want = forY ? 1e14 : 1e14;
            _swap(forY, _inFor(want, forY) - 1);
        }
        _seqStart("dust_grid", pristine);
        for (uint256 a = 1; a <= 12; a++) {
            _swap(a % 2 == 1, a * 37);
        }
        vm.revertToState(pristine);
    }

    /// A Y-only bin at the MAX_LIQUIDITY_PER_BIN ceiling below an empty active bin: a swap
    /// into it adds fees above the ceiling and must revert.
    function _controlledMaxLiquidity() internal {
        ILBPair p = _createPair(1, 5000, 10, 120, 5000, 40000, 0, 350000);
        _mintBin(p, CENTER - 1, 0, MAX_LIQUIDITY_PER_BIN >> 128);
        _begin("max_liquidity", p);
        uint256 pristine = vm.snapshotState();
        _seqStart("over_ceiling", pristine);
        _swap(true, 1e18);
        _seqStart("no_x_liquidity", pristine);
        _swap(false, 1e18); // no X anywhere: book exhausted
        vm.revertToState(pristine);
    }

    function _createPair(
        uint16 binStep,
        uint16 baseFactor,
        uint16 filterPeriod,
        uint16 decayPeriod,
        uint16 reductionFactor,
        uint24 variableFeeControl,
        uint16 protocolShare,
        uint24 maxVolatilityAccumulator
    ) internal returns (ILBPair p) {
        MockToken tx_ = new MockToken();
        MockToken ty = new MockToken();
        address owner = FACTORY.owner();
        vm.prank(owner);
        FACTORY.addQuoteAsset(address(ty));
        vm.prank(owner);
        p = ILBPair(FACTORY.createLBPair(address(tx_), address(ty), CENTER, binStep));
        vm.prank(owner);
        FACTORY.setFeesParametersOnPair(
            address(tx_),
            address(ty),
            binStep,
            baseFactor,
            filterPeriod,
            decayPeriod,
            reductionFactor,
            variableFeeControl,
            protocolShare,
            maxVolatilityAccumulator
        );
        tx_.mint(address(this), type(uint128).max);
        ty.mint(address(this), type(uint128).max);
    }

    function _mintBin(ILBPair p, uint24 id, uint256 x, uint256 y) internal {
        if (x > 0) IERC20(p.getTokenX()).transfer(address(p), x);
        if (y > 0) IERC20(p.getTokenY()).transfer(address(p), y);
        bytes32[] memory configs = new bytes32[](1);
        uint256 dx = x > 0 ? 1e18 : 0;
        uint256 dy = y > 0 ? 1e18 : 0;
        configs[0] = bytes32((dx << 88) | (dy << 24) | uint256(id)); // LiquidityConfigurations
        p.mint(address(this), configs, address(this));
    }

    // =====================================================================================
    // Capture
    // =====================================================================================

    function _meta() internal {
        _line(
            string.concat(
                '{"kind":"meta","chain_id":',
                _u(block.chainid),
                ',"block_number":',
                _u(block.number),
                ',"block_hash":"',
                vm.envOr("CL_FORK_BLOCK_HASH", string("")),
                '","block_timestamp":',
                _u(block.timestamp),
                ',"factory":"',
                vm.toString(address(FACTORY)),
                '","factory_code_hash":"',
                vm.toString(address(FACTORY).codehash),
                '","pair_implementation":"',
                vm.toString(FACTORY.getLBPairImplementation()),
                '","pair_implementation_code_hash":"',
                vm.toString(PAIR_IMPL.codehash),
                '"}'
            )
        );
    }

    function _begin(string memory name, ILBPair p) internal {
        scenario = name;
        pair = p;
        pristineTime = block.timestamp;
        bytes32 hooks = p.getLBHooksParameters();
        hook = address(uint160(uint256(hooks)));
        address hookImpl = _cloneImplementation(hook);
        _line(
            string.concat(
                '{"kind":"pair","scenario":"',
                name,
                '","pair":"',
                vm.toString(address(p)),
                '","factory":"',
                vm.toString(p.getFactory()),
                '","implementation":"',
                vm.toString(p.implementation()),
                '","token_x":"',
                vm.toString(p.getTokenX()),
                '","token_y":"',
                vm.toString(p.getTokenY()),
                '","bin_step":',
                _u(p.getBinStep()),
                ',"hooks_parameters":"',
                vm.toString(hooks),
                '","hook_implementation":"',
                vm.toString(hookImpl),
                '","hook_implementation_code_hash":"',
                vm.toString(hookImpl.codehash),
                '"}'
            )
        );
        // Complete tree walk, both directions to the sentinels.
        delete treeIds;
        delete scratch;
        uint24 active = p.getActiveId();
        for (uint24 id = active; ; ) {
            id = p.getNextNonEmptyBin(true, id);
            if (id == 0 || id == type(uint24).max) break;
            scratch.push(id);
        }
        for (uint256 i = scratch.length; i > 0; i--) {
            treeIds.push(scratch[i - 1]);
        }
        (uint128 ax, uint128 ay) = p.getBin(active);
        if (ax > 0 || ay > 0) treeIds.push(active);
        for (uint24 id = active; ; ) {
            id = p.getNextNonEmptyBin(false, id);
            if (id == 0 || id == type(uint24).max) break;
            treeIds.push(id);
        }
        for (uint256 i; i < treeIds.length; i++) {
            (uint128 x, uint128 y) = p.getBin(treeIds[i]);
            lastBin[treeIds[i]] = bytes32(uint256(x) | (uint256(y) << 128));
            _line(
                string.concat(
                    '{"kind":"bin","scenario":"',
                    name,
                    '","id":',
                    _u(treeIds[i]),
                    ',"x":',
                    _u(x),
                    ',"y":',
                    _u(y),
                    "}"
                )
            );
        }
        _state("pre", "");
    }

    function _walkCount(bool swapForY, uint24 from) internal view returns (uint256 n) {
        for (uint24 id = from; ; ) {
            id = pair.getNextNonEmptyBin(swapForY, id);
            if (id == 0 || id == type(uint24).max) break;
            n++;
        }
    }

    function _cloneImplementation(address clone) internal view returns (address impl) {
        bytes memory code = clone.code;
        if (code.length < 40) return address(0);
        // ImmutableClone runtime: 363d3d373d3d3d3d61 <len> 806035363936013d73 <impl> ...
        assembly {
            impl := shr(96, mload(add(code, 52)))
        }
    }

    function _seqStart(string memory name, uint256 pristine) internal {
        vm.revertToState(pristine);
        vm.warp(pristineTime);
        sequence = name;
        step = 0;
        for (uint256 i; i < treeIds.length; i++) {
            (uint128 x, uint128 y) = pair.getBin(treeIds[i]);
            lastBin[treeIds[i]] = bytes32(uint256(x) | (uint256(y) << 128));
        }
    }

    function _inFor(uint256 amountOut, bool swapForY) internal view returns (uint256 amountIn) {
        (uint128 inNeeded,,) = pair.getSwapIn(uint128(amountOut), swapForY);
        amountIn = inNeeded;
        if (amountIn == 0) amountIn = 1;
    }

    /// Executes one exact-input swap in its own call frame (transfer + swap revert
    /// together) and records everything; returns the output received (0 on revert).
    function _swap(bool swapForY, uint256 amountIn) internal returns (uint256 received) {
        string memory quote;
        try pair.getSwapOut(uint128(amountIn), swapForY) returns (uint128 qLeft, uint128 qOut, uint128 qFee) {
            quote = string.concat(
                '{"amount_in_left":', _u(qLeft), ',"amount_out":', _u(qOut), ',"fee":', _u(qFee), "}"
            );
        } catch (bytes memory qErr) {
            quote = string.concat('{"revert_data":"', vm.toString(qErr), '"}');
        }
        address tokenIn = swapForY ? pair.getTokenX() : pair.getTokenY();
        _deal(tokenIn, address(this), amountIn);
        vm.recordLogs();
        vm.record();
        bool ok;
        bytes memory ret;
        try this.doSwap(swapForY, amountIn) returns (uint256 got, uint256 credited, bytes32 amountsOut) {
            ok = true;
            received = got;
            ret = abi.encode(credited, amountsOut);
        } catch (bytes memory err) {
            ret = err;
        }
        (bytes32[] memory hookReads, bytes32[] memory hookWrites) = vm.accesses(hook);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        string memory head = string.concat(
            '{"kind":"swap","scenario":"',
            scenario,
            '","sequence":"',
            sequence,
            '","step":',
            _u(step),
            ',"timestamp":',
            _u(block.timestamp),
            ',"swap_for_y":',
            swapForY ? "true" : "false",
            ',"amount_in":',
            _u(amountIn),
            ',"quote":',
            quote
        );
        string memory body;
        if (ok) {
            (uint256 credited, bytes32 amountsOut) = abi.decode(ret, (uint256, bytes32));
            body = string.concat(
                ',"status":"ok","received":',
                _u(received),
                ',"credited":',
                _u(credited),
                ',"amounts_out_x":',
                _u(uint128(uint256(amountsOut))),
                ',"amounts_out_y":',
                _u(uint256(amountsOut) >> 128),
                ',"hook_storage_reads":',
                _u(hookReads.length),
                ',"hook_storage_writes":',
                _u(hookWrites.length)
            );
        } else {
            body = string.concat(',"status":"revert","revert_data":"', vm.toString(ret), '"');
        }
        _line(string.concat(head, body, "}"));
        if (ok) {
            _events(logs);
            _state("post", string.concat(',"sequence":"', sequence, '","step":', _u(step)));
        }
        step++;
    }

    function doSwap(bool swapForY, uint256 amountIn)
        external
        returns (uint256 got, uint256 credited, bytes32 amountsOut)
    {
        require(msg.sender == address(this), "self only");
        address tokenIn = swapForY ? pair.getTokenX() : pair.getTokenY();
        address tokenOut = swapForY ? pair.getTokenY() : pair.getTokenX();
        uint256 inBefore = IERC20(tokenIn).balanceOf(address(pair));
        IERC20(tokenIn).transfer(address(pair), amountIn);
        credited = IERC20(tokenIn).balanceOf(address(pair)) - inBefore;
        uint256 outBefore = IERC20(tokenOut).balanceOf(address(this));
        amountsOut = pair.swap(swapForY, address(this));
        got = IERC20(tokenOut).balanceOf(address(this)) - outBefore;
    }

    function _events(Vm.Log[] memory logs) internal {
        uint256 k;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].topics.length == 0 || logs[i].topics[0] != SWAP_TOPIC) continue;
            (uint24 id, bytes32 amIn, bytes32 amOut, uint24 volAcc, bytes32 fees, bytes32 pFees) =
                abi.decode(logs[i].data, (uint24, bytes32, bytes32, uint24, bytes32, bytes32));
            _line(
                string.concat(
                    string.concat(
                        '{"kind":"event","scenario":"',
                        scenario,
                        '","sequence":"',
                        sequence,
                        '","step":',
                        _u(step),
                        ',"index":',
                        _u(k++),
                        ',"id":',
                        _u(id)
                    ),
                    string.concat(
                        ',"amounts_in":',
                        _packed(amIn),
                        ',"amounts_out":',
                        _packed(amOut),
                        ',"volatility_accumulator":',
                        _u(volAcc),
                        ',"total_fees":',
                        _packed(fees),
                        ',"protocol_fees":',
                        _packed(pFees),
                        "}"
                    )
                )
            );
        }
    }

    function _state(string memory label, string memory extra) internal {
        (uint16 bf, uint16 fp, uint16 dp, uint16 rf, uint24 vfc, uint16 ps, uint24 mva) =
            pair.getStaticFeeParameters();
        (uint24 va, uint24 vr, uint24 ir, uint40 tlu) = pair.getVariableFeeParameters();
        string memory a = string.concat(
            '{"kind":"state","scenario":"',
            scenario,
            '","label":"',
            label,
            '"',
            extra,
            ',"timestamp":',
            _u(block.timestamp),
            ',"active_id":',
            _u(pair.getActiveId()),
            ',"static":[',
            _u(bf),
            ",",
            _u(fp),
            ",",
            _u(dp),
            ",",
            _u(rf),
            ",",
            _u(vfc),
            ",",
            _u(ps),
            ",",
            _u(mva),
            "]"
        );
        (uint128 rx, uint128 ry) = pair.getReserves();
        (uint128 fx, uint128 fy) = pair.getProtocolFees();
        string memory b = string.concat(
            ',"variable":[',
            _u(va),
            ",",
            _u(vr),
            ",",
            _u(ir),
            ",",
            _u(tlu),
            '],"reserves":[',
            _u(rx),
            ",",
            _u(ry),
            '],"protocol_fees":[',
            _u(fx),
            ",",
            _u(fy),
            "]"
        );
        uint256 treeSize = _changedBins(label);
        _line(string.concat(a, b, ',"tree_size":', _u(treeSize), "}"));
    }

    /// Re-walks the tree (must equal the recorded membership) and records every tree bin
    /// whose reserves differ from the last recorded state of this sequence.
    function _changedBins(string memory label) internal returns (uint256 n) {
        uint24 active = pair.getActiveId();
        (uint128 ax, uint128 ay) = pair.getBin(active);
        n = _walkCount(true, active) + _walkCount(false, active) + ((ax > 0 || ay > 0) ? 1 : 0);
        require(n == treeIds.length, "bin tree membership changed");
        for (uint256 i; i < treeIds.length; i++) {
            (uint128 x, uint128 y) = pair.getBin(treeIds[i]);
            bytes32 packed = bytes32(uint256(x) | (uint256(y) << 128));
            if (packed == lastBin[treeIds[i]]) continue;
            lastBin[treeIds[i]] = packed;
            _line(
                string.concat(
                    '{"kind":"bin_changed","scenario":"',
                    scenario,
                    '","sequence":"',
                    sequence,
                    '","step":',
                    _u(step),
                    ',"label":"',
                    label,
                    '","id":',
                    _u(treeIds[i]),
                    ',"x":',
                    _u(x),
                    ',"y":',
                    _u(y),
                    "}"
                )
            );
        }
    }

    // =====================================================================================
    // Helpers
    // =====================================================================================

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

    function _packed(bytes32 v) internal pure returns (string memory) {
        return string.concat("[", _u(uint128(uint256(v))), ",", _u(uint256(v) >> 128), "]");
    }

    function _line(string memory s) internal {
        vm.writeLine(outPath, s);
    }

    function _u(uint256 v) internal pure returns (string memory) {
        return string.concat('"', vm.toString(v), '"');
    }
}
