// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Logger {
    struct Call {
        uint256 id;
        address target;
        bytes payload;
        address caller;
        bytes returnData;
    }

    uint256 public callCounter;
    Call[] public calls;

    function execute(address target, bytes calldata payload) external returns (bytes memory) {
        (bool success, bytes memory returnData) = target.call(payload);
        require(success, "call failed");

        callCounter++;
        calls.push(Call(callCounter, target, payload, msg.sender, returnData));

        return returnData;
    }

    function getCalls() external view returns (Call[] memory) {
        return calls;
    }
}
