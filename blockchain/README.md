## LexChainNotary — access control and deployment

`storeDocumentHash` is restricted to **recorders** the owner authorizes, and a
document id can be anchored **once**: a second write reverts with
`DocumentAlreadyRecorded`. The deployer is the owner and the first recorder.
A wrong anchor is fixed only by the owner with
`correctDocumentHash(id, hash, reason)`, which emits `DocumentRecordCorrected`
with the previous hash. `setRecorder` / `transferOwnership` are owner-only.

**Deployment impact.** A contract already on-chain keeps its old, open
`storeDocumentHash` — source changes cannot patch it. To get the protection:

1. Deploy the new contract **with the backend's anchoring key**
   (`CONTRACT_OWNER_PRIVATE_KEY`), so the backend is owner and recorder:
   `forge script script/LexChainNotary.s.sol --rpc-url <rpc> --private-key <key> --broadcast`
2. Point `CONTRACT_ADDRESS` at it and redeploy the backend (the ABI is read
   from `out/`, so run `forge build` wherever the backend reads it).
3. Documents anchored on the old contract are not on the new one, and verify
   reads only `CONTRACT_ADDRESS` — they would read as "no on-chain record".
   Either keep verifying them against the old address, or re-anchor their
   recorded hashes on the new contract (the new timestamp is the re-anchor
   time, not the original finalization). Decide before switching.

---

## Foundry

**Foundry is a blazing fast, portable and modular toolkit for Ethereum application development written in Rust.**

Foundry consists of:

- **Forge**: Ethereum testing framework (like Truffle, Hardhat and DappTools).
- **Cast**: Swiss army knife for interacting with EVM smart contracts, sending transactions and getting chain data.
- **Anvil**: Local Ethereum node, akin to Ganache, Hardhat Network.
- **Chisel**: Fast, utilitarian, and verbose solidity REPL.

## Documentation

https://book.getfoundry.sh/

## Usage

### Build

```shell
$ forge build
```

### Test

```shell
$ forge test
```

### Format

```shell
$ forge fmt
```

### Gas Snapshots

```shell
$ forge snapshot
```

### Anvil

```shell
$ anvil
```

### Deploy

```shell
$ forge script script/Counter.s.sol:CounterScript --rpc-url <your_rpc_url> --private-key <your_private_key>
```

### Cast

```shell
$ cast <subcommand>
```

### Help

```shell
$ forge --help
$ anvil --help
$ cast --help
```
