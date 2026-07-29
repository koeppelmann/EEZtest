#!/usr/bin/env python3
"""Does an L1->L2 cross-chain deposit that MINES on L1 actually execute on L2?

Method (deliberately the documented happy path, nothing exotic):
  1. fresh target address
  2. createCrossChainProxy(target, rollupId) on plain Chiado; wait for code
  3. send value to that proxy via the L1->L2 ingress front
  4. confirm the L1 tx MINED with status 1
  5. poll the target's L2 balance

A mined-with-status-1 L1 leg plus no L2 credit is the failure we are testing for.
Every read is checked; a read failure is reported, never treated as "no credit".
"""
import json
import sys
import time

import requests
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

S = requests.Session()
PLAIN = "https://rpc.chiadochain.net"
FRONT = "http://65.109.26.16:18999"
L2 = "http://65.109.26.16:18688"
REG = to_checksum_address("0xf0656341956d83d047c5e26678130e453952f32c")
ROLLUP_ID = 1
DEPOSIT = 2 * 10**15  # 0.002 xDAI


def rpc(url, m, p, tries=4):
    last = None
    for i in range(tries):
        try:
            r = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            b = r.json()
            return b.get("result"), b.get("error")
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"rpc {m} failed: {last}")


def main() -> None:
    pk = json.load(open("/tmp/claude-1000/-home-ubuntu-code-testsync-rollups/"
                        "29d9b281-e710-4d82-b627-b8f5bd3009b3/scratchpad/fresh_wallet.json"))["privateKey"]
    acct = Account.from_key(pk)
    me = to_checksum_address(acct.address)

    target = to_checksum_address(Account.from_key("0x" + keccak(str(time.time()).encode()).hex()).address)
    print(f"signer  {me}")
    print(f"target  {target}   (fresh; will be credited on L2 if the deposit works)")

    # ── 1. compute the proxy ────────────────────────────────────────────────
    data = "0xb761ba7e" + abi_encode(["address", "uint256"], [target, ROLLUP_ID]).hex()
    res, err = rpc(PLAIN, "eth_call", [{"to": REG, "data": data}, "latest"])
    if err:
        print(f"computeCrossChainProxyAddress failed: {err}")
        sys.exit(1)
    proxy = to_checksum_address("0x" + res[-40:])
    print(f"proxy   {proxy}")

    # ── 2. create it on plain Chiado (the path that works) ──────────────────
    n, _ = rpc(PLAIN, "eth_getTransactionCount", [me, "pending"])
    n = int(n, 16)
    cdata = "0x2dd72120" + abi_encode(["address", "uint256"], [target, ROLLUP_ID]).hex()
    signed = acct.sign_transaction({"to": REG, "value": 0, "data": cdata, "gas": 500_000,
                                    "gasPrice": 3 * 10**9, "nonce": n, "chainId": 10200})
    ch = "0x" + keccak(signed.raw_transaction).hex()
    rpc(PLAIN, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
    print(f"\n[1] createCrossChainProxy tx {ch}")
    for _ in range(30):
        time.sleep(4)
        rc, _ = rpc(PLAIN, "eth_getTransactionReceipt", [ch])
        if rc:
            print(f"    mined block {int(rc['blockNumber'],16)} status={int(rc['status'],16)}")
            break
    code, _ = rpc(PLAIN, "eth_getCode", [proxy, "latest"])
    if not code or code == "0x":
        print("    PROXY HAS NO CODE — aborting, precondition not met")
        sys.exit(1)
    print(f"    proxy code: {(len(code)-2)//2} bytes  ✓ precondition met")

    # ── 3. deposit through the ingress front ───────────────────────────────
    pre, _ = rpc(L2, "eth_getBalance", [target, "latest"])
    pre = int(pre, 16)
    n2, _ = rpc(PLAIN, "eth_getTransactionCount", [me, "pending"])
    n2 = int(n2, 16)
    signed2 = acct.sign_transaction({"to": proxy, "value": DEPOSIT, "data": "0x", "gas": 300_000,
                                     "gasPrice": 3 * 10**9, "nonce": n2, "chainId": 10200})
    dh = "0x" + keccak(signed2.raw_transaction).hex()
    raw2 = "0x" + signed2.raw_transaction.hex()
    print(f"\n[2] deposit {DEPOSIT} wei -> proxy, via the L1->L2 ingress front")
    print(f"    tx {dh}")
    res2, err2 = rpc(FRONT, "eth_sendRawTransaction", [raw2])
    print(f"    front response: result={res2} error={(err2 or {}).get('message')}")
    print(f"    target L2 balance before: {pre}")

    # ── 4/5. did the L1 leg mine, and did L2 credit? ───────────────────────
    l1_mined = None
    l2_credit = None
    t0 = time.time()
    print("\n[3] watching L1 inclusion and L2 credit for 180 s")
    for i in range(36):
        time.sleep(5)
        el = time.time() - t0
        rc, _ = rpc(PLAIN, "eth_getTransactionReceipt", [dh])
        bal, berr = rpc(L2, "eth_getBalance", [target, "latest"])
        if berr:
            print(f"    t={el:5.0f}s  L2 balance READ FAILED: {berr}")
            continue
        bal = int(bal, 16)
        if l1_mined is None and rc:
            l1_mined = (el, int(rc["blockNumber"], 16), int(rc["status"], 16), int(rc["gasUsed"], 16))
            print(f"    t={el:5.0f}s  ★ L1 MINED block {l1_mined[1]} status={l1_mined[2]} gasUsed={l1_mined[3]}")
        if l2_credit is None and bal > pre:
            l2_credit = (el, bal - pre)
            print(f"    t={el:5.0f}s  ★ L2 CREDITED +{bal-pre} wei")
        if l1_mined and l2_credit:
            break
        if i % 4 == 0:
            print(f"    t={el:5.0f}s  L1={'mined' if l1_mined else 'pending'}  L2 bal={bal}")

    print("\n===== VERDICT =====")
    if l1_mined is None:
        print("  L1 leg NEVER MINED — this is the ingress-front drop, not an L2 execution failure.")
    elif l1_mined[2] != 1:
        print(f"  L1 leg mined but REVERTED (status {l1_mined[2]}) — not an L2 execution failure.")
    elif l2_credit is None:
        print("  *** L1 leg MINED with status 1, but L2 NEVER EXECUTED within 180 s ***")
        print("  This is the serious case: a cross-chain message accepted and settled on L1")
        print("  whose L2 side never ran.")
    else:
        print(f"  WORKS: L1 mined at {l1_mined[0]:.0f}s, L2 credited at {l2_credit[0]:.0f}s "
              f"(+{l2_credit[1]} wei). No lost message in this trial.")
    print(f"\n  proxy   {proxy}")
    print(f"  target  {target}")
    print(f"  deposit tx {dh}")


if __name__ == "__main__":
    main()
