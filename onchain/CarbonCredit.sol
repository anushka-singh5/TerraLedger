// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/Base64.sol";
import "@openzeppelin/contracts/utils/Strings.sol";

// minimal read — only needed so tokenURI can reflect Listed status
interface IMarketplaceListing {
    struct Listing { address seller; uint256 price; bool active; }
    function getListing(uint256 tokenId) external view returns (Listing memory);
}

// read isFullyRetired() from TCC so tokenURI flips to "Fully Offset" automatically
interface ITCCToken {
    function isFullyRetired(bytes32 projectId) external view returns (bool);
}

contract CarbonCredit is ERC721, Ownable {
    using Strings for uint256;

    struct CreditData {
        bytes32 projectId;
        uint256 vintage;
        uint256 tonnes;
        string  ipfsCid;
        bool    retired;
        uint256 retiredAt;
        address retiredBy;
        // scores stored on-chain so a buyer can read trust level without hitting IPFS
        uint16  score;
        uint8   gpsScore;
        uint8   ownershipScore;
        uint8   anomalyScore;
        uint8   satelliteScore;
    }

    address public oracle;
    address public marketplace; // optional — enables "Listed" status in tokenURI
    address public tccToken;    // optional — enables "Fully Offset" status in tokenURI

    uint256 private _nextTokenId;
    uint256 private _retiredCount;

    mapping(uint256 => CreditData) public credits;
    mapping(bytes32 => uint256[])  public projectTokens;

    event OracleUpdated(address indexed previous, address indexed next);
    event MarketplaceUpdated(address indexed previous, address indexed next);
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
        ERC721("TerraLedger Certificate", "TLCERT")
        Ownable(msg.sender)
    {
        require(initialOracle != address(0), "CarbonCredit: zero oracle");
        oracle = initialOracle;
    }

    // soulbound: only allow minting (from == address(0)), block all transfers
    function _update(address to, uint256 tokenId, address auth)
        internal override returns (address)
    {
        address from = _ownerOf(tokenId);
        require(from == address(0), "CarbonCredit: certificate is soulbound - non-transferable");
        return super._update(to, tokenId, auth);
    }

    function approve(address, uint256) public pure override {
        revert("CarbonCredit: approvals disabled - soulbound");
    }

    function setApprovalForAll(address, bool) public pure override {
        revert("CarbonCredit: approvals disabled - soulbound");
    }

    function setOracle(address newOracle) external onlyOwner {
        require(newOracle != address(0), "CarbonCredit: zero oracle");
        emit OracleUpdated(oracle, newOracle);
        oracle = newOracle;
    }

    function setMarketplace(address mp) external onlyOwner {
        emit MarketplaceUpdated(marketplace, mp);
        marketplace = mp;
    }

    function setTCCToken(address tcc_) external onlyOwner {
        tccToken = tcc_;
    }

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
        require(to != address(0),             "CarbonCredit: mint to zero address");
        require(tonnes > 0,                   "CarbonCredit: zero tonnes");
        require(bytes(ipfsCid).length > 0,    "CarbonCredit: empty IPFS CID");

        uint16 total = uint16(gpsScore) + ownershipScore + anomalyScore + satelliteScore;
        tokenId = _nextTokenId++;
        _safeMint(to, tokenId);

        credits[tokenId] = CreditData({
            projectId:      projectId,
            vintage:        vintage,
            tonnes:         tonnes,
            ipfsCid:        ipfsCid,
            retired:        false,
            retiredAt:      0,
            retiredBy:      address(0),
            score:          total,
            gpsScore:       gpsScore,
            ownershipScore: ownershipScore,
            anomalyScore:   anomalyScore,
            satelliteScore: satelliteScore
        });
        projectTokens[projectId].push(tokenId);
        emit CreditMinted(tokenId, projectId, to, tonnes, ipfsCid);
    }

    // marks cert retired; NFT stays in wallet as permanent proof — no burn
    function retire(uint256 tokenId) external {
        require(ownerOf(tokenId) == msg.sender, "CarbonCredit: not token owner");
        CreditData storage c = credits[tokenId];
        require(!c.retired, "CarbonCredit: already retired");
        c.retired   = true;
        c.retiredAt = block.timestamp;
        c.retiredBy = msg.sender;
        _retiredCount++;
        emit CreditRetired(tokenId, c.projectId, msg.sender, c.tonnes, block.timestamp);
    }

    function totalMinted() external view returns (uint256) { return _nextTokenId; }
    function totalRetired() external view returns (uint256) { return _retiredCount; }

    function tokensOfOwner(address owner_) external view returns (uint256[] memory) {
        require(owner_ != address(0), "CarbonCredit: zero address");
        uint256 total = _nextTokenId;
        uint256 count = 0;
        for (uint256 i = 0; i < total; i++) {
            if (!credits[i].retired && _ownerOf(i) == owner_) count++;
        }
        uint256[] memory ids = new uint256[](count);
        uint256 j = 0;
        for (uint256 i = 0; i < total; i++) {
            if (!credits[i].retired && _ownerOf(i) == owner_) ids[j++] = i;
        }
        return ids;
    }

    function getProjectTokens(bytes32 projectId) external view returns (uint256[] memory) {
        return projectTokens[projectId];
    }

    function contractURI() external pure returns (string memory) {
        string memory svg = _buildBannerSVG();
        string memory imageUri = string(abi.encodePacked(
            "data:image/svg+xml;base64,",
            Base64.encode(bytes(svg))
        ));
        string memory json = string(abi.encodePacked(
            '{"name":"TerraLedger \xe2\x80\x94 Verified Carbon Credits",',
            '"description":"AI-verified carbon credits on QIE Blockchain. Five AI modules gate every mint: GPS duplicate detection, ownership forensics (OCR+ELA), anomaly AI (IsolationForest), NASA FIRMS satellite, and Llama-3 audit. Fraudulent credit cannot be created in the first place. Every score and fraud attempt is on-chain forever.",',
            '"image":"', imageUri, '",',
            '"external_link":"https://terra-ledger-plum.vercel.app",',
            '"seller_fee_basis_points":0,',
            '"fee_recipient":"0x0000000000000000000000000000000000000000"}'
        ));
        return string(abi.encodePacked(
            "data:application/json;base64,",
            Base64.encode(bytes(json))
        ));
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        CreditData memory c = credits[tokenId];
        require(c.tonnes > 0, "CarbonCredit: nonexistent token");

        string memory status = _statusOf(tokenId, c.retired, c.projectId);
        string memory sColor = _colorForStatus(status);
        string memory projectHex = _bytes32ToHex(c.projectId);

        string memory svgRaw = string(abi.encodePacked(
            _svgTop(sColor),
            _svgMid(tokenId, c, sColor),
            _svgBot(c, status, sColor)
        ));
        string memory imageUri = string(abi.encodePacked(
            "data:image/svg+xml;base64,",
            Base64.encode(bytes(svgRaw))
        ));

        string memory attrsPart1 = string(abi.encodePacked(
            '{"trait_type":"Project ID","value":"0x', projectHex, '"},',
            '{"trait_type":"Vintage Year","value":', c.vintage.toString(), '},',
            '{"trait_type":"Tonnes CO2","value":', c.tonnes.toString(), '},',
            '{"trait_type":"AI Score","value":', uint256(c.score).toString(), '},'
        ));
        string memory attrsPart2 = string(abi.encodePacked(
            '{"trait_type":"GPS Score","value":', uint256(c.gpsScore).toString(), '},',
            '{"trait_type":"Ownership Score","value":', uint256(c.ownershipScore).toString(), '},',
            '{"trait_type":"Anomaly Score","value":', uint256(c.anomalyScore).toString(), '},',
            '{"trait_type":"Satellite Score","value":', uint256(c.satelliteScore).toString(), '},',
            '{"trait_type":"Status","value":"', status, '"}'
        ));

        string memory json = string(abi.encodePacked(
            '{"name":"TerraLedger Certificate #', tokenId.toString(),
            '","description":"Soulbound AI verification certificate. Proves that ',
            c.tonnes.toString(), ' TCC token(s) (1 TCC = 1 tonne CO2) were issued after passing 5 AI checks: GPS overlap, ownership forensics, anomaly detection, NASA satellite, and Llama-3 audit. Non-transferable. Project 0x', projectHex,
            '","image":"', imageUri,
            '","attributes":[', attrsPart1, attrsPart2,
            '],"external_url":"https://ipfs.io/ipfs/', c.ipfsCid, '"}'
        ));

        return string(abi.encodePacked(
            "data:application/json;base64,",
            Base64.encode(bytes(json))
        ));
    }

    function _statusOf(uint256 tokenId, bool retired_, bytes32 projectId)
        internal view returns (string memory)
    {
        if (retired_) return "Retired";
        if (tccToken != address(0)) {
            try ITCCToken(tccToken).isFullyRetired(projectId) returns (bool fullyRetired) {
                if (fullyRetired) return "Fully Offset";
            } catch {}
        }
        if (marketplace != address(0)) {
            try IMarketplaceListing(marketplace).getListing(tokenId) returns
                (IMarketplaceListing.Listing memory l)
            {
                if (l.active) return "Listed";
            } catch {}
        }
        return "Active";
    }

    function _colorForStatus(string memory status) internal pure returns (string memory) {
        bytes32 h = keccak256(bytes(status));
        if (h == keccak256(bytes("Retired")))      return "#6b7280";
        if (h == keccak256(bytes("Fully Offset"))) return "#22d3ee";
        if (h == keccak256(bytes("Listed")))       return "#f5a623";
        return "#00e57a";
    }

    // split into three helpers to stay under Solidity's stack depth limit
    function _svgTop(string memory sColor) internal pure returns (string memory) {
        return string(abi.encodePacked(
            '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">',
            '<rect width="400" height="400" rx="20" fill="#040507"/>',
            '<circle cx="200" cy="150" r="190" fill="', sColor, '" fill-opacity="0.07"/>',
            '<rect x="1" y="1" width="398" height="398" rx="20" fill="none" stroke="#1c2035" stroke-width="1.5"/>',
            '<text x="28" y="44" fill="#00e57a" font-family="monospace" font-size="11" font-weight="700" letter-spacing="3">TERRALEDGER</text>',
            '<text x="28" y="62" fill="#6b7280" font-family="monospace" font-size="10" letter-spacing="1">VERIFIED CARBON CREDIT</text>'
        ));
    }

    function _svgMid(uint256 tokenId, CreditData memory c, string memory sColor)
        internal pure returns (string memory)
    {
        return string(abi.encodePacked(
            '<text x="200" y="155" fill="#e8eaf0" font-family="monospace" font-size="64" font-weight="700" text-anchor="middle">#', tokenId.toString(), '</text>',
            '<text x="200" y="196" fill="', sColor, '" font-family="monospace" font-size="22" text-anchor="middle" font-weight="600">', uint256(c.score).toString(), ' / 100</text>',
            '<text x="200" y="216" fill="#6b7280" font-family="monospace" font-size="9" text-anchor="middle" letter-spacing="2">AI VERIFICATION SCORE</text>',
            '<line x1="28" y1="238" x2="372" y2="238" stroke="#1c2035" stroke-width="1"/>',
            '<text x="28" y="263" fill="#9aa0b0" font-family="monospace" font-size="11">TONNES CO2</text>',
            '<text x="372" y="263" fill="#e8eaf0" font-family="monospace" font-size="13" font-weight="600" text-anchor="end">', c.tonnes.toString(), ' t</text>',
            '<text x="28" y="287" fill="#9aa0b0" font-family="monospace" font-size="11">VINTAGE</text>',
            '<text x="372" y="287" fill="#e8eaf0" font-family="monospace" font-size="13" font-weight="600" text-anchor="end">', c.vintage.toString(), '</text>'
        ));
    }

    function _svgBot(CreditData memory c, string memory status, string memory sColor)
        internal pure returns (string memory)
    {
        // "Fully Offset" needs wider badge than the others
        bool wide = keccak256(bytes(status)) == keccak256(bytes("Fully Offset"));
        string memory badgeW  = wide ? "130" : "96";
        string memory badgeCX = wide ? "93"  : "76";
        return string(abi.encodePacked(
            '<rect x="28" y="310" width="', badgeW, '" height="26" rx="6" fill="', sColor, '" fill-opacity="0.12" stroke="', sColor, '" stroke-opacity="0.4" stroke-width="1"/>',
            '<text x="', badgeCX, '" y="327" fill="', sColor, '" font-family="monospace" font-size="11" font-weight="700" text-anchor="middle" letter-spacing="1">', status, '</text>',
            '<text x="28" y="374" fill="#4b5563" font-family="monospace" font-size="10">',
            'GPS ', uint256(c.gpsScore).toString(),
            '  |  OWN ', uint256(c.ownershipScore).toString(),
            '  |  ANO ', uint256(c.anomalyScore).toString(),
            '  |  SAT ', uint256(c.satelliteScore).toString(),
            '</text></svg>'
        ));
    }

    function _buildBannerSVG() internal pure returns (string memory) {
        string memory part1 = string(abi.encodePacked(
            '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300" viewBox="0 0 600 300">',
            '<rect width="600" height="300" rx="20" fill="#040507"/>',
            '<rect width="600" height="300" rx="20" fill="#00e57a" fill-opacity="0.06"/>',
            '<rect x="1" y="1" width="598" height="298" rx="20" fill="none" stroke="#1c2035" stroke-width="1.5"/>',
            '<text x="40" y="76" fill="#00e57a" font-family="monospace" font-size="34" font-weight="700" letter-spacing="-1">TerraLedger</text>',
            '<text x="40" y="110" fill="#e8eaf0" font-family="monospace" font-size="15">AI Verification Certificate  |  Soulbound</text>',
            '<text x="40" y="132" fill="#6b7280" font-family="monospace" font-size="12">Chain 1990  |  Non-transferable  |  Fraud logged on-chain forever</text>',
            '<line x1="40" y1="155" x2="560" y2="155" stroke="#1c2035" stroke-width="1"/>'
        ));
        string memory part2 = string(abi.encodePacked(
            '<text x="40" y="181" fill="#9aa0b0" font-family="monospace" font-size="12">No passing AI = No NFT</text>',
            '<rect x="40" y="204" width="110" height="22" rx="6" fill="#00e57a" fill-opacity="0.1" stroke="#00e57a" stroke-opacity="0.3" stroke-width="1"/>',
            '<text x="95" y="219" fill="#00e57a" font-family="monospace" font-size="9" text-anchor="middle" font-weight="700">5 AI MODULES</text>',
            '<rect x="158" y="204" width="110" height="22" rx="6" fill="#4f8cff" fill-opacity="0.1" stroke="#4f8cff" stroke-opacity="0.3" stroke-width="1"/>',
            '<text x="213" y="219" fill="#4f8cff" font-family="monospace" font-size="9" text-anchor="middle" font-weight="700">M-of-N ORACLE</text>',
            '<rect x="276" y="204" width="110" height="22" rx="6" fill="#9d7bff" fill-opacity="0.1" stroke="#9d7bff" stroke-opacity="0.3" stroke-width="1"/>',
            '<text x="331" y="219" fill="#9d7bff" font-family="monospace" font-size="9" text-anchor="middle" font-weight="700">NASA SATELLITE</text>',
            '<rect x="394" y="204" width="110" height="22" rx="6" fill="#f5a623" fill-opacity="0.1" stroke="#f5a623" stroke-opacity="0.3" stroke-width="1"/>',
            '<text x="449" y="219" fill="#f5a623" font-family="monospace" font-size="9" text-anchor="middle" font-weight="700">QIE MAINNET</text>',
            '<text x="40" y="268" fill="#374151" font-family="monospace" font-size="10">ERC-721  |  On-chain scores  |  IPFS audit report  |  QIE Pass KYC  |  QUSDC settlement</text>',
            '</svg>'
        ));
        return string(abi.encodePacked(part1, part2));
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
