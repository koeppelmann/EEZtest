#!/usr/bin/env python3
"""Idle-window measurement: L1 batch posting while the L2 produces no transactions.

Correctness properties this collector deliberately has:

  * L2 block transaction counts are read per block, and a read FAILURE is counted
    as a failure — never silently treated as zero.
  * The L1 side is fetched with FULL Blockscout keyset pagination, following
    next_page_params until the returned page predates the window start.  A single
    unpaginated request returns at most 50 items, so an exact count of 50 is a
    truncation artifact rather than a measurement.
  * Every candidate L1 transaction is verified to be postAndVerifyBatch by its
    4-byte selector read from the chain, not assumed from the destination address.
  * Receipt status is checked, so successful / reverted / unreadable are reported
    separately instead of all being called "successful".

Usage:  python measure_idle.py [window_seconds]
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime
import sys
import time

import requests

S = requests.Session()
L2 = "http://65.109.26.16:18688"
L1 = "https://rpc.chiadochain.net"
BLOCKSCOUT = "https://gnosis-chiado.blockscout.com"
REG = "0xf0656341956d83d047c5e26678130e453952f32c"
POST_AND_VERIFY_BATCH_SELECTOR = "0x8b1a095a"
WINDOW = int(sys.argv[1]) if len(sys.argv) > 1 else 420


def rpc(url: str, method: str, params: list) -> object:
    r = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    return r.json().get("result")


def fetch_all_registry_txs(oldest_needed_ts: float) -> list[dict]:
    """Page through Blockscout until we are strictly older than the window start."""
    out: list[dict] = []
    seen: set[str] = set()
    params: dict = {"filter": "to"}
    pages = 0
    while True:
        r = S.get(f"{BLOCKSCOUT}/api/v2/addresses/{REG}/transactions", params=params, timeout=25)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", [])
        pages += 1
        if not items:
            break
        oldest_on_page = None
        for t in items:
            h = t.get("hash")
            if h and h not in seen:
                seen.add(h)
                out.append(t)
            ts = datetime.datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")).timestamp()
            oldest_on_page = ts if oldest_on_page is None else min(oldest_on_page, ts)
        nxt = body.get("next_page_params")
        if not nxt or (oldest_on_page is not None and oldest_on_page < oldest_needed_ts):
            break
        params = {"filter": "to", **nxt}
        if pages > 40:  # safety stop
            print("  WARNING: pagination safety stop hit")
            break
    print(f"  (blockscout: {pages} page(s), {len(out)} unique txs fetched)")
    return out


def main() -> None:
    t0 = time.time()
    start_iso = datetime.datetime.now(datetime.timezone.utc)
    h0 = int(rpc(L2, "eth_blockNumber", []), 16)
    print(f"WINDOW START {start_iso:%Y-%m-%dT%H:%M:%S}Z  L2 block {h0}")
    print(f"observing {WINDOW}s of idle L2 ...", flush=True)
    time.sleep(WINDOW)
    h1 = int(rpc(L2, "eth_blockNumber", []), 16)
    t1 = time.time()
    end_iso = datetime.datetime.now(datetime.timezone.utc)
    print(f"WINDOW END   {end_iso:%Y-%m-%dT%H:%M:%S}Z  L2 block {h1}")

    # ── L2: per-block tx counts, read failures reported ──────────────────────
    def txc(bn: int) -> tuple[int, int]:
        try:
            r = S.post(L2, json={"jsonrpc": "2.0", "id": 1,
                                 "method": "eth_getBlockTransactionCountByNumber",
                                 "params": [hex(bn)]}, timeout=8)
            r.raise_for_status()
            res = r.json().get("result")
            return bn, (int(res, 16) if res is not None else -1)
        except Exception:
            return bn, -1

    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        res = list(ex.map(txc, range(h0, h1 + 1)))
    failed = [b for b, c in res if c < 0]
    counts = [c for b, c in res if c >= 0]
    print(f"\nL2: inspected {len(res)} blocks ({h0}..{h1})")
    print(f"    read failures        : {len(failed)}")
    print(f"    blocks with txs      : {sum(1 for c in counts if c > 0)}")
    print(f"    total L2 transactions: {sum(counts)}")

    # ── L1: paginate, verify selector, verify status ─────────────────────────
    print("\nL1: fetching registry transactions (paginated)")
    allx = fetch_all_registry_txs(t0)
    inwin = []
    for t in allx:
        ts = datetime.datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")).timestamp()
        if t0 <= ts <= t1:
            inwin.append((ts, t))
    inwin.sort()
    print(f"  inbound registry txs inside window: {len(inwin)}")

    def classify(item):
        ts, t = item
        h = t["hash"]
        try:
            tx = rpc(L1, "eth_getTransactionByHash", [h])
            rc = rpc(L1, "eth_getTransactionReceipt", [h])
            sel = (tx or {}).get("input", "")[:10]
            status = int(rc["status"], 16) if rc and rc.get("status") is not None else None
            gas = int(rc["gasUsed"], 16) if rc and rc.get("gasUsed") else None
            return ts, h, sel, status, gas, t.get("block_number")
        except Exception as exc:
            return ts, h, f"ERR:{exc}", None, None, t.get("block_number")

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        classified = list(ex.map(classify, inwin))

    batches = [c for c in classified if c[2] == POST_AND_VERIFY_BATCH_SELECTOR]
    other = [c for c in classified if c[2] != POST_AND_VERIFY_BATCH_SELECTOR]
    ok = [c for c in batches if c[3] == 1]
    reverted = [c for c in batches if c[3] == 0]
    unknown = [c for c in batches if c[3] is None]

    print(f"  matching postAndVerifyBatch ({POST_AND_VERIFY_BATCH_SELECTOR}): {len(batches)}")
    print(f"    successful : {len(ok)}")
    print(f"    reverted   : {len(reverted)}")
    print(f"    unreadable : {len(unknown)}")
    if other:
        sels: dict[str, int] = {}
        for c in other:
            sels[c[2]] = sels.get(c[2], 0) + 1
        print(f"  excluded non-batch inbound txs: {len(other)}  selectors={sels}")

    if ok:
        ok.sort()
        gases = [c[4] for c in ok if c[4]]
        gaps = [ok[i][0] - ok[i - 1][0] for i in range(1, len(ok))]
        print(f"\n  successful batches: {len(ok)}")
        print(f"  gas: min={min(gases)} max={max(gases)} avg={sum(gases)/len(gases):,.0f}")
        if gaps:
            from collections import Counter
            dist = Counter(round(g) for g in gaps)
            print(f"  interarrival: min={min(gaps):.0f}s max={max(gaps):.0f}s mean={sum(gaps)/len(gaps):.2f}s")
            print(f"  gap distribution: {dict(sorted(dist.items()))}")
            print(f"  n_gaps={len(gaps)}")
        dur = t1 - t0
        print(f"  aggregate: {len(ok)} posts / {dur:.0f}s = 1 per {dur/len(ok):.2f}s")
        print(f"  L2 blocks per post: {(h1-h0)/len(ok):.2f}")
        print("\n  all successful batch txs:")
        for ts, h, sel, st, gas, blk in ok:
            print(f"    {datetime.datetime.fromtimestamp(ts, datetime.timezone.utc):%H:%M:%S}Z L1blk={blk} gas={gas} {h}")


if __name__ == "__main__":
    main()
