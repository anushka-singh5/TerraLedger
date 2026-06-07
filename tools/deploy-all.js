// deploys all 9 TerraLedger contracts in dependency order, auto-patches index.html addresses, prints Render env vars
// usage: npx hardhat run tools/deploy-all.js --network qieMainnet
const { ethers } = require("hardhat");
const fs   = require("fs");
const path = require("path");
require("dotenv").config();

const REAL_QUSDC_MAINNET = "0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5"; // QIE-20, 6 decimals
const mask = a => a.slice(0,6) + "..." + a.slice(-4);
const sep  = l => console.log("\n---", l);

async function main() {
  const [deployer] = await ethers.getSigners();
  const chainId   = Number((await ethers.provider.getNetwork()).chainId);
  const isMainnet = chainId === 1990;

  sep(`Context · chain ${chainId}${isMainnet ? " (MAINNET)" : ""}`);
  console.log("  Deployer :", mask(deployer.address), "(masked)");
  console.log("  Balance  :", ethers.formatEther(await ethers.provider.getBalance(deployer.address)),
              isMainnet ? "QIEV3" : "ETH");

  // Payment token: real QUSDC on mainnet, a throwaway mock elsewhere.
  let QUSDC = REAL_QUSDC_MAINNET;
  if (!isMainnet) {
    sep("0  MockQUSDC (non-mainnet)");
    const mock = await (await ethers.getContractFactory("MockQUSDC")).deploy();
    await mock.waitForDeployment();
    QUSDC = await mock.getAddress();
  }
  console.log("  QUSDC    :", QUSDC, isMainnet ? "(real)" : "(mock)");

  sep("1  ProjectRegistry");
  const registry = await (await ethers.getContractFactory("ProjectRegistry")).deploy(deployer.address);
  await registry.waitForDeployment();
  const registryAddr = await registry.getAddress();
  console.log("  →", registryAddr);

  sep("2  CarbonCredit");
  const credit = await (await ethers.getContractFactory("CarbonCredit")).deploy(deployer.address);
  await credit.waitForDeployment();
  const creditAddr = await credit.getAddress();
  console.log("  →", creditAddr);

  sep("3  CarbonOracle");
  const oracle = await (await ethers.getContractFactory("CarbonOracle")).deploy(registryAddr, creditAddr, deployer.address);
  await oracle.waitForDeployment();
  const oracleAddr = await oracle.getAddress();
  console.log("  →", oracleAddr);

  sep("4  Wire oracle → registry + credit");
  await (await registry.setOracle(oracleAddr)).wait();
  await (await credit.setOracle(oracleAddr)).wait();
  console.log("  setOracle ×2 ✓");

  sep("5  CarbonMarketplace");
  const market = await (await ethers.getContractFactory("CarbonMarketplace")).deploy(creditAddr, QUSDC, deployer.address);
  await market.waitForDeployment();
  const marketAddr = await market.getAddress();
  console.log("  →", marketAddr);

  sep("6  QIE Pass (on-chain doc-access gate)");
  const pass = await (await ethers.getContractFactory("MockQIEPass")).deploy();
  await pass.waitForDeployment();
  const qiePassAddr = await pass.getAddress();
  await (await oracle.setQIEPass(qiePassAddr)).wait();
  console.log("  →", qiePassAddr, "· oracle.setQIEPass ✓");

  sep("7  Wire marketplace → CarbonCredit (for Listed status in tokenURI)");
  await (await credit.setMarketplace(marketAddr)).wait();
  console.log("  setMarketplace ✓ — tokenURI will now reflect Listed/Active/Retired");

  sep("8  CarbonCreditToken (TCC — 1 token = 1 tonne CO₂)");
  const tcc = await (await ethers.getContractFactory("CarbonCreditToken")).deploy(deployer.address);
  await tcc.waitForDeployment();
  const tccAddr = await tcc.getAddress();
  console.log("  →", tccAddr);

  sep("9  TCCMarketplace (ERC-20 carbon credit trading)");
  const tccMkt = await (await ethers.getContractFactory("TCCMarketplace")).deploy(tccAddr, QUSDC, deployer.address);
  await tccMkt.waitForDeployment();
  const tccMktAddr = await tccMkt.getAddress();
  console.log("  →", tccMktAddr);

  sep("10 RetirementCertificate (TLRET — soulbound proof of CO2 offset)");
  const retCert = await (await ethers.getContractFactory("RetirementCertificate")).deploy();
  await retCert.waitForDeployment();
  const retCertAddr = await retCert.getAddress();
  console.log("  →", retCertAddr);

  sep("11 Wire everything");
  // TCC ← oracle address (for minting)
  await (await tcc.setOracle(oracleAddr)).wait();
  // TCC ← RetirementCertificate (mints cert on retire)
  await (await tcc.setRetirementCert(retCertAddr)).wait();
  // RetirementCertificate ← TCC address (only TCC can mint certs)
  await (await retCert.setTCCContract(tccAddr)).wait();
  // CarbonOracle ← TCC address (issues TCC alongside NFT on approval)
  await (await oracle.setCreditToken(tccAddr)).wait();
  // TLCERT ← TCC address (reads isFullyRetired for tokenURI status)
  await (await credit.setTCCToken(tccAddr)).wait();
  console.log("  TCC.setOracle ✓");
  console.log("  TCC.setRetirementCert ✓");
  console.log("  RetirementCertificate.setTCCContract ✓");
  console.log("  CarbonOracle.setCreditToken ✓");
  console.log("  CarbonCredit.setTCCToken ✓");

  // deployer is intentionally left out of the record — it's public on-chain anyway.
  const contracts = {
    ProjectRegistry: registryAddr, CarbonCredit: creditAddr, CarbonOracle: oracleAddr,
    CarbonMarketplace: marketAddr, QUSDC, MockQIEPass: qiePassAddr,
    CarbonCreditToken: tccAddr, TCCMarketplace: tccMktAddr,
    RetirementCertificate: retCertAddr,
  };
  const outPath = path.join(__dirname, "..", "addresses", `${chainId}.json`);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify({
    network: isMainnet ? "QIE Mainnet" : `chain ${chainId}`, chainId,
    deployedAt: new Date().toISOString(), contracts,
  }, null, 2));
  console.log(`\n  saved addresses/${chainId}.json`);

  sep("12 Auto-patch index.html ADDRESSES");
  const htmlPath = path.join(__dirname, "..", "index.html");
  let html = fs.readFileSync(htmlPath, "utf8");
  const newBlock = `const ADDRESSES = {
  ProjectRegistry:      '${contracts.ProjectRegistry}',
  CarbonCredit:         '${contracts.CarbonCredit}',
  CarbonOracle:         '${contracts.CarbonOracle}',
  CarbonMarketplace:    '${contracts.CarbonMarketplace}',
  QUSDC:                '${contracts.QUSDC}',
  QIEPass:              '${contracts.MockQIEPass}',
  CarbonCreditToken:    '${contracts.CarbonCreditToken}',
  TCCMarketplace:       '${contracts.TCCMarketplace}',
  RetirementCertificate:'${contracts.RetirementCertificate}',
}`;
  html = html.replace(/const ADDRESSES = \{[\s\S]*?\}/, newBlock);
  fs.writeFileSync(htmlPath, html);
  console.log("  index.html ADDRESSES patched ✓");

  sep("13 Backend env vars to update on Render.com");
  console.log(`  ORACLE_CONTRACT_ADDRESS              = ${contracts.CarbonOracle}`);
  console.log(`  NEXT_PUBLIC_PROJECT_REGISTRY_ADDRESS = ${contracts.ProjectRegistry}`);
  console.log(`  NEXT_PUBLIC_CARBON_CREDIT_ADDRESS    = ${contracts.CarbonCredit}`);
  console.log(`  NEXT_PUBLIC_QIEPASS_ADDRESS          = ${contracts.MockQIEPass}`);
  console.log(`  NEXT_PUBLIC_TCC_ADDRESS              = ${contracts.CarbonCreditToken}`);
  console.log(`  NEXT_PUBLIC_TCC_MARKETPLACE_ADDRESS  = ${contracts.TCCMarketplace}`);
  console.log(`  NEXT_PUBLIC_RET_CERT_ADDRESS         = ${contracts.RetirementCertificate}`);

  console.log("\nDEPLOY_JSON=" + JSON.stringify(contracts));
}

main().catch((err) => { console.error("\n✗ Deploy failed:", err.message); process.exit(1); });
