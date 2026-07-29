"""Worker base class + shared context.

A `WorkerContext` bundles everything a worker needs: config, both chain clients,
the EEZ helper, the compiled contracts, the shared state registry, and the global
stop event.  `Worker` gives every subclass a threaded run loop, structured logging
into its `WorkerState`, and uniform error handling so one worker crashing never
takes down the run.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from ..config import Config
from ..contracts import Contracts
from ..eez import Eez
from ..nonce import NonceManager
from ..rpc import ChainClient
from ..state import StateRegistry, WorkerState


@dataclass
class SubAccount:
    address: str
    private_key: str

    @staticmethod
    def random() -> "SubAccount":
        acct = Account.from_key("0x" + secrets.token_hex(32))
        return SubAccount(to_checksum_address(acct.address), acct.key.hex())

    @staticmethod
    def derive(seed: str, index: int) -> "SubAccount":
        """Deterministic sub-account so re-runs reuse funded keys."""
        import hashlib

        pk = "0x" + hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()
        acct = Account.from_key(pk)
        return SubAccount(to_checksum_address(acct.address), pk)


class WorkerContext:
    def __init__(
        self,
        cfg: Config,
        l1: ChainClient,
        l2: ChainClient,
        eez: Eez,
        contracts: Contracts,
        registry: StateRegistry,
        stop_event: threading.Event,
    ):
        self.cfg = cfg
        self.l1 = l1
        self.l2 = l2
        self.eez = eez
        self.contracts = contracts
        self.registry = registry
        self.stop_event = stop_event
        # Cross-worker shared blackboard (e.g. funder publishes funded accounts).
        self.shared: dict[str, Any] = {}
        self._shared_lock = threading.Lock()
        # Single coordinated nonce allocator for every sub-account send, so
        # concurrent workers sharing the funded pool never collide or gap.
        self.nonces = NonceManager()
        self._account_cache: dict[str, Account] = {}

    def put_shared(self, key: str, value: Any) -> None:
        with self._shared_lock:
            self.shared[key] = value

    def get_shared(self, key: str, default: Any = None) -> Any:
        with self._shared_lock:
            return self.shared.get(key, default)

    def _account_for(self, private_key: str) -> Account:
        acct = self._account_cache.get(private_key)
        if acct is None:
            acct = Account.from_key(private_key)
            self._account_cache[private_key] = acct
        return acct

    def send_sub(
        self,
        client: ChainClient,
        private_key: str,
        *,
        to: str | None,
        value: int = 0,
        data: str = "0x",
        gas: int = 100_000,
        gas_price: int | None = None,
        endpoint: str | None = None,
    ) -> str:
        """Sign + broadcast a tx from an arbitrary sub-account through the shared
        nonce allocator.  On any failure the reserved nonce is rolled back so a
        transient send error cannot wedge that account.  Returns the tx hash.
        """
        acct = self._account_for(private_key)
        addr = to_checksum_address(acct.address)
        tag = f"chain{client.cfg.chain_id}"
        nonce = self.nonces.allocate(tag, addr, client.nonce(addr, "pending"))
        tx: dict[str, Any] = {
            "value": value,
            "data": data,
            "gas": gas,
            "gasPrice": gas_price if gas_price is not None else client.gas_price(),
            "nonce": nonce,
            "chainId": client.cfg.chain_id,
        }
        if to is not None:
            tx["to"] = to_checksum_address(to)
        try:
            signed = acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()
            client.send_raw(raw, endpoint or client.cfg.rpc)
            return "0x" + keccak(signed.raw_transaction).hex()
        except Exception:
            self.nonces.rollback(tag, addr, nonce)
            raise

    def resync_sub(self, client: ChainClient, address: str) -> None:
        self.nonces.resync(f"chain{client.cfg.chain_id}", address)


class Worker:
    """Base class for all workers.  Subclasses implement `step` or `run`."""

    name: str = "worker"
    description: str = ""

    def __init__(self, ctx: WorkerContext):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.wcfg = ctx.cfg.worker_cfg(self.name)
        self.state = WorkerState(self.name, self.description)
        ctx.registry.register(self.state)
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.cfg.worker_enabled(self.name):
            self.state.set_status("disabled")
            self.state.log("worker disabled in config")
            return
        self._thread = threading.Thread(target=self._run_guarded, name=self.name, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def stopping(self) -> bool:
        return self.ctx.stop_event.is_set()

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep — returns early when the stop event fires."""
        self.ctx.stop_event.wait(seconds)

    # ── overridable ──────────────────────────────────────────────────────────
    def setup(self) -> None:
        """One-time preparation before the loop.  Optional."""

    def step(self) -> None:
        """One iteration of work.  Subclasses that use the default loop override this."""
        raise NotImplementedError

    def interval(self) -> float:
        return float(self.wcfg.get("interval_seconds", 5))

    def run(self) -> None:
        """Default loop: setup (retried until it succeeds), then step() until stop."""
        if not self._setup_with_retry():
            return  # stopped before setup could complete
        while not self.stopping():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 — isolate per-iteration failures
                self.state.incr("errors")
                self.state.log(f"step error: {exc}", "error")
            self.sleep(self.interval())

    def _setup_with_retry(self, base_backoff: float = 3.0, max_backoff: float = 30.0) -> bool:
        """Run setup(), retrying with capped backoff until it succeeds or we stop.

        A transient chain/compiler failure in setup() must not permanently break a
        worker (nor drop it into step() with half-initialized state).  Returns True
        once setup succeeds, False if the stop event fires first.
        """
        backoff = base_backoff
        attempt = 0
        while not self.stopping():
            try:
                self.setup()
                if attempt:
                    self.state.log(f"setup succeeded after {attempt} retries")
                return True
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                self.state.incr("setup_errors")
                self.state.log(f"setup failed (attempt {attempt}): {exc}; retrying in {backoff:.0f}s", "warn")
                self.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
        return False

    # ── internals ────────────────────────────────────────────────────────────
    def _run_guarded(self) -> None:
        self.state.set_status("running")
        self.state.log("started")
        try:
            self.run()
            self.state.set_status("done")
            self.state.log("finished")
        except Exception as exc:  # noqa: BLE001
            self.state.set_status("error")
            self.state.log(f"fatal: {exc}", "error")
            self.state.finding(
                title=f"{self.name} crashed",
                severity="high",
                detail=str(exc),
            )

    # ── convenience ──────────────────────────────────────────────────────────
    def now(self) -> float:
        return time.time()

    # Delegate the shared sub-account send path + coordinated nonce allocator so
    # workers can call self.send_sub(...) / self.nonces directly.  These MUST come
    # from the single shared WorkerContext so all workers coordinate.
    @property
    def nonces(self) -> NonceManager:
        return self.ctx.nonces

    def send_sub(self, *args: Any, **kwargs: Any) -> str:
        return self.ctx.send_sub(*args, **kwargs)

    def resync_sub(self, *args: Any, **kwargs: Any) -> None:
        return self.ctx.resync_sub(*args, **kwargs)
