"""Offline unit tests for the EEZtest core — no network required.

Run with:  python -m pytest tests/  (or) python tests/test_core.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eeztest.config import Config, ConfigError  # noqa: E402
from eeztest.contracts import compile_all  # noqa: E402
from eeztest.eez import cross_chain_call_hash, selector  # noqa: E402
from eeztest.rpc import predict_create_address  # noqa: E402
from eeztest.state import StateRegistry, WorkerState  # noqa: E402


def test_selectors_match_eez():
    assert selector("computeCrossChainProxyAddress(address,uint256)") == "0xb761ba7e"
    assert selector("createCrossChainProxy(address,uint256)") == "0x2dd72120"


def test_predict_create_address_deterministic():
    a = predict_create_address("0xCC563C3F7d49bAC23725Ec5aC2B269747e4Cd491", 0)
    b = predict_create_address("0xCC563C3F7d49bAC23725Ec5aC2B269747e4Cd491", 0)
    assert a == b
    assert a.startswith("0x") and len(a) == 42


def test_cross_chain_hash_stable():
    h1 = cross_chain_call_hash(
        is_static=False, source_address="0x" + "11" * 20, source_rollup_id=1,
        target_address="0x" + "22" * 20, target_rollup_id=0, value=0, data=b"",
    )
    h2 = cross_chain_call_hash(
        is_static=False, source_address="0x" + "11" * 20, source_rollup_id=1,
        target_address="0x" + "22" * 20, target_rollup_id=0, value=0, data=b"",
    )
    assert h1 == h2 and h1.startswith("0x") and len(h1) == 66


def test_contracts_compile():
    arts = compile_all()
    for name in ("Counter", "Logger", "SimpleStorage", "Forwarder"):
        assert name in arts, f"{name} did not compile"
        assert arts[name].bytecode.startswith("0x") and len(arts[name].bytecode) > 4
    assert arts["Counter"].encode_call("increment") == "0xd09de08a"


def test_config_requires_key(tmp_path=None):
    raw = {
        "instance_name": "t",
        "l1": {"rpc": "http://x", "chain_id": 1, "xchain_front": "http://x"},
        "l2": {"rpc": "http://y", "chain_id": 2, "xchain_front": "http://y"},
        "eez": {"rollup_id": 1, "registry": "0x" + "00" * 20, "ccm_l2": "0x" + "00" * 20},
        "wallet": {"private_key_env": "DOES_NOT_EXIST_EEZTEST", "private_key": ""},
    }
    try:
        Config.from_dict(raw)
    except ConfigError:
        return
    raise AssertionError("expected ConfigError for missing key")


def test_config_loads_with_inline_key():
    raw = {
        "instance_name": "t",
        "l1": {"rpc": "http://x", "chain_id": 1, "xchain_front": "http://x"},
        "l2": {"rpc": "http://y", "chain_id": 2, "xchain_front": "http://y"},
        "eez": {"rollup_id": 1, "registry": "0x" + "00" * 20, "ccm_l2": "0x" + "00" * 20},
        "wallet": {"private_key": "0x" + "11" * 32},
        "workers": {"funder": {"enabled": True}},
    }
    cfg = Config.from_dict(raw)
    assert cfg.private_key == "0x" + "11" * 32
    assert cfg.worker_enabled("funder")
    assert not cfg.worker_enabled("ddos")


def test_state_snapshot():
    reg = StateRegistry("inst")
    w = WorkerState("worker-a", "desc")
    reg.register(w)
    w.set_status("running")
    w.incr("txs", 3)
    w.gauge("head", 42)
    w.finding("bad thing", "high", "detail here", tx="0xabc")
    snap = reg.snapshot()
    assert snap["instance"] == "inst"
    assert snap["workers"][0]["counters"]["txs"] == 3
    assert snap["workers"][0]["metrics"]["head"] == 42
    assert reg.all_findings()[0]["severity"] == "high"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
