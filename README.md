# TerraLedger

AI-gated carbon credit platform on QIE Blockchain (chain 1990).

The core principle: instead of tokenizing first and verifying never (the Toucan/KlimaDAO failure mode), the AI check IS the gate. No pass = no tokens minted.

Live on QIE Mainnet — dual-token architecture: TLCERT soulbound verification NFT + TCC ERC-20 fungible carbon credits.

---

## How it works

```
submit → 5 AI modules → score ≥ 70?
              ↓ yes                      ↓ no / hard-fail
  oracle mints TLCERT (soulbound)    blocked + logged on-chain
            + TCC tokens (ERC-20)
              ↓
      developer lists TCC on marketplace
      (every listing stores projectId on-chain)
              ↓
      any holder retires TCC via retire()
      → TCC burns permanently
      → TLRET soulbound cert auto-mints to retiree
      → PDF certificate downloaded instantly
              ↓
      when all project TCC retired:
      TLCERT auto-shows "Fully Offset"
      (no button, no admin — tracked per project ID on-chain)
```

Two checks are hard gates regardless of score:
- GPS polygon overlaps an existing registered project by >20%
- Deed GPS coordinates don't match claimed project coordinates

---

### The 5 AI modules

| Module | What | Weight |
|---|---|---|
| GPS overlap | Shapely polygon vs all registered projects | 30 |
| Ownership | OCR + NLP on deed PDF, ELA forgery detection, SHA-256 stored on-chain | 25 |
| Anomaly | IsolationForest trained on Verra registry data — flags abnormal tonnes/ha | 25 |
| Satellite | Real NASA FIRMS fire/deforestation history for the GPS coords (90-day window) | 20 |
| Audit | Llama-3 writes plain-English audit, score breakdown on-chain | — |

---

## Problems we solved

### 1. Fungibility trap — buyers couldn't track which project their TCC came from
TCC is an ERC-20. Once traded, a buyer holding TCC had no way to know which verified project the tokens came from. If they retired against the wrong project ID, the TLCERT would never show offset.

**Fix:** TCCMarketplace v2 stores `bytes32 projectId` in every listing. `getActiveListings()` returns projectIds. The marketplace UI passes the correct projectId to `retire()` automatically when a buyer purchases and retires credits.

### 2. TLCERT stuck "ACTIVE" after all TCC were burned
The original retire flow passed `projectIdZero` (bytes32 0x000...0) to `retiredByProject` mapping, so the per-project counter never reached `issuedByProject`. The cert stayed "ACTIVE" even after every token was gone.

**Fix:** Per-project tracking via `issuedByProject[bytes32]` and `retiredByProject[bytes32]` on CarbonCreditToken. Frontend uses `isFullyRetired(projectId)` with a totalSupply fallback: if `issued > 0 && totalSupply == 0`, the project is fully offset regardless.

### 3. Retirement certificate only existed for the old NFT flow
Before, a PDF certificate downloaded only when retiring the legacy ERC-721 credit NFT. TCC retire() had no certificate.

**Fix:** TLRET (RetirementCertificate) — soulbound ERC-721 that auto-mints on every `retire()` call from CarbonCreditToken. Backend `/retirement-certificate` generates a PDF with retiree name, organisation, amount, projectId and tx hash. Downloads immediately after the on-chain tx confirms.

### 4. retire() failing — gas limit too low
`retire()` burns TCC then mints a TLRET soulbound cert. Combined gas was ~300k+. Default 150k limit caused every retire to fail.

**Fix:** Gas limit bumped to 450,000 for retire calls.

### 5. Manual Retire button on soulbound TLCERT — confusing UX
TLCERT is soulbound and non-transferable. Showing a Retire button on it made no sense. Users were unclear whether retiring the cert or the tokens did the offset.

**Fix:** TLCERT cards now show "Retire TCC →" which opens the TCC retire modal pre-filled with the correct projectId. Soulbound status on the card is clear.

### 6. Wallet connect stuck "Connecting..." forever
`wallet_requestPermissions` on QIE Wallet would hang indefinitely if the user dismissed the popup or the wallet was slow.

**Fix:** `Promise.race` with a 4-second timeout — if permissions don't resolve, the flow falls through to `eth_requestAccounts` directly.

### 7. Wallet auto-reconnecting after explicit disconnect
The `accountsChanged` handler called `location.reload()` on empty accounts, which re-triggered auto-connect. `disconnectWallet()` also didn't actually revoke site permissions.

**Fix:** `wallet_revokePermissions` call in `disconnectWallet()`. `accountsChanged` now calls `disconnectWallet()` on empty accounts instead of reloading.

### 8. Submit form not clearing after successful mint
After AI verification + oracle mint, the form still showed old GPS rows, uploaded deed filename and field values.

**Fix:** `resetSubmitForm()` called after confirmed mint — clears all GPS rows back to one blank row, resets all input fields, clears file upload display.

---

## Stack

- `index.html` — single-file frontend (Ethers.js v5.7.2 CDN), works with QIE Wallet
- `terraledger.py` — FastAPI backend: 5 AI modules, oracle signer, PDF certificate generator (ReportLab)
- `onchain/` — 9 Solidity contracts (Hardhat)

### Contracts

| Contract | Role |
|---|---|
| **CarbonCredit (TLCERT)** | Soulbound ERC-721. Non-transferable verification certificate. Automatically shows "Fully Offset" when all project TCC are retired. Stays in minter's wallet permanently. |
| **CarbonCreditToken (TCC)** | ERC-20, 1 TCC = 1 tonne CO₂. Oracle-minted on verification pass. Tracks `issuedByProject[bytes32]` and `retiredByProject[bytes32]` for per-project offset accounting. Burns on `retire()`. |
| **RetirementCertificate (TLRET)** | Soulbound ERC-721. Auto-minted by CarbonCreditToken on every `retire()` call. Stores retiree address, amount, projectId and timestamp on-chain. Emits `RetirementCertMinted` event. |
| **CarbonOracle** | M-of-N attestation. `FraudRecord` on-chain, `getFraudLog()`. Mints both TLCERT + TCC to user wallet on attestation. |
| **TCCMarketplace** | ERC-20 TCC trading. Buy any quantity at price-per-tonne in QUSDC. Every listing stores `bytes32 projectId` so buyers always know the source project. |
| **ProjectRegistry** | User registers project on-chain (owner = user wallet). GPS bounding box stored for duplicate detection. |
| **CarbonMarketplace** | Legacy TLCERT NFT marketplace (soulbound — list/buy disabled for non-transferable certs). |
| **MockQIEPass** | On-chain KYC identity gate for the demo document-access grant. |
| **QUSDC** | Real QIE ERC-20 stablecoin used for marketplace settlement (6 decimals). |

---

## Mainnet (chain 1990)

RPC: `https://rpc1mainnet.qie.digital/` · Explorer: `https://mainnet.qie.digital/`

| Contract | Address |
|---|---|
| ProjectRegistry | `0xC780b1C96175e59B559315c0Df00B0E8c06750dc` |
| CarbonCredit (TLCERT — soulbound) | `0x7Bb8999aa1163bD9438e3efcf40CE28a73d8677C` |
| CarbonOracle | `0x606b15fD35DcaD1e0104b6e6c1E783BB84252f55` |
| CarbonMarketplace | `0xCf994675A919892AcD43D6E285127b3743402B58` |
| QUSDC | `0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5` |
| MockQIEPass | `0x5D41F203BaFF1CBb503394E6C9fD9F0C95696b7D` |
| **CarbonCreditToken (TCC — 1 token = 1 tonne CO₂)** | `0x54b96be03e161C12B1e07b013CdFdFF69490c624` |
| **TCCMarketplace v2 (with per-listing projectId)** | `0x7E61D69115dd4f1c2823B4d0a28bE71060424691` |
| **RetirementCertificate (TLRET — soulbound)** | `0xb87140fcb58B4Aa8f8a3796B3a961Ec2D0F100F2` |

### Historical txns (v1 contracts)

| | tx |
|---|---|
| Mint #0 (score 97) | `0x635573a19df602a84a32fe1f4a6b143869281d22f7b794ac513930c633879fda` |
| Mint #1 (score 100) | `0x1c42a7b338da98daa3d2301f29645fa664e1075dd9913cb69e0273406c4e5fc0` |
| Mint #2 (score 97) | `0x2fb4a75e3c4b3e2d9abc0fd9aff405ae98feb65a86fae44f1f63e50aa583ab50` |
| Fraud blocked | `0x6d3ae3181e1c2ba236fae30bb1ae3a10ce3b2fb5c488724690a17b1a974bc6c8` |
| Fraud blocked | `0xf7693b8e88da0960fb859faee82255489516eef30dadfde027ee14413f4e1cb6` |
| Retire #2 | `0x8d301c40b6280123f7e6cf99ef06a02326c7af5b34ca097dc7a81979d045dec4` |

---

## Running locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
cp .env.example .env   # fill in PRIVATE_KEY at minimum
python terraledger.py  # :8000

# frontend (separate terminal)
python3 -m http.server 5500
# open http://localhost:5500
```

Ollama is optional — if it's not running the audit falls back to a template report.

## Deploying contracts

```bash
npm install && npx hardhat compile && npx hardhat test
npm run deploy   # needs MAINNET_PRIVATE_KEY in .env
```

After deploy: update contract addresses in `.env` and `index.html` (`ADDRESSES` object), then `addOracle(<backend wallet>)` on CarbonOracle.

## Project layout

```
terraledger.py       backend (FastAPI + AI modules + oracle signer + PDF certs)
index.html           frontend (single file, QIE Wallet, Ethers.js v5)
onchain/             solidity contracts (Hardhat)
tools/               deploy + utility scripts
tests/               hardhat test suite
store/               runtime data (trained model, IPFS reports)
test_docs/           sample deeds for local testing
addresses/           deployed contract addresses per chainId
```

## Notes

- Backend never touches user funds — only uses the oracle key for minting
- Wallet ownership proved via EIP-191 signature on `/verify`
- `.env` must never be committed — contains oracle private key
- `samples/test_deeds/` excluded from git
