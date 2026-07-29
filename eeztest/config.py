"""Configuration loading for EEZtest.

Reads a YAML file into typed dataclasses.  Private keys resolve from environment
variables first (the config names the env var), falling back to an inline value
for quick local runs.  Nothing here touches the network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class ChainConfig:
    rpc: str
    chain_id: int
    xchain_front: str
    extra_mempools: list[str] = field(default_factory=list)
    min_gas_price_wei: int = 1_000_000_000

    @property
    def all_send_endpoints(self) -> list[str]:
        """Every endpoint a signed tx may be broadcast to (front first)."""
        seen: list[str] = []
        for ep in [self.xchain_front, self.rpc, *self.extra_mempools]:
            if ep and ep not in seen:
                seen.append(ep)
        return seen


@dataclass
class EezConfig:
    rollup_id: int
    registry: str
    ccm_l2: str
    crosschain_gas_limit: int = 300_000
    # Address that submits postAndVerifyBatch. Optional, but supplying it makes the
    # "last L1 state-root update" vital exact instead of counting any registry call.
    batch_poster: str = ""


@dataclass
class BlockscoutConfig:
    l1_api: str = ""
    l2_api: str = ""


@dataclass
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8799


@dataclass
class RunConfig:
    duration_seconds: int = 3600
    report_dir: str = "reports"


@dataclass
class Config:
    instance_name: str
    l1: ChainConfig
    l2: ChainConfig
    eez: EezConfig
    blockscout: BlockscoutConfig
    dashboard: DashboardConfig
    run: RunConfig
    workers: dict[str, dict[str, Any]]
    private_key: str
    counterparty_key: str = ""

    # ── loading ────────────────────────────────────────────────────────────
    @staticmethod
    def load(path: str) -> "Config":
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        return Config.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Config":
        try:
            l1 = ChainConfig(**raw["l1"])
            l2 = ChainConfig(**raw["l2"])
            eez = EezConfig(**raw["eez"])
            blockscout = BlockscoutConfig(**raw.get("blockscout", {}))
            dashboard = DashboardConfig(**raw.get("dashboard", {}))
            run = RunConfig(**raw.get("run", {}))
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"malformed config: {exc}") from exc

        wallet = raw.get("wallet", {})
        pk = _resolve_secret(wallet.get("private_key_env"), wallet.get("private_key"))
        if not pk:
            raise ConfigError(
                "no private key: set env "
                f"{wallet.get('private_key_env', 'EEZTEST_PRIVATE_KEY')} or wallet.private_key"
            )
        cpk = _resolve_secret(wallet.get("counterparty_key_env"), wallet.get("counterparty_key"))

        return Config(
            instance_name=raw.get("instance_name", "eez-instance"),
            l1=l1,
            l2=l2,
            eez=eez,
            blockscout=blockscout,
            dashboard=dashboard,
            run=run,
            workers=raw.get("workers", {}),
            private_key=_normalize_key(pk),
            counterparty_key=_normalize_key(cpk) if cpk else "",
        )

    def worker_cfg(self, name: str) -> dict[str, Any]:
        return self.workers.get(name, {})

    def worker_enabled(self, name: str) -> bool:
        return bool(self.worker_cfg(name).get("enabled", False))


def _resolve_secret(env_name: str | None, inline: str | None) -> str:
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val.strip()
    return (inline or "").strip()


def _normalize_key(pk: str) -> str:
    pk = pk.strip()
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk
