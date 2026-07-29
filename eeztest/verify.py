"""Blockscout contract verification via the standard-JSON-input API.

Blockscout exposes a verification endpoint that accepts a Solidity standard-JSON
input plus the contract name and constructor args.  We submit and poll for the
verdict.  Failures here are non-fatal for a test run — they become findings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .contracts import SOLC_VERSION, Deployment


@dataclass
class VerifyResult:
    address: str
    ok: bool
    status: str
    detail: str = ""


def verify_standard_json(
    api_base: str,
    deployment: Deployment,
    *,
    timeout: float = 90.0,
    poll: float = 5.0,
) -> VerifyResult:
    """Submit a standard-JSON verification and poll until resolved or timed out.

    Supports the Blockscout v2 smart-contract verification route.  `api_base`
    should end in `/api/` (e.g. https://gnosis-chiado.blockscout.com/api/).
    """
    if not api_base:
        return VerifyResult(deployment.address, False, "skipped", "no blockscout api configured")

    base = api_base.rstrip("/")
    # Blockscout v2: POST /api/v2/smart-contracts/{address}/verification/via/standard-input
    v2_url = (
        f"{base}/v2/smart-contracts/{deployment.address}/verification/via/standard-input"
    )
    files = {
        "files[0]": (
            "input.json",
            _dump_standard_json(deployment),
            "application/json",
        ),
    }
    data = {
        "compiler_version": _full_solc_version(),
        "contract_name": deployment.name,
        "autodetect_constructor_args": "false",
        "constructor_args": deployment.constructor_args_hex,
        "license_type": "mit",
    }
    try:
        resp = requests.post(v2_url, data=data, files=files, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(deployment.address, False, "error", f"submit failed: {exc}")

    if resp.status_code not in (200, 201, 202):
        # Already-verified is a success from our perspective.
        text = resp.text.lower()
        if "already verified" in text or "already been verified" in text:
            return VerifyResult(deployment.address, True, "already_verified")
        return VerifyResult(
            deployment.address, False, "rejected", f"http {resp.status_code}: {resp.text[:200]}"
        )

    return _poll_status(base, deployment.address, timeout=timeout, poll=poll)


def _poll_status(base: str, address: str, *, timeout: float, poll: float) -> VerifyResult:
    status_url = f"{base}/v2/smart-contracts/{address}"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(status_url, timeout=15)
            if r.status_code == 200:
                body = r.json()
                if body.get("is_verified"):
                    return VerifyResult(address, True, "verified")
                last = body.get("status") or "pending"
        except Exception as exc:  # noqa: BLE001
            last = f"poll error: {exc}"
        time.sleep(poll)
    return VerifyResult(address, False, "timeout", last)


def _dump_standard_json(deployment: Deployment) -> str:
    import json

    return json.dumps(deployment.artifact.standard_json)


def _full_solc_version() -> str:
    """Blockscout wants the long commit-tagged compiler string.

    We only pin the short version; Blockscout resolves the matching build, and if
    it needs the long form the caller can override.  Common Chiado builds accept
    the `vX.Y.Z+commit...` form, but the short form works on recent Blockscout.
    """
    return f"v{SOLC_VERSION}"
