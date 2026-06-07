// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

interface IRetirementCertificate {
    function mint(address to, uint256 amount, bytes32 projectId) external returns (uint256);
}

/// @title CarbonCreditToken — TCC (TerraLedger Carbon Credit)
/// @notice 1 TCC = 1 tonne CO2. Minted by the oracle when a project passes AI
///         verification. Holders can retire (burn) any quantity — each burn is
///         permanently logged on-chain and triggers a soulbound RetirementCertificate
///         NFT minted to the retiree as proof of CO2 offset.
contract CarbonCreditToken is ERC20, Ownable {

    address public oracle;
    address public retirementCert;  // RetirementCertificate contract

    struct RetirementRecord {
        address  retiredBy;
        uint256  amount;        // in TCC (= tonnes)
        bytes32  projectId;
        uint256  timestamp;
    }

    RetirementRecord[] private _retirements;
    uint256 private _totalRetiredTonnes;

    // Per-project accounting — lets TLCERT know when all credits are offset.
    mapping(bytes32 => uint256) public issuedByProject;
    mapping(bytes32 => uint256) public retiredByProject;

    event OracleUpdated(address indexed previous, address indexed next);
    event RetirementCertUpdated(address indexed previous, address indexed next);
    event CreditsIssued(address indexed to, uint256 amount, bytes32 indexed projectId);
    event CreditsRetired(address indexed by, uint256 amount, bytes32 indexed projectId);

    modifier onlyOracle() {
        require(msg.sender == oracle, "TCC: caller is not the oracle");
        _;
    }

    constructor(address owner_) ERC20("TerraLedger Carbon Credit", "TCC") Ownable(owner_) {}

    // ── Admin ─────────────────────────────────────────────────────────────

    function setOracle(address oracle_) external onlyOwner {
        require(oracle_ != address(0), "TCC: zero oracle");
        emit OracleUpdated(oracle, oracle_);
        oracle = oracle_;
    }

    /// @notice Wire the RetirementCertificate contract. Safe to leave unset
    ///         (cert minting is skipped silently if address is zero).
    function setRetirementCert(address cert_) external onlyOwner {
        require(cert_ != address(0), "TCC: zero address");
        emit RetirementCertUpdated(retirementCert, cert_);
        retirementCert = cert_;
    }

    // ── Core: issue / retire ──────────────────────────────────────────────

    /// @notice Oracle mints `amount` TCC to `to` after a project passes AI verification.
    function issue(address to, uint256 amount, bytes32 projectId) external onlyOracle {
        require(to != address(0), "TCC: mint to zero address");
        require(amount > 0,       "TCC: zero amount");
        _mint(to, amount);
        issuedByProject[projectId] += amount;
        emit CreditsIssued(to, amount, projectId);
    }

    /// @notice Permanently retire (burn) `amount` TCC from caller's balance.
    ///         Mints a soulbound RetirementCertificate to the caller as proof.
    function retire(uint256 amount, bytes32 projectId) external {
        require(amount > 0, "TCC: zero amount");

        _burn(msg.sender, amount);
        _totalRetiredTonnes    += amount;
        retiredByProject[projectId] += amount;

        _retirements.push(RetirementRecord({
            retiredBy: msg.sender,
            amount:    amount,
            projectId: projectId,
            timestamp: block.timestamp
        }));

        emit CreditsRetired(msg.sender, amount, projectId);

        // Mint soulbound retirement certificate to the retiree as permanent proof.
        if (retirementCert != address(0)) {
            try IRetirementCertificate(retirementCert).mint(msg.sender, amount, projectId) {} catch {}
        }
    }

    // ── Read API ──────────────────────────────────────────────────────────

    /// @notice True when every TCC issued for a project has been retired.
    ///         TLCERT reads this to show "Fully Offset" status in its tokenURI.
    function isFullyRetired(bytes32 projectId) external view returns (bool) {
        uint256 issued  = issuedByProject[projectId];
        uint256 retired = retiredByProject[projectId];
        return issued > 0 && retired >= issued;
    }

    function totalRetiredTonnes() external view returns (uint256) {
        return _totalRetiredTonnes;
    }

    function getRetirements() external view returns (RetirementRecord[] memory) {
        return _retirements;
    }

    function getRetirementsByWallet(address wallet) external view returns (RetirementRecord[] memory) {
        uint256 count;
        for (uint256 i; i < _retirements.length; i++) {
            if (_retirements[i].retiredBy == wallet) count++;
        }
        RetirementRecord[] memory result = new RetirementRecord[](count);
        uint256 j;
        for (uint256 i; i < _retirements.length; i++) {
            if (_retirements[i].retiredBy == wallet) result[j++] = _retirements[i];
        }
        return result;
    }
}
