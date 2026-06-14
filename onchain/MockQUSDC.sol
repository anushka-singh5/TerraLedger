// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

// MockQUSDC — test token, 6 decimals to match real QUSDC; has a faucet
contract MockQUSDC is ERC20 {
    uint8   private constant DECIMALS        = 6;                 // match real QUSDC / USDC
    uint256 public  constant FAUCET_AMOUNT   = 1_000 * 10 ** 6;  // 1,000 QUSDC
    uint256 public  constant FAUCET_COOLDOWN = 1 hours;

    mapping(address => uint256) public lastFaucet;

    event FaucetClaimed(address indexed to, uint256 amount);

    constructor() ERC20("QIE USD Coin (Test)", "QUSDC") {
        _mint(msg.sender, 1_000_000 * 10 ** DECIMALS);
    }

    function decimals() public pure override returns (uint8) {
        return DECIMALS;
    }

    // 1,000 QUSDC per wallet, once an hour.
    function faucet() external {
        require(
            block.timestamp - lastFaucet[msg.sender] >= FAUCET_COOLDOWN,
            "MockQUSDC: faucet cooldown - wait 1 hour"
        );
        lastFaucet[msg.sender] = block.timestamp;
        _mint(msg.sender, FAUCET_AMOUNT);
        emit FaucetClaimed(msg.sender, FAUCET_AMOUNT);
    }
}
