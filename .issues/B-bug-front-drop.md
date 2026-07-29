## Summary

`eth_sendRawTransaction` to the **L1→L2 ingress front** (`:18999`) returned the
correct canonical transaction hash, but for the following 75 seconds neither
queried endpoint returned the transaction, no L1 receipt appeared, and the
sender's pending nonce did not advance. Submitting the exact same raw bytes to a
plain Chiado RPC afterward produced an L1 receipt in about five seconds.

The tested transaction calls `createCrossChainProxy(address,uint256)` on the L1
registry. It is not a bare value transfer; its destination and calldata are shown
below. I do not know whether this method is within the ingress front's supported
transaction set.

## Reproduction — same raw bytes, two endpoints

One transaction was signed. The same raw bytes were submitted to each endpoint in
turn, so both submissions share one canonical hash.

```
from      0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1
to        0xF0656341956d83d047C5e26678130E453952F32C   (EEZ registry)
nonce     88
value     0
gas       800000
gasPrice  3000000000
chainId   10200
input     0x2dd72120
          0000000000000000000000007e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e
          0000000000000000000000000000000000000000000000000000000000000001
          createCrossChainProxy(0x7e7e…7e7e, rollupId=1)
hash      0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
```

**To the ingress front:**

```
POST http://65.109.26.16:18999   eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
error  : null
returned hash == locally computed canonical hash: true

polling every 5 s for 75 s:
  t= 5s  front=absent  plain=absent  pendingNonce=88
  t=40s  front=absent  plain=absent  pendingNonce=88
  t=75s  front=absent  plain=absent  pendingNonce=88
```

**Then the identical raw bytes to plain Chiado:**

```
POST https://rpc.chiadochain.net  eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
t=5s   MINED block 22313501  status=1  gasUsed=263431
```

Gas limit 800,000 against `gasUsed` 263,431, so this is not an under-gassing
issue. `absent` means `eth_getTransactionByHash` returned null at that endpoint;
`txpool_status`/`txpool_inspect` are not exposed here, so this is not a txpool
dump.

The submissions were sequential, so time and endpoint state also differ, and
because the direct submission then mined the transaction, this test cannot show
whether the front path would eventually have submitted it. What the direct leg
establishes is that the transaction was well-formed, funded and sufficiently
gassed.

I cannot identify which internal stage — RPC front, admission queue, bundle
construction, or L1 submission — prevented observable inclusion during that
window. Composer logs would be needed to locate it.

## Side effect: client-side nonce desynchronisation

In a separate EEZtest run, the same accept-without-observable-submission
behaviour caused a client-side nonce desynchronisation. EEZtest optimistically
advanced its local nonce after receiving a hash; the source-chain nonce had not
advanced, so its next submission was one nonce ahead:

```
invalid nonce 46 for 0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1:
    expected next unreserved nonce 45 (source-chain nonce 45)
```

Recoverable by resyncing to the nonce named in the error, but until the client
does so its later transactions are blocked. This stalled a funding worker at 1 of
12 accounts until we added resync-and-retry.

## Suggested behaviour

If this transaction class is unsupported, the front should reject it
synchronously with an actionable error rather than return a hash. If it is
supported, an accepted transaction should either progress to submission or expose
a later failure state; during this test the caller could not distinguish queued
work from a request the ingress path would not process.

A related question: should ordinary L1 calls such as `createCrossChainProxy` be
sent to the ingress front at all? Documenting which endpoint to use for proxy
creation would help either way.

## Environment

| item | value |
|---|---|
| L1→L2 ingress front (Inbound) | `http://65.109.26.16:18999` |
| L1 | Gnosis Chiado (chainId 10200), RPC `https://rpc.chiadochain.net` |
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290) |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| observed | 2026-07-29, ~11:05 UTC |

Deployed commit/configuration unknown to me; if this is configuration rather than
code, say so and I will retest.

## Standalone reproduction

Uses a fresh target each run, so the proxy is absent before the call. Raises on
any JSON-RPC error rather than reporting it as an absent transaction.

```python
import time, requests
from eth_account import Account
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

FRONT = "http://65.109.26.16:18999"
PLAIN = "https://rpc.chiadochain.net"
REG   = to_checksum_address("0xf0656341956d83d047c5e26678130e453952f32c")
acct  = Account.from_key("0x<funded chiado key>")

def rpc(url, method, params):
    response = requests.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error") is not None:
        raise RuntimeError(f"{url} {method}: {body['error']}")
    if "result" not in body:
        raise RuntimeError(f"{url} {method}: missing result: {body}")
    return body["result"]

target = Account.create().address                      # fresh target every run
data   = "0x2dd72120" + encode(["address", "uint256"], [target, 1]).hex()
proxy  = "0x" + rpc(PLAIN, "eth_call",
                    [{"to": REG, "data": "0xb761ba7e" +
                      encode(["address", "uint256"], [target, 1]).hex()}, "latest"])[-40:]
assert rpc(PLAIN, "eth_getCode", [proxy, "latest"]) == "0x", "proxy already exists"

n = int(rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"]), 16)
signed = acct.sign_transaction({"to": REG, "value": 0, "data": data, "gas": 800000,
                                "gasPrice": 3 * 10**9, "nonce": n, "chainId": 10200})
raw = "0x" + signed.raw_transaction.hex()
h   = "0x" + keccak(signed.raw_transaction).hex()

print("front:", rpc(FRONT, "eth_sendRawTransaction", [raw]))
for elapsed in range(5, 76, 5):
    time.sleep(5)
    print(elapsed,
          rpc(FRONT,  "eth_getTransactionByHash",   [h]),
          rpc(PLAIN,  "eth_getTransactionByHash",   [h]),
          rpc(PLAIN,  "eth_getTransactionReceipt",  [h]),
          rpc(PLAIN,  "eth_getTransactionCount",    [acct.address, "pending"]))

print("plain:", rpc(PLAIN, "eth_sendRawTransaction", [raw]))
receipt = None
for elapsed in range(1, 31):
    time.sleep(1)
    receipt = rpc(PLAIN, "eth_getTransactionReceipt", [h])
    if receipt is not None:
        print("mined after", elapsed, "s", receipt)
        break
if receipt is None:
    raise RuntimeError("no receipt within 30 seconds after direct submission")
if int(receipt["status"], 16) != 1:
    raise RuntimeError(f"direct replay reverted: {receipt}")
```

## What this is not

For clarity, since these were initially conflated: the normal L1→L2 cross-chain
path **works**. With the proxy pre-created, a deposit through the same front
credited L2 in 10 s and settled on L1 in 15 s (block 22314436, status 1). This
issue is only about an accepted request that produced no observable L1 submission
during the measured window.

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).
