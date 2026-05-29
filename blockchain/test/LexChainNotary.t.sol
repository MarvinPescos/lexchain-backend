// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {Test} from "forge-std/Test.sol";
import {LexChainNotary} from "../src/LexChainNotary.sol";

contract LexChainNotaryTest is Test {
    LexChainNotary public lexChainNotary;

    event DocumentRecordVerified(
        string documentId,
        bytes32 dataHash,
        uint256 timestamp
    );

    function setUp() public {
        lexChainNotary = new LexChainNotary();
    }

    function test_StoreDocumentHash() public {
        string memory documentId = "DOC-123";
        bytes32 dataHash = keccak256(abi.encodePacked("sample_document_data"));

        vm.expectEmit(false, false, false, true);
        emit DocumentRecordVerified(documentId, dataHash, block.timestamp);

        //Store the document
        lexChainNotary.storeDocumentHash(documentId, dataHash);

        // Retrive the docuement
        (
            bytes32 retrievedHash,
            uint256 timestamp,
            address issuedBy
        ) = lexChainNotary.getDocumentHash(documentId);

        assertEq(retrievedHash, dataHash, "Hashes do not match");
        assertEq(timestamp, block.timestamp, "Timestamp does not match");
        assertEq(issuedBy, address(this), "Issuer should be the test contract");
    }

    function testFuzz_StoreDocument(
        string memory _documentId,
        bytes32 _dataHash
    ) public {
        lexChainNotary.storeDocumentHash(_documentId, _dataHash);

        (
            bytes32 retrievedHash,
            uint256 timestamp,
            address issuedBy
        ) = lexChainNotary.getDocumentHash(_documentId);

        assertEq(retrievedHash, _dataHash);
        assertEq(timestamp, block.timestamp);
        assertEq(issuedBy, address(this));
    }
}
