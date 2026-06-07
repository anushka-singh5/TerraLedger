// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import "@openzeppelin/contracts/token/ERC721/utils/ERC721Holder.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

// CarbonMarketplace
// List and buy CarbonCredit NFTs for QUSDC. A buy settles atomically —
///         QUSDC moves buyer→seller (+ optional fee) and the NFT moves seller→buyer
///         in one tx, so nobody can be left half-paid. The payment token is settable
///         so a deploy can be re-pointed at a different QUSDC without redeploying.
contract CarbonMarketplace is Ownable, ReentrancyGuard, ERC721Holder {
    IERC721 public immutable creditNFT;       // CarbonCredit
    IERC20  public           paymentToken;    // QUSDC

    address public treasury;
    uint96  public feeBps;                    // platform fee in bps (100 = 1%), off by default
    uint96  public constant MAX_FEE_BPS = 1000;   // never more than 10%, even by mistake

    struct Listing {
        address seller;
        uint256 price;     // in QUSDC smallest unit (6 decimals)
        bool    active;
    }

    mapping(uint256 => Listing) public listings;
    uint256[] private _listedTokenIds;        // for enumeration in the UI

    /// @notice Total QUSDC volume traded (raw 6-decimal units), ever.
    uint256 public totalVolume;
    /// @notice Total number of successful credit sales, ever.
    uint256 public totalSales;

    event Listed(uint256 indexed tokenId, address indexed seller, uint256 price);
    event Unlisted(uint256 indexed tokenId, address indexed seller);
    event Sold(uint256 indexed tokenId, address indexed seller, address indexed buyer, uint256 price, uint256 fee);
    event PaymentTokenUpdated(address indexed previous, address indexed next);
    event FeeUpdated(uint96 feeBps, address treasury);

    constructor(
        address creditNFT_,
        address paymentToken_,
        address treasury_
    ) Ownable(msg.sender) {
        require(creditNFT_ != address(0),     "Marketplace: zero NFT");
        require(paymentToken_ != address(0),  "Marketplace: zero payment token");
        creditNFT    = IERC721(creditNFT_);
        paymentToken = IERC20(paymentToken_);
        treasury     = treasury_ == address(0) ? msg.sender : treasury_;
    }

    // Re-point the marketplace at a different QUSDC contract.
    function setPaymentToken(address newToken) external onlyOwner {
        require(newToken != address(0), "Marketplace: zero token");
        emit PaymentTokenUpdated(address(paymentToken), newToken);
        paymentToken = IERC20(newToken);
    }

    function setFee(uint96 newFeeBps, address newTreasury) external onlyOwner {
        require(newFeeBps <= MAX_FEE_BPS, "Marketplace: fee too high");
        require(newTreasury != address(0), "Marketplace: zero treasury");
        feeBps   = newFeeBps;
        treasury = newTreasury;
        emit FeeUpdated(newFeeBps, newTreasury);
    }

    // List a credit. Seller has to approve() this contract on the NFT
    ///         first, otherwise the eventual safeTransferFrom on buy would revert.
    function list(uint256 tokenId, uint256 price) external {
        require(price > 0, "Marketplace: zero price");
        require(creditNFT.ownerOf(tokenId) == msg.sender, "Marketplace: not token owner");
        require(
            creditNFT.getApproved(tokenId) == address(this)
            || creditNFT.isApprovedForAll(msg.sender, address(this)),
            "Marketplace: approve marketplace on the NFT first"
        );

        if (!_isTracked(tokenId)) {
            _listedTokenIds.push(tokenId);
        }
        listings[tokenId] = Listing({seller: msg.sender, price: price, active: true});
        emit Listed(tokenId, msg.sender, price);
    }

    function unlist(uint256 tokenId) external {
        require(listings[tokenId].seller == msg.sender, "Marketplace: not seller");
        listings[tokenId].active = false;
        emit Unlisted(tokenId, msg.sender);
    }

    // Buy a listed credit. Buyer must have approved this contract for
    ///         `price` QUSDC first. nonReentrant because we touch an ERC20 + ERC721.
    function buy(uint256 tokenId) external nonReentrant {
        Listing memory item = listings[tokenId];
        require(item.active, "Marketplace: not listed");
        require(creditNFT.ownerOf(tokenId) == item.seller, "Marketplace: seller no longer owns NFT");
        require(msg.sender != item.seller, "Marketplace: cannot buy own listing");

        listings[tokenId].active = false;

        uint256 fee       = (item.price * feeBps) / 10_000;
        uint256 toSeller  = item.price - fee;

        // Money first, then the NFT — if any transfer reverts the whole buy unwinds.
        require(
            paymentToken.transferFrom(msg.sender, item.seller, toSeller),
            "Marketplace: QUSDC payment to seller failed"
        );
        if (fee > 0) {
            require(
                paymentToken.transferFrom(msg.sender, treasury, fee),
                "Marketplace: QUSDC fee transfer failed"
            );
        }

        creditNFT.safeTransferFrom(item.seller, msg.sender, tokenId);

        // Track cumulative volume and sales count for on-chain analytics.
        totalVolume += item.price;
        totalSales  += 1;

        emit Sold(tokenId, item.seller, msg.sender, item.price, fee);
    }

    function getListing(uint256 tokenId) external view returns (Listing memory) {
        return listings[tokenId];
    }

    // Only the live listings, unpacked into parallel arrays the UI can map over.
    function getActiveListings() external view returns (uint256[] memory ids, uint256[] memory prices, address[] memory sellers) {
        uint256 count;
        for (uint256 i = 0; i < _listedTokenIds.length; i++) {
            if (listings[_listedTokenIds[i]].active) count++;
        }
        ids     = new uint256[](count);
        prices  = new uint256[](count);
        sellers = new address[](count);
        uint256 j;
        for (uint256 i = 0; i < _listedTokenIds.length; i++) {
            uint256 id = _listedTokenIds[i];
            if (listings[id].active) {
                ids[j]     = id;
                prices[j]  = listings[id].price;
                sellers[j] = listings[id].seller;
                j++;
            }
        }
    }

    // Have we ever pushed this token into _listedTokenIds? Avoids dup entries on relist.
    function _isTracked(uint256 tokenId) internal view returns (bool) {
        return listings[tokenId].seller != address(0);
    }
}
