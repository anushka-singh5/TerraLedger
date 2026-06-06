require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

// Mainnet deploys read MAINNET_PRIVATE_KEY (kept in .env, never committed).
// Bail loudly if someone runs `npm run deploy` without it rather than letting
// hardhat die later with a cryptic "no signer available".
if (!process.env.MAINNET_PRIVATE_KEY && process.env.npm_lifecycle_event === "deploy") {
  console.warn("⚠  MAINNET_PRIVATE_KEY not set — deploy will fail. Add it to .env");
}

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  paths: {
    sources: "./onchain",
    tests:   "./tests",
  },

  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      // viaIR keeps us under the bytecode limit; cancun matches QIE mainnet's EVM.
      evmVersion: "cancun",
      viaIR: true,
    },
  },

  networks: {
    // QIE Mainnet — chain 1990, gas paid in QIEV3.
    qieMainnet: {
      url:      process.env.QIE_MAINNET_RPC_URL || "https://rpc1mainnet.qie.digital/",
      chainId:  1990,
      accounts: process.env.MAINNET_PRIVATE_KEY ? [process.env.MAINNET_PRIVATE_KEY] : [],
      timeout:  60_000,
    },

    // In-memory chain for the test suite.
    hardhat: { chainId: 31337 },
  },

  // Explorer verification. QIE's explorer is Blockscout-style; the /api path is
  // the standard one. If `hardhat verify` rejects it, flatten the source and
  // paste it into the explorer UI by hand instead.
  etherscan: {
    apiKey: { qieMainnet: "no-key-needed" },
    customChains: [
      {
        network: "qieMainnet",
        chainId: 1990,
        urls: { apiURL: "https://mainnet.qie.digital/api", browserURL: "https://mainnet.qie.digital/" },
      },
    ],
  },

  gasReporter: {
    enabled: process.env.REPORT_GAS === "true",
    currency: "USD",
  },
};
