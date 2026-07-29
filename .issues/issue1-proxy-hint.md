# Support L1 txs targeting a deterministic, not-yet-deployed CrossChainProxy — and don't accept txs that are never executed

## What we'd like to be able to do

Send the composer an L1 transaction that targets a **deterministic but not yet
deployed** CrossChainProxy, and have the composer process it — deploying the
proxy as needed. Since the proxy address is `CREATE2(originalAddress, rollupId)`
and those inputs are not feasibly recoverable from the address alone, this
presumably needs an additional **hint** carried with the transaction (target
address + target rollupId) that the composer uses to deploy the proxy before
executing the call.

Today neither half of that works: the deposit is not processed, and — separately
— a transaction submitted to the front can be accepted and then never executed,
with no error surfaced.

## Summary

A transaction submitted to the inbound x-chain front
(`http://65.109.26.16:18999`) is accepted — `eth_sendRawTransaction` returns the
correct canonical transaction hash — but was not returned by either queried endpoint during the
following 75 seconds: no receipt appeared and the sender's nonce did not advance. Submitting **the exact
same raw transaction bytes** to a plain Chiado RPC mines it in ~5 seconds.

Two related questions follow:

1. Should the front accept a transaction it does not submit? Today there is no
   later error or status channel, so the caller has a hash for something that
   was not observed during that window.
2. Is a deposit to a **deterministic but not-yet-deployed** CrossChainProxy meant
   to be supported? A bare value transfer carries only the proxy address, and the
   `(originalAddress, rollupId)` CREATE2 inputs are not feasibly recoverable from
   the proxy address alone without an external mapping or supplied preimage, so
   implicit creation would need an additional mechanism (a hint carried with the
   request, a registry, or a wrapper call). If it is not
   intended, the request should be rejected synchronously with an actionable
   error.

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290, ~1 s blocks) |
| L1 x-chain front (inbound) | `http://65.109.26.16:18999` |
| L1 | Gnosis Chiado (chainId 10200), RPC `https://rpc.chiadochain.net` |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| registry deploy block | 22312162 |
| rollupId used | `1` — read from `ROLLUP_MGR.rollupId()` on `0xca2189a9b1c47e05587c87a5f08e07b628382a1b`; note this is the rollup id and is **not** the L2 chain id (6290) |
| observed | 2026-07-29, ~11:05 UTC |

I do not know the deployed commit; if the behaviour below is configuration
rather than a code defect, please say so and I will re-test against a different
configuration.

## Reproduction 1 — controlled endpoint replay (same raw bytes)

One transaction was signed. The **same raw bytes** were submitted first to the
front, then to plain Chiado. Gas limit is deliberately generous to exclude an
under-gassing explanation.

```
from      0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1
to        0xF0656341956d83d047C5e26678130E453952F32C     (registry)
nonce     88
value     0
gas       800000
gasPrice  3000000000            (3 gwei)
chainId   10200
input     0x2dd72120
          0000000000000000000000007e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e
          0000000000000000000000000000000000000000000000000000000000000001
          (createCrossChainProxy(0x7e7e…7e7e, rollupId=1))
hash      0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
```

**Step 1 — submit raw to the front:**

```
POST http://65.109.26.16:18999   eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
error  : null
returned hash == locally computed canonical hash: true
```

Polling both endpoints every 5 s for 75 s:

```
t=  5s front=absent  plain=absent  pendingNonce=88
t= 40s front=absent  plain=absent  pendingNonce=88
t= 75s front=absent  plain=absent  pendingNonce=88
```

(`absent` = `eth_getTransactionByHash` returned null at that endpoint. This
shows the queried endpoints did not return it; it is not a txpool dump, since
`txpool_status` / `txpool_inspect` are not exposed here.)

**Step 2 — submit the identical raw bytes to plain Chiado:**

```
POST https://rpc.chiadochain.net  eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
t=5s   MINED block 22313501  status=1  gasUsed=263431   (of an 800000 limit)
```

The signed transaction is byte-identical in both submissions. They were
sequential, so submission time and endpoint state also differ; nevertheless the
direct-Chiado result demonstrates the transaction was well-formed, sufficiently
funded and sufficiently gassed (`gasUsed` 263,431 against an 800,000 limit rules
out an insufficient gas limit). During the preceding 75-second window the front
path returned the canonical hash, yet neither queried endpoint returned the
transaction and the source-chain nonce stayed at 88.

This isolates the behaviour to the submission path and/or its state at that time.
It does **not** identify which internal stage — RPC front, admission queue,
bundle construction, or L1 submission — failed to retain or forward it. Composer
logs would be needed for that.

## Reproduction 2 — deposit to an undeployed proxy

Compute the L1 proxy for a fresh address with
`computeCrossChainProxyAddress(addr, 1)` (selector `0xb761ba7e`), confirm
`eth_getCode` is `0x`, then send a dust deposit to it.

Two probes were run, each from its **own dedicated fresh account at nonce 0**, so
neither could be blocked behind the other's nonce.

| | probe A — via the front | probe B — via plain Chiado |
|---|---|---|
| endpoint | `http://65.109.26.16:18999` | `https://rpc.chiadochain.net` |
| sender | `0xEc3f27220efE28328Da9722a31Bdc70A95A519d4` | `0xDf2a7a819Ec290aAa6E2DCF0d28254C2e90011f6` |
| nonce | 0 | 0 |
| computed proxy (target of the tx) | `0x95DCD6bCD0e10Db9228e9854C58076Fd2e4c1E7A` | `0x589E58bC10a87084BcF35C750AfB898bEF92B0bc` |
| value | 1,000,000,000 wei | 1,000,000,000 wei |
| gas limit | 300,000 | 300,000 |
| gas price | 10,000,000,007 wei | 10,000,000,007 wei |
| calldata | `0x` (bare transfer) | `0x` (bare transfer) |
| chainId | 10200 | 10200 |
| tx hash | `0xe476139ed641db89ccc0842b60f878d3482c135277a35dd9cb686118ed6b69c0` | `0xbcc71c03726bc61324c848b967c429a1114ab6eb1f0777017ea6ef6e39f94817` |
| observation | >60 s | >60 s |
| L1 result | **not found on chain** — never included | **mined** block 22313449, status 1, gasUsed **21,000** |
| proxy code after | none | none |
| L2 credit | none | none |

Probe B is the more surprising outcome: the transaction mined as an ordinary
21,000-gas value transfer to an address that has no code, so the 1 gwei of value
now sits at the computed proxy address with no L2 credit and no proxy deployed.
(It would presumably become reachable if that proxy were later deployed at the
same CREATE2 address, but as of now it is stranded.)

Verified after the fact via `eth_getTransactionByHash`, `eth_getTransactionReceipt`
and `eth_getCode` on `https://rpc.chiadochain.net`.

For contrast, the explicit flow — call `createCrossChainProxy(target, 1)` **on
plain Chiado**, wait for the receipt and confirm code exists, then deposit —
completes and the L2 credit appears. That is the only variant that succeeded in
our runs.

## Nonce impact on clients that track nonces optimistically

EEZtest advanced its local nonce after the endpoint returned a hash. Because the
source-chain nonce did not advance, later submissions used higher nonces and were
refused, each off by one:

```
invalid nonce 46 for 0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1:
    expected next unreserved nonce 45 (source-chain nonce 45)
invalid nonce 47 for 0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1:
    expected next unreserved nonce 46 (source-chain nonce 46)
```

So an accepted-but-unsubmitted transaction creates a nonce gap for any client
that optimistically advances local state. This is recoverable — resynchronising
to the nonce named in the error, or resubmitting the missing nonce, clears it —
but until the client does so its subsequent transactions are blocked. In our run
this stalled a funding worker at 1 of 12 accounts until we added
resync-and-retry.

In this run the front rejected the higher-nonce submissions shown above with
`expected next unreserved nonce N` rather than retaining them for later
execution. If the endpoint intentionally requires strictly contiguous nonce
submission, documenting that policy would help clients choose a nonce strategy.
(I did not run a separate controlled pipelining test, so I am reporting only what
these rejections showed.)

## Questions / suggested behaviour

1. Are deposits to an undeployed deterministic proxy intended to be supported?
   If **not**, please reject them synchronously with a "create the proxy first"
   error. If **yes**, the protocol needs a way to supply `(originalAddress,
   rollupId)` — e.g. a hint accompanying the transaction — since they are not feasibly
   recoverable from the proxy address alone without an external mapping or
   supplied preimage.
2. Should ordinary (non-cross-chain) L1 calls such as `createCrossChainProxy` be
   submitted to the front at all? If the front is only for cross-chain-shaped
   transactions, rejecting everything else with a clear error would prevent this
   whole failure mode. Documenting which endpoint to use for proxy creation would
   also help.
3. If the front accepts a transaction it cannot submit or compose, could it
   expose a failure status or error rather than leaving the caller with a hash
   and no further signal?

## Reproducing without our tooling

Standalone script (no EEZtest dependency) — substitute a funded Chiado key:

```python
import requests
from eth_account import Account
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

FRONT = "http://65.109.26.16:18999"
PLAIN = "https://rpc.chiadochain.net"
REG   = to_checksum_address("0xf0656341956d83d047c5e26678130e453952f32c")
acct  = Account.from_key("0x<funded chiado key>")

def rpc(url, m, p):
    r = requests.post(url, json={"jsonrpc":"2.0","id":1,"method":m,"params":p}, timeout=15)
    r.raise_for_status()
    body = r.json()
    return body.get("result"), body.get("error")

n = int(rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"])[0], 16)
target = to_checksum_address("0x" + "7e"*20)
data = "0x2dd72120" + encode(["address","uint256"], [target, 1]).hex()   # createCrossChainProxy
signed = acct.sign_transaction({"to":REG,"value":0,"data":data,"gas":800000,
                                "gasPrice":3*10**9,"nonce":n,"chainId":10200})
raw = "0x" + signed.raw_transaction.hex()
h   = "0x" + keccak(signed.raw_transaction).hex()

import time
print("front:", rpc(FRONT, "eth_sendRawTransaction", [raw]))
for elapsed in range(5, 76, 5):                      # observe the full 75 s window
    time.sleep(5)
    print(elapsed,
          rpc(FRONT, "eth_getTransactionByHash", [h])[0],
          rpc(PLAIN, "eth_getTransactionByHash", [h])[0],
          rpc(PLAIN, "eth_getTransactionReceipt", [h])[0],
          rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"])[0])

print("plain:", rpc(PLAIN, "eth_sendRawTransaction", [raw]))   # same bytes -> mines in ~5 s
```
