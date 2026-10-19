// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

/// @notice Anchors the SHA-256 of a finalized LexChain document.
/// Only recorders the owner authorized can write, and a record is written
/// once: the normal path never overwrites it. A wrong anchor is fixed only by
/// the owner through `correctDocumentHash`, which keeps the previous hash in
/// its event so the correction itself is on the record.
contract LexChainNotary {
    struct DocumentRecords {
        string documentId;
        bytes32 dataHash;
        uint256 timestamp;
        address issuedBy;
    }

    address public owner;
    mapping(address => bool) public recorders;
    mapping(string => DocumentRecords) public documents;

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
    event RecorderSet(address indexed recorder, bool authorized);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error NotOwner();
    error NotRecorder();
    error ZeroAddress();
    error EmptyHash();
    error DocumentAlreadyRecorded(string documentId);
    error DocumentNotRecorded(string documentId);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyRecorder() {
        if (!recorders[msg.sender]) revert NotRecorder();
        _;
    }

    /// @dev The deployer owns the contract and is its first recorder, so the
    /// backend key that deploys it can anchor straight away.
    constructor() {
        owner = msg.sender;
        recorders[msg.sender] = true;
        emit OwnershipTransferred(address(0), msg.sender);
        emit RecorderSet(msg.sender, true);
    }

    function storeDocumentHash(
        string memory _documentId,
        bytes32 _dataHash
    ) public onlyRecorder {
        if (_dataHash == bytes32(0)) revert EmptyHash();
        if (documents[_documentId].timestamp != 0) {
            revert DocumentAlreadyRecorded(_documentId);
        }
        documents[_documentId] = DocumentRecords({
            documentId: _documentId,
            dataHash: _dataHash,
            timestamp: block.timestamp,
            issuedBy: msg.sender
        });
        emit DocumentRecordVerified(_documentId, _dataHash, block.timestamp);
    }

    /// @notice The explicit, owner-only way to replace an anchored hash.
    function correctDocumentHash(
        string memory _documentId,
        bytes32 _dataHash,
        string memory _reason
    ) public onlyOwner {
        if (_dataHash == bytes32(0)) revert EmptyHash();
        DocumentRecords storage record = documents[_documentId];
        if (record.timestamp == 0) revert DocumentNotRecorded(_documentId);

        bytes32 previousHash = record.dataHash;
        record.dataHash = _dataHash;
        record.timestamp = block.timestamp;
        record.issuedBy = msg.sender;
        emit DocumentRecordCorrected(
            _documentId,
            previousHash,
            _dataHash,
            _reason,
            block.timestamp
        );
    }

    function setRecorder(address _recorder, bool _authorized) public onlyOwner {
        if (_recorder == address(0)) revert ZeroAddress();
        recorders[_recorder] = _authorized;
        emit RecorderSet(_recorder, _authorized);
    }

    function transferOwnership(address _newOwner) public onlyOwner {
        if (_newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, _newOwner);
        owner = _newOwner;
    }

    //  if getting something in the storage "memory" is a must
    function getDocumentHash(
        string memory _documentId
    ) public view returns (bytes32, uint256, address) {
        DocumentRecords memory record = documents[_documentId];
        return (record.dataHash, record.timestamp, record.issuedBy);
    }
}
