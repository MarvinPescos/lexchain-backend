// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {Script} from "forge-std/Script.sol";
import {LexChainNotary} from "../src/LexChainNotary.sol";

contract CounterScript is Script {
    LexChainNotary public lexChainNotary;

    function setUp() public {}

    function run() public {
        vm.startBroadcast();

        lexChainNotary = new LexChainNotary();

        vm.stopBroadcast();
    }
}
