"""Contract compilation, deployment, and calldata encoding.

Compiles the Solidity sources in `contracts/` with py-solc-x and exposes a small
`Contract` handle for deploying and encoding calls.  Keeps the compiled artifacts
(abi, bytecode, standard-json input) around so the verifier can submit them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import function_abi_to_4byte_selector, keccak, to_checksum_address

from .rpc import ChainClient, predict_create_address

SOLC_VERSION = "0.8.24"
CONTRACTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contracts")


def _ensure_solc() -> None:
    import solcx

    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        solcx.install_solc(SOLC_VERSION)
    solcx.set_solc_version(SOLC_VERSION)


@dataclass
class Artifact:
    name: str
    abi: list[dict[str, Any]]
    bytecode: str
    source: str
    standard_json: dict[str, Any]

    def selector(self, fn_name: str) -> str:
        for entry in self.abi:
            if entry.get("type") == "function" and entry.get("name") == fn_name:
                return "0x" + function_abi_to_4byte_selector(entry).hex()
        raise KeyError(f"{self.name}.{fn_name} not in ABI")

    def encode_call(self, fn_name: str, args: list[Any] | None = None) -> str:
        args = args or []
        for entry in self.abi:
            if entry.get("type") == "function" and entry.get("name") == fn_name:
                types = [i["type"] for i in entry.get("inputs", [])]
                sel = function_abi_to_4byte_selector(entry)
                return "0x" + sel.hex() + abi_encode(types, args).hex()
        raise KeyError(f"{self.name}.{fn_name} not in ABI")


@lru_cache(maxsize=1)
def compile_all() -> dict[str, Artifact]:
    """Compile every .sol in contracts/ once; return name -> Artifact."""
    _ensure_solc()
    import solcx

    sources: dict[str, dict[str, str]] = {}
    for fname in sorted(os.listdir(CONTRACTS_DIR)):
        if not fname.endswith(".sol"):
            continue
        with open(os.path.join(CONTRACTS_DIR, fname)) as fh:
            sources[fname] = {"content": fh.read()}

    std_input = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    out = solcx.compile_standard(std_input, allow_paths=CONTRACTS_DIR)

    artifacts: dict[str, Artifact] = {}
    for fname, contracts in out.get("contracts", {}).items():
        for cname, cdata in contracts.items():
            bytecode = cdata["evm"]["bytecode"]["object"]
            if not bytecode:
                continue  # interface / abstract
            # Per-contract standard-json (single source) for Blockscout submission.
            per_input = {
                "language": "Solidity",
                "sources": {fname: sources[fname]},
                "settings": std_input["settings"],
            }
            artifacts[cname] = Artifact(
                name=cname,
                abi=cdata["abi"],
                bytecode="0x" + bytecode,
                source=sources[fname]["content"],
                standard_json=per_input,
            )
    return artifacts


@dataclass
class Deployment:
    name: str
    address: str
    tx_hash: str
    artifact: Artifact
    side: str  # "l1" | "l2"
    constructor_args_hex: str = ""


class Contracts:
    """Deploy + call the compiled contracts on a given chain client."""

    def __init__(self) -> None:
        self.artifacts = compile_all()

    def get(self, name: str) -> Artifact:
        return self.artifacts[name]

    def deploy(
        self,
        client: ChainClient,
        name: str,
        *,
        constructor_types: list[str] | None = None,
        constructor_args: list[Any] | None = None,
        gas: int = 1_500_000,
        side: str = "l1",
    ) -> Deployment:
        art = self.artifacts[name]
        ctor_hex = ""
        data = art.bytecode
        if constructor_types:
            ctor_hex = abi_encode(constructor_types, constructor_args or []).hex()
            data = data + ctor_hex
        nonce = client.next_nonce()
        predicted = predict_create_address(client.address, nonce)
        # Deployment goes to the plain RPC, not the cross-chain front.
        h = client.send(to=None, data=data, gas=gas, nonce=nonce, endpoint=client.cfg.rpc)
        return Deployment(
            name=name,
            address=to_checksum_address(predicted),
            tx_hash=h,
            artifact=art,
            side=side,
            constructor_args_hex=ctor_hex,
        )

    @staticmethod
    def encode(art: Artifact, fn: str, args: list[Any] | None = None) -> str:
        return art.encode_call(fn, args)
