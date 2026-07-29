"""Chain vitals — the handful of facts that actually tell you if an EEZ chain is well.

Block height and RPC availability are poor health signals: during both composer
stalls observed on 2026-07-29 the L2 kept producing blocks and every endpoint
answered normally while nothing was being settled on L1 for over an hour.

The signals that do distinguish a healthy chain:

  * seconds since the last L2 block
  * seconds since the last L2 transaction
  * seconds since the last L1<->L2 cross-chain call
  * seconds since the last L1 state-root update (postAndVerifyBatch)
  * how many L2 blocks have been produced since that last L1 update
    ("uncommitted") — this is the one that grows without bound during a stall

Every value is either a real measurement or `None`; a failed read is never
reported as zero or as "nothing happened".
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from eth_utils import to_checksum_address

# postAndVerifyBatch(...) on the EEZ registry — read off-chain from a real tx.
POST_AND_VERIFY_BATCH_SELECTOR = "0x8b1a095a"


@dataclass
class Vitals:
    l2_head: int | None = None
    l2_block_age: float | None = None
    l2_last_tx_age: float | None = None
    l2_last_tx_block: int | None = None
    xchain_last_age: float | None = None
    xchain_last_block: int | None = None
    l1_stateroot_age: float | None = None
    l1_stateroot_block: int | None = None
    uncommitted_blocks: int | None = None
    uncommitted_is_estimate: bool = True
    l2_scan_depth: int | None = None      # how many L2 blocks were inspected
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "l2_head": self.l2_head,
            "l2_block_age": self.l2_block_age,
            "l2_last_tx_age": self.l2_last_tx_age,
            "l2_last_tx_block": self.l2_last_tx_block,
            "xchain_last_age": self.xchain_last_age,
            "xchain_last_block": self.xchain_last_block,
            "l1_stateroot_age": self.l1_stateroot_age,
            "l1_stateroot_block": self.l1_stateroot_block,
            "uncommitted_blocks": self.uncommitted_blocks,
            "uncommitted_is_estimate": self.uncommitted_is_estimate,
            "l2_scan_depth": self.l2_scan_depth,
            "errors": list(self.errors),
        }


class VitalsTracker:
    """Maintains the vitals for one instance, incrementally and cheaply.

    Scans backwards once on first use to establish "last tx" / "last cross-chain
    call", then tracks forward one block range at a time.
    """

    def __init__(self, cfg, ccm_l2: str, registry: str, poster: str | None = None):
        self.cfg = cfg
        self.ccm_l2 = to_checksum_address(ccm_l2).lower()
        self.registry = to_checksum_address(registry).lower()
        self.poster = poster.lower() if poster else None
        self._lock = threading.Lock()
        self._S = requests.Session()

        self._scanned_to: int | None = None       # highest L2 block inspected
        self._last_tx: tuple[int, float] | None = None       # (block, ts)
        self._last_xchain: tuple[int, float] | None = None
        self._last_batch_ts: float | None = None
        self._last_batch_l1_block: int | None = None
        self._l2_head_at_last_batch: int | None = None
        self._seen_batch_key: str | None = None

    # ── low-level ───────────────────────────────────────────────────────────
    def _rpc(self, url: str, method: str, params: list, timeout: float = 10.0):
        r = self._S.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body.get("result")

    def _block(self, n: int, full: bool = False):
        return self._rpc(self.cfg.l2.rpc, "eth_getBlockByNumber", [hex(n), full])

    # ── L2 scanning ─────────────────────────────────────────────────────────
    def _scan_range(self, lo: int, hi: int, errors: list[str]) -> None:
        """Inspect [lo, hi] for transactions and cross-chain calls."""
        if hi < lo:
            return

        def one(n: int):
            try:
                b = self._block(n, full=True)
                if not b:
                    return ("err", n)
                ts = int(b["timestamp"], 16)
                txs = b.get("transactions", [])
                if not txs:
                    return ("empty", n)
                xchain = any((t.get("to") or "").lower() == self.ccm_l2 for t in txs)
                return ("tx", n, ts, xchain)
            except Exception:
                return ("err", n)

        span = list(range(lo, hi + 1))
        failed = 0
        with cf.ThreadPoolExecutor(max_workers=min(24, max(1, len(span)))) as ex:
            for res in ex.map(one, span):
                if res[0] == "err":
                    failed += 1
                elif res[0] == "tx":
                    _, n, ts, xchain = res
                    if self._last_tx is None or n > self._last_tx[0]:
                        self._last_tx = (n, float(ts))
                    if xchain and (self._last_xchain is None or n > self._last_xchain[0]):
                        self._last_xchain = (n, float(ts))
        if failed:
            errors.append(f"{failed} L2 block reads failed in {lo}..{hi}")


    def _l2_block_at_time(self, target_ts: float, head: int) -> int | None:
        """Highest L2 block whose timestamp is <= target_ts, by binary search.

        Used to work out how many L2 blocks were produced after the last L1
        commitment. Deriving it from timestamps rather than from a snapshot taken
        when we happened to start means the number is correct immediately after a
        restart, including a restart in the middle of a stall.
        """
        try:
            lo, hi = 0, head
            best = None
            while lo <= hi:
                mid = (lo + hi) // 2
                b = self._block(mid)
                if not b:
                    return None
                ts = int(b["timestamp"], 16)
                if ts <= target_ts:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best
        except Exception:
            return None

    # ── L1 batch tracking ───────────────────────────────────────────────────
    def _refresh_batch(self, l2_head: int, errors: list[str]) -> None:
        """Find the most recent state-root update on L1 and snapshot the L2 head."""
        try:
            url = self.cfg.l1.rpc
            head = int(self._rpc(url, "eth_blockNumber", []), 16)
            # Widen the search until we actually find a batch. A fixed short
            # window would report "unknown" exactly when the composer has stalled
            # — i.e. precisely when this number matters most. Healthy cadence is
            # ~5 s so 120 blocks normally hits on the first pass; the wider passes
            # only run while something is wrong.
            windows = [120, 400, 1200, 3000]
            lo = max(0, head - windows[0])

            def one(n: int):
                try:
                    b = self._rpc(url, "eth_getBlockByNumber", [hex(n), True], timeout=12)
                    if not b:
                        return None
                    ts = int(b["timestamp"], 16)
                    for t in b.get("transactions", []):
                        if (t.get("to") or "").lower() != self.registry:
                            continue
                        if not (t.get("input") or "").startswith(POST_AND_VERIFY_BATCH_SELECTOR):
                            continue
                        if self.poster and (t.get("from") or "").lower() != self.poster:
                            continue
                        return (n, float(ts), t["hash"])
                    return None
                except Exception:
                    return "err"

            newest = None
            failed = 0
            scanned_from = head + 1
            for w in windows:
                lo = max(0, head - w)
                span = range(lo, scanned_from)          # only the newly-added range
                if len(span) == 0:
                    continue
                with cf.ThreadPoolExecutor(max_workers=8) as ex:
                    for res in ex.map(one, span):
                        if res == "err":
                            failed += 1
                        elif res and (newest is None or res[0] > newest[0]):
                            newest = res
                scanned_from = lo
                if newest is not None:
                    break
            if failed:
                errors.append(f"{failed} L1 block reads failed")
            if newest is None:
                errors.append(f"no state-root update found in the last {windows[-1]} L1 blocks")
            if newest:
                n, ts, h = newest
                if h != self._seen_batch_key:
                    self._seen_batch_key = h
                    # Locate the L2 head as of the batch's own timestamp rather
                    # than trusting the moment we noticed it — otherwise starting
                    # up during a stall reports 0 uncommitted blocks.
                    at = self._l2_block_at_time(ts, l2_head)
                    self._l2_head_at_last_batch = at if at is not None else l2_head
                self._last_batch_ts = ts
                self._last_batch_l1_block = n
        except Exception as exc:  # noqa: BLE001
            errors.append(f"L1 batch scan failed: {exc}")

    # ── public ──────────────────────────────────────────────────────────────
    def sample(self, max_backscan: int = 400) -> Vitals:
        v = Vitals()
        now = time.time()
        with self._lock:
            try:
                head = int(self._rpc(self.cfg.l2.rpc, "eth_blockNumber", []), 16)
                v.l2_head = head
                hb = self._block(head)
                if hb:
                    v.l2_block_age = max(0.0, now - int(hb["timestamp"], 16))
            except Exception as exc:  # noqa: BLE001
                v.errors.append(f"L2 head read failed: {exc}")
                return v

            if self._scanned_to is None:
                self._scan_range(max(0, head - max_backscan), head, v.errors)
            elif head > self._scanned_to:
                # Bound forward work so a long gap cannot stall the poll loop.
                self._scan_range(max(self._scanned_to + 1, head - max_backscan), head, v.errors)
            self._scanned_to = head
            v.l2_scan_depth = max_backscan

            self._refresh_batch(head, v.errors)

            if self._last_tx:
                v.l2_last_tx_block = self._last_tx[0]
                v.l2_last_tx_age = max(0.0, now - self._last_tx[1])
            if self._last_xchain:
                v.xchain_last_block = self._last_xchain[0]
                v.xchain_last_age = max(0.0, now - self._last_xchain[1])
            if self._last_batch_ts:
                v.l1_stateroot_age = max(0.0, now - self._last_batch_ts)
                v.l1_stateroot_block = self._last_batch_l1_block
            if self._l2_head_at_last_batch is not None and v.l2_head is not None:
                v.uncommitted_blocks = max(0, v.l2_head - self._l2_head_at_last_batch)
                v.uncommitted_is_estimate = True
        return v
