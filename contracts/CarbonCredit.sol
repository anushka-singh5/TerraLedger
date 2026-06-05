// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/Base64.sol";
import "@openzeppelin/contracts/utils/Strings.sol";

/// @title CarbonCredit
/// @notice One token = one verified batch of CO2 tonnes. Only the oracle can mint
///         (it's the thing that ran the AI checks). retire() burns the token but
///         keeps the record. Metadata is fully on-chain + links the IPFS audit, so
///         a buyer never has to trust our server to see why a credit passed.
contract CarbonCredit is ERC721, Ownable {
    using Strings for uint256;

    struct CreditData {
        bytes32 projectId;
        uint256 vintage;       // year of carbon removal
        uint256 tonnes;        // verified CO2 tonnes this token represents
        string  ipfsCid;       // IPFS CID of the AI-generated audit report PDF
        bool    retired;
        uint256 retiredAt;
        address retiredBy;
        // Scores live on-chain so a buyer reads the trust score straight from the
        // token — no round-trip to the backend or the IPFS audit JSON.
        uint16  score;          // total, 0-100
        uint8   gpsScore;       // 0-30
        uint8   ownershipScore; // 0-25
        uint8   anomalyScore;   // 0-25
        uint8   satelliteScore; // 0-20
    }

    address public oracle;
    uint256 private _nextTokenId;

    mapping(uint256 => CreditData) public credits;
    mapping(bytes32 => uint256[]) public projectTokens;

    event OracleUpdated(address indexed previous, address indexed next);
    event CreditMinted(
        uint256 indexed tokenId,
        bytes32 indexed projectId,
        address indexed recipient,
        uint256 tonnes,
        string  ipfsCid
    );
    event CreditRetired(
        uint256 indexed tokenId,
        bytes32 indexed projectId,
        address indexed retiredBy,
        uint256 tonnes,
        uint256 retiredAt
    );

    modifier onlyOracle() {
        require(msg.sender == oracle, "CarbonCredit: not oracle");
        _;
    }

    constructor(address initialOracle)
        ERC721("TerraLedger Credit", "TLC")
        Ownable(msg.sender)
    {
        require(initialOracle != address(0), "CarbonCredit: zero oracle");
        oracle = initialOracle;
    }

    function setOracle(address newOracle) external onlyOwner {
        require(newOracle != address(0), "CarbonCredit: zero oracle");
        emit OracleUpdated(oracle, newOracle);
        oracle = newOracle;
    }

    /// @notice Mint a credit. Oracle-only; it calls this once the backend clears a
    ///         project (score >= 70 and no hard-fail). The 70 cutoff is enforced
    ///         off-chain — by the time we're here, the decision is already made.
    function mint(
        address   to,
        bytes32   projectId,
        string calldata ipfsCid,
        uint256   vintage,
        uint256   tonnes,
        uint8     gpsScore,
        uint8     ownershipScore,
        uint8     anomalyScore,
        uint8     satelliteScore
    ) external onlyOracle returns (uint256 tokenId) {
        require(to != address(0), "CarbonCredit: mint to zero address");
        require(tonnes > 0,       "CarbonCredit: zero tonnes");
        require(bytes(ipfsCid).length > 0, "CarbonCredit: empty IPFS CID");

        uint16 total = uint16(gpsScore) + ownershipScore + anomalyScore + satelliteScore;

        tokenId = _nextTokenId++;
        _safeMint(to, tokenId);

        credits[tokenId] = CreditData({
            projectId: projectId,
            vintage:   vintage,
            tonnes:    tonnes,
            ipfsCid:   ipfsCid,
            retired:   false,
            retiredAt: 0,
            retiredBy: address(0),
            score:          total,
            gpsScore:       gpsScore,
            ownershipScore: ownershipScore,
            anomalyScore:   anomalyScore,
            satelliteScore: satelliteScore
        });
        projectTokens[projectId].push(tokenId);

        emit CreditMinted(tokenId, projectId, to, tonnes, ipfsCid);
    }

    /// @notice Retire a credit: burn the token so the offset can't be double-counted.
    ///         We burn but deliberately keep the CreditData around — credits[id] and
    ///         tokenURI(id) still resolve, which is what makes the retirement provable.
    function retire(uint256 tokenId) external {
        require(ownerOf(tokenId) == msg.sender, "CarbonCredit: not token owner");

        CreditData storage c = credits[tokenId];
        c.retired   = true;
        c.retiredAt = block.timestamp;
        c.retiredBy = msg.sender;

        _burn(tokenId);

        emit CreditRetired(tokenId, c.projectId, msg.sender, c.tonnes, block.timestamp);
    }

    /// @notice On-chain JSON metadata, works for live and retired tokens alike —
    ///         a retired token has no owner but its proof should never 404.
    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        CreditData memory c = credits[tokenId];
        // mint enforces tonnes > 0, so this doubles as the existence check.
        require(c.tonnes > 0, "CarbonCredit: nonexistent token");

        string memory status = c.retired ? "Retired" : "Active";
        string memory projectHex = _bytes32ToHex(c.projectId);

        string memory json = string(abi.encodePacked(
            '{"name":"TerraLedger Credit #', tokenId.toString(),
            '","description":"AI-verified carbon credit. ',
            c.tonnes.toString(), ' tonne(s) CO2 | Project 0x', projectHex,
            '","attributes":[',
                '{"trait_type":"Project ID","value":"0x', projectHex, '"},',
                '{"trait_type":"Vintage Year","value":', c.vintage.toString(), '},',
                '{"trait_type":"Tonnes CO2","value":', c.tonnes.toString(), '},',
                '{"trait_type":"AI Score","value":', uint256(c.score).toString(), '},',
                '{"trait_type":"GPS Score","value":', uint256(c.gpsScore).toString(), '},',
                '{"trait_type":"Ownership Score","value":', uint256(c.ownershipScore).toString(), '},',
                '{"trait_type":"Anomaly Score","value":', uint256(c.anomalyScore).toString(), '},',
                '{"trait_type":"Satellite Score","value":', uint256(c.satelliteScore).toString(), '},',
                '{"trait_type":"Status","value":"', status, '"}',
            '],"external_url":"https://ipfs.io/ipfs/', c.ipfsCid, '"}'
        ));

        return string(abi.encodePacked(
            "data:application/json;base64,",
            Base64.encode(bytes(json))
        ));
    }

    function getProjectTokens(bytes32 projectId) external view returns (uint256[] memory) {
        return projectTokens[projectId];
    }

    function totalMinted() external view returns (uint256) {
        return _nextTokenId;
    }

    function _bytes32ToHex(bytes32 b) internal pure returns (string memory) {
        bytes memory hex_ = "0123456789abcdef";
        bytes memory str  = new bytes(64);
        for (uint256 i = 0; i < 32; i++) {
            str[i * 2]     = hex_[uint8(b[i] >> 4)];
            str[i * 2 + 1] = hex_[uint8(b[i] & 0x0f)];
        }
        return string(str);
    }
}
