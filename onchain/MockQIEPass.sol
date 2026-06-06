// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";

// MockQIEPass
// The on-chain side of identity. Real QIE Pass KYC happens off-chain via
///         their REST API; once the backend confirms a wallet there, it (as owner)
///         calls issueIdentity() to mirror that result on-chain, which is what gates
///         document access. verifyMe() is the self-serve shortcut we only leave open
///         off mainnet — on mainnet, identities come through the KYC bridge, period.
contract MockQIEPass is Ownable {
    struct Identity {
        string  fullName;
        string  organization;
        uint256 verifiedAt;
        bool    verified;
    }

    mapping(address => Identity) public identities;
    address[] private _holders;

    event IdentityVerified(address indexed wallet, string fullName, string organization);
    event IdentityRevoked(address indexed wallet);

    constructor() Ownable(msg.sender) {}

    // Self-register an identity. Demo convenience only — the frontend hides
    ///         this on mainnet and routes everyone through real KYC instead.
    function verifyMe(string calldata fullName, string calldata organization) external {
        require(bytes(fullName).length > 0,     "QIEPass: name required");
        require(bytes(organization).length > 0, "QIEPass: organization required");

        if (!identities[msg.sender].verified) {
            _holders.push(msg.sender);
        }
        identities[msg.sender] = Identity({
            fullName:     fullName,
            organization: organization,
            verifiedAt:   block.timestamp,
            verified:     true
        });
        emit IdentityVerified(msg.sender, fullName, organization);
    }

    // The bridge: backend calls this after real QIE Pass KYC clears a wallet.
    function issueIdentity(address wallet, string calldata fullName, string calldata organization)
        external onlyOwner
    {
        if (!identities[wallet].verified) {
            _holders.push(wallet);
        }
        identities[wallet] = Identity(fullName, organization, block.timestamp, true);
        emit IdentityVerified(wallet, fullName, organization);
    }

    function revoke(address wallet) external onlyOwner {
        identities[wallet].verified = false;
        emit IdentityRevoked(wallet);
    }

    function isVerified(address wallet) external view returns (bool) {
        return identities[wallet].verified;
    }

    function getIdentity(address wallet)
        external view
        returns (string memory fullName, string memory organization, uint256 verifiedAt, bool verified)
    {
        Identity memory id = identities[wallet];
        return (id.fullName, id.organization, id.verifiedAt, id.verified);
    }

    function totalVerified() external view returns (uint256) {
        return _holders.length;
    }
}
