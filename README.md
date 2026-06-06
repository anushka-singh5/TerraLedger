# TerraLedger

AI-gated carbon credit minting on QIE Blockchain (chain 1990).

The idea: instead of minting a credit and verifying later (like Toucan/KlimaDAO), the AI check IS the gate. No pass = no NFT.

Live on QIE Mainnet — `CarbonCredit.totalMinted() == 3`, two fraud attempts blocked on-chain.

---

## How it works

Submit a project (GPS polygon, deed PDF, tonnage claim) → backend runs 5 checks → score ≥ 70 and no hard-fail → oracle mints the NFT with scores baked in.

Two checks are hard gates regardless of score:
- GPS polygon overlaps an existing project by >20%
- Deed location doesn't match claimed coordinates

```
submit → 5 AI modules → score ≥ 70? → oracle mints NFT
                ↓
         hard-fail? → blocked, logged on-chain forever
```

### The 5 modules

| Module | What | Weight |
|---|---|---|
| GPS overlap | Shapely polygon vs all registered projects | 30 |
| Ownership | OCR + NLP on deed, ELA forgery detection | 25 |
| Anomaly | IsolationForest on Verra registry data | 25 |
| Satellite | NASA FIRMS fire history for the coords | 20 |
| Audit | Llama-3 writes a report, pinned to IPFS | — |

---

## Stack

- `index.html` — single-file frontend, works with QIE Wallet
- `terraledger.py` — FastAPI backend, 5 AI modules + oracle signer
- `onchain/` — 5 Solidity contracts (Hardhat)

---

## Mainnet (chain 1990)

RPC: `https://rpc1mainnet.qie.digital/` · Explorer: `https://mainnet.qie.digital/`

| Contract | Address |
|---|---|
| ProjectRegistry | `0x9894ee26e81c19ABfe9C381168f0f1e6b5f44112` |
| CarbonCredit | `0x78650e0B619b5ECbb4B127Cb21c319befcE2ab16` |
| CarbonOracle | `0x8A07F795Cef75350D4BE4b939F367cFC3A1ed219` |
| CarbonMarketplace | `0x04442AE6DCb22803A4D4Ca8bB2a49691F603c343` |
| QUSDC | `0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5` |
| MockQIEPass | `0xBc05fd5617167eD6ee45a42CC3844f2484836431` |

### Real txns

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

After deploy: update contract addresses in `.env` and `index.html`, then `addOracle(<backend wallet>)` on CarbonOracle.

## Project layout

```
terraledger.py       backend
index.html           frontend
onchain/             solidity contracts
tools/               deploy + data scripts
tests/               hardhat test suite (28 tests)
store/               runtime data (model, reports)
test_docs/           sample deeds for testing
addresses/           deployed contract addresses
```

## Notes

- Backend holds the oracle key, never user funds
- Wallet ownership proved via EIP-191 signed message on /verify
- `.env` is gitignored — rotate keys after hackathon
