# LexChain anchor cost across chains

Anchor = `storeDocumentHash(string documentId, bytes32 dataHash)` — one document hash per tx.
Headline input: 36-char UUID documentId + bytes32 hash, cold first-write. Prices @ **2026-07-19T13:13:23Z** (UTC).

| Chain | Native token | Gas used (anchor) | Gas price (gwei) | Token USD | Cost per anchor (USD) |
|:------|:-------------|------------------:|-----------------:|----------:|----------------------:|
| ethereum | ETH | 160575 | 0.0407 | $1865.67 | $0.012188 |
| base | ETH | 160575 | 0.0060 | $1865.67 | $0.001797 |
| polygon | POL | 160575 | 280.0973 | $0.080647 | $0.003627 |

**Gas used is (near-)identical across chains** — same EVM bytecode and gas schedule.
Cost differences come entirely from each network's **fee market** (gas price) and **native-token USD price**.

## documentId length sensitivity (execution gas)

Anchor gas increases in **steps** as the documentId spans more 32-byte storage words — each extra word is one extra cold SSTORE (~20k). A documentId <32 bytes fits one slot; 32-63 bytes needs ~three (length + two data words), so 36- and 64-char ids cost about the same. The bytes32 hash is fixed-size. Gas therefore depends on documentId length, but as a step function, not smoothly linear.

| documentId length | exec gas (Ethereum fork) |
|------------------:|-------------------------:|
| 8 chars  | 94198 |
| 36 chars | 138864 |
| 64 chars | 138808 |

## Notes / caveats

- **Verify** (`getDocumentHash`) is a `view` function: **0 gas** when read off-chain via `eth_call`. Its execution gas (~4545 on the Ethereum fork) only applies if another contract calls it on-chain.
- **Total anchor-tx gas** = 21,000 intrinsic + calldata (1544 for the 36-char id) + execution; taken from a real receipt on an anvil fork of each chain.
- **Base L1 data fee:** Base is an OP-stack L2. A real tx also pays an L1 data fee to post calldata to Ethereum, which forge forking does **not** measure — so Base's figure here is the **L2 execution portion only** and understates true cost. Ethereum L1 and Polygon PoS have no separate L1 data fee.
- **Complexity:** `storeDocumentHash` and `getDocumentHash` are **O(1)** in the number of stored documents (single mapping slot); no loops. The only input-size dependence is documentId length (see sweep above).
- Gas prices via `cast gas-price`; token USD via CoinGecko; snapshot in `prices.json`.
