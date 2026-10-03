// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.26;

// Fixed-block token-semantics evidence for the WHI-1436 corpus universe
// (`tests/snapshot/test_corpus.py`).
//
// Runs inside `forge test` on a Mantle mainnet fork pinned at the corpus block (see
// capture_corpus_tokens.sh). For every universe token it takes the first candidate holder
// (an admitted corpus pool holding the token) with a balance of at least 10 raw units and,
// against the deployed token bytecode only:
//   - transfers a tenth of that balance from the holder to a fresh probe address, then
//     the probe's whole received balance back;
//   - records both balance deltas of both transfers and totalSupply before/after.
// A plain ERC-20 moves exactly the requested amount each way and leaves totalSupply
// unchanged; a fee-on-transfer, burning or rebasing-on-transfer token does not. The
// Python test asserts exactness; nothing here encodes the expected answer.

interface Vm {
    function prank(address) external;
    function writeFile(string calldata, string calldata) external;
    function writeLine(string calldata, string calldata) external;
    function readFile(string calldata) external view returns (string memory);
    function parseJsonUint(string calldata, string calldata) external pure returns (uint256);
    function parseJsonAddressArray(string calldata, string calldata) external pure returns (address[] memory);
    function toString(uint256) external pure returns (string memory);
    function toString(address) external pure returns (string memory);
    function toString(bytes32) external pure returns (string memory);
    function envOr(string calldata, string calldata) external view returns (string memory);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function totalSupply() external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function decimals() external view returns (uint8);
}

contract CaptureCorpusTokens {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    string internal constant IN_PATH = "requests/corpus_tokens.json";
    string internal constant OUT_PATH = "../../tests/fixtures/corpus/token_transfers.jsonl";
    address internal constant PROBE = address(uint160(uint256(keccak256("WHI-1436 token probe"))));

    function test_corpus_tokens() external {
        string memory json = vm.readFile(IN_PATH);
        require(vm.parseJsonUint(json, ".chain_id") == block.chainid, "fork chain id != request chain id");
        require(vm.parseJsonUint(json, ".block_number") == block.number, "fork block != corpus block");
        vm.writeFile(OUT_PATH, "");
        vm.writeLine(OUT_PATH, string.concat(
            '{"kind":"meta","schema":"corpus-token-transfers/1"',
            ',"generator":"tools/cl_evidence/test/CaptureCorpusTokens.t.sol (forge test on a Mantle fork)"',
            ',"chain_id":', vm.toString(block.chainid),
            ',"block_number":', vm.toString(block.number),
            ',"block_timestamp":', vm.toString(block.timestamp),
            ',"block_hash":"', vm.envOr("CL_FORK_BLOCK_HASH", string("")),
            '","probe":"', vm.toString(PROBE), '"}'
        ));
        address[] memory tokens = vm.parseJsonAddressArray(json, ".tokens");
        address[] memory candTokens = vm.parseJsonAddressArray(json, ".candidate_tokens");
        address[] memory candHolders = vm.parseJsonAddressArray(json, ".candidate_holders");
        for (uint256 i = 0; i < tokens.length; i++) {
            address holder = address(0);
            for (uint256 j = 0; j < candTokens.length; j++) {
                if (candTokens[j] == tokens[i] && IERC20(tokens[i]).balanceOf(candHolders[j]) >= 10) {
                    holder = candHolders[j];
                    break;
                }
            }
            require(holder != address(0), "no candidate holder with a balance");
            _probe(IERC20(tokens[i]), holder);
        }
        vm.writeLine(OUT_PATH, '{"kind":"end"}');
    }

    function _probe(IERC20 token, address holder) internal {
        uint256 supplyBefore = token.totalSupply();
        uint256 holderBefore = token.balanceOf(holder);
        uint256 probeBefore = token.balanceOf(PROBE);
        uint256 amount = holderBefore / 10;
        vm.prank(holder);
        require(token.transfer(PROBE, amount), "transfer out returned false");
        uint256 holderMid = token.balanceOf(holder);
        uint256 probeMid = token.balanceOf(PROBE);
        uint256 back = probeMid - probeBefore;
        vm.prank(PROBE);
        require(token.transfer(holder, back), "transfer back returned false");
        uint256 holderAfter = token.balanceOf(holder);
        uint256 probeAfter = token.balanceOf(PROBE);
        vm.writeLine(OUT_PATH, string.concat(
            '{"kind":"token","token":"', vm.toString(address(token)),
            '","code_hash":"', vm.toString(address(token).codehash),
            '","decimals":', vm.toString(uint256(token.decimals())),
            ',"holder":"', vm.toString(holder),
            '","amount":"', vm.toString(amount),
            '","holder_sent":"', vm.toString(holderBefore - holderMid),
            '","probe_received":"', vm.toString(probeMid - probeBefore),
            '","back_amount":"', vm.toString(back),
            '","probe_sent":"', vm.toString(probeMid - probeAfter),
            '","holder_received":"', vm.toString(holderAfter - holderMid),
            '","supply_before":"', vm.toString(supplyBefore),
            '","supply_after":"', vm.toString(token.totalSupply()), '"}'
        ));
    }
}
