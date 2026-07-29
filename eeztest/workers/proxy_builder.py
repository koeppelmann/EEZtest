"""Proxy-builder worker — does the composer lazily create a missing CrossChainProxy?

Every EEZ deposit lands on an L1 CrossChainProxy: a CREATE2 contract keyed by
(originalAddress, rollupId).  The funder always pre-creates that proxy, which
hides the interesting question this worker asks:

    what happens when a cross-chain tx targets a proxy that does NOT exist yet —
    does the builder create it on the fly, drop the tx, or revert?  And does the
    answer change depending on which L1 endpoint the tx was broadcast to?

Each iteration runs three probes, always against *fresh random* targets so no two
probes can share a proxy address or an already-warm code slot:

  V1 EXPLICIT (control)  create the L1 proxy via `createCrossChainProxy`, wait for
      the receipt, confirm code at the computed address, then deposit dust and
      wait for the L2 credit.  This path is expected to work; a failure here means
      the whole instance is sick, not just the lazy-create path.
  V2 IMPLICIT            compute the L1 proxy of a different fresh target, confirm
      it has NO code, and deposit dust straight at the empty address.
  MEMPOOL MATRIX         the implicit probe repeated once per L1 endpoint
      (cfg.l1.xchain_front, cfg.l1.rpc, cfg.l1.extra_mempools).  Each endpoint
      trial runs from its OWN dedicated, freshly generated sender account — funded
      from the master L1 key and broadcasting its own nonce 0 — against its own
      fresh target.  The trials are therefore *structurally equivalent* (same
      value, same gas, same gas price, empty calldata, a fresh un-created proxy as
      `to`) but deliberately NOT byte-identical, and above all not nonce-coupled:
      with one shared key an early endpoint that never mines its tx blocks every
      later endpoint behind that nonce, so the per-endpoint outcomes would not be
      controlled equivalents.  Independent senders are what make a difference in
      outcome attributable to endpoint routing rather than to a nonce gap.

Every tx that lands on / creates a proxy is sent with >= cfg.eez.crosschain_gas_limit
(>=200k): an under-gassed one makes the composer loop forever, which would poison
the whole measurement.

Findings produced:
  * "explicit" control-path breakage (create never mined / reverted / mined but no
    code at the computed address / deposit with no L2 credit);
  * computed proxy address instability — the same (address, rollupId) resolving to
    two different proxies, or `deposit()` targeting a different proxy than
    `compute_l1_proxy()` (Bug C family: the derived cross-chain identity must be
    stable);
  * implicit deposit accepted by an endpoint and then never mined (silent drop —
    the endpoint is named in the finding);
  * implicit deposit mined on L1 with no L2 credit (value consumed, nothing
    delivered — the worst outcome);
  * implicit deposit auto-created by the builder (INFO: the good outcome, recorded
    so a later regression to "dropped" is visible in the report);
  * per-endpoint divergence in the matrix — structurally-equivalent probes sent
    from independent senders producing different outcomes, i.e. cross-chain
    admission depends on mempool routing.

Note: this worker is L1-origin only — it spends from the master L1 key and never
needs the funder's `l2_accounts` pool, so it does not consume it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..rpc import ChainClient, RpcError
from .base import SubAccount, Worker


class ProxyBuilderWorker(Worker):
    name = "proxy_builder"
    description = "Probe lazy CrossChainProxy creation (explicit vs implicit) across L1 mempools"

    # ── lifecycle ───────────────────────────────────────────────────────────
    def setup(self) -> None:
        # Dust: big enough to be an unambiguous balance delta, small enough that
        # a few hundred lost probes cost nothing.
        self.dust = int(self.wcfg.get("dust_wei", 1_000_000_000))
        # Never go under the composer's proxy-gas floor (an under-gassed tx that
        # touches a proxy stalls the composer loop — a known failure mode).
        self.gas = max(int(self.cfg.eez.crosschain_gas_limit), 200_000)
        self.mine_timeout = float(self.wcfg.get("mine_timeout_seconds", 90))
        self.credit_timeout = float(self.wcfg.get("credit_timeout_seconds", 90))
        # The matrix runs N probes per iteration, so give each a tighter budget.
        self.matrix_mine_timeout = float(self.wcfg.get("matrix_mine_timeout_seconds", 60))
        self.matrix_credit_timeout = float(self.wcfg.get("matrix_credit_timeout_seconds", 60))
        # Each matrix trial has its own throwaway sender, which must be funded
        # before it can probe; that funding tx has to mine first.
        self.fund_timeout = float(self.wcfg.get("matrix_fund_timeout_seconds", 60))
        self.clear_gaps = bool(self.wcfg.get("clear_nonce_gaps", True))

        # front first, then the plain RPC, then any extra mempools (deduped).
        self.endpoints: list[str] = [ep for ep in self.cfg.l1.all_send_endpoints if ep]
        # Cumulative per-endpoint verdict counts, published as a gauge.
        self.ep_stats: dict[str, dict[str, int]] = {ep: self._new_stats() for ep in self.endpoints}
        # Dedupe key set: a looping worker must not emit the same finding forever.
        self._reported: set[str] = set()

        self.state.gauge("dust_wei", self.dust)
        self.state.gauge("crosschain_gas", self.gas)
        self.state.gauge("l1_endpoints", list(self.endpoints))
        self.state.gauge("endpoint_outcomes", self._ep_gauge())
        if len(self.endpoints) < 2:
            self.state.log(
                "only one L1 endpoint configured — mempool matrix degenerates to a single probe",
                "warn",
            )

    def interval(self) -> float:
        return float(self.wcfg.get("interval_seconds", 15))

    def step(self) -> None:
        # One iteration can be long (three probe families, each with waits), so
        # bail out between phases whenever the run is stopping.
        if not self._affordable():
            return
        self._variant_explicit()
        if self.stopping():
            return
        self._variant_implicit()
        if self.stopping():
            return
        self._mempool_matrix()
        self.state.incr("iterations")

    # ── V1: explicit create, then deposit (control) ─────────────────────────
    def _variant_explicit(self) -> None:
        eez, l1 = self.ctx.eez, self.ctx.l1
        target = SubAccount.random()
        self.state.incr("explicit_probes")

        proxy = self._compute_proxy(target.address, tag="explicit")
        if proxy is None:
            return
        if self._has_code(l1, proxy):
            # A freshly random address whose proxy already has code means the
            # computation is not keyed by the address the way we think it is.
            self.state.incr("explicit_pre_existing_proxy")
            self._finding_once(
                f"pre_existing:{proxy}",
                title="Proxy for a brand-new random address already has code",
                severity="med",
                detail=(
                    f"computed L1 proxy {proxy} for fresh target {target.address} already "
                    "has code before any create — proxy address may not be keyed by the "
                    "original address (or a CREATE2 collision)"
                ),
                proxy=proxy,
                target=target.address,
            )
            return

        # 1. Explicit creation.
        try:
            create_h = eez.create_l1_proxy(target.address)
        except Exception as exc:  # noqa: BLE001 — never kill the loop on one probe
            self.state.incr("explicit_create_send_errors")
            self.state.log(f"[explicit] create_l1_proxy send failed: {exc}", "warn")
            return
        self.state.incr("explicit_creates_sent")

        rcpt = self._wait_receipt(l1, create_h, self.mine_timeout)
        if rcpt is None:
            self.state.incr("explicit_create_never_mined")
            self._finding_once(
                "explicit_create_never_mined",
                title="Explicit createCrossChainProxy never mined",
                severity="high",
                detail=(
                    f"createCrossChainProxy({target.address}) tx {create_h} not mined within "
                    f"{self.mine_timeout:.0f}s — the control path is broken (composer stall?)"
                ),
                tx=create_h,
                target=target.address,
            )
            return
        if not rcpt.ok:
            self.state.incr("explicit_create_reverted")
            self._finding_once(
                "explicit_create_reverted",
                title="Explicit createCrossChainProxy reverted",
                severity="high",
                detail=(
                    f"createCrossChainProxy({target.address}) tx {create_h} reverted "
                    f"(reason={l1.revert_reason(create_h)})"
                ),
                tx=create_h,
                target=target.address,
            )
            return

        # 2. Bug-C style stability check: the same query must resolve identically
        #    before and after the create.
        proxy_after = self._compute_proxy(target.address, tag="explicit-recheck")
        if proxy_after is not None and proxy_after != proxy:
            self.state.incr("proxy_address_unstable")
            self.state.finding(
                title="Computed proxy address is not stable",
                severity="critical",
                detail=(
                    f"computeCrossChainProxyAddress({target.address}) returned {proxy} before "
                    f"the create and {proxy_after} after it — cross-chain identity is not "
                    "deterministic (Bug C family)"
                ),
                target=target.address,
                before=proxy,
                after=proxy_after,
            )

        # 3. Code must actually be there.  Allow a couple of polls: some RPCs
        #    serve eth_getCode from a slightly stale state right after the block.
        if not self._await_code(l1, proxy, timeout=15):
            self.state.incr("explicit_create_no_code")
            self._finding_once(
                "explicit_create_no_code",
                title="createCrossChainProxy mined but no code at the computed address",
                severity="high",
                detail=(
                    f"tx {create_h} succeeded (block {rcpt.block_number}) but {proxy} still has "
                    f"no code — the registry deployed somewhere other than the computed address"
                ),
                tx=create_h,
                proxy=proxy,
                target=target.address,
            )
            return

        # 4. Deposit dust into the now-existing proxy.
        try:
            dep_h, ref = eez.deposit(target.address, self.dust, gas=self.gas)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("explicit_deposit_send_errors")
            self.state.log(f"[explicit] deposit send failed: {exc}", "warn")
            return
        if ref.proxy_address != proxy:
            # deposit() recomputes the ref; it must agree with what we created.
            self.state.incr("proxy_address_unstable")
            self.state.finding(
                title="deposit() targeted a different proxy than the one created",
                severity="critical",
                detail=(
                    f"created/verified {proxy} but deposit() routed value to "
                    f"{ref.proxy_address} for {target.address} (Bug C family)"
                ),
                target=target.address,
                created=proxy,
                deposited_to=ref.proxy_address,
            )
        self.state.incr("explicit_deposits_sent")

        dep_rcpt = self._wait_receipt(l1, dep_h, self.mine_timeout)
        if dep_rcpt is None:
            self.state.incr("explicit_deposit_never_mined")
            self._finding_once(
                "explicit_deposit_never_mined",
                title="Deposit into an existing proxy never mined",
                severity="high",
                detail=(
                    f"deposit {dep_h} to existing proxy {proxy} not mined within "
                    f"{self.mine_timeout:.0f}s — control path stalled"
                ),
                tx=dep_h,
                proxy=proxy,
            )
            return
        if not dep_rcpt.ok:
            self.state.incr("explicit_deposit_reverted")
            self._finding_once(
                "explicit_deposit_reverted",
                title="Deposit into an existing proxy reverted",
                severity="high",
                detail=(
                    f"deposit {dep_h} to existing proxy {proxy} reverted "
                    f"(reason={l1.revert_reason(dep_h)})"
                ),
                tx=dep_h,
                proxy=proxy,
            )
            return

        # 5. The only outcome that counts: the L2 credit.
        if self._wait_credit(target.address, 0, self.credit_timeout):
            self.state.incr("explicit_ok")
            self.state.gauge("last_explicit_target", target.address)
            self.state.log(f"[explicit] {target.address[:10]} credited on L2 via {proxy[:10]}")
        else:
            self.state.incr("explicit_deposit_no_l2_credit")
            self._finding_once(
                "explicit_no_credit",
                title="Deposit into an existing proxy mined but never credited L2",
                severity="high",
                detail=(
                    f"L1 deposit {dep_h} into pre-created proxy {proxy} mined (block "
                    f"{dep_rcpt.block_number}) but {target.address} was never credited on L2 "
                    f"within {self.credit_timeout:.0f}s — lost deposit on the control path"
                ),
                tx=dep_h,
                proxy=proxy,
                target=target.address,
            )

    # ── V2: implicit — deposit at a proxy that does not exist ───────────────
    def _variant_implicit(self) -> None:
        eez, l1 = self.ctx.eez, self.ctx.l1
        target = SubAccount.random()
        self.state.incr("implicit_probes")

        proxy = self._compute_proxy(target.address, tag="implicit")
        if proxy is None:
            return
        if self._has_code(l1, proxy):
            # Nothing to learn: the address is not actually empty.
            self.state.incr("implicit_skipped_existing")
            self.state.log(f"[implicit] {proxy[:10]} already has code, skipping probe", "warn")
            return

        # Deposit straight at the empty address.  eez.deposit defaults to the
        # inbound cross-chain front, which is what a real user would use.
        try:
            dep_h, ref = eez.deposit(target.address, self.dust, gas=self.gas)
        except RpcError as exc:
            # A clean rejection at the RPC layer is a legitimate policy, but the
            # user-visible message matters, so record it.
            self.state.incr("implicit_rejected_by_rpc")
            self._finding_once(
                "implicit_rejected_by_rpc",
                title="Front rejects a deposit to a not-yet-created proxy",
                severity="low",
                detail=(
                    f"eth_sendRawTransaction to {self.cfg.l1.xchain_front} refused a deposit at "
                    f"the un-created proxy {proxy}: {exc} — proxies must be pre-created"
                ),
                proxy=proxy,
                endpoint=self.cfg.l1.xchain_front,
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.state.incr("implicit_send_errors")
            self.state.log(f"[implicit] deposit send failed: {exc}", "warn")
            return
        self.state.incr("implicit_sent")
        self.state.gauge("last_implicit_proxy", ref.proxy_address)

        rcpt = self._wait_receipt(l1, dep_h, self.mine_timeout)
        if rcpt is None:
            # Accepted by the endpoint, then never mined: nothing executes and the
            # sender's nonce is wedged behind a tx nobody will ever include.
            self.state.incr("implicit_dropped")
            self._finding_once(
                "implicit_dropped",
                title="Deposit to a non-existent proxy accepted then never mined",
                severity="med",
                detail=(
                    f"{self.cfg.l1.xchain_front} accepted deposit {dep_h} at the un-created "
                    f"proxy {proxy} but it was not mined within {self.mine_timeout:.0f}s — the "
                    "builder neither creates the proxy lazily nor rejects the tx up front "
                    "(silent drop; wedges the sender's nonce)"
                ),
                tx=dep_h,
                proxy=proxy,
                target=target.address,
                endpoint=self.cfg.l1.xchain_front,
            )
            self._clear_nonce_gap(dep_h)
            return

        if not rcpt.ok:
            # Failing loudly is the well-behaved variant of "no lazy create".
            reason = l1.revert_reason(dep_h)
            self.state.incr("implicit_reverted")
            self.state.gauge("last_implicit_revert_reason", str(reason))
            self._finding_once(
                "implicit_reverted",
                title="Deposit to a non-existent proxy reverts (no lazy creation)",
                severity="low",
                detail=(
                    f"deposit {dep_h} at the un-created proxy {proxy} reverted in block "
                    f"{rcpt.block_number} (reason={reason}) — the builder does not create "
                    "proxies on demand; callers must call createCrossChainProxy first"
                ),
                tx=dep_h,
                proxy=proxy,
                revert_reason=str(reason),
            )
            return

        # Mined successfully — did the builder actually create the proxy and
        # deliver the value?  status==1 alone proves nothing (Bug B lesson: always
        # assert the observable effect, never just the status).
        created = self._await_code(l1, proxy, timeout=15)
        credited = self._wait_credit(target.address, 0, self.credit_timeout)
        if credited:
            self.state.incr("implicit_auto_created")
            self.state.gauge("last_implicit_target", target.address)
            self._finding_once(
                "implicit_auto_created",
                title="Builder lazily creates a missing CrossChainProxy",
                severity="info",
                detail=(
                    f"deposit {dep_h} at the un-created proxy {proxy} mined (block "
                    f"{rcpt.block_number}), proxy code present={created}, and "
                    f"{target.address} was credited on L2 — implicit creation works"
                ),
                tx=dep_h,
                proxy=proxy,
                target=target.address,
            )
        else:
            self.state.incr("implicit_no_l2_credit")
            self._finding_once(
                "implicit_no_credit",
                title="Deposit to a non-existent proxy mined on L1 but never credited L2",
                severity="high",
                detail=(
                    f"L1 deposit {dep_h} at the un-created proxy {proxy} succeeded (block "
                    f"{rcpt.block_number}, proxy code after={created}) but {target.address} "
                    f"was never credited on L2 within {self.credit_timeout:.0f}s — value left "
                    "L1 with nothing delivered"
                ),
                tx=dep_h,
                proxy=proxy,
                target=target.address,
            )

    # ── mempool matrix: one independent implicit probe per L1 endpoint ──────
    def _mempool_matrix(self) -> None:
        """Run one implicit deposit per endpoint, each from its OWN dedicated sender.

        The trials are structurally equivalent, not byte-identical: every endpoint
        gets a fresh random target (so no two probes race for the same proxy
        address) *and* a fresh random sender account that broadcasts its own nonce
        0.  Independence is the point — with a single shared sending key, a tx one
        endpoint accepts but never mines sits at a low nonce and stalls every later
        endpoint's probe, so the per-endpoint outcomes are not controlled
        equivalents and "endpoint routing differs" cannot be concluded.

        Sequence: prepare trials → fund each dedicated sender from the master key
        (via the plain RPC, never the front under test) → broadcast each probe at
        its endpoint → await mining + credit in parallel, so a slow endpoint does
        not serialize the whole matrix.
        """
        try:
            gas_price = self.ctx.l1.gas_price()
        except Exception as exc:  # noqa: BLE001
            self.state.incr("matrix_gas_price_errors")
            self.state.log(f"[matrix] gas price read failed: {exc}", "warn")
            return

        probes: list[dict[str, Any]] = []
        for ep in self.endpoints:
            if self.stopping():
                break
            probe = self._prepare_probe(ep, gas_price)
            if probe is not None:
                probes.append(probe)
        if not probes:
            return

        # 1. Fund the dedicated senders (all of them, then await together).
        for probe in probes:
            if self.stopping():
                break
            self._fund_sender(probe)
        self._await_funding(probes)

        # 2. Broadcast: every probe leaves from its own account, so the order the
        #    endpoints are hit in cannot couple their outcomes.
        for probe in probes:
            if self.stopping():
                break
            if probe["funded"]:
                self._broadcast_probe(probe)

        # Await mining + credit concurrently; one failing probe must not abort
        # the others, hence the per-future try/except.
        with ThreadPoolExecutor(max_workers=min(6, len(probes))) as pool:
            futs = {pool.submit(self._await_probe, p): p for p in probes if p["accepted"]}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    probe = futs[fut]
                    probe["verdict"] = "await_error"
                    probe["error"] = str(exc)
                    self.state.incr("matrix_await_errors")
                    self.state.log(f"[matrix] await failed for {probe['endpoint']}: {exc}", "warn")

        self._score_matrix(probes)

    def _prepare_probe(self, endpoint: str, gas_price: int) -> dict[str, Any] | None:
        """Set up one endpoint trial: fresh target, fresh proxy, dedicated sender.

        Nothing is broadcast here.  The sender is a brand-new key used for exactly
        one transaction, which is what decouples this endpoint's outcome from every
        other endpoint's.
        """
        l1 = self.ctx.l1
        target = SubAccount.random()
        proxy = self._compute_proxy(target.address, tag=f"matrix:{endpoint}")
        if proxy is None:
            return None
        if self._has_code(l1, proxy):
            self.state.incr("matrix_skipped_existing")
            return None

        sender = SubAccount.random()
        # Brand-new key: drop any (impossible but cheap to exclude) stale cursor in
        # the shared allocator so its first and only tx really goes out at nonce 0.
        self.ctx.resync_sub(l1, sender.address)

        probe: dict[str, Any] = {
            "endpoint": endpoint,
            "sender": sender,
            "sender_address": sender.address,
            "target": target.address,
            "proxy": proxy,
            "gas_price": gas_price,
            "nonce": 0,  # dedicated sender's first and only transaction
            "tx": None,
            "fund_tx": None,
            "funded": False,
            "accepted": False,
            "mined": None,
            "status": None,
            "credited": None,
            "verdict": "unknown",
            "error": None,
        }
        self._stats(endpoint)["probes"] += 1
        return probe

    def _fund_sender(self, probe: dict[str, Any]) -> None:
        """Seed one dedicated sender from the master L1 key.

        Deliberately broadcast on the plain RPC: the endpoint under test must not
        get a say in whether its own trial can even be funded.
        """
        sender: SubAccount = probe["sender"]
        # Exactly one probe tx (value + gas) with headroom for a gas-price move.
        amount = self.dust + self.gas * probe["gas_price"] * 3
        probe["fund_amount"] = amount
        try:
            probe["fund_tx"] = self.ctx.l1.send(
                to=sender.address,
                value=amount,
                gas=25_000,
                endpoint=self.cfg.l1.rpc,
            )
        except Exception as exc:  # noqa: BLE001 — a funding failure is our problem, not the endpoint's
            self.state.incr("matrix_sender_fund_errors")
            probe["verdict"] = "inconclusive_unfunded"
            probe["error"] = f"funding: {exc}"
            self._stats(probe["endpoint"])["inconclusive"] += 1
            self.state.log(f"[matrix] could not fund sender for {probe['endpoint']}: {exc}", "warn")

    def _await_funding(self, probes: list[dict[str, Any]]) -> None:
        """Wait for every funding tx to mine; unfunded trials are inconclusive.

        A trial whose sender never got its balance says nothing about the endpoint,
        so it must never be scored as a drop/revert — it is marked inconclusive.
        """
        pending = [p for p in probes if p.get("fund_tx")]
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=min(6, len(pending))) as pool:
            futs = {
                pool.submit(self._wait_receipt, self.ctx.l1, p["fund_tx"], self.fund_timeout): p
                for p in pending
            }
            for fut in as_completed(futs):
                probe = futs[fut]
                try:
                    rcpt = fut.result()
                except Exception as exc:  # noqa: BLE001
                    rcpt = None
                    probe["error"] = f"funding: {exc}"
                if rcpt is not None and rcpt.ok:
                    probe["funded"] = True
                    continue
                self.state.incr("matrix_sender_fund_errors")
                probe["verdict"] = "inconclusive_unfunded"
                self._stats(probe["endpoint"])["inconclusive"] += 1
                self.state.log(
                    f"[matrix] sender {probe['sender_address'][:10]} for {probe['endpoint']} "
                    f"not funded (tx {str(probe['fund_tx'])[:12]}) — trial inconclusive",
                    "warn",
                )

    def _broadcast_probe(self, probe: dict[str, Any]) -> None:
        """Send this trial's dust deposit from its own sender at its own endpoint."""
        stats = self._stats(probe["endpoint"])
        try:
            probe["tx"] = self.ctx.send_sub(
                self.ctx.l1,
                probe["sender"].private_key,
                to=probe["proxy"],
                value=self.dust,
                gas=self.gas,
                gas_price=probe["gas_price"],
                endpoint=probe["endpoint"],
            )
            probe["accepted"] = True
            stats["accepted"] += 1
        except RpcError as exc:
            probe["verdict"] = "rejected_by_rpc"
            probe["error"] = f"rpc: {exc}"
            stats["rejected"] += 1
        except Exception as exc:  # noqa: BLE001 — sign/transport failures are not verdicts
            self.state.incr("matrix_sign_errors")
            probe["verdict"] = "transport_error"
            probe["error"] = f"transport: {exc}"

    def _await_probe(self, probe: dict[str, Any]) -> None:
        """Fill in mined / status / credited for one accepted probe."""
        l1 = self.ctx.l1
        rcpt = self._wait_receipt(l1, probe["tx"], self.matrix_mine_timeout)
        if rcpt is None:
            probe["mined"] = False
            return
        probe["mined"] = True
        probe["status"] = rcpt.status
        probe["block"] = rcpt.block_number
        if not rcpt.ok:
            probe["credited"] = False
            probe["revert_reason"] = str(l1.revert_reason(probe["tx"]))
            return
        probe["credited"] = self._wait_credit(probe["target"], 0, self.matrix_credit_timeout)
        probe["proxy_code_after"] = bool(self._has_code(l1, probe["proxy"]))

    def _score_matrix(self, probes: list[dict[str, Any]]) -> None:
        """Turn raw probe results into verdicts, findings and gauges.

        Every probe came from its own dedicated sender at that sender's nonce 0, so
        there is no shared nonce sequence in which one endpoint's unmined tx could
        stall another's.  That independence is precisely what makes "accepted then
        never mined" a real per-endpoint verdict here (under a shared key it would
        have been ambiguous and had to be called inconclusive).  The only
        inconclusive case left is a trial whose sender we failed to fund, which is
        a harness artifact and is scored as such by `_fund_sender`/`_await_funding`.
        """
        for probe in probes:
            ep = probe["endpoint"]
            stats = self._stats(ep)
            if not probe["accepted"]:
                pass  # verdict already set by _prepare/_fund/_broadcast
            elif probe.get("mined") and probe.get("status") == 1 and probe.get("credited"):
                probe["verdict"] = "auto_created"
                stats["mined"] += 1
                stats["credited"] += 1
            elif probe.get("mined") and probe.get("status") == 1:
                probe["verdict"] = "mined_no_credit"
                stats["mined"] += 1
                stats["no_credit"] += 1
                self._finding_once(
                    f"matrix_no_credit:{ep}",
                    title="Endpoint mines a deposit to a missing proxy but no L2 credit follows",
                    severity="high",
                    detail=(
                        f"probe via {ep}: deposit {probe['tx']} at the un-created proxy "
                        f"{probe['proxy']} succeeded on L1 but {probe['target']} was never "
                        f"credited on L2 within {self.matrix_credit_timeout:.0f}s"
                    ),
                    endpoint=ep,
                    tx=probe["tx"],
                    proxy=probe["proxy"],
                )
            elif probe.get("mined"):
                probe["verdict"] = "reverted"
                stats["mined"] += 1
                stats["reverted"] += 1
            else:
                probe["verdict"] = "accepted_never_mined"
                stats["dropped"] += 1
                self._finding_once(
                    f"matrix_silent_drop:{ep}",
                    title=f"Endpoint accepts a cross-chain deposit then silently drops it ({ep})",
                    severity="high",
                    detail=(
                        f"{ep} returned a tx hash for {probe['tx']} (dust deposit at the "
                        f"un-created proxy {probe['proxy']}, gas {self.gas}) but it was never "
                        f"mined within {self.matrix_mine_timeout:.0f}s — accepted-then-dropped. "
                        f"The sender {probe['sender_address']} is a dedicated fresh account "
                        f"whose nonce 0 this was, so nothing of ours could have been blocking "
                        "it; the tx was simply never included"
                    ),
                    endpoint=ep,
                    tx=probe["tx"],
                    proxy=probe["proxy"],
                    sender=probe["sender_address"],
                    nonce=probe["nonce"],
                )

        # Divergence between endpoints is the headline of this matrix — but only
        # conclusive because the senders were independent.  Verdicts that describe
        # our own harness (transport/await failures, an unfunded sender) are
        # excluded: they are not endpoint behaviour.
        verdicts = {p["endpoint"]: p["verdict"] for p in probes}
        senders = {p["endpoint"]: p["sender_address"] for p in probes}
        distinct = {
            v
            for v in verdicts.values()
            if v not in ("transport_error", "await_error", "inconclusive_unfunded", "unknown")
        }
        if len(distinct) > 1:
            self._finding_once(
                "matrix_divergence:" + "|".join(sorted(f"{k}={v}" for k, v in verdicts.items())),
                title="Structurally-equivalent implicit deposits behave differently per L1 endpoint",
                severity="med",
                detail=(
                    "structurally-equivalent, independent-sender probes (dust deposit at a "
                    "fresh un-created proxy; same value, gas and gas price, empty calldata, "
                    "each from its own dedicated fresh account at nonce 0 — equivalent in "
                    "structure, not byte-identical) produced different outcomes depending on "
                    f"where they were broadcast: {verdicts} — since no two probes shared a "
                    "nonce sequence, none could have stalled another, so cross-chain admission "
                    "depends on mempool routing"
                ),
                verdicts=verdicts,
                senders=senders,
            )

        self.state.gauge("last_matrix", verdicts)
        self.state.gauge("endpoint_outcomes", self._ep_gauge())
        self.state.incr("matrix_rounds")
        self.state.log(f"[matrix] {verdicts}")

        # No nonce-gap cleanup here on purpose: each stuck matrix tx belongs to a
        # throwaway sender that is never used again, so nothing of ours is wedged.
        # (The V2 implicit path still spends the shared master key and does clear.)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _compute_proxy(self, address: str, *, tag: str) -> str | None:
        """compute_l1_proxy that reports instead of raising."""
        try:
            return self.ctx.eez.compute_l1_proxy(address)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("compute_proxy_errors")
            self.state.log(f"[{tag}] compute_l1_proxy({address[:10]}) failed: {exc}", "warn")
            return None

    def _has_code(self, client: ChainClient, address: str) -> bool:
        """has_code that never raises; an unreadable endpoint counts as 'no code'."""
        try:
            return client.has_code(address)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("get_code_errors")
            self.state.log(f"eth_getCode({address[:10]}) failed: {exc}", "warn")
            return False

    def _await_code(self, client: ChainClient, address: str, timeout: float) -> bool:
        """Poll for code — some RPCs answer from a slightly stale state post-block."""
        deadline = self.now() + timeout
        while not self.stopping():
            if self._has_code(client, address):
                return True
            if self.now() >= deadline:
                return False
            self.sleep(3)
        return False

    def _wait_receipt(self, client: ChainClient, tx_hash: str, timeout: float):
        """Interruptible wait_receipt (the client's own version ignores the stop event)."""
        deadline = self.now() + timeout
        while not self.stopping():
            try:
                rcpt = client.get_receipt(tx_hash)
            except Exception as exc:  # noqa: BLE001 — a flaky read is not a verdict
                self.state.incr("receipt_read_errors")
                self.state.log(f"receipt read {tx_hash[:12]} failed: {exc}", "warn")
                rcpt = None
            if rcpt is not None:
                return rcpt
            if self.now() >= deadline:
                return None
            self.sleep(3)
        return None

    def _wait_credit(self, address: str, pre: int, timeout: float) -> bool:
        """Wait for the L2 balance of `address` to rise above `pre`."""
        deadline = self.now() + timeout
        while not self.stopping():
            try:
                if self.ctx.l2.balance(address) > pre:
                    return True
            except Exception as exc:  # noqa: BLE001
                self.state.incr("l2_balance_errors")
                self.state.log(f"l2 balance({address[:10]}) failed: {exc}", "warn")
            if self.now() >= deadline:
                return False
            self.sleep(3)
        return False

    def _clear_nonce_gap(self, stuck_tx: str, nonce: int | None = None) -> None:
        """Best-effort unwedge: replace a never-mined tx with a 0-value self-send.

        A tx the front accepted but never mined blocks every later nonce from the
        shared L1 key — including other workers'.  Re-sending the same nonce via
        the plain RPC at a higher gas price usually evicts it.  Purely best
        effort: if the original does eventually get composed, one of the two
        simply fails, which costs nothing but gas.
        """
        if not self.clear_gaps:
            return
        l1 = self.ctx.l1
        try:
            if nonce is None:
                tx = l1.call("eth_getTransactionByHash", [stuck_tx])
                if not tx or tx.get("nonce") is None:
                    return
                nonce = int(tx["nonce"], 16)
            l1.send(
                to=l1.address,
                value=0,
                gas=25_000,
                gas_price=int(l1.gas_price() * 2),
                nonce=nonce,
                endpoint=self.cfg.l1.rpc,  # deliberately NOT the cross-chain front
            )
            self.state.incr("nonce_gaps_cleared")
            self.state.log(f"replaced stuck nonce {nonce} (tx {stuck_tx[:12]}) via plain RPC", "warn")
        except Exception as exc:  # noqa: BLE001
            self.state.incr("nonce_gap_clear_errors")
            self.state.log(f"could not clear stuck nonce for {stuck_tx[:12]}: {exc}", "warn")

    def _affordable(self) -> bool:
        """Skip the iteration when the L1 key can no longer pay for the probes."""
        try:
            bal = self.ctx.l1.balance(self.ctx.l1.address)
            gp = self.ctx.l1.gas_price()
        except Exception as exc:  # noqa: BLE001
            self.state.incr("balance_read_errors")
            self.state.log(f"L1 balance read failed: {exc}", "warn")
            return True  # do not stall the probe on a single flaky read
        # Master key pays for: explicit create + explicit deposit + implicit
        # deposit, plus one funding tx per endpoint (the matrix probes themselves
        # are paid by their dedicated senders out of that funding, with headroom).
        n_eps = len(self.endpoints)
        txs = 3 + n_eps
        need = 3 * (self.dust + self.gas * gp) + n_eps * (
            self.dust + self.gas * gp * 3 + 25_000 * gp
        )
        self.state.gauge("l1_balance_wei", bal)
        if bal < need:
            self.state.incr("skipped_low_balance")
            self._finding_once(
                "low_l1_balance",
                title="proxy_builder L1 balance too low to keep probing",
                severity="low",
                detail=f"have {bal} wei, one iteration needs ~{need} wei ({txs} txs)",
            )
            return False
        return True

    @staticmethod
    def _new_stats() -> dict[str, int]:
        return {
            "probes": 0, "accepted": 0, "rejected": 0, "mined": 0, "credited": 0,
            "dropped": 0, "reverted": 0, "no_credit": 0, "inconclusive": 0,
        }

    def _stats(self, endpoint: str) -> dict[str, int]:
        """Per-endpoint counter bucket, created on first use."""
        return self.ep_stats.setdefault(endpoint, self._new_stats())

    def _ep_gauge(self) -> dict[str, dict[str, int]]:
        return {ep: dict(stats) for ep, stats in self.ep_stats.items()}

    def _finding_once(self, key: str, *, title: str, severity: str, detail: str, **extra: Any) -> None:
        """Record a finding the first time only; later repeats just bump a counter.

        This worker loops forever, so an un-deduped finding would flood the report
        with hundreds of copies of the same fact.
        """
        if key in self._reported:
            self.state.incr(f"repeat:{key}")
            self.state.log(f"(repeat) {title}", "warn")
            return
        self._reported.add(key)
        self.state.finding(title=title, severity=severity, detail=detail, **extra)
