"""Contract-caller worker — the correctness heart of the suite.

Deploys `Counter` + `Logger` on **both** sides of the EEZ instance, optionally
verifies every deployment on Blockscout, and then drives the three call shapes
this build is supposed to support, one hop at a time:

  a. same-chain L1 baseline  — EOA -> LoggerL1.execute(CounterL1, increment())
  b. L1 -> L2 cross-chain    — EOA -> L1 proxy of LoggerL2 -> LoggerL2.execute(
                               CounterL2, increment())   (+ a direct probe:
                               EOA -> L1 proxy of CounterL2 -> increment())
  c. L2 -> L1 cross-chain    — EOA -> L2 rollupId=0 proxy of LoggerL1 ->
                               LoggerL1.execute(CounterL1, increment())
                               (+ the symmetric direct probe)

Every cross-chain direction is routed through the `Logger` on the DESTINATION
side, because the Logger durably records the raw `returnData` of the inner call.
That recorded blob is the only on-chain evidence of what EEZ actually propagated
back — counter state alone says nothing about the return path, so Bug B is always
asserted against `Logger.getCalls()[-1].returnData`, never against `Counter`.

The L2 permits only *simple* L1<->L2 calls: an outer cross-chain call may not
trigger an inner cross-chain call.  Every action built here is therefore a single
hop — the Logger sits on the destination chain, so `Logger.execute -> Counter` is
a plain same-chain inner call on the far side.

Each direction is probed TWICE per round (Logger route + direct route).  Both
probes must yield a derived sender before Bug C stability is asserted; with fewer
than two observations the round is recorded INCONCLUSIVE rather than passed.  The
same rule governs every read: a failed `eth_call` returns None and makes the probe
inconclusive — a read hiccup is never used as a trusted "empty" baseline.

Findings produced:
  * deploy never mined / reverted / no code at the CREATE address;
  * Blockscout verification rejected or timed out (low);
  * Bug A — the L2->L1 return path reverts with an ExecutionNotFound-like error
    (high), plus its two siblings: partial-apply (L1 incremented while the L2 tx
    reverted) and silent no-op (L2 tx succeeded, L1 never incremented);
  * Bug B — a call reports success with EMPTY returnData while the inner call
    plainly ran; we always assert `len(returnData) > 0` *and* the decoded value
    against the destination counter, never just `status == 1`;
  * Bug C — the cross-chain derived sender (msg.sender seen on the destination)
    does not equal the computed proxy address, or is not stable across txs;
  * the same-chain baseline breaking at all (that would mean plain EVM execution
    is broken, so it is reported high).
"""
from __future__ import annotations

from typing import Any, Callable

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ..contracts import Deployment
from ..verify import verify_standard_json
from .base import Worker

# ABI shapes of the two view functions we lean on.
INCREMENTS_TYPES = ["(address,uint256)[]"]  # Counter.getIncrements() -> Increment[]
CALLS_TYPES = ["(uint256,address,bytes,address,bytes)[]"]  # Logger.getCalls() -> Call[]

# rollupId 0 is L1 / mainnet on the EEZ side.
MAINNET_ROLLUP_ID = 0

# Solidity `Error(string)` revert envelope.
ERROR_STRING_SELECTOR = "08c379a0"

# Custom-error selectors that mark the known-buggy L2->L1 return path (Bug A).
# The exact arity has moved between builds, so we match any of them.
_EXEC_NOT_FOUND_SIGS = (
    "ExecutionNotFound()",
    "ExecutionNotFound(bytes32)",
    "ExecutionNotFound(address,bytes32)",
)
EXEC_NOT_FOUND_SELECTORS = {keccak(s.encode())[:4].hex() for s in _EXEC_NOT_FOUND_SIGS}

# The two probe shapes every cross-chain direction runs per round.
MODE_LOGGER = "logger"
MODE_DIRECT = "direct"


class ContractCallerWorker(Worker):
    name = "contract_caller"
    description = "Deploy Counter/Logger on L1+L2 and exercise every legal call combination"

    # ── setup ───────────────────────────────────────────────────────────────
    def setup(self) -> None:
        self.rounds = int(self.wcfg.get("rounds", 3))
        self.do_verify = bool(self.wcfg.get("verify", True))
        self.deploy_gas = int(self.wcfg.get("deploy_gas", 1_500_000))
        self.deploy_timeout = float(self.wcfg.get("deploy_timeout", 150))
        # Cross-chain settlement is slow; give the far side a generous window.
        self.xchain_timeout = float(self.wcfg.get("xchain_timeout", 240))
        self.poll = float(self.wcfg.get("poll_seconds", 3))
        # Any tx that lands on / creates a CrossChainProxy must carry >= 200k gas,
        # otherwise the composer loops forever on it.
        self.xgas = max(int(self.cfg.eez.crosschain_gas_limit), 200_000)

        # side -> contract name -> Deployment (only entries with live code).
        self.deployed: dict[str, dict[str, Deployment]] = {"l1": {}, "l2": {}}
        # Per-combination pass/fail tallies for the compact dashboard gauge.
        self.results: dict[str, dict[str, int]] = {
            "a_l1_same_chain": {"pass": 0, "fail": 0},
            "b_l1_to_l2": {"pass": 0, "fail": 0},
            "c_l2_to_l1": {"pass": 0, "fail": 0},
        }
        # Probes we could neither pass nor fail (a read failed, too few samples).
        # These are deliberately NOT folded into pass/fail: an unknown is not a pass.
        self.inconclusive: dict[str, int] = {k: 0 for k in self.results}
        # Every distinct derived sender ever observed per direction (Bug C).
        self.identities: dict[str, list[str]] = {"b_l1_to_l2": [], "c_l2_to_l1": []}

        self.state.gauge("rounds", self.rounds)
        self.state.gauge("crosschain_gas", self.xgas)
        self._publish_results()

    # ── main staged flow (run() is overridden: this worker is not a loop) ────
    def run(self) -> None:
        if not self._setup_with_retry():
            return  # stopped before setup could complete

        self._stage_deploy()
        if self.stopping():
            return

        if self.do_verify:
            self._stage_verify()

        # Stage 4: replay the whole combination matrix so intermittent bugs
        # (Bug A in particular is flaky) get more than one chance to show up.
        for rnd in range(1, self.rounds + 1):
            if self.stopping():
                break
            self.state.gauge("current_round", rnd)
            self.state.log(f"round {rnd}/{self.rounds} starting")
            self._stage_combinations(rnd)
            self._publish_results()

        self.state.log("combination rounds complete; idling")
        self.state.set_status("idle")
        while not self.stopping():
            self.sleep(30)
            self._refresh_gauges()

    # ── stage 1: deployments ────────────────────────────────────────────────
    def _stage_deploy(self) -> None:
        """Deploy Counter + Logger on both chains, tolerating partial failure."""
        plan = [
            ("l1", self.ctx.l1, "Counter"),
            ("l1", self.ctx.l1, "Logger"),
            ("l2", self.ctx.l2, "Counter"),
            ("l2", self.ctx.l2, "Logger"),
        ]
        # L2 deploys need L2 gas; the funder is the one that gets balance over
        # there, so give it a moment before we start burning nonces.
        self._await_l2_readiness()

        for side, client, cname in plan:
            if self.stopping():
                return
            self._deploy_one(side, client, cname)

        self.state.gauge(
            "deployed",
            {s: sorted(d.keys()) for s, d in self.deployed.items()},
        )
        missing = [f"{s}.{n}" for s in ("l1", "l2") for n in ("Counter", "Logger") if n not in self.deployed[s]]
        if missing:
            self.state.log(f"continuing with missing deployments: {missing}", "warn")

    def _deploy_one(self, side: str, client, cname: str) -> None:
        try:
            dep = self.ctx.contracts.deploy(client, cname, gas=self.deploy_gas, side=side)
        except Exception as exc:  # noqa: BLE001 — a send failure must not kill the stage
            self.state.incr("deploy_send_errors")
            self.state.finding(
                title=f"{cname} deploy could not be broadcast on {side.upper()}",
                severity="med",
                detail=f"send failed: {exc}",
                side=side,
                contract=cname,
            )
            return

        self.state.incr("deploys_sent")
        receipt = client.wait_receipt(dep.tx_hash, timeout=self.deploy_timeout)
        if receipt is None:
            self.state.incr("deploy_never_mined")
            self.state.finding(
                title=f"{cname} deploy never mined on {side.upper()}",
                severity="med",
                detail=(
                    f"deploy tx {dep.tx_hash} for {cname} not mined within "
                    f"{self.deploy_timeout:.0f}s (composer stall?)"
                ),
                side=side,
                contract=cname,
                tx=dep.tx_hash,
            )
            return
        if not receipt.ok:
            self.state.incr("deploy_reverted")
            self.state.finding(
                title=f"{cname} deploy reverted on {side.upper()}",
                severity="med",
                detail=f"deploy tx {dep.tx_hash} reverted (reason={client.revert_reason(dep.tx_hash)})",
                side=side,
                contract=cname,
                tx=dep.tx_hash,
            )
            return

        # The CREATE address is predicted from (sender, nonce); confirm the code
        # actually landed there before anyone builds calldata against it.
        try:
            has_code = client.has_code(dep.address)
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"code check for {side}.{cname} failed: {exc}", "warn")
            has_code = False
        if not has_code:
            self.state.incr("deploy_no_code")
            self.state.finding(
                title=f"{cname} deployed on {side.upper()} but no code at predicted address",
                severity="high",
                detail=(
                    f"tx {dep.tx_hash} succeeded in block {receipt.block_number} but "
                    f"{dep.address} is empty — CREATE address prediction or state application is off"
                ),
                side=side,
                contract=cname,
                address=dep.address,
                tx=dep.tx_hash,
            )
            return

        self.deployed[side][cname] = dep
        self.state.incr("deployed_ok")
        self.state.gauge(f"{side}_{cname.lower()}", dep.address)
        self.state.log(f"{side.upper()} {cname} @ {dep.address}")

    def _await_l2_readiness(self) -> None:
        """Bounded wait for the primary key to have L2 gas (the funder's job).

        We deliberately do *not* deploy from the funder's sub-account pool: the
        derived-identity assertions (Bug C) must be anchored on one stable
        sender.  The shared pool is only consulted as a readiness signal.
        """
        deadline = self.now() + float(self.wcfg.get("l2_ready_timeout", 90))
        while not self.stopping():
            try:
                bal = self.ctx.l2.balance(self.ctx.l2.address)
            except Exception as exc:  # noqa: BLE001
                self.state.log(f"L2 balance probe failed: {exc}", "warn")
                bal = 0
            if bal > 0:
                self.state.gauge("l2_signer_balance_wei", bal)
                return
            pool = self.ctx.get_shared("l2_accounts", [])
            if self.now() >= deadline:
                self.state.log(
                    f"primary L2 balance still 0 after wait (funder pool={len(pool)}); "
                    "proceeding — L2 deploys may fail",
                    "warn",
                )
                return
            self.sleep(self.poll)

    # ── stage 2: Blockscout verification ────────────────────────────────────
    def _stage_verify(self) -> None:
        apis = {"l1": self.cfg.blockscout.l1_api, "l2": self.cfg.blockscout.l2_api}
        ok = failed = skipped = 0
        for side, deps in self.deployed.items():
            api = apis.get(side) or ""
            for cname, dep in deps.items():
                if self.stopping():
                    break
                if not api:
                    skipped += 1
                    continue
                try:
                    res = verify_standard_json(api, dep)
                except Exception as exc:  # noqa: BLE001 — verification is never fatal
                    failed += 1
                    self.state.log(f"verify {side}.{cname} raised: {exc}", "warn")
                    continue
                if res.ok:
                    ok += 1
                    self.state.log(f"verified {side}.{cname} ({res.status})", "ok")
                else:
                    failed += 1
                    self.state.finding(
                        title=f"Blockscout verification failed for {cname} on {side.upper()}",
                        severity="low",
                        detail=f"status={res.status} detail={res.detail}",
                        side=side,
                        contract=cname,
                        address=dep.address,
                    )
        self.state.gauge("verify_ok", ok)
        self.state.gauge("verify_failed", failed)
        self.state.gauge("verify_skipped", skipped)

    # ── stage 3: the call-combination matrix ────────────────────────────────
    def _stage_combinations(self, rnd: int) -> None:
        for label, fn in (
            ("a_l1_same_chain", self._combo_a_same_chain_l1),
            ("b_l1_to_l2", self._combo_b_l1_to_l2),
            ("c_l2_to_l1", self._combo_c_l2_to_l1),
        ):
            if self.stopping():
                return
            try:
                fn(rnd)
            except Exception as exc:  # noqa: BLE001 — one broken combo must not end the run
                self._fail(label)
                self.state.incr("combo_exceptions")
                self.state.finding(
                    title=f"Combination {label} raised an unexpected exception",
                    severity="med",
                    detail=f"round {rnd}: {exc}",
                    combination=label,
                    round=rnd,
                )

    # a. Same-chain L1 baseline: Logger.execute(CounterL1, increment()).
    def _combo_a_same_chain_l1(self, rnd: int) -> None:
        label = "a_l1_same_chain"
        counter = self.deployed["l1"].get("Counter")
        logger = self.deployed["l1"].get("Logger")
        if counter is None or logger is None:
            self.state.incr("a_skipped_missing_deploy")
            return

        client = self.ctx.l1
        # A failed pre-read is NOT a trustworthy baseline — bail as inconclusive
        # rather than asserting against a fabricated empty/zero state.
        pre = self._read_counter(client, counter)
        pre_calls = self._read_calls(client, logger)
        if pre is None or pre_calls is None:
            self._inconclusive(label, f"round {rnd}: L1 pre-read failed; baseline untrustworthy")
            return
        pre_calls_len = len(pre_calls)

        data = self._encode_logger_execute(logger, counter)
        # Baseline is plain EVM: send to the ordinary RPC, not the xchain front.
        h = client.send(to=logger.address, data=data, gas=self.xgas, endpoint=client.cfg.rpc)
        self.state.incr("a_sent")

        receipt = client.wait_receipt(h, timeout=self.deploy_timeout)
        if receipt is None or not receipt.ok:
            self._fail(label)
            reason = client.revert_reason(h) if receipt is not None else None
            self.state.finding(
                title="Same-chain L1 baseline call failed",
                severity="high",
                detail=(
                    f"Logger.execute -> Counter.increment on L1 "
                    f"{'never mined' if receipt is None else f'reverted (reason={reason})'} "
                    "— plain same-chain execution is broken, every cross-chain result below is suspect"
                ),
                combination=label,
                round=rnd,
                tx=h,
            )
            return

        post = self._read_counter(client, counter)
        if post is None:
            self._inconclusive(label, f"round {rnd}: counter() post-read failed after tx {h}")
            return
        if post != pre + 1:
            self._fail(label)
            self.state.finding(
                title="Same-chain L1 increment did not apply",
                severity="high",
                detail=f"counter {pre} -> {post} after a successful Logger.execute (tx {h})",
                combination=label,
                round=rnd,
                tx=h,
            )
            return

        # Bug B: the Logger stores the raw returnData of the inner call.  A
        # status==1 receipt is NOT enough — assert the bytes exist and decode.
        calls = self._read_calls(client, logger)
        if calls is None:
            self._inconclusive(label, f"round {rnd}: getCalls() post-read failed after tx {h}")
            return
        if len(calls) <= pre_calls_len:
            self._fail(label)
            self.state.finding(
                title="Logger recorded no call despite a successful execute",
                severity="high",
                detail=f"getCalls() length stayed at {pre_calls_len} after tx {h}",
                combination=label,
                round=rnd,
                tx=h,
            )
            return

        _cid, _target, _payload, caller, return_data = calls[-1]
        if not self._assert_return_data(label, rnd, h, return_data, expect=post, direction="L1 same-chain"):
            return
        # Sanity on the same-chain identity: the Logger must see our EOA.
        if to_checksum_address(caller) != client.address:
            self.state.finding(
                title="Same-chain msg.sender mismatch at Logger",
                severity="med",
                detail=f"Logger recorded caller {caller}, expected EOA {client.address} (tx {h})",
                combination=label,
                round=rnd,
                tx=h,
            )

        self._pass(label)
        self.state.log(f"[a] L1 baseline ok: counter {pre} -> {post}", "ok")

    # b. L1 -> L2 cross-chain, probed twice to prove identity stability (Bug C).
    def _combo_b_l1_to_l2(self, rnd: int) -> None:
        label = "b_l1_to_l2"
        counter = self.deployed["l2"].get("Counter")
        logger = self.deployed["l2"].get("Logger")
        if counter is None or logger is None:
            self.state.incr("b_skipped_missing_deploy")
            return

        # The L2-side caller for an L1-origin call is the L2 proxy of our L1 EOA
        # keyed with rollupId 0 (mainnet/L1 is the source side).
        try:
            expected = to_checksum_address(
                self.ctx.eez.compute_l2_proxy(self.ctx.l1.address, MAINNET_ROLLUP_ID)
            )
            self.state.gauge("expected_l2_caller", expected)
        except Exception as exc:  # noqa: BLE001
            expected = None
            self.state.log(f"compute_l2_proxy failed: {exc}", "warn")

        observed: list[str] = []
        # Two hops: the Logger route carries the Bug B evidence, the direct route
        # is the plain EOA -> proxy -> Counter shape.  Both must derive the SAME
        # sender, so together they are the Bug C stability sample.
        for attempt, mode in ((1, MODE_LOGGER), (2, MODE_DIRECT)):
            if self.stopping():
                break
            sender = self._hop_l1_to_l2(label, rnd, attempt, mode, counter, logger)
            if sender is None:
                break  # the hop already booked its own fail / inconclusive
            observed.append(sender)

        self._settle_identity(label, rnd, observed, expected, "L2", "compute_l2_proxy")

    # c. L2 -> L1 cross-chain — the known-buggy return path (Bug A).
    def _combo_c_l2_to_l1(self, rnd: int) -> None:
        label = "c_l2_to_l1"
        counter = self.deployed["l1"].get("Counter")
        logger = self.deployed["l1"].get("Logger")
        if counter is None or logger is None:
            self.state.incr("c_skipped_missing_deploy")
            return

        # The L1-side caller must be the L1 proxy of our L2 EOA.
        try:
            expected = to_checksum_address(self.ctx.eez.compute_l1_proxy(self.ctx.l2.address))
            self.state.gauge("expected_l1_caller", expected)
        except Exception as exc:  # noqa: BLE001
            expected = None
            self.state.log(f"compute_l1_proxy failed: {exc}", "warn")

        observed: list[str] = []
        # Symmetric with direction b: two probes per round, so Bug C stability is
        # backed by two independent observations instead of a single sample.
        for attempt, mode in ((1, MODE_LOGGER), (2, MODE_DIRECT)):
            if self.stopping():
                break
            sender = self._hop_l2_to_l1(label, rnd, attempt, mode, counter, logger)
            if sender is None:
                break
            observed.append(sender)

        self._settle_identity(label, rnd, observed, expected, "L1", "compute_l1_proxy")

    # ── one cross-chain hop, per direction ──────────────────────────────────
    def _hop_l1_to_l2(
        self, label: str, rnd: int, attempt: int, mode: str, counter: Deployment, logger: Deployment
    ) -> str | None:
        """One L1->L2 hop.  Returns the derived L2 sender, or None if the hop
        failed or was inconclusive (bookkeeping is done here in either case)."""
        l2 = self.ctx.l2
        probe, reader, target, payload, gauge_key = self._hop_plan(mode, counter, logger, "l1_proxy_of_l2")

        pre_entries = reader(l2, probe)
        if pre_entries is None:
            self._inconclusive(label, f"round {rnd} hop {attempt} ({mode}): L2 pre-read failed")
            return None
        pre_len = len(pre_entries)

        try:
            h, ref = self.ctx.eez.call_l2_from_l1(target, payload, gas=self.xgas)
        except Exception as exc:  # noqa: BLE001
            self._fail(label)
            self.state.finding(
                title="L1->L2 cross-chain call could not be sent",
                severity="med",
                detail=f"round {rnd} hop {attempt} ({mode}): {exc}",
                combination=label,
                round=rnd,
            )
            return None
        self.state.incr("b_sent")
        self.state.gauge(gauge_key, ref.proxy_address)

        l1_receipt = self.ctx.l1.wait_receipt(h, timeout=self.deploy_timeout)
        if l1_receipt is None:
            self._fail(label)
            self.state.finding(
                title="L1->L2 cross-chain tx never mined on L1",
                severity="med",
                detail=f"tx {h} not mined within {self.deploy_timeout:.0f}s (composer stall?)",
                combination=label,
                round=rnd,
                tx=h,
            )
            return None
        if not l1_receipt.ok:
            self._fail(label)
            self.state.finding(
                title="L1->L2 cross-chain tx reverted on L1",
                severity="high",
                detail=f"tx {h} to proxy {ref.proxy_address} reverted "
                       f"(reason={self.ctx.l1.revert_reason(h)})",
                combination=label,
                round=rnd,
                tx=h,
            )
            return None

        entries = self._wait_entries(reader, l2, probe, pre_len)
        if entries is None:
            if reader(l2, probe) is None:
                self._inconclusive(
                    label,
                    f"round {rnd} hop {attempt} ({mode}): L2 reads failing, cannot tell whether tx {h} applied",
                )
                return None
            self._fail(label)
            self.state.finding(
                title="L1->L2 cross-chain call never applied on L2",
                severity="high",
                detail=(
                    f"L1 tx {h} mined in block {l1_receipt.block_number} but the L2 "
                    f"{probe.name} ({probe.address}) recorded nothing new within "
                    f"{self.xchain_timeout:.0f}s — silent drop"
                ),
                combination=label,
                round=rnd,
                tx=h,
            )
            return None

        if mode == MODE_LOGGER:
            _cid, _target, _payload, caller, return_data = entries[-1]
            sender = to_checksum_address(caller)
            self._note_identity(label, sender)
            # Bug B lives HERE: the Logger on the destination chain captured the
            # real bytes the inner increment() returned.  Counter state alone can
            # never show whether EEZ propagated a return value.
            expect = self._read_counter(l2, counter)
            if expect is None:
                self._inconclusive(
                    label,
                    f"round {rnd} hop {attempt}: CounterL2.counter() read failed; "
                    "returnData checked for presence only",
                )
            if not self._assert_return_data(
                label, rnd, h, return_data, expect=expect, direction="L1->L2"
            ):
                return None
            self.state.log(
                f"[b] L1->L2 logger hop ok: sender={sender} returnData decoded to {expect}", "ok"
            )
        else:
            raw_sender, value = entries[-1]
            sender = to_checksum_address(raw_sender)
            self._note_identity(label, sender)
            self.state.log(f"[b] L1->L2 direct hop ok: sender={sender} value={value}", "ok")
        return sender

    def _hop_l2_to_l1(
        self, label: str, rnd: int, attempt: int, mode: str, counter: Deployment, logger: Deployment
    ) -> str | None:
        """One L2->L1 hop, including the Bug A branches (this is the buggy path)."""
        l1 = self.ctx.l1
        probe, reader, target, payload, gauge_key = self._hop_plan(mode, counter, logger, "l2_proxy_of_l1")

        pre_entries = reader(l1, probe)
        if pre_entries is None:
            self._inconclusive(label, f"round {rnd} hop {attempt} ({mode}): L1 pre-read failed")
            return None
        pre_len = len(pre_entries)
        pre = self._read_counter(l1, counter)

        try:
            h, ref = self.ctx.eez.call_l1_from_l2(target, payload, gas=self.xgas)
        except Exception as exc:  # noqa: BLE001
            self._fail(label)
            self.state.finding(
                title="L2->L1 cross-chain call could not be sent",
                severity="med",
                detail=f"round {rnd} hop {attempt} ({mode}): {exc}",
                combination=label,
                round=rnd,
            )
            return None
        self.state.incr("c_sent")
        self.state.gauge(gauge_key, ref.proxy_address)

        # Watch BOTH sides: the L2 receipt and the L1 state can disagree, and
        # that disagreement is itself the interesting finding.
        l2_receipt = self.ctx.l2.wait_receipt(h, timeout=self.xchain_timeout)
        entries = self._wait_entries(reader, l1, probe, pre_len)
        applied = entries is not None
        # Distinguish "definitely did not apply" from "we could not read L1".
        reads_ok = applied or reader(l1, probe) is not None
        post = self._read_counter(l1, counter)
        applied_txt = "DID" if applied else ("did not" if reads_ok else "may or may not have")

        if l2_receipt is None:
            self._fail(label)
            self.state.finding(
                title="L2->L1 cross-chain tx never mined on L2",
                severity="med",
                detail=(
                    f"tx {h} to proxy {ref.proxy_address} not mined within "
                    f"{self.xchain_timeout:.0f}s; L1 counter {pre} -> {post}"
                ),
                combination=label,
                round=rnd,
                tx=h,
                l1_applied=applied if reads_ok else None,
            )
            return None

        if not l2_receipt.ok:
            self._fail(label)
            reason = self.ctx.l2.revert_reason(h)
            pretty = _decode_revert(reason)
            if _is_execution_not_found(reason):
                self.state.incr("bug_a_execution_not_found")
                self.state.finding(
                    title="Bug A: L2->L1 return path ExecutionNotFound",
                    severity="high",
                    detail=(
                        f"L2 tx {h} to proxy {ref.proxy_address} reverted with {pretty}; "
                        f"L1 counter {pre} -> {post} (inner call {applied_txt} run)"
                    ),
                    combination=label,
                    round=rnd,
                    tx=h,
                    revert=pretty,
                    l1_applied=applied if reads_ok else None,
                )
            else:
                self.state.finding(
                    title="L2->L1 cross-chain tx reverted on L2",
                    severity="high",
                    detail=f"tx {h} reverted with {pretty}; L1 counter {pre} -> {post}",
                    combination=label,
                    round=rnd,
                    tx=h,
                    revert=pretty,
                    l1_applied=applied if reads_ok else None,
                )
            # Partial apply: the far side committed while the origin tx failed.
            if applied:
                self.state.incr("bug_a_partial_apply")
                self.state.finding(
                    title="Bug A: L2->L1 partial apply (L1 committed, L2 tx reverted)",
                    severity="high",
                    detail=(
                        f"L1 {probe.name} {probe.address} recorded the call (counter {pre} -> {post}) "
                        f"but the originating L2 tx {h} reverted ({pretty}) — cross-chain atomicity broken"
                    ),
                    combination=label,
                    round=rnd,
                    tx=h,
                    revert=pretty,
                )
            return None

        # Receipt says success — now insist the far side actually moved.
        if not applied:
            if not reads_ok:
                self._inconclusive(
                    label,
                    f"round {rnd} hop {attempt} ({mode}): L1 reads failing, cannot tell whether tx {h} applied",
                )
                return None
            self._fail(label)
            self.state.incr("silent_noop")
            self.state.finding(
                title="L2->L1 cross-chain call succeeded but L1 never applied it",
                severity="high",
                detail=(
                    f"L2 tx {h} status=1 (block {l2_receipt.block_number}) yet the L1 "
                    f"{probe.name} ({probe.address}) recorded nothing new within "
                    f"{self.xchain_timeout:.0f}s — silent no-op (counter still {post})"
                ),
                combination=label,
                round=rnd,
                tx=h,
            )
            return None

        if mode == MODE_LOGGER:
            _cid, _target, _payload, caller, return_data = entries[-1]
            sender = to_checksum_address(caller)
            self._note_identity(label, sender)
            # Bug B: assert on the bytes the L1 Logger captured, not on counter state.
            if post is None:
                self._inconclusive(
                    label,
                    f"round {rnd} hop {attempt}: CounterL1.counter() read failed; "
                    "returnData checked for presence only",
                )
            if not self._assert_return_data(
                label, rnd, h, return_data, expect=post, direction="L2->L1"
            ):
                return None
            self.state.log(
                f"[c] L2->L1 logger hop ok: counter {pre} -> {post}, sender={sender}", "ok"
            )
        else:
            raw_sender, value = entries[-1]
            sender = to_checksum_address(raw_sender)
            self._note_identity(label, sender)
            self.state.log(f"[c] L2->L1 direct hop ok: sender={sender} value={value}", "ok")
        return sender

    def _hop_plan(
        self, mode: str, counter: Deployment, logger: Deployment, gauge_prefix: str
    ) -> tuple[Deployment, Callable[[Any, Deployment], list[tuple] | None], str, str, str]:
        """(probe deployment, state reader, call target, calldata, gauge key)."""
        if mode == MODE_LOGGER:
            return (
                logger,
                self._read_calls,
                logger.address,
                self._encode_logger_execute(logger, counter),
                f"{gauge_prefix}_logger",
            )
        return (
            counter,
            self._read_increments,
            counter.address,
            counter.artifact.encode_call("increment"),
            f"{gauge_prefix}_counter",
        )

    @staticmethod
    def _encode_logger_execute(logger: Deployment, counter: Deployment) -> str:
        """Logger.execute(counter, counter.increment()) — the destination-side wrapper
        that records the inner call's raw returnData on chain."""
        inner = counter.artifact.encode_call("increment")
        return logger.artifact.encode_call(
            "execute",
            [to_checksum_address(counter.address), bytes.fromhex(inner[2:])],
        )

    # ── shared assertions / helpers ─────────────────────────────────────────
    def _settle_identity(
        self,
        label: str,
        rnd: int,
        observed: list[str],
        expected: str | None,
        side: str,
        computer: str,
    ) -> None:
        """Bug C: derived identity must match the computed proxy and be stable.

        Stability needs >= 2 successfully observed senders; with fewer samples the
        round is inconclusive and is NOT counted as a pass.
        """
        if not observed:
            return
        identity_ok = True
        if expected is not None and observed[0] != expected:
            identity_ok = False
            source = self.ctx.l1.address if side == "L2" else self.ctx.l2.address
            self.state.finding(
                title=f"Bug C: {side} derived sender != computed {side} proxy address",
                severity="high",
                detail=(
                    f"destination {side} saw msg.sender={observed[0]} but "
                    f"{computer}({source})={expected}"
                ),
                combination=label,
                round=rnd,
                observed=observed[0],
                expected=expected,
            )

        stable: bool | None = None
        if len(observed) >= 2:
            stable = all(s == observed[0] for s in observed)
            if not stable:
                identity_ok = False
                self.state.finding(
                    title=f"Bug C: {label} derived sender is not stable across txs",
                    severity="high",
                    detail=f"cross-chain calls from one EOA produced senders {observed}",
                    combination=label,
                    round=rnd,
                    observed=list(observed),
                )
        else:
            self._inconclusive(
                label,
                f"round {rnd}: only {len(observed)} derived sender observed; "
                "Bug C stability not asserted",
            )

        # A hop that delivered but under a bogus identity is not a pass; a hop we
        # could not corroborate is not a pass either.
        for _ in observed:
            if not identity_ok:
                self._fail(label)
            elif stable:
                self._pass(label)

    def _assert_return_data(
        self,
        label: str,
        rnd: int,
        tx: str,
        return_data: Any,
        *,
        expect: int | None,
        direction: str,
    ) -> bool:
        """Bug B guard: returnData must be non-empty AND decode to `expect`.

        `return_data` is the blob the destination-side Logger recorded for the
        inner call — the only durable proof that EEZ carried a return value back.
        """
        if not return_data:
            self._fail(label)
            self.state.incr("bug_b_empty_returndata")
            self.state.finding(
                title="Bug B: call succeeded with EMPTY returnData",
                severity="high",
                detail=(
                    f"{direction} call in tx {tx} reported success and the inner increment ran "
                    f"(destination counter now {expect}) but the Logger captured empty returnData"
                ),
                combination=label,
                round=rnd,
                tx=tx,
            )
            return False
        try:
            decoded = int(abi_decode(["uint256"], bytes(return_data))[0])
        except Exception as exc:  # noqa: BLE001
            self._fail(label)
            self.state.finding(
                title="Cross-chain returnData did not decode as uint256",
                severity="high",
                detail=f"{direction} tx {tx}: returnData=0x{bytes(return_data).hex()} ({exc})",
                combination=label,
                round=rnd,
                tx=tx,
            )
            return False
        if expect is None:
            return True  # non-empty + decodable; the state comparison was inconclusive
        if decoded != expect:
            self._fail(label)
            self.state.finding(
                title="Cross-chain returnData disagrees with on-chain state",
                severity="high",
                detail=f"{direction} tx {tx}: returnData decoded to {decoded}, counter() is {expect}",
                combination=label,
                round=rnd,
                tx=tx,
            )
            return False
        return True

    def _note_identity(self, label: str, sender: str) -> None:
        """Record every distinct derived sender seen for a direction (Bug C)."""
        seen = self.identities[label]
        if sender not in seen:
            seen.append(sender)
            self.state.gauge(f"{label}_identities", list(seen))
            if len(seen) > 1:
                self.state.incr("bug_c_identity_drift")
                self.state.finding(
                    title=f"Bug C: derived sender drifted on {label}",
                    severity="high",
                    detail=f"observed multiple cross-chain identities for one EOA: {seen}",
                    combination=label,
                    observed=list(seen),
                )

    def _wait_entries(
        self,
        reader: Callable[[Any, Deployment], list[tuple] | None],
        client,
        dep: Deployment,
        pre_len: int,
    ) -> list[tuple] | None:
        """Poll the far side until `reader` returns more entries than `pre_len`.

        A read that *fails* is not progress and is not emptiness — it just means
        we keep polling; callers re-read once afterwards to tell a real timeout
        apart from a broken RPC.
        """
        deadline = self.now() + self.xchain_timeout
        while not self.stopping():
            entries = reader(client, dep)
            if entries is not None and len(entries) > pre_len:
                return entries
            if self.now() >= deadline:
                return None
            self.sleep(self.poll)
        return None

    def _read_counter(self, client, dep: Deployment) -> int | None:
        try:
            raw = client.eth_call(dep.address, dep.artifact.encode_call("counter"))
            return int(abi_decode(["uint256"], bytes.fromhex(raw[2:]))[0])
        except Exception as exc:  # noqa: BLE001 — a read hiccup is not a bug report
            self.state.incr("read_errors")
            self.state.log(f"counter() read failed on {dep.side}: {exc}", "warn")
            return None

    def _read_increments(self, client, dep: Deployment) -> list[tuple] | None:
        """Counter.getIncrements(); None (never []) when the read itself failed."""
        try:
            raw = client.eth_call(dep.address, dep.artifact.encode_call("getIncrements"))
            return list(abi_decode(INCREMENTS_TYPES, bytes.fromhex(raw[2:]))[0])
        except Exception as exc:  # noqa: BLE001
            self.state.incr("read_errors")
            self.state.log(f"getIncrements() read failed on {dep.side}: {exc}", "warn")
            return None

    def _read_calls(self, client, dep: Deployment) -> list[tuple] | None:
        """Logger.getCalls(); None (never []) when the read itself failed."""
        try:
            raw = client.eth_call(dep.address, dep.artifact.encode_call("getCalls"))
            return list(abi_decode(CALLS_TYPES, bytes.fromhex(raw[2:]))[0])
        except Exception as exc:  # noqa: BLE001
            self.state.incr("read_errors")
            self.state.log(f"getCalls() read failed on {dep.side}: {exc}", "warn")
            return None

    # ── bookkeeping ─────────────────────────────────────────────────────────
    def _pass(self, label: str) -> None:
        self.results[label]["pass"] += 1
        self.state.incr(f"{label}_pass")
        self._publish_results()

    def _fail(self, label: str) -> None:
        self.results[label]["fail"] += 1
        self.state.incr(f"{label}_fail")
        self._publish_results()

    def _inconclusive(self, label: str, reason: str) -> None:
        """Neither pass nor fail: we could not observe enough to judge."""
        self.inconclusive[label] = self.inconclusive.get(label, 0) + 1
        self.state.incr(f"{label}_inconclusive")
        self.state.gauge("inconclusive", dict(self.inconclusive))
        self.state.log(f"[{label}] inconclusive: {reason}", "warn")

    def _publish_results(self) -> None:
        """Compact per-combination pass/fail line for the dashboard."""
        compact = " ".join(
            f"{k.split('_', 1)[0]}={v['pass']}/{v['pass'] + v['fail']}" for k, v in self.results.items()
        )
        self.state.gauge("combos", compact)
        self.state.gauge("combo_results", {k: dict(v) for k, v in self.results.items()})

    def _refresh_gauges(self) -> None:
        """Keep the live counter values visible while the worker idles."""
        for side, client in (("l1", self.ctx.l1), ("l2", self.ctx.l2)):
            dep = self.deployed[side].get("Counter")
            if dep is None:
                continue
            val = self._read_counter(client, dep)
            if val is not None:
                self.state.gauge(f"{side}_counter_value", val)


# ── module-level revert helpers ──────────────────────────────────────────────
def _decode_revert(reason: Any) -> str:
    """Render `revert_reason()` output as something readable in a finding."""
    if reason is None:
        return "no revert data"
    text = str(reason)
    body = text[2:] if text.startswith("0x") else text
    if body[:8].lower() == ERROR_STRING_SELECTOR:
        try:
            return f'Error("{abi_decode(["string"], bytes.fromhex(body[8:]))[0]}")'
        except Exception:  # noqa: BLE001 — fall through to the raw form
            pass
    for sig in _EXEC_NOT_FOUND_SIGS:
        if body[:8].lower() == keccak(sig.encode())[:4].hex():
            return sig
    return text


def _is_execution_not_found(reason: Any) -> bool:
    """True for the Bug A signature, matched by name or by custom-error selector."""
    if reason is None:
        return False
    text = str(reason)
    if "executionnotfound" in text.lower():
        return True
    body = text[2:] if text.startswith("0x") else text
    return body[:8].lower() in EXEC_NOT_FOUND_SELECTORS
