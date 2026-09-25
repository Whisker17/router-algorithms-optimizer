// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Fixed-block Merchant Moe Liquidity Book v2.2 replay evidence for WHI-1434
// (`tests/snapshot/test_moe_lb.py`).
//
// Runs inside `forge test` on a Mantle mainnet fork pinned at the *bundle's* block (see
// replay_moe_lb.sh). Everything recorded comes from the deployed bytecode only: the LBPair
// clones (implementation 0xf6863Db7...DDB3B), their live LBHooksRewarder /
// LBHooksExtraRewarder swap hooks and the real tokens. For every pair of the published
// bundle it records an independent read of the collected state:
//   - `pool_state`: identity (tokens, decimals, bin step, factory, implementation, code
//     hash), active id, reserves, protocol fees, static/variable fee parameters, the hooks
//     word, the rewarder's extra-hooks word, the hook implementations and token balances;
//   - `bin`: every bin-tree member inside the bundle's walked `bin_range`, re-walked here
//     with getNextNonEmptyBin (plus the first member beyond each end of the range, or the
//     sentinel, as `bin_edge`).
// Then for every (reference case x pair of its token pair) request, from that pristine
// state: the getSwapOut view, the executed exact-input swap (input transferred to the pair,
// crediting recorded; the recipient's received output or the revert data), every per-bin
// `Swap` event, the live hooks' storage reads, and the post-swap state (active id, reserves,
// protocol fees, variable fee parameters and the bins each event touched); and, when the
// swap succeeded, a follow-up reverse swap of half its output through that post-state
// (two sequential swaps through the same pair), recorded the same way.
// No LB swap/fee/price formula appears below; nothing imports the Python under test.
// Integers are decimal strings; the input is produced by make_lb_replay_requests.py.

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function store(address, bytes32, bytes32) external;
    function load(address, bytes32) external view returns (bytes32);
    function record() external;
    function accesses(address) external returns (bytes32[] memory reads, bytes32[] memory writes);
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
    function writeFile(string calldata, string calldata) external;
    function writeLine(string calldata, string calldata) external;
    function readFile(string calldata) external view returns (string memory);
    function parseJsonUint(string calldata, string calldata) external pure returns (uint256);
    function parseJsonString(string calldata, string calldata) external pure returns (string memory);
    function parseJsonAddressArray(string calldata, string calldata) external pure returns (address[] memory);
    function parseJsonUintArray(string calldata, string calldata) external pure returns (uint256[] memory);
    function parseJsonBoolArray(string calldata, string calldata) external pure returns (bool[] memory);
    function parseJsonStringArray(string calldata, string calldata) external pure returns (string[] memory);
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
    function decimals() external view returns (uint8);
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
    function getSwapOut(uint128, bool) external view returns (uint128, uint128, uint128);
    function swap(bool, address) external returns (bytes32);
}

interface ILBHooksRewarder {
    function getExtraHooksParameters() external view returns (bytes32);
}

contract CaptureMoeLBReplay {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    string internal constant IN_PATH = "requests/moe_lb_replay.json";
    string internal constant OUT_PATH = "../../tests/fixtures/moe_lb/evidence.jsonl";
    bytes32 internal constant SWAP_TOPIC =
        keccak256("Swap(address,address,uint24,bytes32,bytes32,uint24,bytes32,bytes32)");

    ILBPair internal pair;
    address internal hook;
    address internal extraHook;

    function test_moe_lb_replay() external {
        string memory json = vm.readFile(IN_PATH);
        require(vm.parseJsonUint(json, ".chain_id") == block.chainid, "fork chain id != request chain id");
        require(vm.parseJsonUint(json, ".block_number") == block.number, "fork block != bundle block");
        vm.writeFile(OUT_PATH, "");
        _line(string.concat(
            '{"kind":"meta","schema":"moe-lb-replay-evidence/1"',
            ',"generator":"tools/cl_evidence/test/CaptureMoeLBReplay.t.sol (forge test on a Mantle fork)"',
            ',"bundle_id":"', vm.parseJsonString(json, ".bundle_id"),
            '","chain_id":', vm.toString(block.chainid),
            ',"block_number":', vm.toString(block.number),
            ',"block_timestamp":', vm.toString(block.timestamp),
            ',"block_hash":"', vm.envOr("CL_FORK_BLOCK_HASH", string("")), '"}'
        ));

        address[] memory pools = vm.parseJsonAddressArray(json, ".pools");
        uint256[] memory los = vm.parseJsonUintArray(json, ".bin_range_lo");
        uint256[] memory his = vm.parseJsonUintArray(json, ".bin_range_hi");
        for (uint256 i = 0; i < pools.length; i++) {
            _select(pools[i]);
            _writePoolState();
            _writeTree(uint24(los[i]), uint24(his[i]));
        }

        string[] memory caseIds = vm.parseJsonStringArray(json, ".case_ids");
        address[] memory reqPools = vm.parseJsonAddressArray(json, ".request_pools");
        bool[] memory forY = vm.parseJsonBoolArray(json, ".swap_for_y");
        uint256[] memory amounts = vm.parseJsonUintArray(json, ".amounts");
        for (uint256 i = 0; i < caseIds.length; i++) {
            _select(reqPools[i]);
            uint256 snap = vm.snapshotState();
            uint256 received = _swap("swap", caseIds[i], forY[i], amounts[i]);
            if (received / 2 > 0) _swap("followup", caseIds[i], !forY[i], received / 2);
            vm.revertToState(snap);
        }
        _line('{"kind":"end"}');
    }

    function _select(address p) internal {
        pair = ILBPair(p);
        bytes32 hooks = pair.getLBHooksParameters();
        hook = address(uint160(uint256(hooks)));
        extraHook = address(0);
        if (hook != address(0) && hook.code.length > 0) {
            try ILBHooksRewarder(hook).getExtraHooksParameters() returns (bytes32 extra) {
                extraHook = address(uint160(uint256(extra)));
            } catch {}
        }
    }

    // =====================================================================================
    // Independent state read
    // =====================================================================================

    function _writePoolState() internal {
        address tx_ = pair.getTokenX();
        address ty = pair.getTokenY();
        string memory head = string.concat(
            '{"kind":"pool_state","pool":"', vm.toString(address(pair)),
            '","code_hash":"', vm.toString(address(pair).codehash),
            '","token_x":"', vm.toString(tx_),
            '","token_y":"', vm.toString(ty),
            '","decimals_x":', vm.toString(uint256(IERC20(tx_).decimals())),
            ',"decimals_y":', vm.toString(uint256(IERC20(ty).decimals())),
            ',"bin_step":', _u(pair.getBinStep()),
            ',"factory":"', vm.toString(pair.getFactory()),
            '","implementation":"', vm.toString(pair.implementation()), '"'
        );
        string memory hooks = string.concat(
            ',"hooks_parameters":"', vm.toString(pair.getLBHooksParameters()),
            '","hook_implementation":"', vm.toString(_cloneImplementation(hook)),
            '","hook_implementation_code_hash":"', vm.toString(_cloneImplementation(hook).codehash),
            '","extra_hooks_parameters":"', vm.toString(_extraParams()),
            '","extra_hook_implementation":"', vm.toString(_cloneImplementation(extraHook)),
            '","extra_hook_implementation_code_hash":"',
            vm.toString(_cloneImplementation(extraHook).codehash), '"'
        );
        (uint128 bx, uint128 by) = (
            uint128(IERC20(tx_).balanceOf(address(pair))), uint128(IERC20(ty).balanceOf(address(pair)))
        );
        _line(string.concat(
            head, hooks, _scalars(), ',"balance_x":', _u(bx), ',"balance_y":', _u(by), "}"
        ));
    }

    function _extraParams() internal view returns (bytes32 extra) {
        if (hook == address(0) || hook.code.length == 0) return bytes32(0);
        try ILBHooksRewarder(hook).getExtraHooksParameters() returns (bytes32 e) {
            extra = e;
        } catch {}
    }

    /// Every tree member inside [lo, hi], walked from the active bin, plus the first
    /// member (or sentinel) beyond each end.
    function _writeTree(uint24 lo, uint24 hi) internal {
        uint24 active = pair.getActiveId();
        (uint128 ax, uint128 ay) = pair.getBin(active);
        if (ax > 0 || ay > 0) _bin("bin", active);
        uint24 id = active;
        while (true) {
            id = pair.getNextNonEmptyBin(true, id);
            if (id == type(uint24).max || id < lo) break;
            _bin("bin", id);
        }
        _edge("below", id);
        id = active;
        while (true) {
            id = pair.getNextNonEmptyBin(false, id);
            if (id == 0 || id > hi) break;
            _bin("bin", id);
        }
        _edge("above", id);
    }

    function _bin(string memory kind, uint24 id) internal {
        (uint128 x, uint128 y) = pair.getBin(id);
        _line(string.concat(
            '{"kind":"', kind, '","pool":"', vm.toString(address(pair)),
            '","id":', _u(id), ',"x":', _u(x), ',"y":', _u(y), "}"
        ));
    }

    function _edge(string memory side, uint24 id) internal {
        _line(string.concat(
            '{"kind":"bin_edge","pool":"', vm.toString(address(pair)),
            '","side":"', side, '","next":', _u(id), "}"
        ));
    }

    function _scalars() internal view returns (string memory) {
        (uint16 bf, uint16 fp, uint16 dp, uint16 rf, uint24 vfc, uint16 ps, uint24 mva) =
            pair.getStaticFeeParameters();
        (uint24 va, uint24 vr, uint24 ir, uint40 tlu) = pair.getVariableFeeParameters();
        (uint128 rx, uint128 ry) = pair.getReserves();
        (uint128 fx, uint128 fy) = pair.getProtocolFees();
        string memory a = string.concat(
            ',"active_id":', _u(pair.getActiveId()),
            ',"static":[', _u(bf), ",", _u(fp), ",", _u(dp), ",", _u(rf), ",", _u(vfc), ",",
            _u(ps), ",", _u(mva), "]"
        );
        return string.concat(
            a,
            ',"variable":[', _u(va), ",", _u(vr), ",", _u(ir), ",", _u(tlu),
            '],"reserves":[', _u(rx), ",", _u(ry),
            '],"protocol_fees":[', _u(fx), ",", _u(fy), "]"
        );
    }

    // =====================================================================================
    // Swaps
    // =====================================================================================

    /// One exact-input swap in its own call frame (transfer + swap revert together);
    /// records everything and returns the output received (0 on revert).
    function _swap(string memory kind, string memory caseId, bool swapForY, uint256 amountIn)
        internal
        returns (uint256 received)
    {
        string memory quote;
        try pair.getSwapOut(uint128(amountIn), swapForY) returns (uint128 qLeft, uint128 qOut, uint128 qFee) {
            quote = string.concat(
                '{"amount_in_left":', _u(qLeft), ',"amount_out":', _u(qOut), ',"fee":', _u(qFee), "}"
            );
        } catch (bytes memory qErr) {
            quote = string.concat('{"revert_data":"', vm.toString(qErr), '"}');
        }
        _deal(swapForY ? pair.getTokenX() : pair.getTokenY(), address(this), amountIn);
        vm.recordLogs();
        vm.record();
        bool ok;
        bytes memory ret;
        try this.doSwap(swapForY, amountIn) returns (uint256 got, uint256 transferred) {
            ok = true;
            received = got;
            ret = abi.encode(transferred);
        } catch (bytes memory err) {
            ret = err;
        }
        uint256 hookReads;
        uint256 extraReads;
        if (hook != address(0)) {
            (bytes32[] memory r,) = vm.accesses(hook);
            hookReads = r.length;
        }
        if (extraHook != address(0)) {
            (bytes32[] memory r,) = vm.accesses(extraHook);
            extraReads = r.length;
        }
        Vm.Log[] memory logs = vm.getRecordedLogs();
        string memory head = string.concat(
            '{"kind":"', kind, '","case_id":"', caseId,
            '","pool":"', vm.toString(address(pair)),
            '","timestamp":', _u(block.timestamp),
            ',"swap_for_y":', swapForY ? "true" : "false",
            ',"amount_in":', _u(amountIn),
            ',"quote":', quote
        );
        if (!ok) {
            _line(string.concat(head, ',"status":"revert","revert_data":"', vm.toString(ret), '"}'));
            return 0;
        }
        uint256 credited = abi.decode(ret, (uint256));
        string memory events = _events(logs);
        _line(string.concat(
            head,
            ',"status":"ok","received":', _u(received),
            ',"credited":', _u(credited),
            ',"hook_storage_reads":', _u(hookReads),
            ',"extra_hook_storage_reads":', _u(extraReads),
            ',"events":[', events, "]",
            _scalars(),
            ',"bins_after":[', _binsAfter(logs), "]}"
        ));
    }

    function doSwap(bool swapForY, uint256 amountIn) external returns (uint256 got, uint256 credited) {
        require(msg.sender == address(this), "self only");
        address tokenIn = swapForY ? pair.getTokenX() : pair.getTokenY();
        address tokenOut = swapForY ? pair.getTokenY() : pair.getTokenX();
        uint256 inBefore = IERC20(tokenIn).balanceOf(address(pair));
        IERC20(tokenIn).transfer(address(pair), amountIn);
        credited = IERC20(tokenIn).balanceOf(address(pair)) - inBefore;
        uint256 outBefore = IERC20(tokenOut).balanceOf(address(this));
        pair.swap(swapForY, address(this));
        got = IERC20(tokenOut).balanceOf(address(this)) - outBefore;
    }

    function _events(Vm.Log[] memory logs) internal view returns (string memory out) {
        bool first = true;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(pair) || logs[i].topics.length == 0 || logs[i].topics[0] != SWAP_TOPIC) {
                continue;
            }
            (uint24 id, bytes32 amIn, bytes32 amOut, uint24 volAcc, bytes32 fees, bytes32 pFees) =
                abi.decode(logs[i].data, (uint24, bytes32, bytes32, uint24, bytes32, bytes32));
            out = string.concat(
                out,
                first ? "" : ",",
                string.concat("[", _u(id), ",", _packed(amIn), ",", _packed(amOut), ","),
                string.concat(_u(volAcc), ",", _packed(fees), ",", _packed(pFees), "]")
            );
            first = false;
        }
    }

    function _binsAfter(Vm.Log[] memory logs) internal view returns (string memory out) {
        bool first = true;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(pair) || logs[i].topics.length == 0 || logs[i].topics[0] != SWAP_TOPIC) {
                continue;
            }
            (uint24 id,,,,,) = abi.decode(logs[i].data, (uint24, bytes32, bytes32, uint24, bytes32, bytes32));
            (uint128 x, uint128 y) = pair.getBin(id);
            out = string.concat(out, first ? "" : ",", "[", _u(id), ",", _u(x), ",", _u(y), "]");
            first = false;
        }
    }

    // =====================================================================================
    // Helpers
    // =====================================================================================

    function _cloneImplementation(address clone) internal view returns (address impl) {
        if (clone == address(0)) return address(0);
        bytes memory code = clone.code;
        if (code.length < 40) return address(0);
        // ImmutableClone runtime: 363d3d373d3d3d3d61 <len> 806035363936013d73 <impl> ...
        assembly {
            impl := shr(96, mload(add(code, 52)))
        }
    }

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
        vm.writeLine(OUT_PATH, s);
    }

    function _u(uint256 v) internal pure returns (string memory) {
        return string.concat('"', vm.toString(v), '"');
    }
}
