// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {Test, console2} from "forge-std/Test.sol";
import {LexChainNotary} from "../src/LexChainNotary.sol";

/// @notice Cross-chain gas benchmark for the LexChain anchor/verify functions.
/// Each test forks one chain (via foundry.toml [rpc_endpoints], env-driven),
/// deploys the notary on that fork, and measures execution gas for a fixed,
/// representative input plus a documentId-length sweep. Gas is captured as a
/// `gasleft()` delta and emitted as parseable `GASLINE ...` records; run the
/// whole suite with `forge test --gas-report` for the standard table too.
///
/// Because Ethereum, Base and Polygon are EVM-equivalent, the execution gas is
/// identical across all three — the point of forking is to prove that against
/// each chain's *real* EVM config; cost differences come from the fee markets.
contract GasBenchmarkTest is Test {
    // Fixed representative inputs. dataHash is always bytes32 (constant size);
    // only the documentId length varies, which is the sole input-size lever.
    string constant ID_SHORT = "DOC-0001"; // 8 chars
    string constant ID_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"; // 36 chars (headline)
    string constant ID_LONG =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; // 64 chars

    /// @dev Cold first-write gas for one anchor. Uses a distinct documentId per
    /// call so every measurement hits fresh (zero->nonzero) storage slots, as a
    /// real standalone anchor tx would.
    function _measureAnchor(
        LexChainNotary notary,
        string memory chain,
        string memory documentId
    ) internal {
        bytes32 dataHash = keccak256(bytes(documentId));
        uint256 g0 = gasleft();
        notary.storeDocumentHash(documentId, dataHash);
        uint256 used = g0 - gasleft();
        console2.log(
            string.concat(
                "GASLINE chain=", chain,
                " fn=storeDocumentHash len=", vm.toString(bytes(documentId).length),
                " gas=", vm.toString(used)
            )
        );
    }

    function _measureVerify(
        LexChainNotary notary,
        string memory chain,
        string memory documentId
    ) internal {
        uint256 g0 = gasleft();
        notary.getDocumentHash(documentId);
        uint256 used = g0 - gasleft();
        console2.log(
            string.concat(
                "GASLINE chain=", chain,
                " fn=getDocumentHash len=", vm.toString(bytes(documentId).length),
                " gas=", vm.toString(used)
            )
        );
    }

    function _run(string memory chain) internal {
        vm.createSelectFork(vm.rpcUrl(chain));
        LexChainNotary notary = new LexChainNotary();
        // headline: 36-char UUID id, cold write, then verify the same id
        _measureAnchor(notary, chain, ID_UUID);
        _measureVerify(notary, chain, ID_UUID);
        // length sweep (distinct ids -> all cold) to expose O(L) growth
        _measureAnchor(notary, chain, ID_SHORT);
        _measureAnchor(notary, chain, ID_LONG);
    }

    function test_gas_ethereum() public {
        _run("ethereum");
    }

    function test_gas_base() public {
        _run("base");
    }

    function test_gas_polygon() public {
        _run("polygon");
    }
}

/// @notice Local precision benchmark (no fork, no RPC): exact per-call gas via
/// gasleft() deltas for (a) deployment, (b) fresh-id anchor (cold storage),
/// (c) same-id overwrite (the contract permits it -- plain mapping assignment,
/// no guard), and (d) every other external/public state-changing function
/// (there are none besides storeDocumentHash; getDocumentHash and the
/// auto-generated `documents` getter are view).
/// Also reconstructs the full anchor TRANSACTION gas
/// (21000 intrinsic + exact calldata gas + execution) to compare against the
/// thesis figure of 160,575.
contract GasBenchmarkLocalTest is Test {
    string constant ID_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"; // thesis headline input
    uint256 constant THESIS_ANCHOR_TX_GAS = 160575;

    /// @dev EVM calldata pricing: 4 gas per zero byte, 16 per non-zero byte.
    function _calldataGas(bytes memory data) internal pure returns (uint256 g) {
        for (uint256 i = 0; i < data.length; i++) {
            g += data[i] == 0 ? 4 : 16;
        }
    }

    function _row(string memory label, uint256 gas_) internal pure {
        console2.log(string.concat("GASROW ", label, " = ", vm.toString(gas_)));
    }

    function test_gas_local_precision() public {
        // (a) deployment (creation execution incl. code deposit; a real deploy
        // tx additionally pays 21000 intrinsic + calldata for the initcode)
        uint256 g0 = gasleft();
        LexChainNotary notary = new LexChainNotary();
        _row("deployment", g0 - gasleft());

        bytes32 dataHash = keccak256(bytes(ID_UUID));

        // (b) fresh document id: first write -> cold zero->nonzero SSTOREs
        g0 = gasleft();
        notary.storeDocumentHash(ID_UUID, dataHash);
        uint256 freshGas = g0 - gasleft();
        _row("storeDocumentHash_fresh", freshGas);

        // (c) same id again: overwrite is PERMITTED (no revert); warm nonzero
        // slots make it much cheaper
        g0 = gasleft();
        notary.storeDocumentHash(ID_UUID, dataHash);
        uint256 overwriteGas = g0 - gasleft();
        _row("storeDocumentHash_overwrite", overwriteGas);
        assertLt(overwriteGas, freshGas, "overwrite should be cheaper than fresh write");

        // (d) no other state-changing functions exist; measure the two views
        // for completeness (0 gas when called off-chain via eth_call)
        g0 = gasleft();
        notary.getDocumentHash(ID_UUID);
        _row("getDocumentHash_view", g0 - gasleft());
        g0 = gasleft();
        notary.documents(ID_UUID);
        _row("documents_getter_view", g0 - gasleft());

        // full anchor-tx reconstruction vs thesis figure
        uint256 cdGas = _calldataGas(
            abi.encodeCall(notary.storeDocumentHash, (ID_UUID, dataHash))
        );
        uint256 totalTx = 21000 + cdGas + freshGas;
        _row("anchor_calldata", cdGas);
        _row("anchor_tx_total", totalTx);
        console2.log(
            string.concat(
                "GASROW thesis_delta = ",
                totalTx >= THESIS_ANCHOR_TX_GAS
                    ? vm.toString(totalTx - THESIS_ANCHOR_TX_GAS)
                    : string.concat("-", vm.toString(THESIS_ANCHOR_TX_GAS - totalTx))
            )
        );
    }
}
