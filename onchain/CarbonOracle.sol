// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./ProjectRegistry.sol";
import "./CarbonCredit.sol";

interface IQIEPass {
    function isVerified(address wallet) external view returns (bool);
}

interface ICarbonCreditToken {
    function issue(address to, uint256 amount, bytes32 projectId) external;
}

/// @title CarbonOracle
/// @notice Where the off-chain AI checks become an on-chain decision. The backend
///         wallet calls submitVerification() with the five module scores; this
///         contract mints (pass), flags fraud forever (hard-fail), or rejects
///         (score < 70). Score weights mirror the backend exactly.
///         The fraud log is public and permanent — every blocked attempt is
///         queryable by wallet, timestamp, and reason.
contract CarbonOracle is Ownable {
    uint256 public constant MIN_SCORE     = 70;
    uint256 public constant MAX_GPS       = 30;
    uint256 public constant MAX_OWNERSHIP = 25;
    uint256 public constant MAX_ANOMALY   = 25;
    uint256 public constant MAX_SATELLITE = 20;

    ProjectRegistry    public immutable registry;
    CarbonCredit       public immutable creditContract;
    ICarbonCreditToken public creditToken;   // TCC ERC-20 — set after deploy via setCreditToken()

    // M-of-N oracle set. Any oracle in this set can attest a result, but a
    // project only finalises once `threshold` distinct oracles attest the *same*
    // result. Threshold 1 with a single oracle is the degenerate "trust the backend"
    // case; bump both to add more decentralisation without redeploying.
    mapping(address => bool) public authorizedOracles;
    address[] private _oracleList;
    uint256 public attestationThreshold;

    // Attestation bookkeeping.
    mapping(bytes32 => bool)                          public finalized;
    mapping(bytes32 => mapping(address => bool))      public hasAttested;
    mapping(bytes32 => mapping(bytes32 => uint256))   public attestationCount;

    // QIE Pass-gated document access. We store the *grant* on-chain, never the
    // deed itself — the backend serves redacted files off-chain once it sees a grant.
    IQIEPass public qiePass;
    mapping(bytes32 => mapping(address => bool)) public hasDocumentAccess;

    // ── Verification records ────────────────────────────────────────────────

    struct VerificationRecord {
        bytes32   projectId;
        address   submitter;
        uint256   totalScore;
        uint256   gpsScore;
        uint256   ownershipScore;
        uint256   anomalyScore;
        uint256   satelliteScore;
        bool      passed;
        bool      fraudAttempt;
        string    reportIpfsCid;
        bytes32   docHash;
        string[]  flags;
        uint256   mintedTokenId;
        uint256   timestamp;
    }

    bytes32[] private _allVerifiedIds;
    mapping(bytes32 => VerificationRecord) public verifications;

    // ── Fraud log ───────────────────────────────────────────────────────────

    /// @notice One entry per hard-fail fraud block. Public and permanent.
    struct FraudRecord {
        bytes32  projectId;
        address  wallet;      // wallet that submitted the fraudulent project
        string   reason;      // hard-fail reason, human-readable
        string[] flags;       // AI module flags e.g. GPS_OVERLAP, OWNERSHIP_GPS_MISMATCH
        uint256  score;       // total score (0 on hard-fail)
        uint256  timestamp;
    }

    FraudRecord[] private _fraudLog;
    uint256 private _fraudsBlocked;

    // ── Events ──────────────────────────────────────────────────────────────

    event BackendUpdated(address indexed previous, address indexed next);
    event OracleAuthorized(address indexed oracle);
    event OracleRevoked(address indexed oracle);
    event AttestationThresholdUpdated(uint256 threshold);
    event AttestationSubmitted(
        bytes32 indexed projectId,
        address indexed oracle,
        bytes32 resultHash,
        uint256 count,
        uint256 threshold
    );
    event QIEPassUpdated(address indexed qiePass);
    event DocumentAccessGranted(
        bytes32 indexed projectId,
        address indexed requester,
        uint256 grantedAt
    );
    /// @notice Emitted whenever a hard-fail fraud attempt is blocked and logged.
    event FraudAttemptLogged(
        bytes32 indexed projectId,
        address indexed wallet,
        string  reason,
        string[] flags,
        string  reportIpfsCid,
        uint256 timestamp
    );
    event CreditApproved(
        bytes32 indexed projectId,
        address indexed recipient,
        uint256 indexed tokenId,
        uint256 totalScore,
        string  reportIpfsCid
    );
    event VerificationFailed(
        bytes32 indexed projectId,
        address indexed submitter,
        uint256 totalScore,
        string[] flags
    );

    // ── Modifiers ───────────────────────────────────────────────────────────

    modifier onlyAuthorizedOracle() {
        require(authorizedOracles[msg.sender], "CarbonOracle: not authorized oracle");
        _;
    }

    // ── Constructor ─────────────────────────────────────────────────────────

    constructor(address registry_, address creditContract_, address backend_)
        Ownable(msg.sender)
    {
        require(registry_       != address(0), "CarbonOracle: zero registry");
        require(creditContract_ != address(0), "CarbonOracle: zero credit contract");
        require(backend_        != address(0), "CarbonOracle: zero backend");

        registry       = ProjectRegistry(registry_);
        creditContract = CarbonCredit(creditContract_);

        authorizedOracles[backend_] = true;
        _oracleList.push(backend_);
        attestationThreshold = 1;
        emit OracleAuthorized(backend_);
    }

    // ── Oracle management ───────────────────────────────────────────────────

    /// @notice Authorize a new oracle wallet.
    function addOracle(address oracle) external onlyOwner {
        require(oracle != address(0), "CarbonOracle: zero oracle");
        require(!authorizedOracles[oracle], "CarbonOracle: already authorized");
        authorizedOracles[oracle] = true;
        _oracleList.push(oracle);
        emit OracleAuthorized(oracle);
    }

    /// @notice Revoke an oracle. Refuses if it would drop the active set below threshold.
    function removeOracle(address oracle) external onlyOwner {
        require(authorizedOracles[oracle], "CarbonOracle: not authorized");
        require(_oracleList.length - 1 >= attestationThreshold, "CarbonOracle: would break threshold");
        authorizedOracles[oracle] = false;
        for (uint256 i = 0; i < _oracleList.length; i++) {
            if (_oracleList[i] == oracle) {
                _oracleList[i] = _oracleList[_oracleList.length - 1];
                _oracleList.pop();
                break;
            }
        }
        emit OracleRevoked(oracle);
    }

    /// @notice How many oracles must agree before a project finalises (1..oracleCount).
    function setAttestationThreshold(uint256 threshold) external onlyOwner {
        require(threshold >= 1 && threshold <= _oracleList.length, "CarbonOracle: bad threshold");
        attestationThreshold = threshold;
        emit AttestationThresholdUpdated(threshold);
    }

    /// @notice Backwards-compat alias — authorizes a wallet as an oracle.
    function setTrustedBackend(address newBackend) external onlyOwner {
        require(newBackend != address(0), "CarbonOracle: zero backend");
        if (!authorizedOracles[newBackend]) {
            authorizedOracles[newBackend] = true;
            _oracleList.push(newBackend);
            emit OracleAuthorized(newBackend);
        }
        emit BackendUpdated(address(0), newBackend);
    }

    function setQIEPass(address qiePass_) external onlyOwner {
        require(qiePass_ != address(0), "CarbonOracle: zero QIE Pass");
        qiePass = IQIEPass(qiePass_);
        emit QIEPassUpdated(qiePass_);
    }

    /// @notice Wire the TCC ERC-20 token. Once set, every approved project also
    ///         gets `tonnes` TCC minted to the project owner (1 TCC = 1 tonne CO₂).
    function setCreditToken(address token_) external onlyOwner {
        require(token_ != address(0), "CarbonOracle: zero token");
        creditToken = ICarbonCreditToken(token_);
    }

    function getOracles() external view returns (address[] memory) { return _oracleList; }
    function oracleCount() external view returns (uint256) { return _oracleList.length; }

    // ── Document access ─────────────────────────────────────────────────────

    /// @notice A KYC-verified buyer requests extended doc access for a project.
    ///         We only record the grant here; the backend serves redacted files.
    function requestDocumentAccess(bytes32 projectId) external {
        require(address(qiePass) != address(0), "CarbonOracle: QIE Pass not set");
        require(qiePass.isVerified(msg.sender), "CarbonOracle: QIE Pass identity required");
        require(verifications[projectId].passed, "CarbonOracle: project not verified");
        hasDocumentAccess[projectId][msg.sender] = true;
        emit DocumentAccessGranted(projectId, msg.sender, block.timestamp);
    }

    // ── Core verification ───────────────────────────────────────────────────

    /// @notice Submit an oracle's attestation for a project's AI result.
    ///         Once `threshold` oracles submit the same result hash, the project finalises:
    ///         mint on pass, fraud-flag on hard-fail, reject on low score.
    function submitVerification(
        bytes32           projectId,
        uint256           gpsScore,
        uint256           ownershipScore,
        uint256           anomalyScore,
        uint256           satelliteScore,
        bool              gpsHardFail,
        bool              ownershipHardFail,
        string calldata   reportIpfsCid,
        string[] calldata flags,
        uint256           vintage,
        uint256           tonnes,
        bytes32           docHash
    ) external onlyAuthorizedOracle {
        require(!finalized[projectId],                "CarbonOracle: already finalized");
        require(!hasAttested[projectId][msg.sender],  "CarbonOracle: oracle already attested");
        require(gpsScore       <= MAX_GPS,            "CarbonOracle: gpsScore out of range");
        require(ownershipScore <= MAX_OWNERSHIP,      "CarbonOracle: ownershipScore out of range");
        require(anomalyScore   <= MAX_ANOMALY,        "CarbonOracle: anomalyScore out of range");
        require(satelliteScore <= MAX_SATELLITE,      "CarbonOracle: satelliteScore out of range");

        ProjectRegistry.Project memory project = registry.getProject(projectId);
        require(project.submittedAt != 0, "CarbonOracle: project not in registry");
        require(
            project.status == ProjectRegistry.ProjectStatus.Pending,
            "CarbonOracle: project not pending"
        );

        bytes32 resultHash = keccak256(abi.encode(
            projectId, gpsScore, ownershipScore, anomalyScore, satelliteScore,
            gpsHardFail, ownershipHardFail, vintage, tonnes, docHash,
            keccak256(bytes(reportIpfsCid)), keccak256(abi.encode(flags))
        ));

        hasAttested[projectId][msg.sender] = true;
        uint256 count = ++attestationCount[projectId][resultHash];

        VerificationRecord storage rec = verifications[projectId];
        rec.projectId      = projectId;
        rec.submitter      = project.owner;
        rec.gpsScore       = gpsScore;
        rec.ownershipScore = ownershipScore;
        rec.anomalyScore   = anomalyScore;
        rec.satelliteScore = satelliteScore;
        rec.reportIpfsCid  = reportIpfsCid;
        rec.docHash        = docHash;
        rec.timestamp      = block.timestamp;

        emit AttestationSubmitted(projectId, msg.sender, resultHash, count, attestationThreshold);

        if (count >= attestationThreshold) {
            _finalize(projectId, project.owner, gpsHardFail, ownershipHardFail, flags, vintage, tonnes);
        }
    }

    function _finalize(
        bytes32           projectId,
        address           owner,
        bool              gpsHardFail,
        bool              ownershipHardFail,
        string[] calldata flags,
        uint256           vintage,
        uint256           tonnes
    ) internal {
        finalized[projectId] = true;
        VerificationRecord storage rec = verifications[projectId];

        bool hardFailed = gpsHardFail || ownershipHardFail;
        uint256 total   = hardFailed
            ? 0
            : (rec.gpsScore + rec.ownershipScore + rec.anomalyScore + rec.satelliteScore);
        bool passed = !hardFailed && total >= MIN_SCORE;

        rec.totalScore   = total;
        rec.passed       = passed;
        rec.fraudAttempt = hardFailed;
        for (uint256 i = 0; i < flags.length; i++) {
            rec.flags.push(flags[i]);
        }
        _allVerifiedIds.push(projectId);

        if (passed) {
            _handleApproval(projectId, owner, rec.reportIpfsCid, vintage, tonnes, total);
        } else if (hardFailed) {
            _handleFraud(projectId, owner, flags, rec.reportIpfsCid, gpsHardFail, ownershipHardFail, total);
        } else {
            _handleRejection(projectId, owner, total, flags);
        }
    }

    function _handleApproval(
        bytes32 projectId,
        address owner,
        string memory reportIpfsCid,
        uint256 vintage,
        uint256 tonnes,
        uint256 totalScore
    ) internal {
        registry.approveProject(projectId);
        VerificationRecord storage rec = verifications[projectId];

        // Mint the ERC-721 verification certificate (proof of AI pass).
        uint256 tokenId = creditContract.mint(
            owner, projectId, reportIpfsCid, vintage, tonnes,
            uint8(rec.gpsScore), uint8(rec.ownershipScore),
            uint8(rec.anomalyScore), uint8(rec.satelliteScore)
        );
        rec.mintedTokenId = tokenId;

        // Mint TCC ERC-20 tokens — 1 TCC = 1 tonne CO₂ — directly to the project owner.
        // If creditToken is not yet wired, this step is skipped silently (non-blocking).
        if (address(creditToken) != address(0) && tonnes > 0) {
            try creditToken.issue(owner, tonnes, projectId) {} catch {}
        }

        emit CreditApproved(projectId, owner, tokenId, totalScore, reportIpfsCid);
    }

    function _handleFraud(
        bytes32           projectId,
        address           owner,
        string[] calldata flags,
        string memory     reportIpfsCid,
        bool              gpsHardFail,
        bool              ownershipHardFail,
        uint256           score
    ) internal {
        string memory reason = _buildFraudReason(gpsHardFail, ownershipHardFail);
        registry.flagFraud(projectId, reason);

        // Increment the public fraud counter.
        _fraudsBlocked++;

        // Append to the permanent fraud log (wallet, reason, flags, timestamp).
        _fraudLog.push();
        FraudRecord storage fr = _fraudLog[_fraudLog.length - 1];
        fr.projectId = projectId;
        fr.wallet    = owner;
        fr.reason    = reason;
        fr.score     = score;
        fr.timestamp = block.timestamp;
        for (uint256 i = 0; i < flags.length; i++) {
            fr.flags.push(flags[i]);
        }

        emit FraudAttemptLogged(projectId, owner, reason, flags, reportIpfsCid, block.timestamp);
    }

    function _handleRejection(
        bytes32           projectId,
        address           owner,
        uint256           totalScore,
        string[] calldata flags
    ) internal {
        string memory reason = string(abi.encodePacked(
            "Score ", Strings.toString(totalScore), "/100 below 70 minimum"
        ));
        registry.rejectProject(projectId, reason);
        emit VerificationFailed(projectId, owner, totalScore, flags);
    }

    function _buildFraudReason(bool gpsHardFail, bool ownershipHardFail)
        internal pure returns (string memory)
    {
        if (gpsHardFail && ownershipHardFail)
            return "Hard fail: GPS overlap >20% AND ownership GPS mismatch";
        if (gpsHardFail)
            return "Hard fail: GPS overlap >20% - duplicate land claim";
        return "Hard fail: ownership document GPS does not match submitted polygon";
    }

    // ── Public read API ─────────────────────────────────────────────────────

    /// @notice Total hard-fail fraud attempts blocked, ever.
    function fraudsBlocked() external view returns (uint256) { return _fraudsBlocked; }

    /// @notice Full fraud log — every blocked hard-fail with wallet, reason, flags, timestamp.
    ///         Public and permanent — anyone can audit the history.
    function getFraudLog() external view returns (FraudRecord[] memory) {
        return _fraudLog;
    }

    /// @notice All fraud records triggered by a specific wallet address.
    function getFraudsByWallet(address wallet) external view returns (FraudRecord[] memory) {
        uint256 count = 0;
        for (uint256 i = 0; i < _fraudLog.length; i++) {
            if (_fraudLog[i].wallet == wallet) count++;
        }
        FraudRecord[] memory result = new FraudRecord[](count);
        uint256 j = 0;
        for (uint256 i = 0; i < _fraudLog.length; i++) {
            if (_fraudLog[i].wallet == wallet) result[j++] = _fraudLog[i];
        }
        return result;
    }

    /// @notice All project IDs that have been verified (pass, fail, or fraud).
    function getAllVerifiedIds() external view returns (bytes32[] memory) {
        return _allVerifiedIds;
    }

    function getVerification(bytes32 projectId)
        external view returns (VerificationRecord memory)
    {
        return verifications[projectId];
    }

    function getDocumentHash(bytes32 projectId) external view returns (bytes32) {
        return verifications[projectId].docHash;
    }

    /// @notice Prove a deed is the exact document that passed — hash your copy, compare.
    function verifyDocument(bytes32 projectId, bytes32 candidateHash)
        external view returns (bool)
    {
        bytes32 stored = verifications[projectId].docHash;
        return stored != bytes32(0) && stored == candidateHash;
    }

    /// @notice Returns projectIds of all fraud-flagged attempts.
    function getFraudAttempts() external view returns (bytes32[] memory) {
        uint256 count;
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].fraudAttempt) count++;
        }
        bytes32[] memory result = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].fraudAttempt)
                result[j++] = _allVerifiedIds[i];
        }
        return result;
    }

    function getApprovedCount() external view returns (uint256 count) {
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].passed) count++;
        }
    }
}
