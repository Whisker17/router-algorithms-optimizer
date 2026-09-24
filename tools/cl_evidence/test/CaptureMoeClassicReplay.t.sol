// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.26;

// Fixed-block Merchant Moe Classic v1 replay evidence for WHI-1432
// (`tests/snapshot/test_moe_classic.py`).
//
// Runs inside `forge test` on a Mantle mainnet fork pinned at the *bundle's* block (see
// replay_moe_classic.sh). For every (reference case x pair of its token pair) request in
// the input file it records, from the deployed bytecode only (the MoePair clones, their
// implementation 0x08477e01...c28B, the explorer-verified MoeRouter and the real tokens):
//   - the pair's pre-state: getReserves(), both token balances, code hash, tokens,
//     decimals (once per pair, `pool_state`);
//   - MoeRouter.getAmountsOut(amountIn, [tokenIn, tokenOut]) -- the deployed periphery's
//     own quote path (MoeLibrary.getAmountOut over MoeLibrary.pairFor's CREATE2 pair);
//   - the real exact-input swap: `amountIn` is transferred to the pair (recording how much
//     the pair's balance actually grew -- a fee-on-transfer token would credit less),
//     then the pair is asked for one wei *more* than the router quote (it must revert
//     `Moe: K`: the quote is the largest output the pair's own invariant accepts) and
//     then for the quote itself (executed; a zero quote is attempted as-is and records
//     the pair's revert reason). Recorded: the output the recipient received and the
//     post-swap reserves and balances;
//   - a follow-up reverse swap of half the output from that post-state, by the same
//     procedure (two sequential swaps through the same, updated pair).
// Plus uint112 overflow probes on the example pair: an input that leaves the input
// reserve at exactly type(uint112).max (must execute) and one wei more (must revert
// `Moe: OVERFLOW`).
// Nothing here imports or reimplements the Python under test (no swap formula appears
// below: every output comes from the deployed router and is checked against the pair).
// Integers are decimal strings; the input is produced by make_classic_replay_requests.py.

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
    function parseJsonAddress(string calldata, string calldata) external pure returns (address);
    function parseJsonAddressArray(string calldata, string calldata) external pure returns (address[] memory);
    function parseJsonUintArray(string calldata, string calldata) external pure returns (uint256[] memory);
    function parseJsonBoolArray(string calldata, string calldata) external pure returns (bool[] memory);
    function parseJsonStringArray(string calldata, string calldata) external pure returns (string[] memory);
    function toString(uint256) external pure returns (string memory);
    function toString(address) external pure returns (string memory);
    function toString(bytes32) external pure returns (string memory);
    function snapshotState() external returns (uint256);
    function revertToState(uint256) external returns (bool);
    function envOr(string calldata, string calldata) external view returns (string memory);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function decimals() external view returns (uint8);
}

interface IMoePair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function factory() external view returns (address);
    function implementation() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
    function swap(uint256, uint256, address, bytes calldata) external;
}

interface IMoeRouter {
    function factory() external view returns (address);
    function getAmountsOut(uint256, address[] calldata) external view returns (uint256[] memory);
}

contract CaptureMoeClassicReplay {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    string internal constant IN_PATH = "requests/moe_classic_replay.json";
    string internal constant OUT_PATH = "../../tests/fixtures/moe_classic/evidence.jsonl";

    IMoePair internal pair;
    IMoeRouter internal router;

    function test_moe_classic_replay() external {
        string memory json = vm.readFile(IN_PATH);
        require(vm.parseJsonUint(json, ".chain_id") == block.chainid, "fork chain id != request chain id");
        require(vm.parseJsonUint(json, ".block_number") == block.number, "fork block != bundle block");
        router = IMoeRouter(vm.parseJsonAddress(json, ".router"));
        vm.writeFile(OUT_PATH, "");
        _line(string.concat(
            '{"kind":"meta","schema":"moe-classic-replay-evidence/1"',
            ',"generator":"tools/cl_evidence/test/CaptureMoeClassicReplay.t.sol (forge test on a Mantle fork)"',
            ',"router":"', vm.toString(address(router)),
            '","router_code_hash":"', vm.toString(address(router).codehash),
            '","router_factory":"', vm.toString(router.factory()),
            '","bundle_id":"', vm.parseJsonString(json, ".bundle_id"),
            '","chain_id":', vm.toString(block.chainid),
            ',"block_number":', vm.toString(block.number),
            ',"block_timestamp":', vm.toString(block.timestamp),
            ',"block_hash":"', vm.envOr("CL_FORK_BLOCK_HASH", string("")), '"}'
        ));

        address[] memory pools = vm.parseJsonAddressArray(json, ".pools");
        for (uint256 i = 0; i < pools.length; i++) {
            pair = IMoePair(pools[i]);
            _writePoolState();
        }

        string[] memory caseIds = vm.parseJsonStringArray(json, ".case_ids");
        address[] memory reqPools = vm.parseJsonAddressArray(json, ".request_pools");
        bool[] memory zfo = vm.parseJsonBoolArray(json, ".zero_for_one");
        uint256[] memory amounts = vm.parseJsonUintArray(json, ".amounts");
        for (uint256 i = 0; i < caseIds.length; i++) {
            pair = IMoePair(reqPools[i]);
            uint256 snap = vm.snapshotState();
            _request(caseIds[i], zfo[i], amounts[i]);
            vm.revertToState(snap);
        }

        pair = IMoePair(vm.parseJsonAddress(json, ".overflow_pool"));
        bool[] memory ozfo = vm.parseJsonBoolArray(json, ".overflow_zero_for_one");
        uint256[] memory oamounts = vm.parseJsonUintArray(json, ".overflow_amounts");
        for (uint256 i = 0; i < ozfo.length; i++) {
            uint256 snap = vm.snapshotState();
            string memory swapped = _swap(ozfo[i], oamounts[i]);
            _line(string.concat(
                '{"kind":"overflow","pool":"', vm.toString(address(pair)),
                '","zero_for_one":', ozfo[i] ? "true" : "false",
                ',"amount_in":', _u(oamounts[i]), ",", swapped, "}"
            ));
            vm.revertToState(snap);
        }
        _line('{"kind":"end"}');
    }

    function _request(string memory caseId, bool zeroForOne, uint256 amount) internal {
        string memory head = string.concat('"case_id":"', caseId, '","pool":"', vm.toString(address(pair)), '"');
        (uint256 out, string memory swapped) = _swapWithOut(zeroForOne, amount);
        _line(string.concat(
            '{"kind":"swap",', head,
            ',"zero_for_one":', zeroForOne ? "true" : "false",
            ',"amount_in":', _u(amount), ",", swapped, "}"
        ));
        uint256 back = out / 2;
        if (back > 0) {
            (, string memory followed) = _swapWithOut(!zeroForOne, back);
            _line(string.concat(
                '{"kind":"followup",', head,
                ',"zero_for_one":', zeroForOne ? "false" : "true",
                ',"amount_in":', _u(back), ",", followed, "}"
            ));
        }
    }

    function _swap(bool zeroForOne, uint256 amount) internal returns (string memory) {
        (, string memory s) = _swapWithOut(zeroForOne, amount);
        return s;
    }

    /// Router quote, transfer, `quote + 1` probe, then the swap for `quote`. Returns the
    /// amount the recipient received (0 when the swap reverted) and the JSON fields.
    function _swapWithOut(bool zeroForOne, uint256 amount) internal returns (uint256 received, string memory s) {
        address tIn = zeroForOne ? pair.token0() : pair.token1();
        address tOut = zeroForOne ? pair.token1() : pair.token0();
        address[] memory path = new address[](2);
        (path[0], path[1]) = (tIn, tOut);
        uint256 quote;
        bool routerOk;
        try router.getAmountsOut(amount, path) returns (uint256[] memory amounts) {
            (quote, routerOk) = (amounts[1], true);
        } catch {}
        s = routerOk ? string.concat('"router_amount_out":', _u(quote)) : '"router_reverted":true';

        _deal(tIn, address(this), amount);
        uint256 pairBefore = IERC20(tIn).balanceOf(address(pair));
        require(IERC20(tIn).transfer(address(pair), amount), "transfer failed");
        s = string.concat(s, ',"credited":', _u(IERC20(tIn).balanceOf(address(pair)) - pairBefore));
        if (!routerOk) return (0, s);

        (uint256 plus0, uint256 plus1) = zeroForOne ? (uint256(0), quote + 1) : (quote + 1, uint256(0));
        try pair.swap(plus0, plus1, address(this), "") {
            s = string.concat(s, ',"plus_one":"accepted"');
        } catch Error(string memory reason) {
            s = string.concat(s, ',"plus_one":"', reason, '"');
        } catch {
            s = string.concat(s, ',"plus_one":"<no reason>"');
        }

        uint256 recvBefore = IERC20(tOut).balanceOf(address(this));
        (uint256 o0, uint256 o1) = zeroForOne ? (uint256(0), quote) : (quote, uint256(0));
        try pair.swap(o0, o1, address(this), "") {
            received = IERC20(tOut).balanceOf(address(this)) - recvBefore;
            s = string.concat(s, ',"executed":true,"received":', _u(received), ",", _reserves());
        } catch Error(string memory reason) {
            s = string.concat(s, ',"executed":false,"revert":"', reason, '"');
        } catch {
            s = string.concat(s, ',"executed":false,"revert":"<no reason>"');
        }
    }

    function _writePoolState() internal {
        address t0 = pair.token0();
        address t1 = pair.token1();
        _line(string.concat(
            '{"kind":"pool_state","pool":"', vm.toString(address(pair)),
            '","code_hash":"', vm.toString(address(pair).codehash),
            '","token0":"', vm.toString(t0),
            '","token1":"', vm.toString(t1),
            '","decimals0":', vm.toString(uint256(IERC20(t0).decimals())),
            ',"decimals1":', vm.toString(uint256(IERC20(t1).decimals())),
            ',"factory":"', vm.toString(pair.factory()),
            '","implementation":"', vm.toString(pair.implementation()),
            '",', _reserves(), "}"
        ));
    }

    function _reserves() internal view returns (string memory) {
        (uint112 r0, uint112 r1, uint32 ts) = pair.getReserves();
        return string.concat(
            '"reserve0":', _u(r0), ',"reserve1":', _u(r1),
            ',"balance0":', _u(IERC20(pair.token0()).balanceOf(address(pair))),
            ',"balance1":', _u(IERC20(pair.token1()).balanceOf(address(pair))),
            ',"block_timestamp_last":', _u(ts)
        );
    }

    // ---- misc (same balance-slot helper as CaptureUniswapV3Replay.t.sol) ----

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

    function _line(string memory s) internal {
        vm.writeLine(OUT_PATH, s);
    }

    function _u(uint256 v) internal pure returns (string memory) {
        return string.concat('"', vm.toString(v), '"');
    }
}
