"""EEZ protocol mechanics: cross-chain proxies, deposits, withdrawals, calls.

An EEZ CrossChainProxy is a CREATE2 contract keyed by (originalAddress, rollupId).
On L1 the registry deploys it; on L2 the CCM predeploy does.  Sending value to a
proxy on L1 credits the mirrored address on L2 (a *deposit*); sending value to a
proxy keyed with rollupId=0 on L2 credits L1 (a *withdrawal*).  Calling a proxy
routes a cross-chain CALL to the mirrored contract on the other side.

This build assumes the L2 permits only *simple* L1<->L2 calls: an outer
cross-chain call may NOT trigger an inner cross-chain call (no nesting).  The
helpers here therefore only ever build single-hop actions.

Key selectors (EEZBase):
    computeCrossChainProxyAddress(address,uint256) = 0xb761ba7e
    createCrossChainProxy(address,uint256)         = 0x2dd72120
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .config import Config
from .rpc import ChainClient, Receipt, RpcError

SEL_COMPUTE_PROXY = "0xb761ba7e"
SEL_CREATE_PROXY = "0x2dd72120"

# rollupId 0 is L1 / mainnet on the EEZ side.
MAINNET_ROLLUP_ID = 0


def _enc_addr_uint(selector: str, address: str, value: int) -> str:
    body = abi_encode(["address", "uint256"], [to_checksum_address(address), value]).hex()
    return selector + body


def cross_chain_call_hash(
    *,
    is_static: bool,
    source_address: str,
    source_rollup_id: int,
    target_address: str,
    target_rollup_id: int,
    value: int,
    data: bytes,
) -> str:
    """keccak256(abi.encode(isStatic, src, srcRid, tgt, tgtRid, value, data)).

    Mirrors EEZBase.computeCrossChainCallHash.  abi.encode left-pads the uint
    rollup ids to 32 bytes, so passing them as uint256 matches the contract's
    uint64 fields byte-for-byte.
    """
    encoded = abi_encode(
        ["bool", "address", "uint256", "address", "uint256", "uint256", "bytes"],
        [
            is_static,
            to_checksum_address(source_address),
            source_rollup_id,
            to_checksum_address(target_address),
            target_rollup_id,
            value,
            data,
        ],
    )
    return "0x" + keccak(encoded).hex()


@dataclass
class ProxyRef:
    original_address: str
    rollup_id: int
    proxy_address: str
    side: str  # "l1" or "l2"
    exists: bool


class Eez:
    """High-level EEZ operations over an L1 + L2 client pair."""

    def __init__(self, cfg: Config, l1: ChainClient, l2: ChainClient):
        self.cfg = cfg
        self.l1 = l1
        self.l2 = l2
        self.registry = to_checksum_address(cfg.eez.registry)
        self.ccm_l2 = to_checksum_address(cfg.eez.ccm_l2)
        self.rollup_id = cfg.eez.rollup_id
        self.gas = cfg.eez.crosschain_gas_limit

    # ── proxy address computation ───────────────────────────────────────────
    def compute_l1_proxy(self, original_address: str, rollup_id: int | None = None) -> str:
        rid = self.rollup_id if rollup_id is None else rollup_id
        data = _enc_addr_uint(SEL_COMPUTE_PROXY, original_address, rid)
        res = self.l1.eth_call(self.registry, data)
        return to_checksum_address("0x" + res[-40:])

    def compute_l2_proxy(self, original_address: str, rollup_id: int) -> str:
        """L2-side proxy for (address, rollupId).  rollupId=0 targets L1."""
        data = _enc_addr_uint(SEL_COMPUTE_PROXY, original_address, rollup_id)
        res = self.l2.eth_call(self.ccm_l2, data)
        return to_checksum_address("0x" + res[-40:])

    def l1_proxy_ref(self, original_address: str, rollup_id: int | None = None) -> ProxyRef:
        rid = self.rollup_id if rollup_id is None else rollup_id
        addr = self.compute_l1_proxy(original_address, rid)
        return ProxyRef(original_address, rid, addr, "l1", self.l1.has_code(addr))

    def l2_proxy_ref(self, original_address: str, rollup_id: int) -> ProxyRef:
        addr = self.compute_l2_proxy(original_address, rollup_id)
        return ProxyRef(original_address, rollup_id, addr, "l2", self.l2.has_code(addr))

    # ── proxy creation ──────────────────────────────────────────────────────
    def create_l1_proxy(
        self, original_address: str, rollup_id: int | None = None, endpoint: str | None = None
    ) -> str:
        """Deploy the L1 CrossChainProxy for (address, rollupId).

        NOTE: this is an ordinary L1 contract call, NOT a cross-chain action, so it
        must go to the plain L1 RPC.  The cross-chain front holds submitted txs to
        compose them into the next sync block; a non-cross-chain tx sent there is
        accepted (a hash is returned) but then never forwarded to L1 and never
        mined — a silent drop.  Default to the plain RPC unless told otherwise.
        """
        rid = self.rollup_id if rollup_id is None else rollup_id
        data = _enc_addr_uint(SEL_CREATE_PROXY, original_address, rid)
        return self.l1.send(
            to=self.registry,
            data=data,
            gas=max(self.gas, 400_000),
            endpoint=endpoint if endpoint is not None else self.cfg.l1.rpc,
        )

    def create_l2_proxy(
        self, original_address: str, rollup_id: int, endpoint: str | None = None
    ) -> str:
        data = _enc_addr_uint(SEL_CREATE_PROXY, original_address, rollup_id)
        return self.l2.send(to=self.ccm_l2, data=data, gas=max(self.gas, 400_000), endpoint=endpoint)

    def ensure_l1_proxy(self, original_address: str, rollup_id: int | None = None) -> ProxyRef:
        ref = self.l1_proxy_ref(original_address, rollup_id)
        if ref.exists:
            return ref
        h = self.create_l1_proxy(original_address, ref.rollup_id)
        self.l1.wait_receipt(h, timeout=90)
        ref.exists = self.l1.has_code(ref.proxy_address)
        return ref

    # ── deposits (L1 -> L2) and withdrawals (L2 -> L1) ──────────────────────
    def deposit(
        self,
        to_l2_address: str,
        value: int,
        *,
        endpoint: str | None = None,
        gas: int | None = None,
    ) -> tuple[str, ProxyRef]:
        """Fund `to_l2_address` on L2 by sending `value` to its L1 proxy.

        The L1 proxy must exist first (an under-gassed create/deposit is the
        classic composer stall), so callers should ensure it, or accept that the
        composer creates it lazily on some builds.  Broadcast defaults to the L1
        cross-chain front.
        """
        ref = self.l1_proxy_ref(to_l2_address, self.rollup_id)
        h = self.l1.send(
            to=ref.proxy_address,
            value=value,
            gas=gas or self.gas,
            endpoint=endpoint if endpoint is not None else self.cfg.l1.xchain_front,
        )
        return h, ref

    def withdraw(
        self,
        to_l1_address: str,
        value: int,
        *,
        endpoint: str | None = None,
        gas: int | None = None,
    ) -> tuple[str, ProxyRef]:
        """Move `value` from L2 to `to_l1_address` on L1 via the rollupId=0 proxy."""
        ref = self.l2_proxy_ref(to_l1_address, MAINNET_ROLLUP_ID)
        h = self.l2.send(
            to=ref.proxy_address,
            value=value,
            gas=gas or self.gas,
            endpoint=endpoint if endpoint is not None else self.cfg.l2.xchain_front,
        )
        return h, ref

    # ── cross-chain calls ───────────────────────────────────────────────────
    def call_l2_from_l1(
        self,
        target_l2_address: str,
        calldata: str,
        *,
        value: int = 0,
        endpoint: str | None = None,
        gas: int | None = None,
    ) -> tuple[str, ProxyRef]:
        """From L1, call `target_l2_address` on L2 through its L1 proxy."""
        ref = self.ensure_l1_proxy(target_l2_address, self.rollup_id)
        h = self.l1.send(
            to=ref.proxy_address,
            value=value,
            data=calldata,
            gas=gas or self.gas,
            endpoint=endpoint if endpoint is not None else self.cfg.l1.xchain_front,
        )
        return h, ref

    def call_l1_from_l2(
        self,
        target_l1_address: str,
        calldata: str,
        *,
        value: int = 0,
        endpoint: str | None = None,
        gas: int | None = None,
    ) -> tuple[str, ProxyRef]:
        """From L2, call `target_l1_address` on L1 through the rollupId=0 proxy."""
        ref = self.l2_proxy_ref(target_l1_address, MAINNET_ROLLUP_ID)
        if not ref.exists:
            ch = self.create_l2_proxy(target_l1_address, MAINNET_ROLLUP_ID)
            self.l2.wait_receipt(ch, timeout=60)
            ref.exists = self.l2.has_code(ref.proxy_address)
        h = self.l2.send(
            to=ref.proxy_address,
            value=value,
            data=calldata,
            gas=gas or self.gas,
            endpoint=endpoint if endpoint is not None else self.cfg.l2.xchain_front,
        )
        return h, ref


def selector(signature: str) -> str:
    """4-byte function selector, 0x-prefixed."""
    return "0x" + keccak(signature.encode())[:4].hex()
