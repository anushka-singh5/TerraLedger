// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

interface IRetirementCertificate {
    function mint(address to, uint256 amount, bytes32 projectId) external returns (uint256);
}

contract CarbonCreditToken is ERC20, Ownable {

    address public oracle;
    address public retirementCert;

    struct RetirementRecord {
        address  retiredBy;
        uint256  amount;
        bytes32  projectId;
        uint256  timestamp;
    }

    RetirementRecord[] private _retirements;
    uint256 private _totalRetiredTonnes;

    // tracked per-project so TLCERT can flip to "Fully Offset" when issued == retired
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

    // 1 TCC = 1 tonne CO₂ — whole units only, no fractions
    function decimals() public pure override returns (uint8) { return 0; }

    function setOracle(address oracle_) external onlyOwner {
        require(oracle_ != address(0), "TCC: zero oracle");
        emit OracleUpdated(oracle, oracle_);
        oracle = oracle_;
    }

    // safe to leave unset — TLRET mint is skipped if address is zero
    function setRetirementCert(address cert_) external onlyOwner {
        require(cert_ != address(0), "TCC: zero address");
        emit RetirementCertUpdated(retirementCert, cert_);
        retirementCert = cert_;
    }

    function issue(address to, uint256 amount, bytes32 projectId) external onlyOracle {
        require(to != address(0), "TCC: mint to zero address");
        require(amount > 0,       "TCC: zero amount");
        _mint(to, amount);
        issuedByProject[projectId] += amount;
        emit CreditsIssued(to, amount, projectId);
    }

    function retire(uint256 amount, bytes32 projectId) external {
        require(amount > 0, "TCC: zero amount");

        _burn(msg.sender, amount);
        _totalRetiredTonnes        += amount;
        retiredByProject[projectId] += amount;

        _retirements.push(RetirementRecord({
            retiredBy: msg.sender,
            amount:    amount,
            projectId: projectId,
            timestamp: block.timestamp
        }));

        emit CreditsRetired(msg.sender, amount, projectId);

        // best-effort TLRET mint — don't let a cert contract bug block the burn
        if (retirementCert != address(0)) {
            try IRetirementCertificate(retirementCert).mint(msg.sender, amount, projectId) {} catch {}
        }
    }

    // TLCERT reads this to show "Fully Offset" in tokenURI
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
