// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/Base64.sol";
import "@openzeppelin/contracts/utils/Strings.sol";

/// @title RetirementCertificate — TLRET
/// @notice Soulbound ERC-721 minted automatically when TCC tokens are retired.
///         Proves on-chain that the holder permanently offset X tonnes of CO₂.
///         Non-transferable — the proof stays with whoever retired the credits.
contract RetirementCertificate is ERC721, Ownable {
    using Strings for uint256;

    struct RetirementData {
        address retiredBy;
        uint256 amount;     // tonnes CO₂ offset
        bytes32 projectId;
        uint256 timestamp;
    }

    address public tccContract;
    uint256 private _nextTokenId;

    mapping(uint256 => RetirementData) public retirements;
    mapping(address => uint256[])      private _walletTokens;

    event RetirementCertMinted(
        uint256 indexed tokenId,
        address indexed retiredBy,
        uint256         amount,
        bytes32 indexed projectId
    );

    modifier onlyTCC() {
        require(msg.sender == tccContract, "TLRET: only TCC contract");
        _;
    }

    constructor() ERC721("TerraLedger Retirement Certificate", "TLRET") Ownable(msg.sender) {}

    // ── Soulbound ─────────────────────────────────────────────────────────

    function _update(address to, uint256 tokenId, address auth)
        internal override returns (address)
    {
        address from = _ownerOf(tokenId);
        require(from == address(0), "TLRET: soulbound - non-transferable");
        return super._update(to, tokenId, auth);
    }

    function approve(address, uint256) public pure override {
        revert("TLRET: approvals disabled - soulbound");
    }

    function setApprovalForAll(address, bool) public pure override {
        revert("TLRET: approvals disabled - soulbound");
    }

    // ── Admin ─────────────────────────────────────────────────────────────

    function setTCCContract(address tcc_) external onlyOwner {
        require(tcc_ != address(0), "TLRET: zero address");
        tccContract = tcc_;
    }

    // ── Mint — called by CarbonCreditToken on every retire() ──────────────

    function mint(address to, uint256 amount, bytes32 projectId)
        external onlyTCC returns (uint256 tokenId)
    {
        require(to     != address(0), "TLRET: zero address");
        require(amount > 0,           "TLRET: zero amount");

        tokenId = _nextTokenId++;
        _safeMint(to, tokenId);

        retirements[tokenId] = RetirementData({
            retiredBy: to,
            amount:    amount,
            projectId: projectId,
            timestamp: block.timestamp
        });
        _walletTokens[to].push(tokenId);

        emit RetirementCertMinted(tokenId, to, amount, projectId);
    }

    // ── Read API ──────────────────────────────────────────────────────────

    function getWalletCerts(address wallet) external view returns (uint256[] memory) {
        return _walletTokens[wallet];
    }

    function totalMinted() external view returns (uint256) { return _nextTokenId; }

    // ── tokenURI — fully on-chain SVG ─────────────────────────────────────

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        RetirementData memory r = retirements[tokenId];
        require(r.timestamp != 0, "TLRET: nonexistent token");

        string memory imageUri = string(abi.encodePacked(
            "data:image/svg+xml;base64,",
            Base64.encode(bytes(_buildSVG(tokenId, r)))
        ));
        string memory projectHex = _bytes32ToHex(r.projectId);

        string memory json = string(abi.encodePacked(
            '{"name":"Carbon Offset Certificate #', tokenId.toString(),
            '","description":"Soulbound proof that ', r.amount.toString(),
            ' tonne(s) of CO2 were permanently retired on QIE Blockchain. Project: 0x', projectHex,
            '. Non-transferable - the offset stays with whoever retired the credits.",',
            '"image":"', imageUri, '",',
            '"attributes":[',
            '{"trait_type":"Tonnes Retired","value":', r.amount.toString(), '},',
            '{"trait_type":"Project ID","value":"0x', projectHex, '"},',
            '{"trait_type":"Retired By","value":"', Strings.toHexString(r.retiredBy), '"},',
            '{"trait_type":"Timestamp","value":', r.timestamp.toString(), '}',
            ']}'
        ));

        return string(abi.encodePacked(
            "data:application/json;base64,",
            Base64.encode(bytes(json))
        ));
    }

    // ── SVG helpers ───────────────────────────────────────────────────────

    function _buildSVG(uint256 tokenId, RetirementData memory r)
        internal pure returns (string memory)
    {
        return string(abi.encodePacked(_svgTop(tokenId, r.amount), _svgBot(r)));
    }

    function _svgTop(uint256 tokenId, uint256 amount) internal pure returns (string memory) {
        return string(abi.encodePacked(
            '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">',
            '<rect width="400" height="400" rx="20" fill="#040507"/>',
            '<circle cx="200" cy="160" r="190" fill="#22d3ee" fill-opacity="0.07"/>',
            '<rect x="1" y="1" width="398" height="398" rx="20" fill="none" stroke="#1c2035" stroke-width="1.5"/>',
            '<text x="28" y="44" fill="#22d3ee" font-family="monospace" font-size="11" font-weight="700" letter-spacing="3">TERRALEDGER</text>',
            '<text x="28" y="62" fill="#6b7280" font-family="monospace" font-size="10" letter-spacing="1">CARBON OFFSET CERTIFICATE</text>',
            '<text x="200" y="152" fill="#e8eaf0" font-family="monospace" font-size="64" font-weight="700" text-anchor="middle">#', tokenId.toString(), '</text>',
            '<text x="200" y="192" fill="#22d3ee" font-family="monospace" font-size="26" text-anchor="middle" font-weight="600">', amount.toString(), ' t CO2</text>',
            '<text x="200" y="212" fill="#6b7280" font-family="monospace" font-size="9" text-anchor="middle" letter-spacing="2">PERMANENTLY OFFSET</text>',
            '<line x1="28" y1="234" x2="372" y2="234" stroke="#1c2035" stroke-width="1"/>'
        ));
    }

    function _svgBot(RetirementData memory r) internal pure returns (string memory) {
        return string(abi.encodePacked(
            '<text x="28" y="260" fill="#9aa0b0" font-family="monospace" font-size="11">PROJECT</text>',
            '<text x="372" y="260" fill="#e8eaf0" font-family="monospace" font-size="10" text-anchor="end">', _pidShort(r.projectId), '</text>',
            '<text x="28" y="284" fill="#9aa0b0" font-family="monospace" font-size="11">RETIRED BY</text>',
            '<text x="372" y="284" fill="#e8eaf0" font-family="monospace" font-size="10" text-anchor="end">', _addrShort(r.retiredBy), '</text>',
            '<text x="28" y="308" fill="#9aa0b0" font-family="monospace" font-size="11">BLOCK TIME</text>',
            '<text x="372" y="308" fill="#e8eaf0" font-family="monospace" font-size="10" text-anchor="end">', r.timestamp.toString(), '</text>',
            '<rect x="28" y="328" width="130" height="26" rx="6" fill="#22d3ee" fill-opacity="0.12" stroke="#22d3ee" stroke-opacity="0.4" stroke-width="1"/>',
            '<text x="93" y="345" fill="#22d3ee" font-family="monospace" font-size="11" font-weight="700" text-anchor="middle" letter-spacing="1">FULLY OFFSET</text>',
            '<text x="28" y="382" fill="#4b5563" font-family="monospace" font-size="10">QIE Chain 1990  |  Soulbound  |  Non-transferable</text>',
            '</svg>'
        ));
    }

    // ── Byte helpers ──────────────────────────────────────────────────────

    function _pidShort(bytes32 pid) internal pure returns (string memory) {
        bytes memory hex_ = "0123456789abcdef";
        bytes memory r = new bytes(13); // "0x" + 8 hex + "..."
        r[0] = '0'; r[1] = 'x';
        for (uint256 i = 0; i < 4; i++) {
            r[2 + i * 2] = hex_[uint8(pid[i] >> 4)];
            r[3 + i * 2] = hex_[uint8(pid[i] & 0x0f)];
        }
        r[10] = '.'; r[11] = '.'; r[12] = '.';
        return string(r);
    }

    function _addrShort(address addr) internal pure returns (string memory) {
        bytes memory b = bytes(Strings.toHexString(addr)); // 42 chars: "0x" + 40 hex
        bytes memory r = new bytes(13); // "0x1234...5678"
        for (uint256 i = 0; i < 6; i++) r[i] = b[i];
        r[6] = '.'; r[7] = '.'; r[8] = '.';
        r[9] = b[38]; r[10] = b[39]; r[11] = b[40]; r[12] = b[41];
        return string(r);
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
