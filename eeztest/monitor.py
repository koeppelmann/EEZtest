"""Chain monitor — keeps the shared registry's chain heads fresh for the dashboard.

A light background loop that polls both chains for head/gas/balance and publishes
them under registry.chain so the dashboard and the report always show live L1/L2
context alongside worker activity.
"""
from __future__ import annotations

import threading

from .rpc import ChainClient
from .state import StateRegistry


class ChainMonitor:
    def __init__(
        self,
        registry: StateRegistry,
        l1: ChainClient,
        l2: ChainClient,
        stop_event: threading.Event,
        interval: float = 3.0,
    ):
        self.registry = registry
        self.l1 = l1
        self.l2 = l2
        self.stop_event = stop_event
        self.interval = interval
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="chain-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.registry.set_chain("l1", self._probe(self.l1))
            self.registry.set_chain("l2", self._probe(self.l2))
            self.stop_event.wait(self.interval)

    def _probe(self, client: ChainClient) -> dict:
        info: dict = {"rpc": client.cfg.rpc, "chain_id": client.cfg.chain_id}
        try:
            info["head"] = client.block_number()
        except Exception as exc:  # noqa: BLE001
            info["head"] = f"error: {exc}"
        try:
            info["gas_price_wei"] = client.gas_price()
        except Exception:  # noqa: BLE001
            pass
        try:
            info["signer"] = client.address
            info["signer_balance_wei"] = client.balance(client.address)
        except Exception:  # noqa: BLE001
            pass
        return info
