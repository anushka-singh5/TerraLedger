# TerraLedger

Carbon credit platform on QIE Blockchain where the AI check is the gate — no passing score means no tokens minted. Built for QIE Blockchain Hackathon 2026.

The problem with existing carbon markets (Toucan, KlimaDAO etc.) is they tokenize first and verify never. We flipped it: verification happens before anything hits the chain.

---

## How it works

```
submit project + deed PDF
        ↓
   5 AI modules run in parallel
        ↓ score ≥ 70 + no hard fails
   oracle mints TLCERT (soulbound) + TCC tokens
        ↓
   list TCC on marketplace (projectId locked in listing)
        ↓
   buyer purchases TCC, retires via retire()
   → TCC burns permanently
   → TLRET soulbound cert auto-mints
   → PDF certificate downloaded
        ↓
   when all TCC for a project are retired:
   TLCERT flips to "Fully Offset" automatically — no admin call
```

Two hard fails that block regardless of score:
- GPS polygon overlaps an existing project by >20%
- Deed GPS coordinates don't match the submitted polygon

---

## AI modules

| Module | What it checks | Max score |
|---|---|---|
| GPS overlap | Shapely polygon intersection against all registered projects | 30 |
| Ownership | OCR + NLP on deed PDF, ELA forgery detection, SHA-256 on-chain | 25 |
| Anomaly | IsolationForest on Verra registry data, flags abnormal tonnes/ha | 25 |
| Satellite | NASA FIRMS fire/deforestation data for coordinates, 90-day window | 20 |
| Audit | Llama-3 plain-English audit, score breakdown stored on-chain | — |

---

## Things that broke and how we fixed them

**TCC buyers had no idea which project their tokens came from.** ERC-20s are fungible — once in your wallet you lose provenance. Fixed by storing `bytes32 projectId` in every TCCMarketplace listing so the retire flow always knows which project to credit.

**TLCERT stayed "ACTIVE" forever even after all TCC burned.** retire() was passing bytes32(0) as projectId, so `retiredByProject` never matched `issuedByProject`. Fixed with per-project accounting on CarbonCreditToken and `isFullyRetired()`.

**Buyers had no retirement proof.** The old PDF cert flow only worked for the ERC-721 path. Added TLRET — soulbound cert that auto-mints on every `retire()` call, plus a PDF download from the backend.

**retire() kept reverting.** Burning TCC and minting a TLRET in one call costs ~350k gas. The default limit was 150k. Bumped to 450k.

**Retired tab showed nothing for buyers.** Buyers don't own TLCERTs, only TLRET certs. Fixed by loading `RetirementCertificate.getWalletCerts(wallet)` in the retired tab.

**Wallet connect hung forever.** `wallet_requestPermissions` on QIE Wallet would just hang if the user dismissed it. Added `Promise.race` with a 4s timeout, falls through to `eth_requestAccounts`.

**TCC showed as 0.000...001 on the explorer.** Contract inherited ERC20's default 18 decimals but oracle was issuing whole numbers. Added `decimals()` override returning 0.

---

## Stack

- `index.html` — single-file frontend, Ethers.js v5, works with QIE Wallet
- `terraledger.py` — FastAPI, 5 AI modules, oracle signer, PDF cert generator (ReportLab)
- `onchain/` — 9 Solidity contracts, Hardhat

### Contracts

| Contract | What it does |
|---|---|
| CarbonCredit (TLCERT) | Soulbound ERC-721, one per verified project. Flips to "Fully Offset" when all project TCC retire. Non-transferable. |
| CarbonCreditToken (TCC) | ERC-20, 1 token = 1 tonne CO₂, decimals=0. Oracle mints on approval. Burns on retire(). |
| RetirementCertificate (TLRET) | Soulbound ERC-721 proof of offset. Auto-minted by TCC on retire(), stores amount + projectId + timestamp. |
| CarbonOracle | M-of-N attestation, fraud log on-chain. Mints TLCERT + TCC together on approval. |
| TCCMarketplace | Buy/sell TCC at price-per-tonne in QUSDC. Every listing stores projectId. |
| ProjectRegistry | On-chain project register. GPS bounding box for duplicate detection. |
| CarbonMarketplace | Original TLCERT marketplace — soulbound certs can't actually transfer, kept for status display. |
| MockQIEPass | On-chain gate for the document-access demo flow. |

---

## Live on QIE Mainnet — chain 1990

Explorer: `https://mainnet.qie.digital/` · RPC: `https://rpc1mainnet.qie.digital/`

| Contract | Address |
|---|---|
| ProjectRegistry | `0x4Ad9378bf710F2F21bbB1884D0F6bBeF7C27Ae05` |
| CarbonCredit (TLCERT) | `0xdFfB3a6892D77C16b77c0d7cf640A48fb4f86a45` |
| CarbonOracle | `0x7550320b313b4c0Cf1AB1ecDeA2EB601cea0DAAE` |
| CarbonMarketplace | `0x8B5a31BaC85f9803b78C407B528C8758f58854bC` |
| QUSDC | `0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5` |
| MockQIEPass | `0x2aDBb3c3a840f154f9C4518e60FA389A193F7D00` |
| CarbonCreditToken (TCC) | `0x649979124d8938BBf31Cd2d24F6C460Ea768369a` |
| TCCMarketplace | `0x0b75BeDf2026A060187D3C8D6d68a8E7dd161f7e` |
| RetirementCertificate (TLRET) | `0x39A7AeBBaA5d159Cc20bBE27c1B1ea55162FE123` |

Real transactions you can verify:

| | tx hash |
|---|---|
| Mint Credit #0 — AI score 98, 25k TCC | `0xe8c94ded7ec96daf4a708733616967d706df8d6bc6d4107b4e6ee2de4b514eac` |
| List TCC on marketplace | `0x7bb6cd83b63c3a8d6ad060be1506688ae132b285894351807ded955b4079be76` |
| Buy 500 TCC | `0x3eec35c3495217af9b8a5fbc244e426b9bf28a7d8fa1c5b86f97520286762e92` |
| Retire 24,500 TCC + TLRET cert | `0x5a0057dbe846ca3b8da144719969dc664c608633048add6670d08dfb86c30192` |

---

## Running locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
cp .env.example .env   # at minimum set PRIVATE_KEY
python terraledger.py  # starts on :8000
```

Frontend is a single HTML file — just open with any static server:
```bash
python3 -m http.server 5500
```

Ollama is optional. Without it the audit module falls back to a template.

## Deploying contracts

```bash
npm install
npx hardhat compile
npm run deploy   # reads MAINNET_PRIVATE_KEY from .env
```

After deploy the script auto-patches `index.html` with new addresses and prints the env vars to update on Render.

## Layout

```
terraledger.py    FastAPI backend — AI modules, oracle signer, PDF certs
index.html        frontend — single file, QIE Wallet, Ethers.js v5
onchain/          Solidity contracts
tools/            deploy scripts
tests/            Hardhat test suite
store/            trained models, IPFS reports
addresses/        deployed addresses per chain
```

The oracle wallet only signs verification txns — it never touches user funds. Wallet ownership is proved via EIP-191 on `/verify`.
