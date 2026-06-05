// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "./ProjectRegistry.sol";
import "./CarbonCredit.sol";

/// @notice All we need from QIE Pass on-chain. Real impl is MockQIEPass.
interface IQIEPass {
    function isVerified(address wallet) external view returns (bool);
}

/// @title CarbonOracle
/// @notice Where the off-chain AI checks become an on-chain decision. The backend
///         wallet calls submitVerification() with the five module scores; this
///         contract then mints (pass), flags fraud forever (hard-fail), or rejects
///         (score < 70). The score weights below mirror the backend exactly — if
///         you change one, change the other.
contract CarbonOracle is Ownable {
    uint256 public constant MIN_SCORE    = 70;
    uint256 public constant MAX_GPS      = 30;
    uint256 public constant MAX_OWNERSHIP = 25;
    uint256 public constant MAX_ANOMALY  = 25;
    uint256 public constant MAX_SATELLITE = 20;

    ProjectRegistry public immutable registry;
    CarbonCredit    public immutable creditContract;

    // The trust model is M-of-N, not one all-powerful backend. Any oracle in this
    // set can attest a result, but a project only finalises once `threshold`
    // distinct oracles attest the *same* result. Threshold 1 with a single oracle
    // is the degenerate "just trust the backend" case; bump both to remove the SPOF.
    mapping(address => bool) public authorizedOracles;
    address[] private _oracleList;
    uint256 public attestationThreshold;

    // Attestation bookkeeping.
    mapping(bytes32 => bool) public finalized;                              // reached threshold, done
    mapping(bytes32 => mapping(address => bool)) public hasAttested;        // one vote per oracle per project
    mapping(bytes32 => mapping(bytes32 => uint256)) public attestationCount;// project => resultHash => votes

    // QIE Pass-gated document access. We store the *grant* on-chain, never the deed
    // itself — the backend hands out the redacted docs off-chain once it sees the
    // requester holds a grant here.
    IQIEPass public qiePass;
    mapping(bytes32 => mapping(address => bool)) public hasDocumentAccess;

    struct VerificationRecord {
        bytes32   projectId;
        address   submitter;       // project owner at time of verification
        uint256   totalScore;      // 0 when hard-fail triggered
        uint256   gpsScore;
        uint256   ownershipScore;
        uint256   anomalyScore;
        uint256   satelliteScore;
        bool      passed;
        bool      fraudAttempt;    // true when a hard-fail rule triggered
        string    reportIpfsCid;   // AI audit report on IPFS
        bytes32   docHash;         // SHA-256 of ownership document (integrity proof)
        string[]  flags;           // human-readable rejection reasons
        uint256   mintedTokenId;   // populated on success; 0 means not minted
        uint256   timestamp;
    }

    bytes32[] private _allVerifiedIds;
    mapping(bytes32 => VerificationRecord) public verifications;

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

    event FraudAttemptLogged(
        bytes32 indexed projectId,
        address indexed submitter,
        uint256 totalScore,
        string[] flags,
        string reportIpfsCid,
        uint256 timestamp
    );
    event CreditApproved(
        bytes32 indexed projectId,
        address indexed recipient,
        uint256 indexed tokenId,
        uint256 totalScore,
        string reportIpfsCid
    );
    event VerificationFailed(
        bytes32 indexed projectId,
        address indexed submitter,
        uint256 totalScore,
        string[] flags
    );

    modifier onlyAuthorizedOracle() {
        require(authorizedOracles[msg.sender], "CarbonOracle: not authorized oracle");
        _;
    }

    constructor(
        address registry_,
        address creditContract_,
        address backend_
    ) Ownable(msg.sender) {
        require(registry_       != address(0), "CarbonOracle: zero registry");
        require(creditContract_ != address(0), "CarbonOracle: zero credit contract");
        require(backend_        != address(0), "CarbonOracle: zero backend");

        registry       = ProjectRegistry(registry_);
        creditContract = CarbonCredit(creditContract_);

        authorizedOracles[backend_] = true;
        _oracleList.push(backend_);
        attestationThreshold = 1;   // start single-oracle; raise once more come online
        emit OracleAuthorized(backend_);
    }

    function addOracle(address oracle) external onlyOwner {
        require(oracle != address(0), "CarbonOracle: zero oracle");
        require(!authorizedOracles[oracle], "CarbonOracle: already authorized");
        authorizedOracles[oracle] = true;
        _oracleList.push(oracle);
        emit OracleAuthorized(oracle);
    }

    /// @notice Revoke an oracle. Refuses if it would leave fewer oracles than the threshold.
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

    /// @notice How many oracles must agree before a project finalises. 1..oracleCount.
    function setAttestationThreshold(uint256 threshold) external onlyOwner {
        require(threshold >= 1 && threshold <= _oracleList.length, "CarbonOracle: bad threshold");
        attestationThreshold = threshold;
        emit AttestationThresholdUpdated(threshold);
    }

    /// @notice Kept around so older tooling that called setTrustedBackend still works —
    ///         it just authorizes the wallet as an oracle.
    function setTrustedBackend(address newBackend) external onlyOwner {
        require(newBackend != address(0), "CarbonOracle: zero backend");
        if (!authorizedOracles[newBackend]) {
            authorizedOracles[newBackend] = true;
            _oracleList.push(newBackend);
            emit OracleAuthorized(newBackend);
        }
        emit BackendUpdated(address(0), newBackend);
    }

    function getOracles() external view returns (address[] memory) {
        return _oracleList;
    }

    function oracleCount() external view returns (uint256) {
        return _oracleList.length;
    }

    function setQIEPass(address qiePass_) external onlyOwner {
        require(qiePass_ != address(0), "CarbonOracle: zero QIE Pass");
        qiePass = IQIEPass(qiePass_);
        emit QIEPassUpdated(qiePass_);
    }

    /// @notice A KYC'd buyer asks for the extended docs on a verified project. We
    ///         only record the grant here; the backend serves the redacted files.
    function requestDocumentAccess(bytes32 projectId) external {
        require(address(qiePass) != address(0), "CarbonOracle: QIE Pass not set");
        require(qiePass.isVerified(msg.sender), "CarbonOracle: QIE Pass identity required");
        require(verifications[projectId].passed, "CarbonOracle: project not verified");
        hasDocumentAccess[projectId][msg.sender] = true;
        emit DocumentAccessGranted(projectId, msg.sender, block.timestamp);
    }

    /// @notice One oracle's attestation of a project's verification result. The
    ///         backend calls this after running the five modules. docHash is the
    ///         SHA-256 of the deed (proof, not the deed); the two *HardFail flags
    ///         are the gates that zero the score regardless of the points.
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
        require(!finalized[projectId], "CarbonOracle: already finalized");
        require(!hasAttested[projectId][msg.sender], "CarbonOracle: oracle already attested");
        require(gpsScore       <= MAX_GPS,       "CarbonOracle: gpsScore out of range");
        require(ownershipScore <= MAX_OWNERSHIP, "CarbonOracle: ownershipScore out of range");
        require(anomalyScore   <= MAX_ANOMALY,   "CarbonOracle: anomalyScore out of range");
        require(satelliteScore <= MAX_SATELLITE, "CarbonOracle: satelliteScore out of range");

        ProjectRegistry.Project memory project = registry.getProject(projectId);
        require(project.submittedAt != 0, "CarbonOracle: project not in registry");
        require(
            project.status == ProjectRegistry.ProjectStatus.Pending,
            "CarbonOracle: project not pending"
        );

        // Hash the full result so two oracles only "agree" if every field matches.
        bytes32 resultHash = keccak256(abi.encode(
            projectId, gpsScore, ownershipScore, anomalyScore, satelliteScore,
            gpsHardFail, ownershipHardFail, vintage, tonnes, docHash,
            keccak256(bytes(reportIpfsCid)), keccak256(abi.encode(flags))
        ));

        hasAttested[projectId][msg.sender] = true;
        uint256 count = ++attestationCount[projectId][resultHash];

        // Stash the scores now so they're queryable even before we hit threshold.
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

    /// @dev Fires once threshold is reached. Everyone who got us here committed to
    ///      the same resultHash, so reading scores back from storage is safe.
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
        bool passed     = !hardFailed && total >= MIN_SCORE;

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
            _handleFraud(projectId, owner, flags, rec.reportIpfsCid, gpsHardFail, ownershipHardFail);
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
        // Pass the scores through to the NFT so they live with the token.
        uint256 tokenId = creditContract.mint(
            owner, projectId, reportIpfsCid, vintage, tonnes,
            uint8(rec.gpsScore), uint8(rec.ownershipScore),
            uint8(rec.anomalyScore), uint8(rec.satelliteScore)
        );
        rec.mintedTokenId = tokenId;
        emit CreditApproved(projectId, owner, tokenId, totalScore, reportIpfsCid);
    }

    function _handleFraud(
        bytes32 projectId,
        address owner,
        string[] calldata flags,
        string memory reportIpfsCid,
        bool gpsHardFail,
        bool ownershipHardFail
    ) internal {
        string memory reason = _buildFraudReason(gpsHardFail, ownershipHardFail);
        registry.flagFraud(projectId, reason);
        emit FraudAttemptLogged(projectId, owner, 0, flags, reportIpfsCid, block.timestamp);
    }

    function _handleRejection(
        bytes32 projectId,
        address owner,
        uint256 totalScore,
        string[] calldata flags
    ) internal {
        string memory reason = string(abi.encodePacked(
            "Score ", Strings.toString(totalScore), "/100 - minimum required: ", Strings.toString(MIN_SCORE)
        ));
        registry.rejectProject(projectId, reason);
        emit VerificationFailed(projectId, owner, totalScore, flags);
    }

    function _buildFraudReason(bool gpsHardFail, bool ownershipHardFail)
        internal pure returns (string memory)
    {
        if (gpsHardFail && ownershipHardFail) {
            return "Hard fail: GPS overlap >20% AND ownership GPS mismatch";
        } else if (gpsHardFail) {
            return "Hard fail: GPS overlap >20% - duplicate land claim detected";
        } else {
            return "Hard fail: ownership document GPS does not match submitted polygon";
        }
    }

    function getVerification(bytes32 projectId)
        external view returns (VerificationRecord memory)
    {
        return verifications[projectId];
    }

    function getAllVerifiedIds() external view returns (bytes32[] memory) {
        return _allVerifiedIds;
    }

    function getDocumentHash(bytes32 projectId) external view returns (bytes32) {
        return verifications[projectId].docHash;
    }

    /// @notice Prove a deed is the exact one that passed, without revealing it:
    ///         hash your copy and compare against what we stored.
    function verifyDocument(bytes32 projectId, bytes32 candidateHash)
        external view returns (bool)
    {
        bytes32 stored = verifications[projectId].docHash;
        return stored != bytes32(0) && stored == candidateHash;
    }

    /// @notice Every project ever flagged as a fraud attempt. On-chain forever —
    ///         that permanence is half the point.
    function getFraudAttempts() external view returns (bytes32[] memory) {
        uint256 count;
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].fraudAttempt) count++;
        }
        bytes32[] memory result = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].fraudAttempt) {
                result[j++] = _allVerifiedIds[i];
            }
        }
        return result;
    }

    function getApprovedCount() external view returns (uint256 count) {
        for (uint256 i = 0; i < _allVerifiedIds.length; i++) {
            if (verifications[_allVerifiedIds[i]].passed) count++;
        }
    }
}
