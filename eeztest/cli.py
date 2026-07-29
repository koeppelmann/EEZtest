"""EEZtest command-line interface.

    eeztest run     --config config.yaml [--duration N] [--report-dir DIR]
    eeztest check   --config config.yaml        # connectivity + config sanity, no txs
    eeztest report  --config config.yaml        # serve dashboard only (no workers)

`run` is the autonomous session: start every enabled worker, serve the dashboard,
and write a report after the configured duration.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config, ConfigError
from .rpc import ChainClient
from .runner import Runner


def _load(args: argparse.Namespace) -> Config:
    try:
        cfg = Config.load(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"[eeztest] config error: {exc}", file=sys.stderr)
        sys.exit(2)
    if getattr(args, "duration", None):
        cfg.run.duration_seconds = args.duration
        cfg.__dict__  # no-op to satisfy linters
    if getattr(args, "report_dir", None):
        cfg.run.report_dir = args.report_dir
    return cfg


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load(args)
    print(f"[check] instance: {cfg.instance_name}")
    ok = True
    for label, chain in (("L1", cfg.l1), ("L2", cfg.l2)):
        client = ChainClient(chain, cfg.private_key)
        try:
            head = client.block_number()
            cid = int(client.call("eth_chainId"), 16)
            bal = client.balance(client.address)
            match = "ok" if cid == chain.chain_id else f"MISMATCH (config {chain.chain_id})"
            print(f"[check] {label} {chain.rpc}: head={head} chainId={cid} [{match}] signer_balance={bal}")
            if cid != chain.chain_id:
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"[check] {label} {chain.rpc}: UNREACHABLE ({exc})")
            ok = False
    # front reachability (best-effort)
    for label, chain in (("L1-front", cfg.l1), ("L2-front", cfg.l2)):
        try:
            from .rpc import rpc_call

            head = int(rpc_call(chain.xchain_front, "eth_blockNumber"), 16)
            print(f"[check] {label} {chain.xchain_front}: head={head}")
        except Exception as exc:  # noqa: BLE001
            print(f"[check] {label} {chain.xchain_front}: unreachable ({exc})")
    print(f"[check] registry={cfg.eez.registry} rollup_id={cfg.eez.rollup_id}")
    enabled = [n for n in cfg.workers if cfg.worker_enabled(n)]
    print(f"[check] enabled workers: {', '.join(enabled) or '(none)'}")
    print("[check] OK" if ok else "[check] problems detected")
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    Runner(cfg).run()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Serve the dashboard against a live registry with no workers (monitor only)."""
    import time

    cfg = _load(args)
    cfg.workers = {k: {**v, "enabled": False} for k, v in cfg.workers.items()}
    runner = Runner(cfg)
    runner.monitor.start()
    runner.dashboard.start()
    print(f"[report] dashboard → http://{cfg.dashboard.host}:{cfg.dashboard.port} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop_event.set()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eeztest", description="Autonomous test framework for EEZ chains")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="run the full autonomous test session")
    pr.add_argument("--config", "-c", required=True)
    pr.add_argument("--duration", type=int, help="override run duration (seconds)")
    pr.add_argument("--report-dir", help="override report output directory")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("check", help="verify connectivity + config, send no transactions")
    pc.add_argument("--config", "-c", required=True)
    pc.set_defaults(func=cmd_check)

    prep = sub.add_parser("report", help="serve the live dashboard only (monitor, no workers)")
    prep.add_argument("--config", "-c", required=True)
    prep.set_defaults(func=cmd_report)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
