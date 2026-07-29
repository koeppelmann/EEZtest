// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IEEZProxyRegistry {
    function computeCrossChainProxyAddress(address originalAddress, uint256 rollupId)
        external
        view
        returns (address);

    function createCrossChainProxy(address originalAddress, uint256 rollupId) external returns (address);
}

/// @title ProxyDepositHelper
/// @notice One-transaction deposit to an L2 address whose CrossChainProxy may not
///         exist yet: deploys the proxy if it is missing, then forwards the value
///         to it so the normal deposit path runs.
///
/// Sending value directly to a not-yet-deployed proxy address silently produces an
/// ordinary transfer to a codeless address — the L1 transfer succeeds, no deposit
/// is registered, and the value simply sits there. This helper makes creation and
/// deposit a single atomic step: if the forward fails, the whole call reverts and
/// the caller keeps their funds.
contract ProxyDepositHelper {
    IEEZProxyRegistry public immutable registry;

    event Deposited(
        address indexed originalAddress,
        uint256 indexed rollupId,
        address proxy,
        uint256 value,
        bool proxyCreated
    );

    error ZeroValue();
    error ProxyMissingAfterCreate(address proxy);
    error ForwardFailed(address proxy);

    constructor(address registry_) {
        registry = IEEZProxyRegistry(registry_);
    }

    /// @notice Deposit `msg.value` to `originalAddress` on rollup `rollupId`,
    ///         creating that pair's CrossChainProxy first if it does not exist.
    /// @return proxy The proxy the value was forwarded to.
    function depositTo(address originalAddress, uint256 rollupId) external payable returns (address proxy) {
        if (msg.value == 0) revert ZeroValue();

        proxy = registry.computeCrossChainProxyAddress(originalAddress, rollupId);

        bool created;
        if (proxy.code.length == 0) {
            registry.createCrossChainProxy(originalAddress, rollupId);
            if (proxy.code.length == 0) revert ProxyMissingAfterCreate(proxy);
            created = true;
        }

        // A plain value transfer to the proxy is what triggers the deposit.
        // Forward all remaining gas so the proxy's own logic cannot run short.
        (bool ok, ) = proxy.call{value: msg.value}("");
        if (!ok) revert ForwardFailed(proxy);

        emit Deposited(originalAddress, rollupId, proxy, msg.value, created);
    }

    /// @notice The proxy address `depositTo` would use, for callers that want to
    ///         check or pre-compute it.
    function proxyFor(address originalAddress, uint256 rollupId) external view returns (address proxy, bool exists) {
        proxy = registry.computeCrossChainProxyAddress(originalAddress, rollupId);
        exists = proxy.code.length != 0;
    }
}
