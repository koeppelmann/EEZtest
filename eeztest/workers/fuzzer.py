"""Fuzzer worker — randomized L2 traffic from the funder's sub-accounts.

This is *correctness* fuzzing, not load generation (that is the ddos worker's
job).  Every iteration picks a few funded sub-accounts and fires a small burst of
randomized, individually-signed L2 transactions at the plain L2 RPC: dust
transfers between pool accounts, random-length random calldata to random targets,
self-transfers, zero-value txs, txs to the zero address, and random gas limits in
the 21k..200k band.  Each sub-account signs with its OWN key, so the burst
exercises many independent senders rather than one serialized one; every send
goes through `WorkerContext.send_sub`, which allocates nonces from the single
process-wide `NonceManager` shared with the ddos/congestion workers (they draw
from the same funded pool) and rolls the nonce back when a broadcast fails.

Deliberately L2-local: the EEZ build under test permits only *simple* L1<->L2
calls, so the fuzzer never builds a cross-chain action (an outer cross-chain call
may not trigger an inner one).  It does defend against the classic composer
stall: if a randomly chosen target turns out to have code — it could be a
CrossChainProxy — the tx is given at least `cfg.eez.crosschain_gas_limit` gas,
because an under-gassed tx landing on a proxy makes the composer loop forever.

Findings:
  * "L2 head stalled under fuzz" (high) — the L2 head stopped advancing for
    longer than `stall_seconds` (default 30s) while we were pushing traffic;
    the chain must keep making progress under random input.
  * "L2 txs accepted but never mined" (low) — txs the RPC accepted (it returned
    a hash) that still have no receipt after `stuck_seconds` (default 60s),
    once the count crosses `stuck_threshold`.  Silent mempool blackholing.
"""
from __future__ import annotations

import random
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from eth_utils import to_checksum_address

from .base import SubAccount, Worker

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Tx flavours the fuzzer emits.  Weights bias towards ordinary value transfers so
# the chain sees mostly-valid traffic with a steady trickle of oddities.
KINDS = ["transfer", "calldata", "self_transfer", "zero_value", "zero_address"]
KIND_WEIGHTS = [40, 30, 10, 10, 10]

MAX_CONCURRENCY = 16


@dataclass
class Pending:
    """A tx the RPC accepted; we still owe it a receipt check."""

    tx_hash: str
    sent_at: float
    kind: str
    sender: str


class FuzzerWorker(Worker):
    name = "fuzzer"
    description = "Randomized L2 transaction fuzzing from the funded sub-account pool"

    # ── lifecycle ────────────────────────────────────────────────────────────
    def setup(self) -> None:
        self.batch_size = int(self.wcfg.get("batch_size", 8))
        self.concurrency = max(1, min(MAX_CONCURRENCY, int(self.wcfg.get("concurrency", 8))))
        # Dust: values stay tiny so an account survives thousands of fuzz txs.
        self.max_value_wei = int(self.wcfg.get("max_value_wei", 1_000))
        self.max_calldata_bytes = int(self.wcfg.get("max_calldata_bytes", 128))
        self.stall_seconds = float(self.wcfg.get("stall_seconds", 30))
        self.stuck_seconds = float(self.wcfg.get("stuck_seconds", 60))
        self.stuck_threshold = int(self.wcfg.get("stuck_threshold", 5))
        self.receipt_sample = int(self.wcfg.get("receipt_sample", 12))

        self.rng = random.Random()
        # No local nonce bookkeeping: the sub-account pool is shared with other
        # workers, so every send goes through the context's NonceManager.
        # Small has-code cache: avoids an eth_getCode per random target.
        self._code_cache: dict[str, bool] = {}

        self._pending: list[Pending] = []
        self._pending_lock = threading.Lock()

        # Head-progress tracking for the stall invariant.
        self._last_head: int | None = None
        self._last_head_change: float = self.now()
        self._stall_reported = False
        # Only re-raise the stuck-tx finding once per additional threshold worth.
        self._stuck_total = 0
        self._next_stuck_report = self.stuck_threshold

        self.state.gauge("batch_size", self.batch_size)
        self.state.gauge("concurrency", self.concurrency)
        self.state.gauge("max_value_wei", self.max_value_wei)

    def interval(self) -> float:
        return float(self.wcfg.get("interval_seconds", 3))

    # ── main iteration ───────────────────────────────────────────────────────
    def step(self) -> None:
        accounts: list[SubAccount] = list(self.ctx.get_shared("l2_accounts", []) or [])
        if not accounts:
            self.state.set_status("idle")
            self.state.log("waiting for funded accounts")
            return
        self.state.set_status("running")
        self.state.gauge("pool_size", len(accounts))

        # Head invariant first: it must hold whether or not any send succeeds.
        self._check_head()

        # Pick the senders for this burst, then filter out the broke ones.
        picks = self._pick_senders(accounts)
        usable: list[tuple[SubAccount, int]] = []
        for sub in picks:
            try:
                bal = self.ctx.l2.balance(sub.address)
            except Exception as exc:  # noqa: BLE001 — a flaky read must not kill the step
                self.state.incr("balance_read_errors")
                self.state.log(f"balance read failed for {sub.address[:10]}: {exc}", "warn")
                continue
            if bal < self._min_balance():
                self.state.incr("skipped_low_balance")
                continue
            usable.append((sub, bal))

        if not usable:
            self.state.incr("bursts_skipped")
            self.state.log("no sub-account holds enough balance to fuzz with", "warn")
        else:
            self._burst(usable, accounts)

        self._poll_pending()

    # ── burst ────────────────────────────────────────────────────────────────
    def _burst(self, usable: list[tuple[SubAccount, int]], pool: list[SubAccount]) -> None:
        """Fire one randomized batch concurrently; never let a task raise out."""
        try:
            gas_price = self.ctx.l2.gas_price()
        except Exception as exc:  # noqa: BLE001
            self.state.incr("gas_price_errors")
            self.state.log(f"gas_price failed: {exc}", "warn")
            return
        self.state.gauge("gas_price_wei", gas_price)

        # Build the plan on this thread (RNG is not thread-safe) and let the pool
        # do only the signing + broadcasting.
        plan = [
            self._plan_tx(self.rng.choice(usable), pool, gas_price)
            for _ in range(self.batch_size)
        ]

        workers = max(1, min(self.concurrency, len(plan)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fuzz") as pool_exec:
            futures = [pool_exec.submit(self._send_one, item) for item in plan]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 — belt and braces
                    self.state.incr("task_errors")
                    self.state.log(f"fuzz task error: {exc}", "warn")
        self.state.incr("bursts")

    def _plan_tx(
        self, sender: tuple[SubAccount, int], pool: list[SubAccount], gas_price: int
    ) -> dict:
        """Describe one randomized tx.  Pure RNG, no network."""
        sub, balance = sender
        kind = self.rng.choices(KINDS, weights=KIND_WEIGHTS, k=1)[0]

        to = self._random_target(pool, sub)
        value = 0
        data = "0x"

        if kind == "transfer":
            value = self._dust(balance)
        elif kind == "self_transfer":
            to = sub.address
            value = self._dust(balance)
        elif kind == "zero_address":
            to = ZERO_ADDRESS
            value = self._dust(balance) if self.rng.random() < 0.5 else 0
        elif kind == "zero_value":
            value = 0
        elif kind == "calldata":
            n = self.rng.randint(0, self.max_calldata_bytes)
            data = "0x" + secrets.token_hex(n) if n else "0x"
            # A random blob occasionally carries value too.
            value = self._dust(balance) if self.rng.random() < 0.25 else 0

        return {
            "sub": sub,
            "kind": kind,
            "to": to,
            "value": value,
            "data": data,
            "gas": self.rng.randint(21_000, 200_000),
            "gas_price": gas_price,
        }

    def _send_one(self, item: dict) -> None:
        """Sign with the sub-account's own key and broadcast to the plain L2 RPC.

        Signing + nonce allocation both live in `ctx.send_sub`, which shares one
        NonceManager with every other worker using this pool.
        """
        sub: SubAccount = item["sub"]
        kind: str = item["kind"]
        to: str = item["to"]
        gas: int = item["gas"]

        # A random target that happens to hold code may be a CrossChainProxy; an
        # under-gassed tx landing on one wedges the composer, so bump the limit.
        if self._target_has_code(to):
            gas = max(gas, self.cfg.eez.crosschain_gas_limit)
        # Plain value transfers to an EOA cannot fit in <21k either way.
        if item["data"] != "0x":
            gas = max(gas, 21_000 + 16 * (len(item["data"]) - 2) // 2)

        try:
            h = self.ctx.send_sub(
                self.ctx.l2,
                sub.private_key,
                to=to_checksum_address(to),
                value=int(item["value"]),
                data=item["data"],
                gas=int(gas),
                gas_price=int(item["gas_price"]),
                endpoint=self.ctx.l2.cfg.rpc,
            )
        except Exception as exc:  # noqa: BLE001 — rejections are data, not failures
            self.state.incr("send_errors")
            self.state.incr(f"send_error_{kind}")
            self.state.log(f"send rejected ({kind}, gas={gas}): {exc}", "warn")
            # send_sub already rolled the reserved nonce back; nothing to clean up.
            return

        self.state.incr("txs_sent")
        self.state.incr(f"sent_{kind}")
        with self._pending_lock:
            self._pending.append(Pending(h, self.now(), kind, sub.address))

    # ── receipts / stuck detection ───────────────────────────────────────────
    def _poll_pending(self) -> None:
        """Check a sample of accepted txs; age out the ones that never mine."""
        with self._pending_lock:
            if not self._pending:
                self.state.gauge("pending_txs", 0)
                return
            sample = self._pending[: self.receipt_sample]

        landed: set[str] = set()
        stuck: list[Pending] = []
        now = self.now()
        for p in sample:
            if self.stopping():
                break
            try:
                receipt = self.ctx.l2.get_receipt(p.tx_hash)
            except Exception:  # noqa: BLE001 — transient RPC hiccup, retry next step
                continue
            if receipt is not None:
                landed.add(p.tx_hash)
                self.state.incr("txs_landed")
                self.state.incr(f"landed_{p.kind}")
                # Random gas limits mean reverts are expected and not a finding.
                self.state.incr("txs_reverted" if not receipt.ok else "txs_succeeded")
            elif now - p.sent_at > self.stuck_seconds:
                stuck.append(p)

        if stuck:
            self._record_stuck(stuck)

        drop = landed | {p.tx_hash for p in stuck}
        with self._pending_lock:
            if drop:
                self._pending = [p for p in self._pending if p.tx_hash not in drop]
            self.state.gauge("pending_txs", len(self._pending))

    def _record_stuck(self, stuck: list[Pending]) -> None:
        self.state.incr("stuck_tx", by=len(stuck))
        self._stuck_total += len(stuck)
        self.state.gauge("stuck_tx_total", self._stuck_total)
        if self._stuck_total < self._next_stuck_report:
            return
        sample = stuck[0]
        self.state.finding(
            title="L2 txs accepted but never mined",
            severity="low",
            detail=(
                f"{self._stuck_total} fuzz txs were accepted by the L2 RPC but had no receipt "
                f"after {self.stuck_seconds:.0f}s (threshold {self.stuck_threshold}); "
                f"example {sample.tx_hash} from {sample.sender} (kind={sample.kind})"
            ),
            tx=sample.tx_hash,
            account=sample.sender,
            stuck_total=self._stuck_total,
        )
        # Next report only after another threshold's worth of stuck txs.
        self._next_stuck_report = self._stuck_total + self.stuck_threshold

    # ── head-progress invariant ──────────────────────────────────────────────
    def _check_head(self) -> None:
        try:
            head = self.ctx.l2.block_number()
        except Exception as exc:  # noqa: BLE001
            self.state.incr("head_read_errors")
            self.state.log(f"l2 block_number failed: {exc}", "warn")
            return
        self.state.gauge("l2_head", head)

        if self._last_head is None or head > self._last_head:
            self._last_head = head
            self._last_head_change = self.now()
            self._stall_reported = False
            return

        stalled_for = self.now() - self._last_head_change
        self.state.gauge("head_stalled_seconds", round(stalled_for, 1))
        if stalled_for > self.stall_seconds and not self._stall_reported:
            self._stall_reported = True
            self.state.incr("head_stalls")
            self.state.finding(
                title="L2 head stalled under fuzz",
                severity="high",
                detail=(
                    f"L2 head stuck at block {head} for {stalled_for:.0f}s "
                    f"(> {self.stall_seconds:.0f}s) while the fuzzer was sending traffic — "
                    f"{self.state.snapshot()['counters'].get('txs_sent', 0)} txs sent so far"
                ),
                block=head,
                stalled_seconds=round(stalled_for, 1),
            )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _pick_senders(self, accounts: list[SubAccount]) -> list[SubAccount]:
        """A few distinct random accounts per burst (fewer if the pool is small)."""
        k = min(len(accounts), max(1, min(self.batch_size, self.concurrency)))
        return self.rng.sample(accounts, k)

    def _random_target(self, pool: list[SubAccount], sender: SubAccount) -> str:
        """Mostly other pool members, sometimes a never-seen random address."""
        roll = self.rng.random()
        if roll < 0.7 and len(pool) > 1:
            other = self.rng.choice(pool)
            if other.address != sender.address:
                return other.address
        if roll < 0.85:
            return sender.address
        return to_checksum_address("0x" + secrets.token_hex(20))

    def _dust(self, balance: int) -> int:
        """A value well under the account's balance — never enough to drain it."""
        ceiling = max(1, min(self.max_value_wei, balance // 1000))
        return self.rng.randint(1, ceiling)

    def _min_balance(self) -> int:
        """Rough floor: worst-case gas for one tx plus a dust value, times a margin."""
        try:
            gp = self.ctx.l2.gas_price()
        except Exception:  # noqa: BLE001
            gp = self.cfg.l2.min_gas_price_wei
        return 4 * (200_000 * gp + self.max_value_wei)

    def _target_has_code(self, address: str) -> bool:
        key = to_checksum_address(address)
        cached = self._code_cache.get(key)
        if cached is not None:
            return cached
        try:
            has = self.ctx.l2.has_code(key)
        except Exception:  # noqa: BLE001 — assume EOA, worst case the tx reverts
            return False
        # Cache negatives too: a random address is overwhelmingly likely to stay
        # code-free, and this keeps eth_getCode traffic bounded.
        if len(self._code_cache) > 4096:
            self._code_cache.clear()
        self._code_cache[key] = has
        return has
