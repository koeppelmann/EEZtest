"""Congestion worker — race a cross-chain write against a same-chain write.

Two writers, one slot of state.  Each round this worker aims an L2->L1 cross-chain
`SimpleStorage.store(X)` and a plain L1 `SimpleStorage.store(Y)` at the *same*
contract inside the same block window, then reads the survivor back.  Either
writer may legitimately win the race — what must never happen is that the final
value is neither X nor Y, or that the value flickers to a third state: that means
a write was lost or applied only partially across the composed block.

The mirror-image race is also run: an L1->L2 deposit crediting an account while
that same account spends from its L2 balance, looking for a double credit or a
spend that never gets charged.  That race runs against an *exclusive* actor
account (`SubAccount.derive("congestion-actor", 0)`) that no other worker ever
touches, and it accounts for the spend's receipt gas exactly — otherwise a
balance delta could not be attributed to this worker's own two legs.  The actor
is seeded from the primary key (a deposit to its L1 proxy) when it runs dry.

Every sub-account transaction here (the counterparty's L1 write, the actor's L2
spend) goes through `ctx.send_sub`, i.e. the single cross-worker nonce allocator;
primary-key transactions keep using `client.send`, which rolls its own nonce back
on a failed broadcast.  Nothing in this worker ever resets the shared primary
client's nonce, because other threads may be holding reservations on it.

Everything here is single-hop.  The L2 permits only *simple* L1<->L2 calls, so
the cross-chain leg is one `call_l1_from_l2` onto a plain L1 contract — never a
nested cross-chain action.  Both legs that touch a CrossChainProxy are sent with
`cfg.eez.crosschain_gas_limit` (>=200k); an under-gassed proxy tx makes the
composer loop forever, which would wedge the whole run.

Correctness is judged on the *decoded* L1 storage value, never on `status == 1`
(Bug B: a cross-chain call can report success while nothing landed).

Findings produced:
  * LOST WRITE — final value is neither X nor Y though a leg reported success (high);
  * partial apply — X was observed on L1 and then vanished without Y winning (high);
  * L2->L1 return path reverted with ExecutionNotFound after the inner call ran
    (Bug A, high) or never settled at all (med);
  * cross-chain derived sender (our L1 proxy) not stable across rounds (Bug C, high);
  * deposit credited twice / racing spend never charged (high);
  * deposit mined on L1 but never credited while the account was busy (med).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .base import SubAccount, Worker

# Where the racing L2 spend sends its dust.  A burn address nothing else touches,
# so its balance delta is a clean witness that the spend really executed.
SINK = to_checksum_address("0x000000000000000000000000000000000000dEaD")

# Seed for this worker's EXCLUSIVE actor.  Deliberately outside the funder's pool
# seed so no other worker ever signs from (or spends) this account: only then is
# a balance delta attributable to this worker's own deposit/spend legs.
ACTOR_SEED = "congestion-actor"

# Candidate signatures for the known L2->L1 return-path regression (Bug A).
EXECUTION_NOT_FOUND_SIGS = (
    "ExecutionNotFound()",
    "ExecutionNotFound(bytes32)",
    "ExecutionNotFound(address)",
    "ExecutionNotFound(address,uint256)",
)


class CongestionWorker(Worker):
    name = "congestion"
    description = "Race an L2->L1 write against a parallel L1 write to the same state"

    # ── lifecycle ────────────────────────────────────────────────────────────
    def setup(self) -> None:
        # Dust everywhere: this worker runs for the whole session.
        self.settle_seconds = float(self.wcfg.get("settle_seconds", 60))
        self.deposit_wei = int(self.wcfg.get("deposit_wei", 50_000_000_000_000))
        self.spend_wei = int(self.wcfg.get("spend_wei", 10_000_000_000_000))
        self.l1_gas = int(self.wcfg.get("l1_write_gas", 120_000))
        self.xgas = self.cfg.eez.crosschain_gas_limit

        # Exclusive actor for the deposit-vs-spend race: derived from a seed no
        # other worker uses, so its balance only moves because of our two legs.
        self.actor = SubAccount.derive(ACTOR_SEED, 0)
        self.actor_seed_wei = int(self.wcfg.get("actor_seed_wei", 400_000_000_000_000))
        self.state.gauge("actor", self.actor.address)
        # A deposit of ours that mined on L1 but had not credited L2 when the
        # window closed can still land later and inflate the *next* round's
        # delta.  While one is outstanding we hold the balance race instead of
        # attributing someone else's timing to a double credit.
        self._credit_target: int | None = None
        self._credit_waits = 0

        # Monotonic, disjoint value streams so a final read identifies the winner
        # unambiguously (X values are low, Y values are high).
        self._x = int(self.wcfg.get("x_base", 1_000_000))
        self._y = int(self.wcfg.get("y_base", 900_000_000))

        self.storage: Any = None  # Deployment, once it lands
        self.art = self.ctx.contracts.get("SimpleStorage")
        self._expected_sender: str | None = None  # our L1 proxy (Bug C baseline)
        self._ready = False

        # Optional second signer for the L1 leg — avoids fighting the main key's
        # nonce while both legs are in flight.  Its sends go through the shared
        # allocator (ctx.send_sub), so another worker holding the same key cannot
        # collide with us; we only keep the key + address here.
        self.cp_key: str = ""
        self.cp_address: str = ""
        if self.cfg.counterparty_key:
            try:
                acct = Account.from_key(self.cfg.counterparty_key)
                self.cp_key = self.cfg.counterparty_key
                self.cp_address = to_checksum_address(acct.address)
                self.state.gauge("counterparty", self.cp_address)
            except Exception as exc:  # noqa: BLE001 — bad key is a config problem, not fatal
                self.state.log(f"counterparty key unusable: {exc}", "warn")
        if not self.cp_key:
            self.state.log("no counterparty key — L1 leg uses the main key's own nonce management")

        self.state.gauge("settle_seconds", self.settle_seconds)
        self.state.gauge("crosschain_gas", self.xgas)

    def interval(self) -> float:
        return float(self.wcfg.get("interval_seconds", 20))

    def step(self) -> None:
        # Preparation is retried from inside the loop so a cold chain (no L2
        # balance yet, funder still working) backs off instead of crashing.
        if not self._prepare():
            return
        self._check_sender_stability()
        self._race_write()
        if self.stopping():
            return
        self._race_deposit_vs_spend()

    # ── preparation ─────────────────────────────────────────────────────────
    def _prepare(self) -> bool:
        """Deploy the shared L1 target and confirm we can originate L2->L1 calls."""
        if self._ready:
            return True

        # 1. The contested contract: one SimpleStorage on L1, reused every round.
        if self.storage is None:
            try:
                dep = self.ctx.contracts.deploy(self.ctx.l1, "SimpleStorage", side="l1")
            except Exception as exc:  # noqa: BLE001
                self.state.incr("deploy_send_errors")
                self.state.log(f"SimpleStorage deploy send failed: {exc}", "warn")
                return False
            rcpt = self.ctx.l1.wait_receipt(dep.tx_hash, timeout=180)
            if rcpt is None or not rcpt.ok or not self.ctx.l1.has_code(dep.address):
                self.state.incr("deploy_failed")
                # No reset_nonce() here: the deploy was broadcast, and the primary
                # L1 client is shared with other workers that may hold nonce
                # reservations — resetting it under them creates the very gaps it
                # is meant to fix.
                self.state.log(f"SimpleStorage deploy {dep.tx_hash} did not produce code", "warn")
                return False
            self.storage = dep
            self.state.gauge("storage_l1", dep.address)
            self.state.log(f"contested SimpleStorage at {dep.address} (L1)")

        # 2. The L2->L1 leg is paid from the funding key's L2 balance — the funder
        #    bridges it, so back off until it shows up.
        try:
            l2_bal = self.ctx.l2.balance(self.ctx.l2.address)
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"L2 balance read failed: {exc}", "warn")
            return False
        self.state.gauge("origin_l2_balance_wei", l2_bal)
        if l2_bal <= 0:
            self.state.incr("waiting_for_l2_balance")
            self.state.log("funding account has no L2 balance yet — backing off", "warn")
            return False

        # 3. Our L1 proxy is the msg.sender the L1 target will see; it must exist
        #    before the return path can execute (and it is the Bug C baseline).
        try:
            ref = self.ctx.eez.ensure_l1_proxy(self.ctx.l2.address)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("proxy_errors")
            self.state.log(f"ensure_l1_proxy failed: {exc}", "warn")
            return False
        if not ref.exists:
            self.state.incr("proxy_create_failed")
            self.state.log(f"L1 proxy {ref.proxy_address} still has no code", "warn")
            return False
        self._expected_sender = ref.proxy_address
        self.state.gauge("derived_sender_proxy", ref.proxy_address)

        self._ready = True
        self.state.log("ready: target deployed, L2 balance present, L1 proxy live")
        return True

    def _check_sender_stability(self) -> None:
        """Bug C: the computed cross-chain proxy must not move between txs."""
        try:
            now = self.ctx.eez.compute_l1_proxy(self.ctx.l2.address)
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"compute_l1_proxy failed: {exc}", "warn")
            return
        if self._expected_sender and now != self._expected_sender:
            self.state.incr("derived_sender_changed")
            self.state.finding(
                title="Cross-chain derived sender is not stable",
                severity="high",
                detail=(
                    f"L1 proxy for {self.ctx.l2.address} was {self._expected_sender} "
                    f"and is now {now} — msg.sender at cross-chain targets is not deterministic"
                ),
                original=self.ctx.l2.address,
                was=self._expected_sender,
                now=now,
            )
            self._expected_sender = now
            self.state.gauge("derived_sender_proxy", now)

    # ── race 1: L2->L1 write vs. direct L1 write ─────────────────────────────
    def _race_write(self) -> None:
        x, y = self._next_values()
        target = self.storage.address
        data_x = self.art.encode_call("store", [x])
        data_y = self.art.encode_call("store", [y])
        pre = self._read_value()

        # Both writers wait on the same barrier so they hit the mempools together;
        # a broken barrier (one leg failed to even sign) must not hang the other.
        gate = threading.Barrier(2, timeout=30)
        observed: list[int] = []

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="congestion") as pool:
            f_x = pool.submit(self._leg_crosschain, target, data_x, gate)
            f_y = pool.submit(self._leg_direct_l1, target, data_y, gate)
            f_w = pool.submit(self._watch_value, observed)
            res_x = self._settled(f_x)
            res_y = self._settled(f_y)
            self._settled(f_w)

        final = self._read_value()
        self.state.incr("write_races")
        self.state.gauge("last_race", {"x": x, "y": y, "pre": pre, "final": final})
        self.state.gauge("last_observed_values", observed)

        # The cross-chain leg's own diagnostics (Bug A lives here).
        self._judge_crosschain_leg(res_x, x, observed)

        if final is None:
            self.state.incr("final_read_failed")
            self.state.log("could not read SimpleStorage.value after the race", "warn")
            return

        # Primary signal: the decoded slot value (X vs Y vs neither).  X and Y are
        # unique to this round, so this classification involves no balance
        # arithmetic and stays sound no matter who else is transacting.
        x_landed = final == x or x in observed
        y_landed = final == y or y in observed
        any_success = res_x.get("l2_ok") or res_y.get("l1_ok")

        if final == x:
            self.state.incr("crosschain_won")
            self.state.log(f"race {x}/{y}: cross-chain write won (value={final})")
        elif final == y:
            self.state.incr("l1_won")
            self.state.log(f"race {x}/{y}: direct L1 write won (value={final})")
            if x_landed:
                # X was applied and then legitimately overwritten — fine, but note it.
                self.state.incr("crosschain_applied_then_overwritten")
        elif final == pre and not any_success:
            # Nobody actually got a tx through; not an anomaly, just a stalled round.
            self.state.incr("no_writer_landed")
            self.state.log(f"race {x}/{y}: neither leg landed (value unchanged at {final})", "warn")
        else:
            self.state.incr("lost_writes")
            self.state.finding(
                title="Lost write: contested slot holds neither racer's value",
                severity="high",
                detail=(
                    f"raced cross-chain store({x}) against direct L1 store({y}) on "
                    f"{target}; pre={pre}, final={final}, values observed during the "
                    f"race={observed}. L2 leg ok={res_x.get('l2_ok')} "
                    f"(tx={res_x.get('tx')}), L1 leg ok={res_y.get('l1_ok')} "
                    f"(tx={res_y.get('tx')}). A write was dropped or partially applied."
                ),
                contract=target,
                x=x,
                y=y,
                final=final,
                observed=observed,
                l2_tx=res_x.get("tx"),
                l1_tx=res_y.get("tx"),
            )

        # Partial apply: X was visible mid-race but the slot ended somewhere else
        # and Y never showed up either.
        if x in observed and final != x and not y_landed:
            self.state.incr("partial_applies")
            self.state.finding(
                title="Cross-chain write applied then reverted out of state",
                severity="high",
                detail=(
                    f"store({x}) was observed on L1 during the race but the slot ended at "
                    f"{final} while the competing store({y}) never appeared — the composed "
                    f"block appears to have been partially applied/rolled back"
                ),
                contract=target,
                x=x,
                y=y,
                final=final,
                observed=observed,
            )

    def _leg_crosschain(self, target: str, data: str, gate: threading.Barrier) -> dict[str, Any]:
        """Leg A: L2 -> L1 single hop onto the contested contract (value 0)."""
        out: dict[str, Any] = {"leg": "crosschain"}
        try:
            self._pass_gate(gate)
            h, ref = self.ctx.eez.call_l1_from_l2(target, data, value=0, gas=self.xgas)
            out["tx"] = h
            out["proxy"] = ref.proxy_address
            self.state.incr("crosschain_sent")
            rcpt = self.ctx.l2.wait_receipt(h, timeout=self.settle_seconds)
            if rcpt is None:
                out["l2_ok"] = False
                out["mined"] = False
                return out
            out["mined"] = True
            out["l2_ok"] = rcpt.ok
            out["block"] = rcpt.block_number
            if not rcpt.ok:
                out["revert"] = self.ctx.l2.revert_reason(h)
        except Exception as exc:  # noqa: BLE001 — a failed leg must not kill the round
            out["error"] = str(exc)
            self.state.incr("crosschain_send_errors")
        return out

    def _leg_direct_l1(self, target: str, data: str, gate: threading.Barrier) -> dict[str, Any]:
        """Leg B: a plain L1 tx to the same contract, timed for the same window."""
        out: dict[str, Any] = {"leg": "direct_l1"}
        try:
            self._pass_gate(gate)
            if self.cp_key:
                # Non-primary signer: nonce comes from the shared cross-worker
                # allocator (and is rolled back for us if the broadcast fails).
                # Plain write: straight to the L1 RPC, not the cross-chain front.
                h = self.ctx.send_sub(
                    self.ctx.l1,
                    self.cp_key,
                    to=target,
                    data=data,
                    gas=self.l1_gas,
                    endpoint=self.cfg.l1.rpc,
                )
            else:
                # Primary key: let the client reserve (and self-roll-back) its
                # own nonce — never hand it one, and never reset it here.
                h = self.ctx.l1.send(
                    to=target,
                    data=data,
                    gas=self.l1_gas,
                    endpoint=self.cfg.l1.rpc,
                )
            out["tx"] = h
            self.state.incr("l1_writes_sent")
            rcpt = self.ctx.l1.wait_receipt(h, timeout=self.settle_seconds)
            out["mined"] = rcpt is not None
            out["l1_ok"] = bool(rcpt is not None and rcpt.ok)
            if rcpt is not None:
                out["block"] = rcpt.block_number
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            self.state.incr("l1_write_send_errors")
            # Both send paths roll their own nonce back; nothing to reset here.
        return out

    def _watch_value(self, sink: list[int]) -> None:
        """Sample the contested slot through the race so we can see flicker."""
        deadline = self.now() + self.settle_seconds
        while self.now() < deadline and not self.stopping():
            v = self._read_value()
            if v is not None and (not sink or sink[-1] != v):
                sink.append(v)
            self.sleep(1.5)

    def _judge_crosschain_leg(self, res: dict[str, Any], x: int, observed: list[int]) -> None:
        """Record the L2->L1 return-path failure modes (Bug A and friends)."""
        if res.get("error"):
            self.state.log(f"cross-chain leg send error: {res['error']}", "warn")
            return
        if not res.get("mined"):
            self.state.incr("crosschain_never_settled")
            self.state.finding(
                title="L2->L1 cross-chain write never settled",
                severity="med",
                detail=(
                    f"store({x}) sent via the L2 outbound front (tx={res.get('tx')}) had no "
                    f"receipt after {self.settle_seconds:.0f}s while an L1 writer raced it — "
                    f"return path stalled under contention"
                ),
                tx=res.get("tx"),
                x=x,
            )
            return
        if res.get("l2_ok"):
            return

        # Mined and reverted: did the inner call already run before the return leg blew up?
        revert = res.get("revert") or ""
        inner_ran = x in observed
        if _is_execution_not_found(revert):
            self.state.incr("execution_not_found")
            self.state.finding(
                title="L2->L1 return path reverted with ExecutionNotFound",
                severity="high",
                detail=(
                    f"cross-chain store({x}) (tx={res.get('tx')}) reverted with "
                    f"ExecutionNotFound; the inner L1 call "
                    f"{'DID already run (value observed on L1)' if inner_ran else 'left no trace'}"
                    f" — return-path bookkeeping lost the execution"
                ),
                tx=res.get("tx"),
                revert=revert,
                inner_ran=inner_ran,
                x=x,
            )
        else:
            self.state.incr("crosschain_reverted")
            self.state.finding(
                title="L2->L1 cross-chain write reverted under contention",
                severity="med" if not inner_ran else "high",
                detail=(
                    f"cross-chain store({x}) (tx={res.get('tx')}) reverted (revert={revert!r}); "
                    f"inner call {'already applied on L1' if inner_ran else 'not observed on L1'}"
                ),
                tx=res.get("tx"),
                revert=revert,
                inner_ran=inner_ran,
                x=x,
            )

    # ── race 2: deposit credit vs. L2 spend from the same balance ────────────
    def _race_deposit_vs_spend(self) -> None:
        # The contested account is OURS ALONE (derived from ACTOR_SEED, never
        # published to `l2_accounts`), so every wei of the delta measured below
        # comes from this round's deposit leg, spend leg, or their gas.
        acct = self.actor

        gas_price = self.ctx.l2.gas_price()
        fee_budget = 21_000 * gas_price

        # The deposit lands on a CrossChainProxy, so the proxy must exist first
        # (it is also how we seed the actor, so do it before the balance check).
        try:
            ref = self.ctx.eez.ensure_l1_proxy(acct.address)
            if not ref.exists:
                self.state.incr("balance_race_proxy_missing")
                return
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"ensure_l1_proxy for {acct.address[:10]} failed: {exc}", "warn")
            return

        try:
            b0 = self.ctx.l2.balance(acct.address)
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"L2 balance read failed: {exc}", "warn")
            return

        # Settle any credit still owed to the actor before measuring anything.
        if self._credit_target is not None:
            if b0 < self._credit_target and self._await_credit(acct.address, self._credit_target):
                b0 = self.ctx.l2.balance(acct.address)
            if b0 >= self._credit_target:
                self._credit_target = None
                self._credit_waits = 0
                self.state.log("outstanding actor credit arrived — balance race resuming")
            else:
                self._credit_waits += 1
                self.state.incr("balance_race_credit_in_flight")
                if self._credit_waits >= 3:
                    self.state.log(
                        "owed credit never arrived after 3 rounds; resuming balance races "
                        "(the lost-deposit finding already records it)",
                        "warn",
                    )
                    self._credit_target = None
                    self._credit_waits = 0
                return

        if b0 <= self.spend_wei + fee_budget:
            # Nobody else funds this account, so top it up from the primary key.
            if not self._seed_actor(b0):
                self.state.incr("balance_race_skipped")
                return
            try:
                b0 = self.ctx.l2.balance(acct.address)
            except Exception as exc:  # noqa: BLE001
                self.state.log(f"L2 balance read failed: {exc}", "warn")
                return
            if b0 <= self.spend_wei + fee_budget:
                self.state.incr("balance_race_skipped")
                return

        sink_pre = self.ctx.l2.balance(SINK)
        gate = threading.Barrier(2, timeout=30)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="congestion-bal") as pool:
            f_dep = pool.submit(self._leg_deposit, acct.address, gate)
            f_spend = pool.submit(self._leg_spend, gas_price, gate)
            dep = self._settled(f_dep)
            spend = self._settled(f_spend)

        # Give the credit time to arrive, then measure.
        self._await_credit(acct.address, b0 + self.deposit_wei - self.spend_wei - fee_budget)
        b1 = self.ctx.l2.balance(acct.address)
        sink_post = self.ctx.l2.balance(SINK)
        delta = b1 - b0
        # Exact gas from the spend's receipt — the only other wei that can leave
        # this account.  Unknown (unmined) degrades to 0, i.e. the looser bound.
        fee = int(spend.get("fee") or 0)
        spent_ok = bool(spend.get("ok")) and (sink_post - sink_pre) >= self.spend_wei
        self.state.incr("balance_races")
        self.state.gauge(
            "last_balance_race",
            {
                "account": acct.address,
                "pre": b0,
                "post": b1,
                "deposit": self.deposit_wei,
                "spend": self.spend_wei,
                "fee": fee,
                "delta": delta,
                "expected_delta_if_both_applied": self.deposit_wei - self.spend_wei - fee,
            },
        )

        if delta > self.deposit_wei:
            # Impossible under any interleaving: the account cannot end up richer
            # than (start + one deposit) while it also paid out a spend.
            self.state.incr("double_credit")
            self.state.finding(
                title="Double-spend/double-credit anomaly in deposit vs. spend race",
                severity="high",
                detail=(
                    f"exclusive actor {acct.address} started at {b0} wei, was credited one "
                    f"deposit of {self.deposit_wei} wei while spending {self.spend_wei} wei "
                    f"(gas {fee} wei), and ended at {b1} wei (delta {delta} > deposit). "
                    f"No other worker signs for this account. Deposit tx={dep.get('tx')}, "
                    f"spend tx={spend.get('tx')}"
                ),
                account=acct.address,
                pre=b0,
                post=b1,
                fee=fee,
                deposit_tx=dep.get("tx"),
                spend_tx=spend.get("tx"),
            )
        elif spent_ok and delta > self.deposit_wei - self.spend_wei - fee:
            self.state.incr("spend_not_charged")
            self.state.finding(
                title="Racing L2 spend executed but was not charged",
                severity="high",
                detail=(
                    f"exclusive actor {acct.address} sent {self.spend_wei} wei to {SINK} (sink "
                    f"credited, tx={spend.get('tx')}, gas {fee} wei) while a deposit of "
                    f"{self.deposit_wei} wei landed, yet its balance moved by {delta} wei "
                    f"(expected at most {self.deposit_wei - self.spend_wei - fee}) — the spend "
                    f"was not deducted"
                ),
                account=acct.address,
                pre=b0,
                post=b1,
                fee=fee,
                spend_tx=spend.get("tx"),
                deposit_tx=dep.get("tx"),
            )
        elif dep.get("l1_ok") and delta <= -self.spend_wei:
            # Spend charged, deposit mined on L1, nothing arrived on L2.  It may
            # still credit later, so hold the next race until it does.
            self._credit_target = b1 + self.deposit_wei
            self.state.incr("deposit_lost_under_contention")
            self.state.finding(
                title="Deposit mined on L1 but never credited while the account was spending",
                severity="med",
                detail=(
                    f"deposit {dep.get('tx')} of {self.deposit_wei} wei to {acct.address} mined on "
                    f"L1, but the L2 balance moved by only {delta} wei (spend {self.spend_wei} wei "
                    f"+ {fee} wei gas, tx={spend.get('tx')}) within the settle window"
                ),
                account=acct.address,
                pre=b0,
                post=b1,
                fee=fee,
                deposit_tx=dep.get("tx"),
                spend_tx=spend.get("tx"),
            )
        else:
            self.state.incr("balance_race_consistent")

    def _leg_deposit(self, address: str, gate: threading.Barrier) -> dict[str, Any]:
        out: dict[str, Any] = {"leg": "deposit"}
        try:
            self._pass_gate(gate)
            h, _ref = self.ctx.eez.deposit(address, self.deposit_wei, gas=self.xgas)
            out["tx"] = h
            self.state.incr("deposits_sent")
            rcpt = self.ctx.l1.wait_receipt(h, timeout=self.settle_seconds)
            out["l1_ok"] = bool(rcpt is not None and rcpt.ok)
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            self.state.incr("deposit_send_errors")
        return out

    def _leg_spend(self, gas_price: int, gate: threading.Barrier) -> dict[str, Any]:
        """Plain L2 value transfer signed by the exclusive actor itself.

        Routed through the shared nonce allocator; the receipt's gas is reported
        back so the caller can reason about the balance delta exactly.
        """
        out: dict[str, Any] = {"leg": "spend"}
        try:
            self._pass_gate(gate)
            # A plain transfer, so the plain RPC — not the cross-chain front.
            h = self.ctx.send_sub(
                self.ctx.l2,
                self.actor.private_key,
                to=SINK,
                value=self.spend_wei,
                gas=21_000,
                gas_price=gas_price,
                endpoint=self.cfg.l2.rpc,
            )
            out["tx"] = h
            self.state.incr("spends_sent")
            rcpt = self.ctx.l2.wait_receipt(h, timeout=self.settle_seconds)
            out["ok"] = bool(rcpt is not None and rcpt.ok)
            if rcpt is not None:
                out["gas_used"] = rcpt.gas_used
                out["fee"] = rcpt.gas_used * _effective_gas_price(rcpt, gas_price)
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            self.state.incr("spend_send_errors")
            # Safe here (unlike a shared key): only this worker ever allocates
            # nonces for the actor, so re-seeding from the node cannot race.
            self.ctx.resync_sub(self.ctx.l2, self.actor.address)
        return out

    def _seed_actor(self, balance: int) -> bool:
        """Top the exclusive actor up from the primary key (deposit to its proxy).

        Nothing else funds this account, so without this the balance race would
        run once and then starve.  Returns True once the credit is visible.
        """
        need = self.actor_seed_wei
        try:
            h, _ref = self.ctx.eez.deposit(self.actor.address, need, gas=self.xgas)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("actor_seed_send_errors")
            self.state.log(f"actor seed deposit failed: {exc}", "warn")
            return False
        self.state.incr("actor_seeds_sent")
        rcpt = self.ctx.l1.wait_receipt(h, timeout=self.settle_seconds)
        if rcpt is None or not rcpt.ok:
            self.state.incr("actor_seed_l1_failed")
            self.state.log(f"actor seed deposit {h} did not mine successfully on L1", "warn")
            return False
        if self._await_credit(self.actor.address, balance + need):
            self.state.log(f"seeded exclusive actor {self.actor.address} with {need} wei")
            return True
        # No L2 balance to race with: the funder never touches this account and
        # our own seed did not land, so there is no usable L2 account this round.
        # It may still credit, so the next round waits for it before measuring.
        self._credit_target = balance + need
        self.state.incr("no_l2_accounts")
        self.state.log(
            f"actor seed {h} mined on L1 but {self.actor.address} was not credited on L2", "warn"
        )
        return False

    def _await_credit(self, address: str, target_balance: int) -> bool:
        deadline = self.now() + self.settle_seconds
        while self.now() < deadline and not self.stopping():
            try:
                if self.ctx.l2.balance(address) >= target_balance:
                    return True
            except Exception:  # noqa: BLE001 — transient RPC hiccup, keep polling
                pass
            self.sleep(3)
        return False

    # ── small helpers ───────────────────────────────────────────────────────
    def _next_values(self) -> tuple[int, int]:
        self._x += 1
        self._y += 1
        return self._x, self._y

    def _read_value(self) -> int | None:
        """Decoded SimpleStorage.value on L1 — never trust a receipt status alone."""
        try:
            res = self.ctx.l1.eth_call(self.storage.address, self.art.encode_call("value", []))
        except Exception:  # noqa: BLE001
            return None
        if not res or res == "0x":
            return None
        return int(res, 16)

    @staticmethod
    def _pass_gate(gate: threading.Barrier) -> None:
        """Release both legs together; a broken/timed-out gate just fires anyway."""
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass

    def _settled(self, future: Any) -> dict[str, Any]:
        """Collect a leg's result without ever letting it propagate out of the step."""
        try:
            return future.result(timeout=self.settle_seconds * 2 + 30) or {}
        except Exception as exc:  # noqa: BLE001
            self.state.incr("leg_errors")
            self.state.log(f"race leg failed: {exc}", "warn")
            return {}


def _effective_gas_price(receipt: Any, fallback: int) -> int:
    """Gas price actually charged, from the receipt when the node reports it."""
    try:
        raw = receipt.raw.get("effectiveGasPrice")
        if raw is not None:
            return int(raw, 16) if isinstance(raw, str) else int(raw)
    except Exception:  # noqa: BLE001 — any oddity falls back to the sent price
        pass
    return fallback


def _is_execution_not_found(revert: str | None) -> bool:
    """Match the Bug A custom error, as a selector or as a string revert reason."""
    if not revert:
        return False
    data = revert.lower()
    if "executionnotfound" in data:
        return True
    if data.startswith("0x") and len(data) >= 10:
        head = data[:10]
        for sig in EXECUTION_NOT_FOUND_SIGS:
            if head == "0x" + keccak(sig.encode())[:4].hex():
                return True
        # Error(string) payload carrying the name in ASCII.
        if "executionnotfound".encode().hex() in data:
            return True
    return False
