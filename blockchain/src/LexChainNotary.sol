// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

contract LexChainNotary {
    struct DocumentRecords {
        string documentId;
        bytes32 dataHash;
        uint256 timestamp;
        address issuedBy;
    }

    mapping(string => DocumentRecords) public documents;

    event DocumentRecordVerified(
        string documentId,
        bytes32 dataHash,
        uint256 timestamp
    );

    function storeDocumentHash(
        string memory _documentId,
        bytes32 _dataHash
    ) public {
        documents[_documentId] = DocumentRecords({
            documentId: _documentId,
            dataHash: _dataHash,
            timestamp: block.timestamp,
            issuedBy: msg.sender
        });
        emit DocumentRecordVerified(_documentId, _dataHash, block.timestamp);
    }

    //  if getting something in the storage "memory" is a must
    function getDocumentHash(
        string memory _documentId
    ) public view returns (bytes32, uint256, address) {
        DocumentRecords memory record = documents[_documentId];
        return (record.dataHash, record.timestamp, record.issuedBy);
    }
}
