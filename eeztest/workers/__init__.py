"""Workers: independent, threaded activity generators for an EEZ instance.

Each worker subclasses `Worker`, owns a `WorkerState`, and runs a loop until the
shared stop event is set.  The registry of built-in workers lives in `ALL`.
"""
from __future__ import annotations

from .base import Worker, WorkerContext

# Populated at import time below; kept explicit so the CLI can select by name.
from .congestion import CongestionWorker
from .contract_caller import ContractCallerWorker
from .ddos import DdosWorker
from .funder import FunderWorker
from .fuzzer import FuzzerWorker
from .proxy_builder import ProxyBuilderWorker
from .stateroot_race import StateRootRaceWorker

ALL: dict[str, type[Worker]] = {
    "funder": FunderWorker,
    "fuzzer": FuzzerWorker,
    "contract_caller": ContractCallerWorker,
    "congestion": CongestionWorker,
    "ddos": DdosWorker,
    "proxy_builder": ProxyBuilderWorker,
    "stateroot_race": StateRootRaceWorker,
}

__all__ = ["Worker", "WorkerContext", "ALL"]
