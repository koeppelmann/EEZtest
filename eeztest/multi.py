"""Multi-instance support: monitor and test several EEZ devnets from one process.

A single EEZtest process can supervise N independent EEZ instances (devnets).
Each instance gets its own `StateRegistry`, chain clients, workers and monitor;
the dashboard then exposes them side by side with an instance switcher.

Config shape (`instances.yaml`):

    defaults:            # optional — merged under every instance
      wallet: {...}
      workers: {...}
      run: {duration_seconds: 3600}
    dashboard:
      host: 0.0.0.0
      port: 8799
    instances:
      - instance_name: eez-chiado-6290
        l1: {...}
        l2: {...}
        eez: {...}
      - instance_name: eez-devnet-12s
        ...

Instances are isolated: one unreachable devnet never affects another.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config import Config, ConfigError, DashboardConfig
from .contracts import Contracts
from .eez import Eez
from .monitor import ChainMonitor
from .report import write_report
from .rpc import ChainClient
from .state import StateRegistry
from .workers import ALL, WorkerContext


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` onto a copy of `base`."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class MultiConfig:
    dashboard: DashboardConfig
    instances: list[Config] = field(default_factory=list)

    @staticmethod
    def load(path: str) -> "MultiConfig":
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        return MultiConfig.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "MultiConfig":
        # A single-instance config is a valid multi-config with one entry.
        if "instances" not in raw:
            return MultiConfig(
                dashboard=DashboardConfig(**raw.get("dashboard", {})),
                instances=[Config.from_dict(raw)],
            )
        defaults = raw.get("defaults", {}) or {}
        dash = DashboardConfig(**raw.get("dashboard", {}))
        instances: list[Config] = []
        errors: list[str] = []
        for entry in raw.get("instances", []):
            merged = _deep_merge(defaults, entry or {})
            # The shared dashboard owns the port; per-instance dashboard keys are ignored.
            merged.pop("dashboard", None)
            try:
                instances.append(Config.from_dict(merged))
            except ConfigError as exc:
                errors.append(f"{entry.get('instance_name', '<unnamed>')}: {exc}")
        if not instances:
            raise ConfigError("no usable instances in config" + (f" ({'; '.join(errors)})" if errors else ""))
        for err in errors:
            print(f"[eeztest] skipping instance — {err}")
        return MultiConfig(dashboard=dash, instances=instances)


class InstanceRunner:
    """One EEZ instance: its own clients, registry, workers and monitor.

    Deliberately does NOT own the dashboard or the process lifetime — the
    supervisor does.  Construction never touches the network, so an unreachable
    devnet still shows up on the dashboard (its monitor reports the error).
    """

    def __init__(self, cfg: Config, stop_event: threading.Event):
        self.cfg = cfg
        self.stop_event = stop_event
        self.instance_id = cfg.instance_name
        self.registry = StateRegistry(cfg.instance_name)
        self.registry.run_duration = cfg.run.duration_seconds
        self.error: str | None = None
        self.workers: list = []

        self.l1 = ChainClient(cfg.l1, cfg.private_key, stop_event=stop_event)
        self.l2 = ChainClient(cfg.l2, cfg.private_key, stop_event=stop_event)
        self.eez = Eez(cfg, self.l1, self.l2)
        self.monitor = ChainMonitor(self.registry, self.l1, self.l2, stop_event)

    def start(self, contracts: Contracts, run_workers: bool = True) -> None:
        self.registry.set_chain("signer", self.l1.address)
        self.monitor.start()
        if not run_workers:
            self.registry.set_chain("mode", "monitor-only")
            return
        self.registry.set_chain("mode", "testing")
        ctx = WorkerContext(
            self.cfg, self.l1, self.l2, self.eez, contracts, self.registry, self.stop_event
        )
        order = ["funder", "contract_caller", "proxy_builder", "congestion", "fuzzer", "ddos"]
        names = order + [n for n in ALL if n not in order]
        for name in names:
            try:
                worker = ALL[name](ctx)
                self.workers.append(worker)
                worker.start()
            except Exception as exc:  # noqa: BLE001 — a bad worker must not kill the instance
                print(f"[eeztest][{self.instance_id}] worker {name} failed to start: {exc}")

    def join(self, deadline: float) -> list[str]:
        alive: list[str] = []
        for w in self.workers:
            w.join(timeout=max(0.0, deadline - time.time()))
            if getattr(w, "_thread", None) is not None and w._thread.is_alive():
                alive.append(w.name)
        return alive


class Supervisor:
    """Runs N instances concurrently behind one dashboard."""

    def __init__(self, mcfg: MultiConfig, run_workers: bool = True):
        self.mcfg = mcfg
        self.run_workers = run_workers
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.instances: list[InstanceRunner] = []
        self.contracts: Contracts | None = None
        for cfg in mcfg.instances:
            try:
                self.instances.append(InstanceRunner(cfg, self.stop_event))
            except Exception as exc:  # noqa: BLE001
                print(f"[eeztest] instance {cfg.instance_name} failed to initialize: {exc}")

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.run_workers:
            # Compile once and share across instances (the artifacts are identical).
            try:
                self.contracts = Contracts()
            except Exception as exc:  # noqa: BLE001
                print(f"[eeztest] contract compilation failed ({exc}); running monitor-only")
                self.contracts = None
        for inst in self.instances:
            try:
                inst.start(self.contracts, run_workers=self.run_workers and self.contracts is not None)
                mode = "testing" if (self.run_workers and self.contracts is not None) else "monitor-only"
                print(f"[eeztest] instance {inst.instance_id}: {mode} "
                      f"(L1 {inst.cfg.l1.chain_id} / L2 {inst.cfg.l2.chain_id})")
            except Exception as exc:  # noqa: BLE001
                inst.error = str(exc)
                print(f"[eeztest] instance {inst.instance_id} failed to start: {exc}")

    def stop(self) -> None:
        self.stop_event.set()

    def shutdown(self, grace: float = 30.0) -> dict[str, list[str]]:
        self.stop_event.set()
        deadline = time.time() + grace
        still: dict[str, list[str]] = {}
        for inst in self.instances:
            alive = inst.join(deadline)
            if alive:
                still[inst.instance_id] = alive
        return still

    # ── reporting ───────────────────────────────────────────────────────────
    def write_reports(self, report_dir: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for inst in self.instances:
            try:
                out.append(write_report(inst.registry, report_dir))
            except Exception as exc:  # noqa: BLE001
                print(f"[eeztest] report failed for {inst.instance_id}: {exc}")
        return out

    # ── dashboard data ──────────────────────────────────────────────────────
    def instance_ids(self) -> list[str]:
        return [i.instance_id for i in self.instances]

    def get(self, instance_id: str) -> InstanceRunner | None:
        for i in self.instances:
            if i.instance_id == instance_id:
                return i
        return None

    def overview(self) -> dict[str, Any]:
        """Compact cross-instance summary for the dashboard's landing view."""
        items = []
        for inst in self.instances:
            snap = inst.registry.snapshot()
            findings = inst.registry.all_findings()
            sev: dict[str, int] = {}
            for f in findings:
                sev[f["severity"]] = sev.get(f["severity"], 0) + 1
            workers = snap.get("workers", [])
            chain = snap.get("chain", {})
            items.append(
                {
                    "instance": inst.instance_id,
                    "mode": chain.get("mode", "unknown"),
                    "error": inst.error,
                    "l1": _chain_brief(chain.get("l1")),
                    "l2": _chain_brief(chain.get("l2")),
                    "workers_total": len(workers),
                    "workers_running": sum(1 for w in workers if w["status"] == "running"),
                    "workers_error": sum(1 for w in workers if w["status"] == "error"),
                    "findings_total": len(findings),
                    "findings_by_severity": sev,
                    "elapsed": snap.get("elapsed", 0),
                    "run_duration": snap.get("run_duration", 0),
                }
            )
        return {
            "started_at": self.started_at,
            "elapsed": time.time() - self.started_at,
            "instances": items,
        }

    def snapshot(self, instance_id: str) -> dict[str, Any] | None:
        inst = self.get(instance_id)
        return inst.registry.snapshot() if inst else None

    def findings(self, instance_id: str) -> list[dict[str, Any]] | None:
        inst = self.get(instance_id)
        return inst.registry.all_findings() if inst else None

    def all_findings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for inst in self.instances:
            for f in inst.registry.all_findings():
                out.append({**f, "instance": inst.instance_id})
        return sorted(out, key=lambda f: f["ts"])


def _chain_brief(c: dict[str, Any] | None) -> dict[str, Any]:
    if not c:
        return {"head": None, "ok": False}
    head = c.get("head")
    ok = isinstance(head, int)
    return {
        "head": head if ok else None,
        "ok": ok,
        "chain_id": c.get("chain_id"),
        "rpc": c.get("rpc"),
        "error": None if ok else str(head),
    }
