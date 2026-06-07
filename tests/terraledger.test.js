/**
 * TerraLedger — full contract test suite (mainnet-readiness).
 *
 * Covers the complete stack on a local Hardhat chain:
 *   ProjectRegistry · CarbonCredit · CarbonOracle · CarbonMarketplace ·
 *   MockQUSDC · MockQIEPass
 *
 * Run:  npx hardhat test
 */
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");

// Standard non-overlapping polygon + its bbox in microdegrees (lat/lng × 1e6).
const POLY = [[1.55, 110.36], [1.55, 110.40], [1.51, 110.40], [1.51, 110.36]];
const u = (n) => Math.round(n * 1e6);
function bboxOf(poly) {
  const lats = poly.map((p) => p[0]), lngs = poly.map((p) => p[1]);
  return [u(Math.min(...lats)), u(Math.min(...lngs)), u(Math.max(...lats)), u(Math.max(...lngs))];
}
const geo = (poly) => JSON.stringify({ type: "Polygon", coordinates: [[...poly, poly[0]]] });
const DOC_HASH = ethers.keccak256(ethers.toUtf8Bytes("genuine-land-deed"));
const ZERO = ethers.ZeroHash;

async function deployFixture() {
  const [owner, oracle2, user, buyer, corp, stranger] = await ethers.getSigners();

  const Registry = await ethers.getContractFactory("ProjectRegistry");
  const registry = await Registry.deploy(owner.address);

  const Credit = await ethers.getContractFactory("CarbonCredit");
  const credit = await Credit.deploy(owner.address);

  const Oracle = await ethers.getContractFactory("CarbonOracle");
  const oracle = await Oracle.deploy(await registry.getAddress(), await credit.getAddress(), owner.address);
  const oracleAddr = await oracle.getAddress();

  await registry.setOracle(oracleAddr);
  await credit.setOracle(oracleAddr);

  const QUSDC = await ethers.getContractFactory("MockQUSDC");
  const qusdc = await QUSDC.deploy();

  const Pass = await ethers.getContractFactory("MockQIEPass");
  const pass = await Pass.deploy();
  await oracle.setQIEPass(await pass.getAddress());

  const Market = await ethers.getContractFactory("CarbonMarketplace");
  const market = await Market.deploy(await credit.getAddress(), await qusdc.getAddress(), owner.address);

  return { owner, oracle2, user, buyer, corp, stranger, registry, credit, oracle, qusdc, pass, market };
}

// Submit a project to the registry (as `signer`) with computed bbox.
async function submitProject(registry, signer, id, poly = POLY, hectares = 1200, tonnes = 8400) {
  const bb = bboxOf(poly);
  await registry.connect(signer).submitProject(id, geo(poly), hectares, tonnes, bb[0], bb[1], bb[2], bb[3]);
}

// Standard passing attestation args (score 100).
function passArgs(id, { gps = 30, own = 25, anom = 25, sat = 20, gpsHF = false, ownHF = false,
                        cid = "QmAuditCid", flags = [], vintage = 2025, tonnes = 8400, doc = DOC_HASH } = {}) {
  return [id, gps, own, anom, sat, gpsHF, ownHF, cid, flags, vintage, tonnes, doc];
}

describe("TerraLedger", () => {
  describe("Deployment & wiring", () => {
    it("wires oracle into registry + credit, seeds 1 oracle, threshold 1, QIE Pass set", async () => {
      const { registry, credit, oracle, owner, pass } = await loadFixture(deployFixture);
      expect(await registry.oracle()).to.equal(await oracle.getAddress());
      expect(await credit.oracle()).to.equal(await oracle.getAddress());
      expect(await oracle.oracleCount()).to.equal(1n);
      expect(await oracle.attestationThreshold()).to.equal(1n);
      expect(await oracle.authorizedOracles(owner.address)).to.equal(true);
      expect(await oracle.qiePass()).to.equal(await pass.getAddress());
    });

    it("MockQUSDC uses 6 decimals (matches real QUSDC)", async () => {
      const { qusdc } = await loadFixture(deployFixture);
      expect(await qusdc.decimals()).to.equal(6);
    });
  });

  describe("Happy path — submit → verify → mint with on-chain scores", () => {
    it("mints an NFT to the project owner and stores all scores on-chain", async () => {
      const { registry, credit, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-OK-1");
      await submitProject(registry, user, id);

      await expect(oracle.submitVerification(...passArgs(id)))
        .to.emit(oracle, "CreditApproved");

      expect(await credit.totalMinted()).to.equal(1n);
      const c = await credit.credits(0);
      expect(c.score).to.equal(100);
      expect(c.gpsScore).to.equal(30);
      expect(c.ownershipScore).to.equal(25);
      expect(c.anomalyScore).to.equal(25);
      expect(c.satelliteScore).to.equal(20);
      expect(c.tonnes).to.equal(8400n);
      expect(await credit.ownerOf(0)).to.equal(user.address); // minted to project owner
      expect((await registry.getProject(id)).status).to.equal(1); // Approved
    });

    it("tokenURI embeds the AI Score attribute", async () => {
      const { registry, credit, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-OK-2");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id));

      const uri = await credit.tokenURI(0);
      const json = JSON.parse(Buffer.from(uri.split(",")[1], "base64").toString());
      const ai = json.attributes.find((a) => a.trait_type === "AI Score");
      expect(ai.value).to.equal(100);
    });

    it("stores the document hash and verifies integrity", async () => {
      const { registry, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-OK-3");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id));

      expect(await oracle.getDocumentHash(id)).to.equal(DOC_HASH);
      expect(await oracle.verifyDocument(id, DOC_HASH)).to.equal(true);
      expect(await oracle.verifyDocument(id, ethers.keccak256(ethers.toUtf8Bytes("forged")))).to.equal(false);
    });
  });

  describe("Fraud & rejection gating", () => {
    it("GPS hard-fail flags fraud on-chain and does NOT mint", async () => {
      const { registry, credit, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-FRAUD");
      await submitProject(registry, user, id);

      await expect(oracle.submitVerification(...passArgs(id, { gps: 0, gpsHF: true, flags: ["DUPLICATE_GPS_DETECTED"] })))
        .to.emit(oracle, "FraudAttemptLogged");

      expect(await credit.totalMinted()).to.equal(0n);
      const rec = await oracle.getVerification(id);
      expect(rec.fraudAttempt).to.equal(true);
      expect(rec.passed).to.equal(false);
      expect((await registry.getProject(id)).status).to.equal(3); // FraudFlagged
    });

    it("score below 70 is rejected, no mint", async () => {
      const { registry, credit, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-LOW");
      await submitProject(registry, user, id);

      // 20 + 10 + 10 + 10 = 50 < 70
      await expect(oracle.submitVerification(...passArgs(id, { gps: 20, own: 10, anom: 10, sat: 10 })))
        .to.emit(oracle, "VerificationFailed");

      expect(await credit.totalMinted()).to.equal(0n);
      expect((await registry.getProject(id)).status).to.equal(2); // Rejected
    });

    it("rejects out-of-range scores", async () => {
      const { registry, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-RANGE");
      await submitProject(registry, user, id);
      await expect(oracle.submitVerification(...passArgs(id, { gps: 31 })))
        .to.be.revertedWith("CarbonOracle: gpsScore out of range");
    });
  });

  describe("M-of-N decentralised oracle", () => {
    it("requires threshold distinct oracles attesting the SAME result before minting", async () => {
      const { registry, credit, oracle, oracle2, user } = await loadFixture(deployFixture);
      await oracle.addOracle(oracle2.address);
      await oracle.setAttestationThreshold(2);
      expect(await oracle.oracleCount()).to.equal(2n);

      const id = ethers.encodeBytes32String("CP-MOFN");
      await submitProject(registry, user, id);

      // First attestation (owner = oracle #1): recorded but NOT finalised
      await oracle.submitVerification(...passArgs(id));
      expect(await oracle.finalized(id)).to.equal(false);
      expect(await credit.totalMinted()).to.equal(0n);

      // Second oracle, identical args → finalises + mints
      await expect(oracle.connect(oracle2).submitVerification(...passArgs(id)))
        .to.emit(oracle, "CreditApproved");
      expect(await oracle.finalized(id)).to.equal(true);
      expect(await credit.totalMinted()).to.equal(1n);
    });

    it("does NOT finalise if the two oracles disagree on the result", async () => {
      const { registry, credit, oracle, oracle2, user } = await loadFixture(deployFixture);
      await oracle.addOracle(oracle2.address);
      await oracle.setAttestationThreshold(2);

      const id = ethers.encodeBytes32String("CP-DISAGREE");
      await submitProject(registry, user, id);

      await oracle.submitVerification(...passArgs(id, { anom: 25 }));        // score 100
      await oracle.connect(oracle2).submitVerification(...passArgs(id, { anom: 24 })); // score 99 → different hash
      expect(await oracle.finalized(id)).to.equal(false);
      expect(await credit.totalMinted()).to.equal(0n);
    });

    it("blocks an oracle from attesting the same project twice", async () => {
      const { registry, oracle, oracle2, user } = await loadFixture(deployFixture);
      await oracle.addOracle(oracle2.address);
      await oracle.setAttestationThreshold(2);
      const id = ethers.encodeBytes32String("CP-TWICE");
      await submitProject(registry, user, id);

      await oracle.submitVerification(...passArgs(id));
      await expect(oracle.submitVerification(...passArgs(id)))
        .to.be.revertedWith("CarbonOracle: oracle already attested");
    });

    it("blocks re-verification after finalisation", async () => {
      const { registry, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-FINAL");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id));
      await expect(oracle.submitVerification(...passArgs(id)))
        .to.be.revertedWith("CarbonOracle: already finalized");
    });

    it("cannot drop oracle count below threshold", async () => {
      const { oracle, oracle2 } = await loadFixture(deployFixture);
      await oracle.addOracle(oracle2.address);
      await oracle.setAttestationThreshold(2);
      await expect(oracle.removeOracle(oracle2.address))
        .to.be.revertedWith("CarbonOracle: would break threshold");
    });
  });

  describe("Access control", () => {
    it("non-oracle cannot submit verification", async () => {
      const { registry, oracle, user, stranger } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-AC1");
      await submitProject(registry, user, id);
      await expect(oracle.connect(stranger).submitVerification(...passArgs(id)))
        .to.be.revertedWith("CarbonOracle: not authorized oracle");
    });

    it("non-oracle cannot mint directly on CarbonCredit", async () => {
      const { credit, stranger } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-AC2");
      await expect(credit.connect(stranger).mint(stranger.address, id, "cid", 2025, 100, 30, 25, 25, 20))
        .to.be.revertedWith("CarbonCredit: not oracle");
    });

    it("non-owner cannot add an oracle (OZ Ownable)", async () => {
      const { oracle, stranger } = await loadFixture(deployFixture);
      await expect(oracle.connect(stranger).addOracle(stranger.address))
        .to.be.revertedWithCustomError(oracle, "OwnableUnauthorizedAccount");
    });

    it("non-oracle cannot approve a project on the registry", async () => {
      const { registry, user, stranger } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-AC3");
      await submitProject(registry, user, id);
      await expect(registry.connect(stranger).approveProject(id))
        .to.be.revertedWith("ProjectRegistry: not oracle");
    });
  });

  describe("On-chain GPS bounding-box overlap", () => {
    it("findOverlaps returns approved projects whose bbox intersects", async () => {
      const { registry, oracle, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-BBOX");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id)); // approves it

      const bb = bboxOf(POLY);
      const hits = await registry.findOverlaps(bb[0], bb[1], bb[2], bb[3]);
      expect(hits).to.include(id);

      // A far-away box returns nothing
      const none = await registry.findOverlaps(u(40.0), u(-100.0), u(40.1), u(-99.9));
      expect(none.length).to.equal(0);
    });

    it("rejects an invalid bbox (min > max)", async () => {
      const { registry, user } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-BADBOX");
      await expect(
        registry.connect(user).submitProject(id, geo(POLY), 100, 100, u(2.0), 0, u(1.0), 0)
      ).to.be.revertedWith("ProjectRegistry: bad bbox");
    });
  });

  describe("QIE Pass-gated document access", () => {
    it("blocks access without a QIE Pass identity", async () => {
      const { registry, oracle, user, corp } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-DOC1");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id));
      await expect(oracle.connect(corp).requestDocumentAccess(id))
        .to.be.revertedWith("CarbonOracle: QIE Pass identity required");
    });

    it("grants access to a QIE Pass-verified buyer for a verified project", async () => {
      const { registry, oracle, pass, user, corp } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-DOC2");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id));

      await pass.connect(corp).verifyMe("Acme Corp Officer", "Acme Carbon Buyers");
      await expect(oracle.connect(corp).requestDocumentAccess(id))
        .to.emit(oracle, "DocumentAccessGranted");
      expect(await oracle.hasDocumentAccess(id, corp.address)).to.equal(true);
    });

    it("blocks access to a project that did not pass verification", async () => {
      const { registry, oracle, pass, user, corp } = await loadFixture(deployFixture);
      const id = ethers.encodeBytes32String("CP-DOC3");
      await submitProject(registry, user, id);
      await oracle.submitVerification(...passArgs(id, { gps: 20, own: 10, anom: 10, sat: 10 })); // rejected

      await pass.connect(corp).verifyMe("Acme", "Acme");
      await expect(oracle.connect(corp).requestDocumentAccess(id))
        .to.be.revertedWith("CarbonOracle: project not verified");
    });
  });

  describe("Soulbound certificate — non-transferable", () => {
    async function mintTo(fx, signer, idStr) {
      const id = ethers.encodeBytes32String(idStr);
      await submitProject(fx.registry, signer, id);
      await fx.oracle.submitVerification(...passArgs(id));
      const tokenId = (await fx.credit.totalMinted()) - 1n;
      return { id, tokenId };
    }

    it("certificate stays in minter's wallet — transfer reverts", async () => {
      const fx = await loadFixture(deployFixture);
      const { credit, user, buyer } = fx;
      const { tokenId } = await mintTo(fx, user, "CP-SOUL1");
      expect(await credit.ownerOf(tokenId)).to.equal(user.address);
      await expect(
        credit.connect(user).transferFrom(user.address, buyer.address, tokenId)
      ).to.be.revertedWith("CarbonCredit: certificate is soulbound - non-transferable");
    });

    it("approve is disabled on soulbound certificate", async () => {
      const fx = await loadFixture(deployFixture);
      const { credit, user, buyer } = fx;
      const { tokenId } = await mintTo(fx, user, "CP-SOUL2");
      await expect(
        credit.connect(user).approve(buyer.address, tokenId)
      ).to.be.revertedWith("CarbonCredit: approvals disabled - soulbound");
    });

    it("setApprovalForAll is disabled on soulbound certificate", async () => {
      const fx = await loadFixture(deployFixture);
      const { credit, user, buyer } = fx;
      await expect(
        credit.connect(user).setApprovalForAll(buyer.address, true)
      ).to.be.revertedWith("CarbonCredit: approvals disabled - soulbound");
    });

    it("caps the marketplace fee at 10%", async () => {
      const { market, owner } = await loadFixture(deployFixture);
      await expect(market.setFee(1001, owner.address)).to.be.revertedWith("Marketplace: fee too high");
    });

    it("retire marks certificate as retired — NFT stays in wallet, blocks double-retire", async () => {
      const fx = await loadFixture(deployFixture);
      const { credit, user } = fx;
      const { tokenId } = await mintTo(fx, user, "CP-RETIRE");

      await expect(credit.connect(user).retire(tokenId)).to.emit(credit, "CreditRetired");
      const c = await credit.credits(tokenId);
      expect(c.retired).to.equal(true);
      expect(c.retiredBy).to.equal(user.address);
      // NFT stays in wallet — ownerOf still resolves (soulbound, not burned)
      expect(await credit.ownerOf(tokenId)).to.equal(user.address);
      // double-retire blocked
      await expect(credit.connect(user).retire(tokenId)).to.be.revertedWith("CarbonCredit: already retired");
    });

    it("only the token owner can retire", async () => {
      const fx = await loadFixture(deployFixture);
      const { credit, user, stranger } = fx;
      const { tokenId } = await mintTo(fx, user, "CP-RET2");
      await expect(credit.connect(stranger).retire(tokenId)).to.be.revertedWith("CarbonCredit: not token owner");
    });
  });
});
