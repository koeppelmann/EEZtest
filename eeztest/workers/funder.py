"""Funder worker — seeds L2 sub-accounts by depositing to their L1 proxies.

The canonical EEZ funding path: to give an L2 address a balance, send value to
its L1 CrossChainProxy.  This worker derives a pool of deterministic sub-accounts,
ensures each one's L1 proxy exists (an under-gassed create is the classic composer
stall, so we use the configured >=200k gas), deposits to each, and waits for the
L2 credit.  Funded accounts are published on the shared blackboard under
`l2_accounts` for the fuzzer and other workers to consume.

Findings:
  * a deposit whose L1 tx mines but never credits L2 within the window
    (the silent-stall / lost-deposit failure mode);
  * a create/deposit that the composer never mines at all.
"""
from __future__ import annotations

from .base import SubAccount, Worker

FUND_SEED = "eeztest-l2-pool-v1"


class FunderWorker(Worker):
    name = "funder"
    description = "Fund L2 sub-accounts by depositing to their L1 proxies"

    def setup(self) -> None:
        self.count = int(self.wcfg.get("l2_accounts", 25))
        self.amount = int(self.wcfg.get("fund_amount_wei", 300_000_000_000_000))
        self.accounts = [SubAccount.derive(FUND_SEED, i) for i in range(self.count)]
        self.funded: list[SubAccount] = []
        self.state.gauge("target_accounts", self.count)
        self.state.gauge("amount_wei", self.amount)
        # Publish the (as-yet-unfunded) pool immediately so peers can see it grow.
        self.ctx.put_shared("l2_accounts", self.funded)

        # Sanity: does the funding key hold enough L1 balance?  This is a soft
        # probe — an unreachable L1 here must not crash the worker (it degrades
        # into per-account deposit attempts that record their own findings).
        try:
            bal = self.ctx.l1.balance(self.ctx.l1.address)
            need = self.count * (
                self.amount + self.cfg.eez.crosschain_gas_limit * self.ctx.l1.gas_price()
            )
            self.state.gauge("funder_l1_balance_wei", bal)
            self.state.gauge("estimated_need_wei", need)
            if bal < need:
                self.state.finding(
                    title="Funder L1 balance below plan",
                    severity="low",
                    detail=f"have {bal} wei, plan needs ~{need} wei; will fund as far as balance allows",
                )
        except Exception as exc:  # noqa: BLE001
            self.state.log(f"L1 balance probe failed: {exc}", "warn")

    def run(self) -> None:
        if not self._setup_with_retry():
            return
        for i, acct in enumerate(self.accounts):
            if self.stopping():
                break
            try:
                self._fund_one(i, acct)
            except Exception as exc:  # noqa: BLE001 — one account's failure must not end the pass
                self.state.incr("fund_errors")
                self.state.log(f"fund {acct.address[:10]} failed: {exc}", "warn")
        self.state.gauge("funded_count", len(self.funded))
        self.state.log(f"funding pass complete: {len(self.funded)}/{self.count} funded")
        # Keep the worker alive so its state stays visible; top up any that drained.
        while not self.stopping():
            self.sleep(30)
            self._maintain()

    def _fund_one(self, index: int, acct: SubAccount) -> None:
        l2 = self.ctx.l2
        eez = self.ctx.eez
        pre = l2.balance(acct.address)
        if pre >= self.amount // 2:
            self._mark_funded(acct)
            self.state.incr("already_funded")
            return

        # Ensure the L1 proxy exists first (avoids the under-gassed-create stall).
        try:
            ref = eez.ensure_l1_proxy(acct.address)
            if not ref.exists:
                self.state.incr("proxy_create_failed")
                self.state.log(f"proxy for {acct.address[:10]} not created", "warn")
                return
        except Exception as exc:  # noqa: BLE001
            self.state.incr("proxy_errors")
            self.state.log(f"ensure_l1_proxy failed: {exc}", "warn")
            return

        try:
            h, _ = eez.deposit(acct.address, self.amount)
        except Exception as exc:  # noqa: BLE001
            self.state.incr("deposit_send_errors")
            self.state.log(f"deposit send failed: {exc}", "warn")
            return

        self.state.incr("deposits_sent")
        l1_receipt = self.ctx.l1.wait_receipt(h, timeout=120)
        if l1_receipt is None:
            self.state.incr("deposit_l1_never_mined")
            self.state.finding(
                title="Deposit L1 tx never mined",
                severity="med",
                detail=f"deposit {h} for {acct.address} not mined within 120s (composer stall?)",
                tx=h,
                account=acct.address,
            )
            return
        if not l1_receipt.ok:
            self.state.incr("deposit_l1_reverted")
            reason = self.ctx.l1.revert_reason(h)
            self.state.finding(
                title="Deposit L1 tx reverted",
                severity="med",
                detail=f"deposit {h} reverted (reason={reason})",
                tx=h,
                account=acct.address,
            )
            return

        # Wait for the L2 credit — the part that actually matters.
        if self._wait_credit(acct.address, pre, timeout=120):
            self._mark_funded(acct)
            self.state.incr("funded")
        else:
            self.state.incr("deposit_no_l2_credit")
            self.state.finding(
                title="Deposit mined on L1 but no L2 credit",
                severity="high",
                detail=(
                    f"L1 deposit {h} mined (block {l1_receipt.block_number}) but "
                    f"{acct.address} never credited on L2 within 120s — lost deposit / silent stall"
                ),
                tx=h,
                account=acct.address,
            )

    def _wait_credit(self, address: str, pre: int, timeout: float) -> bool:
        deadline = self.now() + timeout
        while self.now() < deadline and not self.stopping():
            if self.ctx.l2.balance(address) > pre:
                return True
            self.sleep(3)
        return False

    def _mark_funded(self, acct: SubAccount) -> None:
        if all(a.address != acct.address for a in self.funded):
            self.funded.append(acct)
            self.ctx.put_shared("l2_accounts", list(self.funded))
            self.state.gauge("funded_count", len(self.funded))

    def _maintain(self) -> None:
        """Re-publish the pool + refresh a live balance gauge for the dashboard."""
        if not self.funded:
            return
        sample = self.funded[0]
        self.state.gauge("sample_account", sample.address)
        try:
            self.state.gauge("sample_l2_balance_wei", self.ctx.l2.balance(sample.address))
        except Exception:  # noqa: BLE001
            pass
        self.ctx.put_shared("l2_accounts", list(self.funded))
