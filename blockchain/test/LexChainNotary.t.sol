// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {Test} from "forge-std/Test.sol";
import {LexChainNotary} from "../src/LexChainNotary.sol";

contract LexChainNotaryTest is Test {
    LexChainNotary public lexChainNotary;

    address stranger = makeAddr("stranger");
    address recorder = makeAddr("recorder");

    event DocumentRecordVerified(
        string documentId,
        bytes32 dataHash,
        uint256 timestamp
    );
    event DocumentRecordCorrected(
        string documentId,
        bytes32 previousHash,
        bytes32 dataHash,
        string reason,
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
        vm.assume(_dataHash != bytes32(0));
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

    // === Who may write ===

    function test_UnauthorizedCallerCannotStore() public {
        vm.prank(stranger);
        vm.expectRevert(LexChainNotary.NotRecorder.selector);
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("x"));
    }

    function test_UnauthorizedCallerCannotReplaceARecord() public {
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("real"));

        vm.prank(stranger);
        vm.expectRevert(LexChainNotary.NotRecorder.selector);
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("forged"));

        (bytes32 dataHash, , ) = lexChainNotary.getDocumentHash("DOC-1");
        assertEq(dataHash, keccak256("real"));
    }

    function test_OwnerAuthorizesAndRevokesRecorders() public {
        lexChainNotary.setRecorder(recorder, true);
        vm.prank(recorder);
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("x"));
        (, , address issuedBy) = lexChainNotary.getDocumentHash("DOC-1");
        assertEq(issuedBy, recorder);

        lexChainNotary.setRecorder(recorder, false);
        vm.prank(recorder);
        vm.expectRevert(LexChainNotary.NotRecorder.selector);
        lexChainNotary.storeDocumentHash("DOC-2", keccak256("y"));
    }

    function test_OnlyOwnerManagesRecorders() public {
        vm.prank(stranger);
        vm.expectRevert(LexChainNotary.NotOwner.selector);
        lexChainNotary.setRecorder(stranger, true);
    }

    function test_OwnershipTransfer() public {
        lexChainNotary.transferOwnership(stranger);
        assertEq(lexChainNotary.owner(), stranger);

        vm.expectRevert(LexChainNotary.NotOwner.selector);
        lexChainNotary.setRecorder(recorder, true);
    }

    // === Write once ===

    function test_DuplicateIdIsRefused() public {
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("first"));

        vm.expectRevert(
            abi.encodeWithSelector(
                LexChainNotary.DocumentAlreadyRecorded.selector,
                "DOC-1"
            )
        );
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("second"));

        (bytes32 dataHash, , ) = lexChainNotary.getDocumentHash("DOC-1");
        assertEq(dataHash, keccak256("first"));
    }

    function test_EmptyHashIsRefused() public {
        vm.expectRevert(LexChainNotary.EmptyHash.selector);
        lexChainNotary.storeDocumentHash("DOC-1", bytes32(0));
    }

    // === Correction ===

    function test_OwnerCorrectsARecordOnTheRecord() public {
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("wrong"));
        vm.warp(block.timestamp + 1 days);

        vm.expectEmit(false, false, false, true);
        emit DocumentRecordCorrected(
            "DOC-1",
            keccak256("wrong"),
            keccak256("right"),
            "wrong file anchored",
            block.timestamp
        );
        lexChainNotary.correctDocumentHash(
            "DOC-1",
            keccak256("right"),
            "wrong file anchored"
        );

        (bytes32 dataHash, uint256 timestamp, ) = lexChainNotary
            .getDocumentHash("DOC-1");
        assertEq(dataHash, keccak256("right"));
        assertEq(timestamp, block.timestamp);
    }

    function test_RecorderCannotCorrect() public {
        lexChainNotary.setRecorder(recorder, true);
        lexChainNotary.storeDocumentHash("DOC-1", keccak256("x"));

        vm.prank(recorder);
        vm.expectRevert(LexChainNotary.NotOwner.selector);
        lexChainNotary.correctDocumentHash("DOC-1", keccak256("y"), "no");
    }

    function test_CorrectingAMissingRecordIsRefused() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                LexChainNotary.DocumentNotRecorded.selector,
                "DOC-404"
            )
        );
        lexChainNotary.correctDocumentHash("DOC-404", keccak256("x"), "none");
    }
}
