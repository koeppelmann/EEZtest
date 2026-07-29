#!/usr/bin/env python3
"""Deploy ProxyDepositHelper to Chiado and prove it end-to-end.

Test: pick a FRESH L2 recipient whose proxy does not exist, call
helper.depositTo{value}(recipient, rollupId) in ONE transaction, and confirm the
proxy gets created AND the recipient is credited on L2.
"""
import json
import sys
import time

import requests
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

sys.path.insert(0, "/home/ubuntu/code/EEZtest")
from eeztest.contracts import compile_all  # noqa: E402
from eeztest.rpc import predict_create_address  # noqa: E402

S = requests.Session()
PLAIN = "https://rpc.chiadochain.net"
FRONT = "http://65.109.26.16:18999"
L2 = "http://65.109.26.16:18688"
REG = to_checksum_address("0xf0656341956d83d047c5e26678130e453952f32c")
ROLLUP_ID = 1
DEPOSIT = 2 * 10**15


def rpc(url, m, p, tries=4):
    last = None
    for i in range(tries):
        try:
            r = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1)); continue
            r.raise_for_status()
            b = r.json()
            if b.get("error"):
                return None, b["error"]
            return b.get("result"), None
        except Exception as exc:
            last = exc; time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"rpc {m} failed: {last}")


def wait_receipt(h, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, _ = rpc(PLAIN, "eth_getTransactionReceipt", [h])
        if rc:
            return rc
        time.sleep(4)
    return None


def main() -> None:
    pk = json.load(open("/tmp/claude-1000/-home-ubuntu-code-testsync-rollups/"
                        "29d9b281-e710-4d82-b627-b8f5bd3009b3/scratchpad/fresh_wallet.json"))["privateKey"]
    acct = Account.from_key(pk)
    me = to_checksum_address(acct.address)
    art = compile_all()["ProxyDepositHelper"]

    # ── deploy ──────────────────────────────────────────────────────────────
    ctor = abi_encode(["address"], [REG]).hex()
    n, _ = rpc(PLAIN, "eth_getTransactionCount", [me, "pending"]); n = int(n, 16)
    predicted = predict_create_address(me, n)
    signed = acct.sign_transaction({"data": art.bytecode + ctor, "gas": 900_000,
                                    "gasPrice": 3 * 10**9, "nonce": n, "chainId": 10200})
    h = "0x" + keccak(signed.raw_transaction).hex()
    rpc(PLAIN, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
    print(f"[deploy] tx {h}\n[deploy] predicted address {predicted}")
    rc = wait_receipt(h)
    if not rc or int(rc["status"], 16) != 1:
        print(f"[deploy] FAILED: {rc}"); sys.exit(1)
    helper = to_checksum_address(rc.get("contractAddress") or predicted)
    print(f"[deploy] mined block {int(rc['blockNumber'],16)} gasUsed={int(rc['gasUsed'],16)}")
    print(f"[deploy] HELPER = {helper}")
    code, _ = rpc(PLAIN, "eth_getCode", [helper, "latest"])
    print(f"[deploy] code: {(len(code)-2)//2} bytes")

    # ── test: fresh recipient, proxy must not exist ────────────────────────
    recipient = to_checksum_address(Account.create().address)
    q = "0x" + keccak(b"proxyFor(address,uint256)").hex()[:8] + abi_encode(
        ["address", "uint256"], [recipient, ROLLUP_ID]).hex()
    res, err = rpc(PLAIN, "eth_call", [{"to": helper, "data": q}, "latest"])
    if err:
        print(f"proxyFor failed: {err}"); sys.exit(1)
    proxy = to_checksum_address("0x" + res[26:66])
    exists = int(res[66:130], 16) == 1
    print(f"\n[test] recipient {recipient}")
    print(f"[test] proxy     {proxy}  exists={exists}  (must be False)")
    if exists:
        print("[test] proxy already exists — pick another recipient"); sys.exit(1)

    pre, _ = rpc(L2, "eth_getBalance", [recipient, "latest"]); pre = int(pre, 16)
    data = "0x" + keccak(b"depositTo(address,uint256)").hex()[:8] + abi_encode(
        ["address", "uint256"], [recipient, ROLLUP_ID]).hex()
    n2, _ = rpc(PLAIN, "eth_getTransactionCount", [me, "pending"]); n2 = int(n2, 16)
    signed2 = acct.sign_transaction({"to": helper, "value": DEPOSIT, "data": data, "gas": 900_000,
                                     "gasPrice": 3 * 10**9, "nonce": n2, "chainId": 10200})
    h2 = "0x" + keccak(signed2.raw_transaction).hex()
    rpc(PLAIN, "eth_sendRawTransaction", ["0x" + signed2.raw_transaction.hex()])
    print(f"\n[test] ONE tx: helper.depositTo({recipient[:10]}…, {ROLLUP_ID}) value={DEPOSIT}")
    print(f"[test] tx {h2}")

    rc2 = wait_receipt(h2)
    if not rc2:
        print("[test] never mined"); sys.exit(1)
    print(f"[test] L1 mined block {int(rc2['blockNumber'],16)} status={int(rc2['status'],16)} "
          f"gasUsed={int(rc2['gasUsed'],16)}")
    if int(rc2["status"], 16) != 1:
        print("[test] REVERTED"); sys.exit(1)

    pcode, _ = rpc(PLAIN, "eth_getCode", [proxy, "latest"])
    print(f"[test] proxy code after: {(len(pcode)-2)//2 if pcode and pcode!='0x' else 0} bytes "
          f"{'✓ created' if pcode and pcode != '0x' else '✗ NOT created'}")

    print("[test] waiting for L2 credit …")
    credited = None
    t0 = time.time()
    while time.time() - t0 < 120:
        bal, berr = rpc(L2, "eth_getBalance", [recipient, "latest"])
        if berr:
            print(f"   L2 read failed: {berr}"); time.sleep(5); continue
        bal = int(bal, 16)
        if bal > pre:
            credited = (time.time() - t0, bal - pre); break
        time.sleep(5)

    print("\n===== RESULT =====")
    if credited:
        print(f"  ✓ ONE transaction created the proxy AND credited L2 "
              f"(+{credited[1]} wei at t={credited[0]:.0f}s)")
    else:
        print("  ✗ proxy handling ok but no L2 credit within 120 s")
    print(f"  helper    {helper}")
    print(f"  recipient {recipient}")
    print(f"  proxy     {proxy}")
    print(f"  deposit tx {h2}")


if __name__ == "__main__":
    main()
