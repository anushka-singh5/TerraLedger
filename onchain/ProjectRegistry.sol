// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";

contract ProjectRegistry is Ownable {
    enum ProjectStatus { Pending, Approved, Rejected, FraudFlagged }

    struct Project {
        bytes32 id;
        address owner;
        string polygonGeoJSON;
        uint256 areaHectares;
        uint256 claimedTonnes;
        uint256 submittedAt;
        ProjectStatus status;
        string statusReason;
        // bbox in microdegrees (lat/lng × 1e6) — no floats in Solidity.
        // used as a cheap pre-filter in findOverlaps(); Shapely does the real check off-chain.
        int64 minLat;
        int64 minLng;
        int64 maxLat;
        int64 maxLng;
    }

    address public oracle;

    bytes32[] private _allIds;
    mapping(bytes32 => Project) public projects;
    mapping(address => bytes32[]) public ownerProjects;

    event ProjectSubmitted(bytes32 indexed id, address indexed owner, uint256 areaHectares, uint256 claimedTonnes);
    event ProjectApproved(bytes32 indexed id, address indexed owner);
    event ProjectRejected(bytes32 indexed id, address indexed owner, string reason);
    event FraudFlagged(bytes32 indexed id, address indexed owner, string reason);
    event OracleUpdated(address indexed previous, address indexed next);

    modifier onlyOracle() {
        require(msg.sender == oracle, "ProjectRegistry: not oracle");
        _;
    }

    constructor(address initialOracle) Ownable(msg.sender) {
        require(initialOracle != address(0), "ProjectRegistry: zero oracle");
        oracle = initialOracle;
    }

    function setOracle(address newOracle) external onlyOwner {
        require(newOracle != address(0), "ProjectRegistry: zero oracle");
        emit OracleUpdated(oracle, newOracle);
        oracle = newOracle;
    }

    function submitProject(
        bytes32 id,
        string calldata polygonGeoJSON,
        uint256 areaHectares,
        uint256 claimedTonnes,
        int64 minLat,
        int64 minLng,
        int64 maxLat,
        int64 maxLng
    ) external {
        require(projects[id].submittedAt == 0, "project ID already registered");
        require(bytes(polygonGeoJSON).length > 0, "ProjectRegistry: empty polygon");
        require(areaHectares > 0, "ProjectRegistry: zero area");
        require(claimedTonnes > 0, "ProjectRegistry: zero tonnes");
        require(minLat <= maxLat && minLng <= maxLng, "ProjectRegistry: bad bbox");

        projects[id] = Project({
            id: id,
            owner: msg.sender,
            polygonGeoJSON: polygonGeoJSON,
            areaHectares: areaHectares,
            claimedTonnes: claimedTonnes,
            submittedAt: block.timestamp,
            status: ProjectStatus.Pending,
            statusReason: "",
            minLat: minLat,
            minLng: minLng,
            maxLat: maxLat,
            maxLng: maxLng
        });

        _allIds.push(id);
        ownerProjects[msg.sender].push(id);

        emit ProjectSubmitted(id, msg.sender, areaHectares, claimedTonnes);
    }

    function approveProject(bytes32 id) external onlyOracle {
        _requirePending(id);
        projects[id].status = ProjectStatus.Approved;
        emit ProjectApproved(id, projects[id].owner);
    }

    function rejectProject(bytes32 id, string calldata reason) external onlyOracle {
        _requirePending(id);
        projects[id].status = ProjectStatus.Rejected;
        projects[id].statusReason = reason;
        emit ProjectRejected(id, projects[id].owner, reason);
    }

    // one-way — there's intentionally no un-flag
    function flagFraud(bytes32 id, string calldata reason) external onlyOracle {
        require(projects[id].submittedAt != 0, "ProjectRegistry: unknown project");
        projects[id].status = ProjectStatus.FraudFlagged;
        projects[id].statusReason = reason;
        emit FraudFlagged(id, projects[id].owner, reason);
    }

    function getProject(bytes32 id) external view returns (Project memory) {
        return projects[id];
    }

    function getProjectPolygon(bytes32 id) external view returns (string memory) {
        return projects[id].polygonGeoJSON;
    }

    // backend pulls all polygons from here to run overlap detection
    function getAllProjectIds() external view returns (bytes32[] memory) {
        return _allIds;
    }

    function getApprovedProjectIds() external view returns (bytes32[] memory) {
        uint256 count;
        for (uint256 i = 0; i < _allIds.length; i++) {
            if (projects[_allIds[i]].status == ProjectStatus.Approved) count++;
        }
        bytes32[] memory approved = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < _allIds.length; i++) {
            if (projects[_allIds[i]].status == ProjectStatus.Approved) {
                approved[j++] = _allIds[i];
            }
        }
        return approved;
    }

    function getOwnerProjects(address owner) external view returns (bytes32[] memory) {
        return ownerProjects[owner];
    }

    function getProjectBBox(bytes32 id)
        external view returns (int64 minLat, int64 minLng, int64 maxLat, int64 maxLng)
    {
        Project storage p = projects[id];
        return (p.minLat, p.minLng, p.maxLat, p.maxLng);
    }

    // returns approved projects whose bbox overlaps the query — candidate duplicates only,
    // Shapely still makes the final call off-chain
    function findOverlaps(int64 minLat, int64 minLng, int64 maxLat, int64 maxLng)
        external view returns (bytes32[] memory)
    {
        uint256 count;
        for (uint256 i = 0; i < _allIds.length; i++) {
            Project storage p = projects[_allIds[i]];
            if (p.status == ProjectStatus.Approved && _bboxIntersect(p, minLat, minLng, maxLat, maxLng)) {
                count++;
            }
        }
        bytes32[] memory hits = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < _allIds.length; i++) {
            Project storage p = projects[_allIds[i]];
            if (p.status == ProjectStatus.Approved && _bboxIntersect(p, minLat, minLng, maxLat, maxLng)) {
                hits[j++] = _allIds[i];
            }
        }
        return hits;
    }

    function _bboxIntersect(Project storage p, int64 minLat, int64 minLng, int64 maxLat, int64 maxLng)
        internal view returns (bool)
    {
        return p.minLat <= maxLat && p.maxLat >= minLat
            && p.minLng <= maxLng && p.maxLng >= minLng;
    }

    function _requirePending(bytes32 id) internal view {
        require(projects[id].submittedAt != 0, "ProjectRegistry: unknown project");
        require(projects[id].status == ProjectStatus.Pending, "ProjectRegistry: not pending");
    }
}
