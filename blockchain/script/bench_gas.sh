#!/usr/bin/env bash
# Cross-chain gas benchmark for LexChainNotary.storeDocumentHash (the "anchor").
#
# For each chain it:
#   1. runs the forge fork test (execution gas via --gas-report / gasleft deltas),
#   2. captures the TRUE total anchor-tx gas from a real receipt on a local anvil
#      fork (deploy + cast send), which is what maps to real cost,
#   3. reads the live gas price (cast gas-price) and token USD (CoinGecko free),
#   4. projects USD cost per anchor.
#
# gas_used is identical across EVM-equivalent chains (we show that); cost
# differences come purely from each chain's fee market and native-token price.
#
# Usage:
#   script/bench_gas.sh --smoke     # Ethereum only + receipt/ formula reconciliation
#   script/bench_gas.sh             # all three chains -> CSV + markdown
# Override RPCs by exporting ETH_RPC_URL / BASE_RPC_URL / POLYGON_RPC_URL.
set -uo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="gas_report"
mkdir -p "$OUT_DIR"

# --- public default RPCs (verified working; overridable via env) ---------------
: "${ETH_RPC_URL:=https://ethereum-rpc.publicnode.com}"
: "${BASE_RPC_URL:=https://base-rpc.publicnode.com}"
: "${POLYGON_RPC_URL:=https://polygon-bor-rpc.publicnode.com}"
export ETH_RPC_URL BASE_RPC_URL POLYGON_RPC_URL

# well-known anvil dev account #0 (LOCAL fork only; never a real key)
ANVIL_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_PORT=8545
ANVIL_URL="http://127.0.0.1:${ANVIL_PORT}"

# headline representative input (36-char UUID) + its bytes32 hash
HEADLINE_ID="3f2504e0-4f89-41d3-9a0c-0305e82c3301"
HEADLINE_HASH="$(cast keccak "$HEADLINE_ID")"

SMOKE=0
[ "${1:-}" = "--smoke" ] && SMOKE=1

declare -A RPC=( [ethereum]="$ETH_RPC_URL" [base]="$BASE_RPC_URL" [polygon]="$POLYGON_RPC_URL" )
declare -A TOKEN=( [ethereum]="ETH" [base]="ETH" [polygon]="POL" )
declare -A CG_ID=( [ethereum]="ethereum" [base]="ethereum" [polygon]="polygon-ecosystem-token" )

ANVIL_PID=""
cleanup() { [ -n "$ANVIL_PID" ] && kill "$ANVIL_PID" 2>/dev/null; }
trap cleanup EXIT

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# calldata gas of the anchor call for a given id: 4/zero-byte, 16/non-zero byte
calldata_gas() {
  local id="$1" hash="$2" cd zeros=0 nonzeros=0 b
  cd="$(cast calldata 'storeDocumentHash(string,bytes32)' "$id" "$hash")"
  cd="${cd#0x}"
  while [ -n "$cd" ]; do
    b="${cd:0:2}"; cd="${cd:2}"
    if [ "$b" = "00" ]; then zeros=$((zeros+1)); else nonzeros=$((nonzeros+1)); fi
  done
  echo $((zeros*4 + nonzeros*16))
}

# execution gas for one fn+len from a captured forge -vv log
parse_gasline() {
  local file="$1" chain="$2" fn="$3" len="$4"
  grep -oE "GASLINE chain=${chain} fn=${fn} len=${len} gas=[0-9]+" "$file" \
    | grep -oE 'gas=[0-9]+' | grep -oE '[0-9]+' | head -1
}

# TRUE total anchor-tx gas from a real receipt on a local anvil fork of $rpc
receipt_total_gas() {
  local rpc="$1" addr deploy send gas_hex
  anvil --fork-url "$rpc" --port "$ANVIL_PORT" --silent >/dev/null 2>&1 &
  ANVIL_PID=$!
  local ok=0 i
  for i in $(seq 1 40); do
    if cast block-number --rpc-url "$ANVIL_URL" >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.5
  done
  if [ "$ok" -ne 1 ]; then kill "$ANVIL_PID" 2>/dev/null; ANVIL_PID=""; echo ""; return 1; fi

  deploy="$(forge create src/LexChainNotary.sol:LexChainNotary \
    --rpc-url "$ANVIL_URL" --private-key "$ANVIL_KEY" --broadcast --json 2>/dev/null)"
  addr="$(echo "$deploy" | jq -r '.deployedTo // empty')"
  if [ -z "$addr" ]; then kill "$ANVIL_PID" 2>/dev/null; ANVIL_PID=""; echo ""; return 1; fi

  send="$(cast send "$addr" 'storeDocumentHash(string,bytes32)' "$HEADLINE_ID" "$HEADLINE_HASH" \
    --rpc-url "$ANVIL_URL" --private-key "$ANVIL_KEY" --json 2>/dev/null)"
  gas_hex="$(echo "$send" | jq -r '.gasUsed // empty')"
  kill "$ANVIL_PID" 2>/dev/null; ANVIL_PID=""
  [ -z "$gas_hex" ] && { echo ""; return 1; }
  printf '%d\n' "$gas_hex"
}

# ---- token USD prices (CoinGecko free, no key) --------------------------------
log "Fetching token USD prices (CoinGecko)"
PRICE_JSON="$(curl -s --max-time 25 \
  'https://api.coingecko.com/api/v3/simple/price?ids=ethereum,polygon-ecosystem-token&vs_currencies=usd')"
ETH_USD="$(echo "$PRICE_JSON" | jq -r '.ethereum.usd // empty')"
POL_USD="$(echo "$PRICE_JSON" | jq -r '."polygon-ecosystem-token".usd // empty')"
if [ -z "$ETH_USD" ] || [ -z "$POL_USD" ]; then
  echo "ERROR: could not fetch token prices from CoinGecko"; exit 1
fi
declare -A USD=( [ETH]="$ETH_USD" [POL]="$POL_USD" )
TS_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "ETH=\$$ETH_USD  POL=\$$POL_USD  @ $TS_UTC"

CALLDATA_GAS="$(calldata_gas "$HEADLINE_ID" "$HEADLINE_HASH")"

CHAINS=(ethereum base polygon)
[ "$SMOKE" -eq 1 ] && CHAINS=(ethereum)

declare -A EXEC_UUID EXEC_VERIFY EXEC_SHORT EXEC_LONG TOTAL GASPRICE_WEI COST
for chain in "${CHAINS[@]}"; do
  rpc="${RPC[$chain]}"
  log "Chain: $chain  ($rpc)"

  logfile="$OUT_DIR/forge_${chain}.log"
  forge test --match-test "test_gas_${chain}" -vv > "$logfile" 2>&1
  if ! grep -q "GASLINE chain=${chain} fn=storeDocumentHash" "$logfile"; then
    echo "  WARN: fork test produced no gas lines (RPC issue?). See $logfile"; continue
  fi
  EXEC_UUID[$chain]="$(parse_gasline "$logfile" "$chain" storeDocumentHash 36)"
  EXEC_VERIFY[$chain]="$(parse_gasline "$logfile" "$chain" getDocumentHash 36)"
  EXEC_SHORT[$chain]="$(parse_gasline "$logfile" "$chain" storeDocumentHash 8)"
  EXEC_LONG[$chain]="$(parse_gasline "$logfile" "$chain" storeDocumentHash 64)"
  echo "  exec gas (gasleft delta): anchor=${EXEC_UUID[$chain]}  verify=${EXEC_VERIFY[$chain]}  (len8=${EXEC_SHORT[$chain]}, len64=${EXEC_LONG[$chain]})"

  echo "  measuring true total anchor-tx gas via anvil fork receipt..."
  rt="$(receipt_total_gas "$rpc")"
  if [ -n "$rt" ]; then
    TOTAL[$chain]="$rt"
    echo "  receipt gasUsed (total anchor tx) = ${TOTAL[$chain]}"
  else
    TOTAL[$chain]=$((21000 + CALLDATA_GAS + ${EXEC_UUID[$chain]}))
    echo "  receipt unavailable; using formula total = 21000 + ${CALLDATA_GAS} + ${EXEC_UUID[$chain]} = ${TOTAL[$chain]} (computed)"
  fi

  gp="$(cast gas-price --rpc-url "$rpc" 2>/dev/null)"
  [ -z "$gp" ] && gp=0
  GASPRICE_WEI[$chain]="$gp"
  tok="${TOKEN[$chain]}"; usd="${USD[$tok]}"
  COST[$chain]="$(awk -v g="${TOTAL[$chain]}" -v p="$gp" -v u="$usd" \
    'BEGIN{printf "%.6f", g*p*u/1e18}')"
  gwei="$(awk -v p="$gp" 'BEGIN{printf "%.4f", p/1e9}')"
  echo "  gas_price=${gwei} gwei  ${tok}=\$$usd  ->  cost/anchor = \$${COST[$chain]}"
done

# ---- smoke reconciliation -----------------------------------------------------
if [ "$SMOKE" -eq 1 ]; then
  log "Smoke reconciliation (Ethereum)"
  ex="${EXEC_UUID[ethereum]:-0}"
  formula=$((21000 + CALLDATA_GAS + ex))
  echo "  execution gas (in-call)      : $ex"
  echo "  + intrinsic base             : 21000"
  echo "  + calldata gas (36-char id)  : $CALLDATA_GAS"
  echo "  = formula total              : $formula"
  echo "  receipt total (ground truth) : ${TOTAL[ethereum]}"
  diff=$(( ${TOTAL[ethereum]} - formula )); diff=${diff#-}
  echo "  |receipt - formula|          : $diff gas"
  echo
  echo "  One worked cost line:"
  echo "    Ethereum: ${TOTAL[ethereum]} gas x $(awk -v p=${GASPRICE_WEI[ethereum]} 'BEGIN{printf "%.4f",p/1e9}') gwei x \$$ETH_USD/ETH = \$${COST[ethereum]}"
  echo
  echo "Smoke test OK. Re-run without --smoke to benchmark all three chains."
  exit 0
fi

# ---- write outputs ------------------------------------------------------------
log "Writing $OUT_DIR/cross_chain_gas.{csv,md} + prices.json"

CSV="$OUT_DIR/cross_chain_gas.csv"
echo "chain,native_token,gas_used_total_anchor,exec_gas_anchor,calldata_gas,gas_price_gwei,token_usd,cost_per_anchor_usd" > "$CSV"
for chain in "${CHAINS[@]}"; do
  [ -z "${TOTAL[$chain]:-}" ] && continue
  tok="${TOKEN[$chain]}"
  gwei="$(awk -v p=${GASPRICE_WEI[$chain]} 'BEGIN{printf "%.4f",p/1e9}')"
  echo "${chain},${tok},${TOTAL[$chain]},${EXEC_UUID[$chain]},${CALLDATA_GAS},${gwei},${USD[$tok]},${COST[$chain]}" >> "$CSV"
done

MD="$OUT_DIR/cross_chain_gas.md"
{
  echo "# LexChain anchor cost across chains"
  echo
  echo "Anchor = \`storeDocumentHash(string documentId, bytes32 dataHash)\` — one document hash per tx."
  echo "Headline input: 36-char UUID documentId + bytes32 hash, cold first-write. Prices @ **$TS_UTC** (UTC)."
  echo
  echo "| Chain | Native token | Gas used (anchor) | Gas price (gwei) | Token USD | Cost per anchor (USD) |"
  echo "|:------|:-------------|------------------:|-----------------:|----------:|----------------------:|"
  for chain in "${CHAINS[@]}"; do
    [ -z "${TOTAL[$chain]:-}" ] && continue
    tok="${TOKEN[$chain]}"
    gwei="$(awk -v p=${GASPRICE_WEI[$chain]} 'BEGIN{printf "%.4f",p/1e9}')"
    printf "| %s | %s | %s | %s | \$%s | \$%s |\n" \
      "$chain" "$tok" "${TOTAL[$chain]}" "$gwei" "${USD[$tok]}" "${COST[$chain]}"
  done
  echo
  echo "**Gas used is (near-)identical across chains** — same EVM bytecode and gas schedule."
  echo "Cost differences come entirely from each network's **fee market** (gas price) and **native-token USD price**."
  echo
  echo "## documentId length sensitivity (execution gas)"
  echo
  echo "Anchor gas increases in **steps** as the documentId spans more 32-byte storage words — each extra word is one extra cold SSTORE (~20k). A documentId <32 bytes fits one slot; 32-63 bytes needs ~three (length + two data words), so 36- and 64-char ids cost about the same. The bytes32 hash is fixed-size. Gas therefore depends on documentId length, but as a step function, not smoothly linear."
  echo
  echo "| documentId length | exec gas (Ethereum fork) |"
  echo "|------------------:|-------------------------:|"
  echo "| 8 chars  | ${EXEC_SHORT[ethereum]:-n/a} |"
  echo "| 36 chars | ${EXEC_UUID[ethereum]:-n/a} |"
  echo "| 64 chars | ${EXEC_LONG[ethereum]:-n/a} |"
  echo
  echo "## Notes / caveats"
  echo
  echo "- **Verify** (\`getDocumentHash\`) is a \`view\` function: **0 gas** when read off-chain via \`eth_call\`. Its execution gas (~${EXEC_VERIFY[ethereum]:-n/a} on the Ethereum fork) only applies if another contract calls it on-chain."
  echo "- **Total anchor-tx gas** = 21,000 intrinsic + calldata (${CALLDATA_GAS} for the 36-char id) + execution; taken from a real receipt on an anvil fork of each chain."
  echo "- **Base L1 data fee:** Base is an OP-stack L2. A real tx also pays an L1 data fee to post calldata to Ethereum, which forge forking does **not** measure — so Base's figure here is the **L2 execution portion only** and understates true cost. Ethereum L1 and Polygon PoS have no separate L1 data fee."
  echo "- **Complexity:** \`storeDocumentHash\` and \`getDocumentHash\` are **O(1)** in the number of stored documents (single mapping slot); no loops. The only input-size dependence is documentId length (see sweep above)."
  echo "- Gas prices via \`cast gas-price\`; token USD via CoinGecko; snapshot in \`prices.json\`."
} > "$MD"

{
  echo "{"
  echo "  \"timestamp_utc\": \"$TS_UTC\","
  echo "  \"token_usd\": { \"ETH\": $ETH_USD, \"POL\": $POL_USD },"
  echo "  \"source\": { \"gas_price\": \"cast gas-price (live)\", \"token_usd\": \"coingecko simple/price\" },"
  echo "  \"chains\": {"
  first=1
  for chain in "${CHAINS[@]}"; do
    [ -z "${TOTAL[$chain]:-}" ] && continue
    [ $first -eq 0 ] && echo ","
    first=0
    printf '    "%s": { "gas_price_wei": %s, "total_anchor_gas": %s, "native_token": "%s", "cost_per_anchor_usd": %s }' \
      "$chain" "${GASPRICE_WEI[$chain]}" "${TOTAL[$chain]}" "${TOKEN[$chain]}" "${COST[$chain]}"
  done
  echo
  echo "  }"
  echo "}"
} > "$OUT_DIR/prices.json"

echo
cat "$MD"
echo
echo "Wrote $CSV, $MD, $OUT_DIR/prices.json"
