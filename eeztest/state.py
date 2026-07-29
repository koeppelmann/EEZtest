"""Thread-safe shared state that workers publish to and the dashboard reads.

Each worker owns one `WorkerState`.  Workers run in their own threads and mutate
their state through the small locked methods here; the dashboard snapshots the
whole registry as plain dicts for JSON serialization.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque


@dataclass
class Event:
    ts: float
    level: str  # info | ok | warn | error
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "level": self.level, "message": self.message}


class WorkerState:
    """Live state for a single worker, safe for concurrent read/write."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._lock = threading.Lock()
        self._status = "init"  # init | running | idle | error | done | disabled
        self._started_at: float | None = None
        self._last_activity: float | None = None
        self._counters: dict[str, int] = {}
        self._metrics: dict[str, Any] = {}
        self._events: Deque[Event] = deque(maxlen=200)
        self._findings: list[dict[str, Any]] = []

    # ── mutation ───────────────────────────────────────────────────────────
    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
            if status == "running" and self._started_at is None:
                self._started_at = time.time()

    def incr(self, key: str, by: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + by
            self._last_activity = time.time()

    def gauge(self, key: str, value: Any) -> None:
        with self._lock:
            self._metrics[key] = value
            self._last_activity = time.time()

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._events.append(Event(time.time(), level, message))
            self._last_activity = time.time()

    def finding(self, title: str, severity: str, detail: str, **extra: Any) -> None:
        """Record a durable, report-worthy observation (bug, anomaly, invariant break)."""
        with self._lock:
            self._findings.append(
                {
                    "ts": time.time(),
                    "worker": self.name,
                    "title": title,
                    "severity": severity,  # info | low | med | high | critical
                    "detail": detail,
                    **extra,
                }
            )
            self._events.append(Event(time.time(), _sev_level(severity), f"FINDING: {title}"))

    # ── read ───────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "description": self.description,
                "status": self._status,
                "started_at": self._started_at,
                "last_activity": self._last_activity,
                "uptime": (time.time() - self._started_at) if self._started_at else 0,
                "counters": dict(self._counters),
                "metrics": dict(self._metrics),
                "events": [e.to_dict() for e in list(self._events)[-40:]],
                "findings": list(self._findings),
            }

    def findings(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._findings)


class StateRegistry:
    """The full run's shared state: chain heads + every worker's state."""

    def __init__(self, instance_name: str) -> None:
        self.instance_name = instance_name
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerState] = {}
        self._chain: dict[str, Any] = {}
        self.started_at = time.time()
        self.run_duration = 0

    def register(self, worker: WorkerState) -> WorkerState:
        with self._lock:
            self._workers[worker.name] = worker
        return worker

    def set_chain(self, key: str, value: Any) -> None:
        with self._lock:
            self._chain[key] = value

    def all_findings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            out.extend(w.findings())
        return sorted(out, key=lambda f: f["ts"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = list(self._workers.values())
            chain = dict(self._chain)
            started = self.started_at
            duration = self.run_duration
        elapsed = time.time() - started
        return {
            "instance": self.instance_name,
            "started_at": started,
            "elapsed": elapsed,
            "run_duration": duration,
            "remaining": max(0, duration - elapsed) if duration else None,
            "chain": chain,
            "workers": [w.snapshot() for w in workers],
        }


def _sev_level(severity: str) -> str:
    return {
        "info": "info",
        "low": "warn",
        "med": "warn",
        "high": "error",
        "critical": "error",
    }.get(severity, "info")
