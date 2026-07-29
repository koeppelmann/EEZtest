"""Dashboard HTTP server.

Serves a live view of one or many EEZ instances plus the JSON endpoints the page
polls.  Runs uvicorn in a background thread so it never blocks the workers.

Endpoints (multi-instance):
    GET /                              the dashboard page
    GET /api/overview                  cross-instance summary (all devnets)
    GET /api/instances                 instance ids
    GET /api/instances/{id}/state      one instance's full snapshot
    GET /api/instances/{id}/findings   one instance's findings
    GET /api/findings                  findings across all instances
    GET /api/health

Legacy single-instance aliases (/api/state, /api/findings) resolve to the first
instance so an existing single-instance deployment keeps working.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..state import StateRegistry

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _page() -> str:
    with open(os.path.join(_STATIC_DIR, "index.html")) as fh:
        return fh.read()


def build_app(supervisor: Any) -> FastAPI:
    """Build the app around a Supervisor (multi-instance)."""
    app = FastAPI(title="EEZtest dashboard")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page()

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "instances": supervisor.instance_ids()})

    @app.get("/api/overview")
    def overview() -> JSONResponse:
        return JSONResponse(supervisor.overview())

    @app.get("/api/instances")
    def instances() -> JSONResponse:
        return JSONResponse({"instances": supervisor.instance_ids()})

    @app.get("/api/instances/{instance_id}/state")
    def instance_state(instance_id: str) -> JSONResponse:
        snap = supervisor.snapshot(instance_id)
        if snap is None:
            return JSONResponse({"error": "unknown instance"}, status_code=404)
        return JSONResponse(snap)

    @app.get("/api/instances/{instance_id}/findings")
    def instance_findings(instance_id: str) -> JSONResponse:
        f = supervisor.findings(instance_id)
        if f is None:
            return JSONResponse({"error": "unknown instance"}, status_code=404)
        return JSONResponse({"findings": f})

    @app.get("/api/findings")
    def all_findings() -> JSONResponse:
        return JSONResponse({"findings": supervisor.all_findings()})

    # ── legacy single-instance aliases ──────────────────────────────────────
    @app.get("/api/state")
    def state() -> JSONResponse:
        ids = supervisor.instance_ids()
        if not ids:
            return JSONResponse({"error": "no instances"}, status_code=404)
        return JSONResponse(supervisor.snapshot(ids[0]))

    return app


def build_app_single(registry: StateRegistry) -> FastAPI:
    """Compatibility shim: wrap one registry in a supervisor-like facade."""

    class _Facade:
        def instance_ids(self) -> list[str]:
            return [registry.instance_name]

        def snapshot(self, iid: str) -> dict | None:
            return registry.snapshot() if iid == registry.instance_name else None

        def findings(self, iid: str) -> list | None:
            return registry.all_findings() if iid == registry.instance_name else None

        def all_findings(self) -> list:
            return [{**f, "instance": registry.instance_name} for f in registry.all_findings()]

        def overview(self) -> dict:
            snap = registry.snapshot()
            findings = registry.all_findings()
            sev: dict[str, int] = {}
            for f in findings:
                sev[f["severity"]] = sev.get(f["severity"], 0) + 1
            chain = snap.get("chain", {})
            workers = snap.get("workers", [])

            def brief(c):
                head = (c or {}).get("head")
                ok = isinstance(head, int)
                return {"head": head if ok else None, "ok": ok,
                        "chain_id": (c or {}).get("chain_id"), "rpc": (c or {}).get("rpc"),
                        "error": None if ok else str(head)}

            return {
                "started_at": snap.get("started_at"),
                "elapsed": snap.get("elapsed", 0),
                "instances": [{
                    "instance": registry.instance_name,
                    "mode": chain.get("mode", "testing"),
                    "error": None,
                    "l1": brief(chain.get("l1")), "l2": brief(chain.get("l2")),
                    "workers_total": len(workers),
                    "workers_running": sum(1 for w in workers if w["status"] == "running"),
                    "workers_error": sum(1 for w in workers if w["status"] == "error"),
                    "findings_total": len(findings), "findings_by_severity": sev,
                    "elapsed": snap.get("elapsed", 0), "run_duration": snap.get("run_duration", 0),
                }],
            }

    return build_app(_Facade())


class DashboardServer:
    """Runs uvicorn in a daemon thread.

    Accepts either a Supervisor (multi-instance) or a StateRegistry (single).
    """

    def __init__(self, target: Any, host: str, port: int):
        self.target = target
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._server = None

    def start(self) -> None:
        import uvicorn

        if isinstance(self.target, StateRegistry):
            app = build_app_single(self.target)
        else:
            app = build_app(self.target)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
