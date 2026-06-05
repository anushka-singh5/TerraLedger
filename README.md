# TerraLedger

**AI-gated carbon credit verification on QIE Blockchain.**
QIE Blockchain Hackathon 2026 · AI + Web3 track.

🟢 **Live on QIE Mainnet (chain 1990).**

---

## Contents

1. [What this is](#what-this-is)
2. [How it works](#how-it-works)
3. [Architecture](#architecture)
4. [Live on QIE Mainnet](#live-on-qie-mainnet-chain-1990) — addresses + **real transactions**
5. [Prerequisites](#prerequisites)
6. [Running locally](#running-locally)
7. [Deploying from scratch](#deploying-from-scratch)
8. [Testing](#testing)
9. [Project layout](#project-layout)
10. [Security notes](#security-notes)

> **Reviewers:** the live contract addresses and real mainnet transactions
> (mints, on-chain fraud blocks, a retirement) are in
> [Live on QIE Mainnet](#live-on-qie-mainnet-chain-1990).

---

## What this is

Most on-chain carbon platforms (Toucan, KlimaDAO, and friends) tokenize a credit
first and trust that someone, somewhere, verified it. TerraLedger flips that
order: **a credit cannot be minted until an AI verification pass clears it.** No
pass, no NFT. The check is the gate, not an afterthought.

To be clear about what we are *not* claiming — TerraLedger does not solve carbon
measurement (the MRV problem). We don't pretend to measure how much CO₂ a forest
actually sequesters. What we do is make it impossible to mint a credit that fails
a battery of fraud and plausibility checks: stolen GPS polygons, forged ownership
deeds, statistically impossible tonnage, land that isn't actually forested, and
reused paperwork. **On TerraLedger, a fraudulent credit can't be minted in the
first place.**

Every minted credit carries its AI score and four sub-scores *on-chain*, in the
NFT itself, so anyone can audit why it passed without trusting our backend.

---

## How it works

```
Submit project ──> AI verification (5 modules) ──> score >= 70 ? ──> oracle mints NFT
   (GPS, deed,            |                              |              (score on-chain)
    tonnage, docs)        └─ hard-fail any gate ─────────┘
                                  no mint, fraud logged
```

1. A developer submits a project: GPS polygon, claimed tonnage, ownership deed
   (PDF), project metadata, and a signed message proving wallet ownership.
2. The backend runs five independent checks and computes a 0–100 score.
3. Two checks are **hard gates** — GPS overlap with an existing project, or an
   ownership deed whose location doesn't match the claimed coordinates — fail
   either and the score is irrelevant, the mint is blocked.
4. Score ≥ 70 and no hard-fail → the backend oracle calls the on-chain
   `CarbonOracle`, which mints the `CarbonCredit` NFT with all scores baked in.
5. The credit can be listed, bought (in QUSDC), and finally **retired** — which
   burns the NFT and issues a retirement certificate. A retired credit can never
   be resold.

### The five AI modules

| Module | What it checks | Weight |
|---|---|---|
| GPS overlap | Polygon vs. every registered project (Shapely); >20% overlap = **hard fail** | 30 |
| Ownership forensics | Deed OCR + NLP, ELA forgery detection, location match (**hard fail** on mismatch), document-reuse | 25 |
| Anomaly detection | IsolationForest trained on real Verra registry data — flags impossible tonnage/area ratios | 25 |
| Satellite | NASA FIRMS fire history + land-cover plausibility for the coordinates | 20 |
| AI audit | Llama-3 (via Ollama) writes a human-readable audit report, pinned to IPFS | — |

**Scoring:** GPS 30 + Ownership 25 + Anomaly 25 + Satellite 20 = 100. Minimum 70
to mint. The two hard-fail gates override the score entirely.

---

## Architecture

```
┌─────────────┐      ┌──────────────────────┐      ┌────────────────────────┐
│ index.html  │──────│  terraledger.py      │──────│  QIE Mainnet (1990)    │
│ (frontend)  │ HTTP │  FastAPI + 5 AI mods │ web3 │  CarbonOracle (signer) │
│  QIE Wallet │      │  oracle signer       │      │  → CarbonCredit (mint) │
└─────────────┘      └──────────┬───────────┘      │  → ProjectRegistry     │
       │                        │                  │  → CarbonMarketplace   │
       │ eth_sendTransaction    │ REST             │  → MockQIEPass         │
       │ (buy/list/retire)      ▼                  └────────────────────────┘
       └────────────────► QIE Pass API ──> issueIdentity (KYC mirrored on-chain)
```

- **Frontend** (`index.html`) — single self-contained file. Wallet ownership,
  project submission, marketplace, retirement, and the credential viewer. The
  backend URL is resolved at runtime (`?api=`, `localStorage`, or same-origin),
  so the same file works locally and deployed.
- **Backend** (`terraledger.py`) — FastAPI. Runs the AI modules and is the only
  party holding the oracle key. It signs and submits the mint transaction; it
  never custodies user funds.
- **Contracts** — five Solidity contracts (below), deployed and wired by
  `scripts/deploy-all.js`.
- **QIE Pass** — KYC is a real off-chain REST API. The backend verifies a user
  through it, then (as the owner of `MockQIEPass`) mirrors the result on-chain
  via `issueIdentity`, so the on-chain document-access gate reflects real KYC.

### Contracts

| Contract | Role |
|---|---|
| `ProjectRegistry.sol` | Project registration, GPS bounding-box store + `findOverlaps()` dup guard, fraud log |
| `CarbonCredit.sol` | ERC-721 credit. Oracle-gated mint, AI scores in `CreditData`, `retire()` burns |
| `CarbonOracle.sol` | M-of-N attestation bridge from backend to mint; QIE Pass doc-access grants |
| `CarbonMarketplace.sol` | List / buy / unlist in QUSDC (6-decimal QIE-20) |
| `MockQIEPass.sol` | On-chain mirror of off-chain KYC, gates deep-document access |

`MockQUSDC.sol` also exists — a local-only test token. Mainnet uses the real QUSDC.

---

## Live on QIE Mainnet (chain 1990)

Everything below is deployed and live. RPC `https://rpc1mainnet.qie.digital/` ·
explorer `https://mainnet.qie.digital/` · gas token **QIEV3**.

### Contract addresses

| Contract | Address | Explorer |
|---|---|---|
| ProjectRegistry | `0x9894ee26e81c19ABfe9C381168f0f1e6b5f44112` | [view](https://mainnet.qie.digital/address/0x9894ee26e81c19ABfe9C381168f0f1e6b5f44112) |
| CarbonCredit (NFT) | `0x78650e0B619b5ECbb4B127Cb21c319befcE2ab16` | [view](https://mainnet.qie.digital/address/0x78650e0B619b5ECbb4B127Cb21c319befcE2ab16) |
| CarbonOracle | `0x8A07F795Cef75350D4BE4b939F367cFC3A1ed219` | [view](https://mainnet.qie.digital/address/0x8A07F795Cef75350D4BE4b939F367cFC3A1ed219) |
| CarbonMarketplace | `0x04442AE6DCb22803A4D4Ca8bB2a49691F603c343` | [view](https://mainnet.qie.digital/address/0x04442AE6DCb22803A4D4Ca8bB2a49691F603c343) |
| QUSDC (real, 6 dec) | `0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5` | [view](https://mainnet.qie.digital/address/0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5) |
| MockQIEPass | `0xBc05fd5617167eD6ee45a42CC3844f2484836431` | [view](https://mainnet.qie.digital/address/0xBc05fd5617167eD6ee45a42CC3844f2484836431) |

### Verified on-chain activity — real mainnet transactions

These are **real transactions on QIE Mainnet** (not testnet, not simulated),
produced by running the live system end-to-end. Current on-chain state:
`CarbonCredit.totalMinted() == 3`.

| What | Result | Transaction |
|---|---|---|
| Mint — Credit #0 | passed, AI score **97/100** | [`0x635573…79fda`](https://mainnet.qie.digital/tx/0x635573a19df602a84a32fe1f4a6b143869281d22f7b794ac513930c633879fda) |
| Mint — Credit #1 | passed, AI score **100/100** | [`0x1c42a7…e5fc0`](https://mainnet.qie.digital/tx/0x1c42a7b338da98daa3d2301f29645fa664e1075dd9913cb69e0273406c4e5fc0) |
| Mint — Credit #2 | passed, AI score **97/100** | [`0x2fb4a7…83ab50`](https://mainnet.qie.digital/tx/0x2fb4a75e3c4b3e2d9abc0fd9aff405ae98feb65a86fae44f1f63e50aa583ab50) |
| Fraud blocked | hard-fail logged on-chain, no mint | [`0x6d3ae3…4bc6c8`](https://mainnet.qie.digital/tx/0x6d3ae3181e1c2ba236fae30bb1ae3a10ce3b2fb5c488724690a17b1a974bc6c8) |
| Fraud blocked | hard-fail logged on-chain, no mint | [`0xf7693b…3f4e1cb6`](https://mainnet.qie.digital/tx/0xf7693b8e88da0960fb859faee82255489516eef30dadfde027ee14413f4e1cb6) |
| Retire — Credit #2 | NFT burned, offset claimed | [`0x8d301c…045dec4`](https://mainnet.qie.digital/tx/0x8d301c40b6280123f7e6cf99ef06a02326c7af5b34ca097dc7a81979d045dec4) |

The two fraud-blocked transactions are the core claim made real: a submission
that fails a hard gate is recorded on-chain as a fraud attempt and **no NFT is
ever minted**. The minted credits each carry their AI score on-chain (readable
via `CarbonCredit.credits(tokenId)`), and Credit #2 has been retired (burned).

---

## Prerequisites

- **Node.js** 18+ and npm (Hardhat, contract deploy)
- **Python** 3.10+ (the backend)
- **Tesseract OCR** — `brew install tesseract` (deed text extraction)
- **Ollama** + a Llama-3 model — `brew install ollama && ollama pull llama3`
  (the audit module; the backend degrades gracefully if it's down)
- A **QIE-compatible wallet** (QIE Wallet / MetaMask) on chain 1990
- For deploying: a funded mainnet key + a few QIEV3 for gas
- Optional API keys: NASA FIRMS (satellite), Pinata (IPFS pinning), QIE Pass
  partner keys (KYC). Each module fails closed and reports `*_MODULE_UNAVAILABLE`
  rather than erroring if its key is missing.

---

## Running locally

```bash
# 1. Backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
ollama serve &                       # if not already running
cp .env.example .env                 # then fill in the values (see below)
python terraledger.py                # serves on :8000, Swagger at /docs

# 2. Frontend (any static server, separate terminal)
python3 -m http.server 5500
# open http://localhost:5500/index.html
# the frontend auto-discovers the backend at localhost:8000
```

Point the frontend at a remote backend with `?api=https://your-backend` on the
URL, or `localStorage.setItem('TERRALEDGER_API', '...')`.

### Environment variables

Create a `.env` (it's gitignored — never commit it):

```ini
# ── Blockchain (oracle signer) ──────────────────────────────
PRIVATE_KEY=                 # oracle wallet key the backend signs mints with
ORACLE_CONTRACT_ADDRESS=0x8A07F795Cef75350D4BE4b939F367cFC3A1ed219
QIE_RPC_URL=https://rpc1mainnet.qie.digital/
NEXT_PUBLIC_QIE_CHAIN_ID=1990

# ── Contract addresses (read by backend + frontend) ─────────
NEXT_PUBLIC_PROJECT_REGISTRY_ADDRESS=0x9894ee26e81c19ABfe9C381168f0f1e6b5f44112
NEXT_PUBLIC_CARBON_CREDIT_ADDRESS=0x78650e0B619b5ECbb4B127Cb21c319befcE2ab16
NEXT_PUBLIC_CARBON_ORACLE_ADDRESS=0x8A07F795Cef75350D4BE4b939F367cFC3A1ed219
NEXT_PUBLIC_MARKETPLACE_ADDRESS=0x04442AE6DCb22803A4D4Ca8bB2a49691F603c343
NEXT_PUBLIC_QUSDC_ADDRESS=0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5
NEXT_PUBLIC_QIEPASS_ADDRESS=0xBc05fd5617167eD6ee45a42CC3844f2484836431
NEXT_PUBLIC_QIE_RPC=https://rpc1mainnet.qie.digital/

# ── Deploy (Hardhat) — separate key from the backend signer ─
MAINNET_PRIVATE_KEY=
QIE_MAINNET_RPC_URL=https://rpc1mainnet.qie.digital/

# ── Optional API keys (modules fail closed if absent) ───────
NASA_FIRMS_API_KEY=
PINATA_JWT=
QIE_PASS_BASE_URL=https://pass-api.qie.digital
QIE_PASS_PUBLIC_KEY=
QIE_PASS_SECRET_KEY=

# ── Ollama (AI audit) ───────────────────────────────────────
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3

# ── Misc ────────────────────────────────────────────────────
ALLOWED_ORIGINS=*
AI_BACKEND_URL=http://localhost:8000
```

---

## Deploying from scratch

This is the path if you ever need to redeploy (e.g. fresh wallet, or another
chain). The whole contract set goes up in one command.

```bash
npm install
npx hardhat compile
npx hardhat test                                  # 28 tests, all green

# Set MAINNET_PRIVATE_KEY in .env, fund it with QIEV3, then:
npm run deploy                                    # → scripts/deploy-all.js on qieMainnet
```

`deploy-all.js` deploys ProjectRegistry → CarbonCredit → CarbonOracle, wires the
oracle into both, deploys the marketplace (bound to the real QUSDC on mainnet),
deploys a fresh MockQIEPass, and writes everything to
`deployments/<chainId>.json`. It prints the addresses (and a `DEPLOY_JSON=` line
you can paste).

After deploying:

1. Copy the new addresses into `.env` (both `NEXT_PUBLIC_*` and
   `ORACLE_CONTRACT_ADDRESS`) and into the `ADDRESSES` block in `index.html`.
2. Authorize the backend's signer as an oracle — `addOracle(<backend address>)`
   on `CarbonOracle` — so it can submit attestations.
3. Restart the backend. `GET /health` should report the chain as connected and
   the oracle as ready.

> The deployer is also the oracle admin. Keep `MAINNET_PRIVATE_KEY` (deploy) and
> `PRIVATE_KEY` (backend signer) as separate keys, and rotate both after the
> hackathon — they've lived in a local `.env` during development.

---

## Testing

```bash
npx hardhat test          # contract suite (Hardhat in-memory chain + MockQUSDC)
```

For an end-to-end smoke test against a live chain, submit a project through the
frontend with the included sample documents in `samples/` and watch the NFT
appear on the explorer.

---

## Project layout

```
terraledger.py              FastAPI backend — 5 AI modules + the oracle signer
index.html                  single-file frontend (wallet, submit, marketplace, retire)
requirements.txt            Python deps
hardhat.config.js           Hardhat config (QIE Mainnet + local)
package.json                npm scripts (compile / test / deploy)
.env.example                template for the .env you create locally

contracts/                  Solidity sources
  ProjectRegistry.sol         land registry + GPS bbox overlap guard
  CarbonCredit.sol            ERC-721 credit, oracle-gated mint, retire()
  CarbonOracle.sol            M-of-N attestation → mint / fraud-flag
  CarbonMarketplace.sol       list / buy / unlist in QUSDC
  MockQIEPass.sol             on-chain mirror of QIE Pass KYC
  MockQUSDC.sol               local-only test token (mainnet uses real QUSDC)

scripts/
  deploy-all.js               one-shot deploy + wire, writes deployments/<chainId>.json
  build_verra_stats.py        builds the anomaly-model volume stats
  import_verra_csv.py         imports a Verra registry CSV for training

test/terraledger.test.js    contract test suite (28 tests)
deployments/1990.json       live mainnet addresses
assets/                     logo
samples/                    demo deeds for testing (genuine + forged + test_deeds/)
data/                       runtime state — trained model, doc/reuse registry,
                            reputation, generated reports (gitignored)
```

---

## Security notes

- The backend is the **only** holder of the oracle key and the only party that
  can mint. It never touches user funds — buy/list/retire are signed directly by
  the user's wallet.
- Wallet ownership is proven on `/verify` by an EIP-191 signed message, recovered
  server-side, so you can't submit a project on someone else's behalf.
- Per-wallet rate limiting and a fraud-strike reputation store
  (`data/reputation.json`) throttle abuse.
- `.env` and all keys are gitignored. The keys used during the hackathon should
  be rotated before any real-world use.
