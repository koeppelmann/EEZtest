// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Forwarder {
    function forward(address target, bytes calldata data) external returns (bytes memory) {
        (bool ok, bytes memory ret) = target.call(data);
        require(ok, "forward failed");
        return ret;
    }
}
