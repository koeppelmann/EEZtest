"""Dashboard HTTP server.

Serves a single-page live view of the run and a JSON snapshot endpoint the page
polls.  Runs in a background thread (uvicorn) so it never blocks the workers.
"""
from __future__ import annotations

import os
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..state import StateRegistry

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def build_app(registry: StateRegistry) -> FastAPI:
    app = FastAPI(title="EEZtest dashboard")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        with open(os.path.join(_STATIC_DIR, "index.html")) as fh:
            return fh.read()

    @app.get("/api/state")
    def state() -> JSONResponse:
        return JSONResponse(registry.snapshot())

    @app.get("/api/findings")
    def findings() -> JSONResponse:
        return JSONResponse({"findings": registry.all_findings()})

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


class DashboardServer:
    """Runs uvicorn in a daemon thread."""

    def __init__(self, registry: StateRegistry, host: str, port: int):
        self.registry = registry
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._server = None

    def start(self) -> None:
        import uvicorn

        app = build_app(self.registry)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
