"""Run orchestration: wire config → clients → workers → dashboard → report.

`Runner.run()` is the whole autonomous session: it brings up both chain clients,
the EEZ helper, compiled contracts, the shared state registry, a chain monitor,
the dashboard server, every enabled worker, then blocks for the configured
duration (or until interrupted), and finally writes the report.
"""
from __future__ import annotations

import signal
import threading
import time

from .config import Config
from .contracts import Contracts
from .dashboard import DashboardServer
from .eez import Eez
from .monitor import ChainMonitor
from .report import write_report
from .rpc import ChainClient
from .state import StateRegistry
from .workers import ALL, WorkerContext


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.registry = StateRegistry(cfg.instance_name)
        self.registry.run_duration = cfg.run.duration_seconds

        self.l1 = ChainClient(cfg.l1, cfg.private_key, stop_event=self.stop_event)
        self.l2 = ChainClient(cfg.l2, cfg.private_key, stop_event=self.stop_event)
        self.eez = Eez(cfg, self.l1, self.l2)
        self.contracts = Contracts()

        self.ctx = WorkerContext(
            cfg, self.l1, self.l2, self.eez, self.contracts, self.registry, self.stop_event
        )
        self.monitor = ChainMonitor(self.registry, self.l1, self.l2, self.stop_event)
        self.dashboard = DashboardServer(self.registry, cfg.dashboard.host, cfg.dashboard.port)
        self.workers = []

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ANN001, ARG001
            print("\n[eeztest] stop signal received; shutting down…")
            self.stop_event.set()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except ValueError:
            # Not on the main thread (e.g. embedded); caller drives stop_event.
            pass

    def start(self) -> None:
        print(f"[eeztest] instance: {self.cfg.instance_name}")
        print(f"[eeztest] L1={self.cfg.l1.rpc} (chain {self.cfg.l1.chain_id})")
        print(f"[eeztest] L2={self.cfg.l2.rpc} (chain {self.cfg.l2.chain_id})")
        print(f"[eeztest] signer={self.l1.address}")

        self.monitor.start()
        self.dashboard.start()
        print(f"[eeztest] dashboard → http://{self.cfg.dashboard.host}:{self.cfg.dashboard.port}")

        # Order matters a little: funder first so peers can pick up funded accounts.
        order = ["funder", "contract_caller", "proxy_builder", "congestion", "fuzzer", "ddos"]
        names = order + [n for n in ALL if n not in order]
        for name in names:
            cls = ALL[name]
            worker = cls(self.ctx)
            self.workers.append(worker)
            worker.start()
            status = "enabled" if self.cfg.worker_enabled(name) else "disabled"
            print(f"[eeztest] worker {name}: {status}")

    def run(self) -> tuple[str, str]:
        self._install_signal_handlers()
        self.start()
        duration = self.cfg.run.duration_seconds
        print(f"[eeztest] running for {duration}s (Ctrl-C to stop early)")
        deadline = time.time() + duration
        try:
            while time.time() < deadline and not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()

        print("[eeztest] stopping workers…")
        # Bounded global shutdown: the stop event already interrupts receipt waits
        # and interruptible sleeps, so workers should exit promptly.  Join each
        # against a shared deadline; note any that are still alive so the report
        # reflects reality rather than silently snapshotting mid-flight.
        shutdown_deadline = time.time() + 30
        still_alive = []
        for w in self.workers:
            remaining = max(0.0, shutdown_deadline - time.time())
            w.join(timeout=remaining)
            if w._thread is not None and w._thread.is_alive():
                still_alive.append(w.name)
        if still_alive:
            print(f"[eeztest] workers still running at shutdown: {', '.join(still_alive)}")
            self.registry.set_chain("shutdown_note", f"workers still active: {', '.join(still_alive)}")
        self.dashboard.stop()

        md, js = write_report(self.registry, self.cfg.run.report_dir)
        print(f"[eeztest] report written:\n  {md}\n  {js}")
        return md, js
