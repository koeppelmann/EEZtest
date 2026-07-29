"""Coordinated nonce allocation shared across workers.

Multiple workers (fuzzer, ddos, congestion) send transactions from the *same*
pool of L2 sub-accounts.  If each keeps its own nonce counter they collide and
create gaps, and the resulting stuck/rejected transactions get misreported as
chain stalls rather than harness artifacts.  `NonceManager` is the single
allocator every sub-account send goes through: reserve → broadcast → confirm or
roll back, all serialized per (chain, address).
"""
from __future__ import annotations

import re
import threading
from typing import Callable


class NonceManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[tuple[str, str], int] = {}
        # Per-(chain,address) send serialization.  The EEZ cross-chain fronts
        # enforce STRICTLY SEQUENTIAL nonces — they reject a future nonce with
        # "expected next unreserved nonce N" instead of queueing it like a normal
        # mempool.  Allocating in order is therefore not enough: the broadcasts
        # must also *arrive* in order, so callers hold this lock across
        # allocate+sign+send.
        self._send_locks: dict[tuple[str, str], threading.Lock] = {}
        self._send_locks_guard = threading.Lock()

    def send_lock(self, chain_tag: str, address: str) -> threading.Lock:
        key = (chain_tag, address.lower())
        with self._send_locks_guard:
            lk = self._send_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._send_locks[key] = lk
            return lk

    def allocate(self, chain_tag: str, address: str, pending_nonce: int) -> int:
        """Reserve the next nonce for (chain, address).

        `pending_nonce` is the node's current pending count, fetched by the caller
        *outside* the lock.  We hand out the max of our own cursor and the node's
        pending value, so a fresh key seeds from the chain and a busy key keeps
        advancing monotonically.
        """
        key = (chain_tag, address.lower())
        with self._lock:
            cur = self._next.get(key)
            n = cur if (cur is not None and cur >= pending_nonce) else pending_nonce
            self._next[key] = n + 1
            return n

    def rollback(self, chain_tag: str, address: str, nonce: int) -> None:
        """Return a reserved nonce after a failed broadcast (only if it was the last)."""
        key = (chain_tag, address.lower())
        with self._lock:
            if self._next.get(key) == nonce + 1:
                self._next[key] = nonce

    def resync(self, chain_tag: str, address: str) -> None:
        """Forget our cursor so the next allocate re-seeds from the node's pending nonce."""
        key = (chain_tag, address.lower())
        with self._lock:
            self._next.pop(key, None)

    def reserve_with(
        self, chain_tag: str, address: str, fetch_pending: Callable[[], int]
    ) -> int:
        """Convenience: fetch pending (outside the lock) then allocate."""
        pending = fetch_pending()
        return self.allocate(chain_tag, address, pending)

    def force_next(self, chain_tag: str, address: str, nonce: int) -> None:
        """Pin the next nonce for (chain, address), overriding our cursor."""
        key = (chain_tag, address.lower())
        with self._lock:
            self._next[key] = nonce


# The EEZ cross-chain fronts reject an out-of-sequence nonce with a message that
# names the nonce they actually want, e.g.
#   "invalid nonce 46 for 0x…: expected next unreserved nonce 45 (source-chain nonce 45)"
# That is authoritative and worth obeying: because the front can accept a tx and
# then silently drop it, the chain's nonce may never advance even though the
# sender counted the tx as sent — desynchronising the local cursor and wedging
# every later tx.  Parsing the expected value lets a sender resync and retry.
_EXPECTED_NONCE_RE = re.compile(r"expected next unreserved nonce\s+(\d+)", re.I)


def parse_expected_nonce(message: str) -> int | None:
    m = _EXPECTED_NONCE_RE.search(message or "")
    return int(m.group(1)) if m else None
