"""Thin JSON-RPC client + transaction helpers.

Raw requests (not a Web3 provider) so a single signed transaction can be aimed
at a specific endpoint — the cross-chain front, the plain RPC, or an arbitrary
extra mempool — which is exactly what the proxy-routing and mempool tests need.
Signing is done with eth-account.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
import rlp
from eth_account import Account
from eth_account.datastructures import SignedTransaction
from eth_utils import keccak, to_checksum_address

from .config import ChainConfig


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


_id_counter = itertools.count(1)


def rpc_call(url: str, method: str, params: list[Any] | None = None, timeout: float = 15.0) -> Any:
    payload = {"jsonrpc": "2.0", "id": next(_id_counter), "method": method, "params": params or []}
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body and body["error"] is not None:
        err = body["error"]
        raise RpcError(err.get("message", "rpc error"), err.get("code"), err.get("data"))
    return body.get("result")


@dataclass
class Receipt:
    tx_hash: str
    block_number: int
    status: int
    gas_used: int
    raw: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == 1


class ChainClient:
    """A signing + broadcasting client bound to one chain (L1 or L2)."""

    def __init__(self, cfg: ChainConfig, private_key: str, stop_event: "threading.Event | None" = None):
        self.cfg = cfg
        self.account = Account.from_key(private_key)
        self.address = to_checksum_address(self.account.address)
        self._nonce_lock = threading.Lock()
        self._local_nonce: int | None = None
        # When set, long receipt waits abort promptly so shutdown isn't blocked.
        self.stop_event = stop_event

    # ── reads (always via the plain RPC) ────────────────────────────────────
    def call(self, method: str, params: list[Any] | None = None, timeout: float = 15.0) -> Any:
        return rpc_call(self.cfg.rpc, method, params, timeout)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def gas_price(self) -> int:
        try:
            gp = int(self.call("eth_gasPrice"), 16)
        except Exception:
            gp = 0
        return max(gp, self.cfg.min_gas_price_wei)

    def balance(self, address: str, block: str = "latest") -> int:
        return int(self.call("eth_getBalance", [to_checksum_address(address), block]), 16)

    def get_code(self, address: str, block: str = "latest") -> str:
        return self.call("eth_getCode", [to_checksum_address(address), block])

    def has_code(self, address: str) -> bool:
        code = self.get_code(address)
        return bool(code) and code != "0x"

    def nonce(self, address: str, block: str = "pending") -> int:
        return int(self.call("eth_getTransactionCount", [to_checksum_address(address), block]), 16)

    def eth_call(self, to: str, data: str, block: str = "latest", value: int = 0) -> str:
        tx: dict[str, Any] = {"to": to_checksum_address(to), "data": data}
        if value:
            tx["value"] = hex(value)
        return self.call("eth_call", [tx, block])

    # ── nonce management (serialized per client) ────────────────────────────
    def next_nonce(self) -> int:
        with self._nonce_lock:
            chain_nonce = self.nonce(self.address, "pending")
            if self._local_nonce is None or chain_nonce > self._local_nonce:
                self._local_nonce = chain_nonce
            n = self._local_nonce
            self._local_nonce += 1
            return n

    def rollback_nonce(self, nonce: int) -> None:
        """Return a reserved nonce after a failed broadcast (only if it was the last)."""
        with self._nonce_lock:
            if self._local_nonce == nonce + 1:
                self._local_nonce = nonce

    def reset_nonce(self) -> None:
        with self._nonce_lock:
            self._local_nonce = None

    # ── signing + broadcasting ──────────────────────────────────────────────
    def build_tx(
        self,
        *,
        to: str | None,
        value: int = 0,
        data: str = "0x",
        gas: int = 100_000,
        gas_price: int | None = None,
        nonce: int | None = None,
    ) -> dict[str, Any]:
        tx: dict[str, Any] = {
            "value": value,
            "data": data,
            "gas": gas,
            "gasPrice": gas_price if gas_price is not None else self.gas_price(),
            "nonce": nonce if nonce is not None else self.next_nonce(),
            "chainId": self.cfg.chain_id,
        }
        if to is not None:
            tx["to"] = to_checksum_address(to)
        return tx

    def sign(self, tx: dict[str, Any]) -> SignedTransaction:
        return self.account.sign_transaction(tx)

    @staticmethod
    def raw_hex(signed: SignedTransaction) -> str:
        return "0x" + signed.raw_transaction.hex()

    @staticmethod
    def tx_hash(signed: SignedTransaction) -> str:
        return "0x" + keccak(signed.raw_transaction).hex()

    def send_raw(self, raw_hex: str, endpoint: str | None = None) -> str:
        """Broadcast a pre-signed tx to a specific endpoint (front by default)."""
        url = endpoint or self.cfg.xchain_front or self.cfg.rpc
        return rpc_call(url, "eth_sendRawTransaction", [raw_hex])

    def send_raw_multi(self, raw_hex: str, endpoints: list[str]) -> dict[str, Any]:
        """Fan the SAME signed tx at several endpoints; report each outcome."""
        results: dict[str, Any] = {}
        for url in endpoints:
            try:
                results[url] = {"ok": True, "result": rpc_call(url, "eth_sendRawTransaction", [raw_hex])}
            except Exception as exc:  # noqa: BLE001 — we want every endpoint's verdict
                results[url] = {"ok": False, "error": str(exc)}
        return results

    def send(
        self,
        *,
        to: str | None,
        value: int = 0,
        data: str = "0x",
        gas: int = 100_000,
        gas_price: int | None = None,
        nonce: int | None = None,
        endpoint: str | None = None,
    ) -> str:
        # Reserve the nonce ourselves so we can roll it back if the broadcast
        # fails — otherwise a failed send leaves a permanent gap that blocks every
        # later tx on this shared client (misreported downstream as a chain stall).
        auto_nonce = nonce is None
        n = self.next_nonce() if auto_nonce else nonce
        try:
            tx = self.build_tx(to=to, value=value, data=data, gas=gas, gas_price=gas_price, nonce=n)
            signed = self.sign(tx)
            h = self.tx_hash(signed)
            self.send_raw(self.raw_hex(signed), endpoint)
            return h
        except Exception:
            if auto_nonce:
                self.rollback_nonce(n)
            raise

    # ── receipts ────────────────────────────────────────────────────────────
    def get_receipt(self, tx_hash: str) -> Receipt | None:
        r = self.call("eth_getTransactionReceipt", [tx_hash])
        if not r:
            return None
        return Receipt(
            tx_hash=tx_hash,
            block_number=int(r["blockNumber"], 16),
            status=int(r.get("status", "0x0"), 16),
            gas_used=int(r.get("gasUsed", "0x0"), 16),
            raw=r,
        )

    def wait_receipt(self, tx_hash: str, timeout: float = 120.0, poll: float = 2.0) -> Receipt | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event is not None and self.stop_event.is_set():
                return None
            r = self.get_receipt(tx_hash)
            if r is not None:
                return r
            if self.stop_event is not None:
                # Interruptible wait — wakes immediately on stop.
                if self.stop_event.wait(poll):
                    return None
            else:
                time.sleep(poll)
        return None

    def revert_reason(self, tx_hash: str) -> str | None:
        """Best-effort revert data for a failed tx via debug_traceTransaction."""
        try:
            tr = self.call("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
            if isinstance(tr, dict):
                return tr.get("output") or tr.get("error")
        except Exception:
            return None
        return None


def predict_create_address(sender: str, nonce: int) -> str:
    """CREATE address = keccak256(rlp([sender, nonce]))[12:]."""
    sender_bytes = bytes.fromhex(to_checksum_address(sender)[2:])
    encoded = rlp.encode([sender_bytes, nonce])
    return to_checksum_address("0x" + keccak(encoded)[12:].hex())
