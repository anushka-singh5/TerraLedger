/**
 * Redeploys ONLY TCCMarketplace with the updated Listing struct (projectId field).
 * Existing TCC and TCCMarketplace addresses in .env must be set.
 * Run: npx hardhat run tools/redeploy-tcc-marketplace.js --network qieMainnet
 */
const { ethers } = require("hardhat");
const fs = require("fs");
const path = require("path");
require("dotenv").config();

async function main() {
  const [deployer] = await ethers.getSigners();
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  console.log("Chain:", chainId, "| Deployer:", deployer.address);
  console.log("Balance:", ethers.formatEther(await ethers.provider.getBalance(deployer.address)), "QIEV3");

  const TCC_ADDR   = process.env.NEXT_PUBLIC_TCC_ADDRESS;
  const QUSDC_ADDR = process.env.NEXT_PUBLIC_QUSDC_ADDRESS;
  if (!TCC_ADDR || !QUSDC_ADDR) throw new Error("NEXT_PUBLIC_TCC_ADDRESS or NEXT_PUBLIC_QUSDC_ADDRESS not set in .env");

  console.log("\nDeploying TCCMarketplace v2 (with projectId per listing)…");
  const factory = await ethers.getContractFactory("TCCMarketplace");
  const mkt = await factory.deploy(TCC_ADDR, QUSDC_ADDR, deployer.address);
  await mkt.waitForDeployment();
  const newAddr = await mkt.getAddress();
  console.log("TCCMarketplace →", newAddr);

  // Update addresses/<chainId>.json
  const addrFile = path.join(__dirname, "..", "addresses", `${chainId}.json`);
  if (fs.existsSync(addrFile)) {
    const data = JSON.parse(fs.readFileSync(addrFile, "utf8"));
    data.contracts.TCCMarketplace = newAddr;
    data.deployedAt = new Date().toISOString();
    fs.writeFileSync(addrFile, JSON.stringify(data, null, 2));
    console.log("Updated addresses/" + chainId + ".json");
  }

  console.log("\nNew TCCMarketplace address:", newAddr);
  console.log("Update NEXT_PUBLIC_TCC_MARKETPLACE_ADDRESS in .env and ADDRESSES.TCCMarketplace in index.html");
}

main().catch(e => { console.error(e.message); process.exit(1); });
