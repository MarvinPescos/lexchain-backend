# Thesis gas-benchmark verification — LexChainNotary

Verified 2026-07-21 with Foundry (forge 1.7.1), dedicated precision test
`test/GasBenchmark.t.sol:GasBenchmarkLocalTest` (exact `gasleft()` deltas)
cross-checked against `forge test --gas-report`.

`getDocumentHash` confirmed declared `view` (src/LexChainNotary.sol:34).
`storeDocumentHash` is the ONLY external/public state-changing function;
overwriting an existing documentId is permitted (plain mapping assignment, no guard).

## Gas per function

| Function | Gas | Notes | Frequency in LexChain |
|:--|--:|:--|:--|
| deployment | 659,750 | one-time; tx-equivalent (gas-report). In-call creation execution: 595,092 | once, per chain |
| `storeDocumentHash` (fresh id) | **160,575** | cold first write, 36-char UUID id; tx-equivalent = 21,000 intrinsic + 1,544 calldata + execution. Execution-only (`gasleft()` delta): 138,918 | **core write path — once per document anchored** |
| `storeDocumentHash` (same id again) | 41,187 | overwrite is permitted (does NOT revert); warm/nonzero slots. tx-equivalent (gas-report Min); execution-only: 7,130 | rare — re-anchoring / correcting a document |
| `getDocumentHash` | 0 (off-chain) | `view`; free via `eth_call`. In-call cost if another contract calls it: 4,650 | every verification — free |
| `documents(string)` getter | 0 (off-chain) | auto-generated `view` getter. In-call: 5,311 | rarely used directly |
| *(other state-changing functions)* | — | none exist | — |

## Thesis figure check: 160,575

**CONFIRMED — no change needed.** `forge test --gas-report` reports the fresh-write
`storeDocumentHash` at exactly **160,575** (Max column, the fresh-id call), matching
the thesis figure (which came from a real anvil-fork receipt).

The dedicated test's arithmetic reconstruction (21,000 + 1,544 calldata + 138,918
execution = 161,462) lands **+887 gas (+0.55%)** above it — expected: the in-test
`gasleft()` delta includes internal CALL overhead that a top-level transaction does
not pay. Within normal variance; the receipt-backed 160,575 is the correct figure.

## Live gas prices (for the USD refresh)

Fetched via `cast gas-price`, per-chain timestamps below (UTC). USD deliberately
not computed here — take ETH/POL prices from CoinGecko at write-up time and record
source + timestamp in the table caption.

| Chain | Gas price (gwei) | Timestamp (UTC) | RPC |
|:--|--:|:--|:--|
| Base | 0.0060 | 2026-07-21T05:54:50Z | https://mainnet.base.org (as requested) |
| Ethereum | 0.0569 | 2026-07-21T05:55:05Z | https://ethereum-rpc.publicnode.com — **fallback**; requested https://eth.llamarpc.com was down (HTTP 521) |
| Polygon | 276.7982 | 2026-07-21T05:55:06Z | https://polygon-bor-rpc.publicnode.com — **fallback**; requested https://polygon-rpc.com rejected requests (HTTP 401, "API key disabled") |
