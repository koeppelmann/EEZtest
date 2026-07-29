"""DDoS worker — flood the L2 with high-rate dust transfers and watch it bend.

Unlike the fuzzer (which probes *correctness* of cross-chain paths), this worker
is a pure *load* generator.  It broadcasts a torrent of tiny value transfers among
the already-funded L2 sub-accounts and measures whether the chain keeps up, slows
down, or stops.  Everything is treated as a ceiling: the configured concurrency,
per-second rate and burst size bound how hard we push.

Pacing is a monotonic **token bucket**: tokens accrue at exactly the configured
`rate_per_second` (fractional remainder carried across ticks), every submission
spends one token, and the bucket never holds more than `burst` tokens.  The
average submission rate therefore never exceeds the configured rate, and a
back-pressure throttle simply lowers the accrual rate instead of quantising it.

Spending is also bounded: each sender's L2 balance is tracked and an account
whose balance drops below a floor (a few tx-costs) stops being allocated work, so
the flood degrades into "no funded senders" rather than a storm of
insufficient-funds rejects.  An optional `max_spend_wei` caps the total attempted
spend (gas + dust) for the whole run.

Every sub-account send goes through `ctx.send_sub`, i.e. the *shared* coordinated
nonce allocator — the ddos worker shares its funded pool with the fuzzer and the
congestion worker, and a private nonce counter here would collide with theirs and
be misreported as a chain stall.

We deliberately aim the flood at the plain L2 RPC (and, if configured, the extra
mempools round-robin) — these are ordinary value transfers, NOT cross-chain
actions, so they must never touch the xchain front.

Findings this worker produces:
  * CRITICAL "L2 halted under load" — the head stopped advancing for >15s while we
    were actively sending (records the send rate at the moment of the halt).
  * HIGH "mempool refusing txs" — the head keeps advancing but the accept-rate
    collapses to ~0, i.e. the node is bouncing our transactions (records the
    dominant reject reason, e.g. txpool full).
  * INFO on back-pressure recovery — how long the RPC took to recover after we
    throttled in response to heavy transport errors.
It also gauges the max sustained accepted tx/block, a mempool_cap_estimate (the
in-mempool depth at which the node first started rejecting) and the attempted
spend.
"""
from __future__ import annotations

import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ..rpc import RpcError
from .base import Worker

GAS_PER_TRANSFER = 21_000  # plain EOA->EOA transfer


class DdosWorker(Worker):
    name = "ddos"
    description = "Flood the L2 with high-rate dust transfers and measure degradation"

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def setup(self) -> None:
        # Wait for the funder to publish at least two accounts (need a sender and a
        # recipient).  Back off politely rather than spin.
        accounts = self._await_accounts()
        self.subs = list(accounts) if accounts else []
        if not self.subs:
            return  # stopping() fired before we ever got a pool
        self.state.gauge("accounts", len(self.subs))

        # Params — all CEILINGS.
        self.concurrency = int(self.wcfg.get("concurrency", 50))
        self.rate_max = float(self.wcfg.get("rate_per_second", 200))
        # Token-bucket depth: how much of a lull we may spend at once.  Defaults to
        # the concurrency so a burst can never outrun the executor.
        self.burst = float(self.wcfg.get("burst", self.wcfg.get("burst_size", self.concurrency)))
        self.burst = max(1.0, self.burst)
        self.dust = int(self.wcfg.get("dust_wei", 1))
        # Broadcast endpoints: plain RPC first, then any extra mempools, round-robin.
        self.endpoints = [self.cfg.l2.rpc, *self.cfg.l2.extra_mempools]
        # cfg.l2 is a ChainConfig (no gas_price); read it from the L2 client once and
        # cache so the hot path never pays an RPC per tx.
        self.gas_price = self.ctx.l2.gas_price()

        # ── spend guard ──────────────────────────────────────────────────────
        # Cost of one flood tx, and the balance floor below which an account is
        # considered depleted and stops being allocated work.
        self.tx_cost = GAS_PER_TRANSFER * self.gas_price + self.dust
        floor_mult = int(self.wcfg.get("balance_floor_txs", 4))
        self.spend_floor = max(1, floor_mult) * self.tx_cost
        # Optional hard budget on total *attempted* spend (0/absent = unlimited).
        self.max_spend = int(self.wcfg.get("max_spend_wei", 0))
        self._acct_lock = threading.Lock()
        self._balance = [0] * len(self.subs)
        self._live: list[int] = []
        self._live_set: set[int] = set()
        self._spend_attempted = 0
        self._budget_reported = False
        self._no_senders_reported = False
        self._next_revive = 0.0
        self._seed_balances()
        self.state.gauge("tx_cost_wei", self.tx_cost)
        self.state.gauge("spend_floor_wei", self.spend_floor)
        if self.max_spend:
            self.state.gauge("max_spend_wei", self.max_spend)

        # Shared counters (source of truth for the rate maths); mirrored to state.
        self._slock = threading.Lock()
        self._stats = {"sent": 0, "accepted": 0, "rejected": 0, "rpc_errors": 0, "inflight": 0}
        self._reasons: Counter = Counter()

        # Dynamic rate (throttled down on heavy transport errors, restored on recovery).
        self.rate_cur = float(self.rate_max)
        self._degraded_since: float | None = None
        self._recovery_reported = False

        # Monotonic token bucket — the only thing that decides how many txs a tick
        # may submit.  Fractional tokens are carried, so the long-run average is
        # exactly rate_cur and never above rate_max.
        self._bucket_lock = threading.Lock()
        self._tokens = 0.0
        self._tokens_at = time.monotonic()

        # Round-robin cursors.
        self._sender_rr = 0
        self._endpoint_rr = 0
        self._rr_lock = threading.Lock()

        # One-shot finding / estimate guards.
        self._halt_reported = False
        self._refuse_reported = False
        self._cap_estimate: int | None = None
        self._max_accepted_per_block = 0

        self.state.log(
            f"flooding: concurrency<={self.concurrency} rate<={self.rate_max:.0f}/s "
            f"burst<={self.burst:.0f} endpoints={len(self.endpoints)} "
            f"senders={len(self._live)}/{len(self.subs)}"
        )

    def run(self) -> None:
        if not self._setup_with_retry():
            return
        if not getattr(self, "subs", None):
            return  # stopped while waiting for a funded pool
        self._flood()

    # ── setup helpers ─────────────────────────────────────────────────────────
    def _await_accounts(self):
        """Block (interruptibly) until >=2 funded accounts exist, or we stop."""
        warned = False
        while not self.stopping():
            accts = self.ctx.get_shared("l2_accounts", [])
            if len(accts) >= 2:
                return list(accts)
            if not warned:
                self.state.log("waiting for >=2 funded l2_accounts", "warn")
                self.state.set_status("idle")
                warned = True
            self.sleep(5)
        return None

    def _seed_balances(self) -> None:
        """Read every sender's L2 balance once; only solvent ones enter the rotation."""
        for i, sub in enumerate(self.subs):
            try:
                bal = self.ctx.l2.balance(sub.address)
            except Exception:  # noqa: BLE001 — unreadable balance: assume usable, the
                bal = self.spend_floor  # spend guard re-checks on the first shortfall
            self._balance[i] = bal
            if bal >= self.spend_floor:
                self._live.append(i)
                self._live_set.add(i)
        self.state.gauge("live_senders", len(self._live))

    # ── the flood ─────────────────────────────────────────────────────────────
    def _flood(self) -> None:
        self.state.set_status("running")
        tick = 0.05  # 50ms dispatch tick
        # Cap outstanding work so a stalled node can't let our queue grow without
        # bound (the executor has `concurrency` workers; allow a little slack).
        max_inflight = self.concurrency * 2

        last_head = self._head()
        last_head_change = self.now()
        accepted_at_block = self._accepted()  # accepted count as of last head advance
        prev = self._snapshot_stats()
        prev_t = self.now()
        next_metrics = self.now() + 2.0

        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="ddos") as pool:
            while not self.stopping():
                t0 = self.now()

                # Submission budget: free in-flight slots, capped by the token bucket.
                with self._slock:
                    inflight = self._stats["inflight"]
                slots = max(0, max_inflight - inflight)
                n = self._take_tokens(slots) if (slots and self._can_spend()) else 0
                for _ in range(n):
                    endpoint = self._next_endpoint()
                    with self._slock:
                        self._stats["inflight"] += 1
                    pool.submit(self._fire, endpoint)

                # Metrics + liveness roughly every 2s.
                now = self.now()
                if now >= next_metrics:
                    cur = self._snapshot_stats()
                    dt = max(1e-6, now - prev_t)
                    head = self._head()
                    head_advanced = head is not None and last_head is not None and head > last_head
                    self._emit_metrics(head, last_head, cur, prev, dt)
                    last_head, last_head_change, accepted_at_block = self._liveness(
                        head, last_head, last_head_change, cur, prev, dt, accepted_at_block
                    )
                    self._backpressure(cur, prev, dt, now, head_advanced)
                    self._revive_depleted(now)
                    prev, prev_t = cur, now
                    next_metrics = now + 2.0

                # Pace the tick (interruptible).
                elapsed = self.now() - t0
                if elapsed < tick:
                    self.sleep(tick - elapsed)

        self.state.set_status("done")
        self.state.log("flood stopped")

    # ── rate limiting (monotonic token bucket) ────────────────────────────────
    def _take_tokens(self, limit: int) -> int:
        """Accrue tokens for the elapsed monotonic time and consume up to `limit`.

        Tokens accrue at the *current* rate ceiling and saturate at `burst`; the
        fractional remainder is kept so short ticks never round up into an
        overshoot (the old `int(rate*tick)+1` heuristic ran ~10% hot).
        """
        if limit <= 0:
            return 0
        now = time.monotonic()
        with self._bucket_lock:
            elapsed = max(0.0, now - self._tokens_at)
            self._tokens_at = now
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate_cur)
            n = int(min(float(limit), self._tokens))
            if n > 0:
                self._tokens -= n
            return n

    # ── spend guard ───────────────────────────────────────────────────────────
    def _can_spend(self) -> bool:
        """False once the run's attempted-spend budget is exhausted or nobody is solvent."""
        with self._acct_lock:
            live = len(self._live)
            spent = self._spend_attempted
        if not live:
            if not self._no_senders_reported:
                self._no_senders_reported = True
                self.state.log(
                    "all senders below the balance floor — pausing flood until refunded", "warn"
                )
            return False
        if self.max_spend and spent >= self.max_spend:
            if not self._budget_reported:
                self._budget_reported = True
                self.state.log(
                    f"attempted-spend budget reached ({spent} >= {self.max_spend} wei) — "
                    f"pausing flood",
                    "warn",
                )
            return False
        return True

    def _charge(self, index: int) -> None:
        """Debit the local balance estimate for one submitted tx; retire if depleted."""
        with self._acct_lock:
            self._spend_attempted += self.tx_cost
            self._balance[index] -= self.tx_cost
            low = self._balance[index] < self.spend_floor and index in self._live_set
        if low:
            self._refresh_balance(index)

    def _refresh_balance(self, index: int) -> None:
        """Re-read one sender's balance and add/remove it from the rotation."""
        try:
            bal = self.ctx.l2.balance(self.subs[index].address)
        except Exception:  # noqa: BLE001 — keep the estimate; we retry on the next charge
            return
        retired = revived = False
        with self._acct_lock:
            self._balance[index] = bal
            if bal < self.spend_floor:
                if index in self._live_set:
                    self._live_set.discard(index)
                    self._live = [j for j in self._live if j != index]
                    retired = True
            elif index not in self._live_set:
                self._live_set.add(index)
                self._live.append(index)
                revived = True
            live = len(self._live)
        if retired:
            self.state.incr("senders_depleted")
        if revived:
            self.state.incr("senders_revived")
            self._no_senders_reported = False
        if retired or revived:
            self.state.gauge("live_senders", live)

    def _revive_depleted(self, now: float) -> None:
        """Periodically re-check retired senders — the funder may have topped them up."""
        if now < self._next_revive:
            return
        self._next_revive = now + 30.0
        with self._acct_lock:
            dead = [i for i in range(len(self.subs)) if i not in self._live_set]
        for i in dead:
            if self.stopping():
                return
            self._refresh_balance(i)

    def _fire(self, endpoint: str) -> None:
        """Broadcast one dust transfer from a rotating solvent sender."""
        try:
            if self.stopping():
                return
            i = self._next_sender()
            if i is None:
                return  # every sender is below the floor
            sub = self.subs[i]
            to = self._pick_recipient(i)

            with self._slock:
                self._stats["sent"] += 1
            self.state.incr("sent_total")
            self._charge(i)
            try:
                # Shared coordinated nonce allocator — never a private counter, other
                # workers send from these very same accounts.
                self.ctx.send_sub(
                    self.ctx.l2,
                    sub.private_key,
                    to=to,
                    value=self.dust,
                    gas=GAS_PER_TRANSFER,
                    gas_price=self.gas_price,
                    endpoint=endpoint,
                )
                with self._slock:
                    self._stats["accepted"] += 1
                self.state.incr("accepted_total")
            except Exception as exc:  # noqa: BLE001 — every reject is data, not fatal
                self._record_reject(i, exc)
        finally:
            with self._slock:
                self._stats["inflight"] -= 1

    def _record_reject(self, sender_idx: int, exc: Exception) -> None:
        bucket, reason = _classify(exc)
        with self._slock:
            self._stats["rejected"] += 1
            if bucket == "rpc_error":
                self._stats["rpc_errors"] += 1
            self._reasons[reason] += 1
        self.state.incr("rejected_total")
        # A nonce gap (too low / already known) means the shared cursor drifted from
        # the node — drop it so the next allocate re-seeds from the pending nonce.
        if reason in ("nonce too low", "already known"):
            try:
                self.ctx.resync_sub(self.ctx.l2, self.subs[sender_idx].address)
                self.state.incr("nonce_resyncs")
            except Exception:  # noqa: BLE001 — resync is best-effort
                pass
        # The node says this account can't pay: believe it and re-check the balance,
        # which retires the sender if it really is below the floor.
        elif reason == "insufficient funds":
            self._refresh_balance(sender_idx)

    # ── metrics & liveness ────────────────────────────────────────────────────
    def _emit_metrics(self, head, last_head, cur, prev, dt) -> None:
        head_delta = ((head - last_head) / dt) if (head is not None and last_head is not None) else 0
        self.state.gauge("l2_head", head)
        self.state.gauge("head_delta_bps", round(head_delta, 3))
        self.state.gauge("sent_total", cur["sent"])
        self.state.gauge("accepted_total", cur["accepted"])
        self.state.gauge("rejected_total", cur["rejected"])
        self.state.gauge("inflight", cur["inflight"])
        self.state.gauge("send_rate_cur", round((cur["sent"] - prev["sent"]) / dt, 1))
        self.state.gauge("accept_rate_cur", round((cur["accepted"] - prev["accepted"]) / dt, 1))
        self.state.gauge("rate_ceiling_cur", round(self.rate_cur, 1))
        with self._acct_lock:
            self.state.gauge("attempted_spend_wei", self._spend_attempted)
            self.state.gauge("live_senders", len(self._live))
        dom = self._dominant_reason()
        if dom:
            self.state.gauge("dominant_reject", dom)

    def _liveness(self, head, last_head, last_head_change, cur, prev, dt, accepted_at_block):
        """Detect halts and accept-rate collapse; update block-throughput gauges."""
        sent_rate = (cur["sent"] - prev["sent"]) / dt
        accepted_delta = cur["accepted"] - prev["accepted"]
        now = self.now()

        if head is None:
            return last_head, last_head_change, accepted_at_block

        if last_head is None or head > last_head:
            # Head advanced: record throughput per block, reset the halt timer.
            if last_head is not None and head > last_head:
                blocks = head - last_head
                per_block = (cur["accepted"] - accepted_at_block) / blocks
                if per_block > self._max_accepted_per_block:
                    self._max_accepted_per_block = per_block
                    self.state.gauge("max_accepted_per_block", round(per_block, 1))
            return head, now, cur["accepted"]

        # Head did NOT advance since last sample.
        stalled_for = now - last_head_change
        if stalled_for > 15 and sent_rate > 0 and not self._halt_reported:
            self._halt_reported = True
            self.state.finding(
                title="L2 halted under load",
                severity="critical",
                detail=(
                    f"L2 head stuck at {head} for {stalled_for:.0f}s while sending "
                    f"~{sent_rate:.0f} tx/s (accepted ~{accepted_delta/dt:.0f}/s) — chain "
                    f"appears halted under DDoS load"
                ),
                head=head,
                send_rate=round(sent_rate, 1),
                stalled_seconds=round(stalled_for, 1),
            )
        return last_head, last_head_change, accepted_at_block

    def _backpressure(self, cur, prev, dt, now, head_moving) -> None:
        """Throttle on heavy transport errors; note recovery when they clear."""
        sent_delta = max(0, cur["sent"] - prev["sent"])
        acc_delta = cur["accepted"] - prev["accepted"]
        rej_delta = cur["rejected"] - prev["rejected"]
        rpcerr_delta = cur["rpc_errors"] - prev["rpc_errors"]
        err_frac = (rpcerr_delta / sent_delta) if sent_delta else 0.0

        # Accept-rate collapse while the head still advances => the node is refusing
        # our txs (mempool bounce), distinct from a full halt.
        if (
            sent_delta > 0
            and acc_delta <= max(1, sent_delta * 0.02)
            and rej_delta > 0
            and head_moving
            and not self._refuse_reported
        ):
            self._refuse_reported = True
            dom = self._dominant_reason() or "unknown"
            self.state.finding(
                title="mempool refusing txs",
                severity="high",
                detail=(
                    f"accept-rate collapsed to ~{acc_delta/dt:.1f}/s (of ~{sent_delta/dt:.0f}/s "
                    f"sent) while the L2 head is still advancing — node is bouncing txs; "
                    f"dominant reason: {dom}"
                ),
                dominant_reason=dom,
                send_rate=round(sent_delta / dt, 1),
            )

        # Estimate mempool capacity: depth (accepted-but-not-yet-mined) at the moment
        # the node first starts rejecting on a fullness reason.
        if self._cap_estimate is None and self._rejecting_on_fullness():
            depth = cur["accepted"] - self._accepted_at_last_block()
            self._cap_estimate = max(0, depth)
            self.state.gauge("mempool_cap_estimate", self._cap_estimate)

        # Transport-level back-pressure control.  Only the token accrual rate moves;
        # the bucket itself keeps the average honest at whatever rate we pick.
        if err_frac > 0.25 and sent_delta >= 5:
            new_rate = max(5.0, self.rate_cur / 2)
            if new_rate < self.rate_cur:
                self.rate_cur = new_rate
                self.state.log(
                    f"back-pressure: rpc error_frac={err_frac:.0%}, throttling to "
                    f"{self.rate_cur:.0f}/s",
                    "warn",
                )
            if self._degraded_since is None:
                self._degraded_since = now
                self._recovery_reported = False
        else:
            # Clean window: probe upward toward the ceiling (never past it).
            if self.rate_cur < self.rate_max:
                self.rate_cur = min(float(self.rate_max), self.rate_cur * 2)
                self.state.gauge("rate_ceiling_cur", round(self.rate_cur, 1))
            # Fully recovered — record how long the degradation lasted, once.
            if (
                self._degraded_since is not None
                and self.rate_cur >= self.rate_max
                and not self._recovery_reported
            ):
                recovery = now - self._degraded_since
                self._recovery_reported = True
                self._degraded_since = None
                self.state.gauge("last_recovery_seconds", round(recovery, 1))
                self.state.finding(
                    title="RPC recovered after back-pressure throttle",
                    severity="info",
                    detail=(
                        f"RPC transport errors cleared and rate restored to {self.rate_max:.0f}/s "
                        f"after {recovery:.0f}s of throttling"
                    ),
                    recovery_seconds=round(recovery, 1),
                )

    # ── small helpers ─────────────────────────────────────────────────────────
    def _pick_recipient(self, sender_idx: int) -> str:
        # Recipients need no balance, so the whole pool is fair game.
        j = sender_idx
        while j == sender_idx:
            j = random.randrange(len(self.subs))
        return self.subs[j].address

    def _next_sender(self) -> int | None:
        """Round-robin over the *solvent* senders; None when the pool is depleted."""
        with self._acct_lock:
            if not self._live:
                return None
            self._sender_rr = (self._sender_rr + 1) % len(self._live)
            return self._live[self._sender_rr]

    def _next_endpoint(self) -> str:
        with self._rr_lock:
            ep = self.endpoints[self._endpoint_rr % len(self.endpoints)]
            self._endpoint_rr += 1
            return ep

    def _snapshot_stats(self) -> dict:
        with self._slock:
            return dict(self._stats)

    def _accepted(self) -> int:
        with self._slock:
            return self._stats["accepted"]

    def _accepted_at_last_block(self) -> int:
        # Approximation used only for the cap estimate; the exact block boundary is
        # tracked in _liveness, so fall back to the current accepted total minus the
        # per-block throughput if unknown.
        return int(self._accepted() - self._max_accepted_per_block)

    def _dominant_reason(self) -> str | None:
        with self._slock:
            if not self._reasons:
                return None
            return self._reasons.most_common(1)[0][0]

    def _rejecting_on_fullness(self) -> bool:
        with self._slock:
            return any(
                k in ("txpool full",) and v > 0 for k, v in self._reasons.items()
            )

    def _head(self):
        try:
            return self.ctx.l2.block_number()
        except Exception:  # noqa: BLE001 — a read failure is itself a symptom, not fatal
            return None


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a broadcast failure to (bucket, short_reason).

    bucket is "reject" for a logical node rejection (returned as a JSON-RPC error)
    or "rpc_error" for a transport/connection failure — the two are handled
    differently: rejects feed the dominant-reason stats, transport errors trigger
    back-pressure throttling.
    """
    msg = str(exc).lower()
    if isinstance(exc, RpcError):
        if "nonce too low" in msg or ("nonce" in msg and "low" in msg):
            reason = "nonce too low"
        elif "already known" in msg or "already exists" in msg or "known transaction" in msg:
            reason = "already known"
        elif "replacement" in msg or "underpriced" in msg:
            reason = "underpriced"
        elif "txpool" in msg or "mempool" in msg or "pool is full" in msg or "too many" in msg:
            reason = "txpool full"
        elif "insufficient funds" in msg:
            reason = "insufficient funds"
        elif "intrinsic gas" in msg or "gas too low" in msg:
            reason = "intrinsic gas"
        else:
            reason = msg[:60] or "rpc rejected"
        return "reject", reason
    # requests transport errors (ConnectionError, Timeout, HTTPError, ...).
    return "rpc_error", "rpc transport error"
