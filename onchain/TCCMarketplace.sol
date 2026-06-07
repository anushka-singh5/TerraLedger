// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

contract TCCMarketplace is Ownable, ReentrancyGuard {

    IERC20 public immutable tcc;
    IERC20 public           qusdc;

    address public treasury;
    uint96  public feeBps;
    uint96  public constant MAX_FEE_BPS = 1000;

    struct Listing {
        address seller;
        uint256 amountLeft;
        uint256 pricePerTonne;  // QUSDC per TCC (6 decimals)
        bytes32 projectId;      // source project — buyer needs this to retire correctly
        bool    active;
    }

    mapping(uint256 => Listing) public listings;
    uint256 public nextListingId;

    uint256 public totalVolume;
    uint256 public totalSales;

    event Listed(uint256 indexed listingId, address indexed seller, uint256 amount, uint256 pricePerTonne, bytes32 indexed projectId);
    event Unlisted(uint256 indexed listingId, address indexed seller, uint256 remaining);
    event Sold(uint256 indexed listingId, address indexed seller, address indexed buyer, uint256 amount, uint256 quscPaid, uint256 fee);
    event FeeUpdated(uint96 feeBps, address treasury);

    constructor(address tcc_, address qusdc_, address treasury_) Ownable(msg.sender) {
        require(tcc_   != address(0), "TCCMkt: zero TCC");
        require(qusdc_ != address(0), "TCCMkt: zero QUSDC");
        tcc    = IERC20(tcc_);
        qusdc  = IERC20(qusdc_);
        treasury = treasury_ == address(0) ? msg.sender : treasury_;
    }

    function setFee(uint96 newFeeBps, address newTreasury) external onlyOwner {
        require(newFeeBps <= MAX_FEE_BPS, "TCCMkt: fee too high");
        require(newTreasury != address(0), "TCCMkt: zero treasury");
        feeBps = newFeeBps; treasury = newTreasury;
        emit FeeUpdated(newFeeBps, newTreasury);
    }

    // projectId stored on-chain so buyers always know which project to retire against
    function list(uint256 amount, uint256 pricePerTonne, bytes32 projectId) external returns (uint256 listingId) {
        require(amount > 0,         "TCCMkt: zero amount");
        require(pricePerTonne > 0,  "TCCMkt: zero price");
        require(projectId != bytes32(0), "TCCMkt: zero projectId");
        require(tcc.transferFrom(msg.sender, address(this), amount), "TCCMkt: TCC transfer failed");
        listingId = nextListingId++;
        listings[listingId] = Listing({seller: msg.sender, amountLeft: amount, pricePerTonne: pricePerTonne, projectId: projectId, active: true});
        emit Listed(listingId, msg.sender, amount, pricePerTonne, projectId);
    }

    function buy(uint256 listingId, uint256 amount) external nonReentrant {
        Listing storage item = listings[listingId];
        require(item.active, "TCCMkt: not active");
        require(amount > 0, "TCCMkt: zero amount");
        require(amount <= item.amountLeft, "TCCMkt: not enough TCC left");
        require(msg.sender != item.seller, "TCCMkt: cannot buy own listing");

        uint256 totalCost = amount * item.pricePerTonne;
        uint256 fee       = (totalCost * feeBps) / 10_000;
        uint256 toSeller  = totalCost - fee;

        item.amountLeft -= amount;
        if (item.amountLeft == 0) item.active = false;

        require(qusdc.transferFrom(msg.sender, item.seller, toSeller),  "TCCMkt: QUSDC to seller failed");
        if (fee > 0) require(qusdc.transferFrom(msg.sender, treasury, fee), "TCCMkt: fee failed");

        require(tcc.transfer(msg.sender, amount), "TCCMkt: TCC transfer failed");

        totalVolume += totalCost;
        totalSales  += 1;

        emit Sold(listingId, item.seller, msg.sender, amount, totalCost, fee);
    }

    function unlist(uint256 listingId) external {
        Listing storage item = listings[listingId];
        require(item.seller == msg.sender, "TCCMkt: not seller");
        require(item.active,               "TCCMkt: not active");
        uint256 remaining = item.amountLeft;
        item.active = false;
        item.amountLeft = 0;
        require(tcc.transfer(msg.sender, remaining), "TCCMkt: return failed");
        emit Unlisted(listingId, msg.sender, remaining);
    }

    function getActiveListings() external view returns (
        uint256[]  memory ids,
        address[]  memory sellers,
        uint256[]  memory amounts,
        uint256[]  memory prices,
        bytes32[]  memory projectIds
    ) {
        uint256 count;
        for (uint256 i; i < nextListingId; i++) { if (listings[i].active) count++; }
        ids        = new uint256[](count);
        sellers    = new address[](count);
        amounts    = new uint256[](count);
        prices     = new uint256[](count);
        projectIds = new bytes32[](count);
        uint256 j;
        for (uint256 i; i < nextListingId; i++) {
            if (listings[i].active) {
                ids[j]        = i;
                sellers[j]    = listings[i].seller;
                amounts[j]    = listings[i].amountLeft;
                prices[j]     = listings[i].pricePerTonne;
                projectIds[j] = listings[i].projectId;
                j++;
            }
        }
    }
}
