## Two things, one of which I need to correct from the original filing

**Corrected framing.** This issue originally asked for the composer to process an
L1 transaction targeting a deterministic-but-undeployed CrossChainProxy. That was
not a well-posed request, and the correction matters:

- A bare value transfer is `to: <20 bytes>, data: 0x`. **Nothing distinguishes the
  CREATE2 proxy address of `(originalAddress, rollupId)` from an ordinary EOA.**
  Expecting general detection would mean inverting CREATE2 over an unbounded
  preimage space.
- There is one narrow checkable case — target ==
  `computeCrossChainProxyAddress(msg.sender, rollupId)`, i.e. depositing to *your
  own* proxy. That is a heuristic. It silently fails for a deposit to anyone
  else's proxy, and a heuristic that works sometimes is worse here than none.
- **Value sent to an undeployed proxy address cannot be credited retroactively.**
  Even if that proxy is deployed later, no deposit event ever occurred, so there
  is no record of the intended L2 recipient. The funds are stranded permanently.

So the ask is *not* "auto-detect bare transfers". It is (1) an explicit entrypoint
that carries the preimage, and (2) not accepting transactions that are then never
executed. The second part is a plain bug and is independent of all of the above.

---

## Part 1 (bug) — the L1→L2 ingress front accepts an L1 transaction and never executes it

A transaction submitted to the **L1→L2 ingress front** (Inbound,
`http://65.109.26.16:18999`) is accepted — `eth_sendRawTransaction` returns the
correct canonical hash — but was not returned by either queried endpoint during
the following 75 seconds, no receipt appeared, and the sender's nonce did not
advance. Submitting **the exact same raw bytes** to a plain Chiado RPC mines it in
~5 seconds.

Note this half has nothing to do with proxy detection: the transaction is an
explicit `createCrossChainProxy` **contract call**, whose semantics are entirely
unambiguous.

### Controlled endpoint replay (same raw bytes)

One transaction was signed; the same raw bytes were submitted to each endpoint in
turn. Gas is deliberately generous to exclude an under-gassing explanation.

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

**Step 1 — raw bytes to the front:**

```
POST http://65.109.26.16:18999   eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
error  : null
returned hash == locally computed canonical hash: true

t=  5s front=absent  plain=absent  pendingNonce=88
t= 40s front=absent  plain=absent  pendingNonce=88
t= 75s front=absent  plain=absent  pendingNonce=88
```

**Step 2 — the identical raw bytes to plain Chiado:**

```
POST https://rpc.chiadochain.net  eth_sendRawTransaction
result : 0x8b32285a5707db90eedbcde1136865c5784ac1bc11ae2f736aae554fd762c7f6
t=5s   MINED block 22313501  status=1  gasUsed=263431   (of an 800000 limit)
```

The signed transaction is byte-identical in both submissions. They were
sequential, so submission time and endpoint state also differ; nevertheless the
direct-Chiado result shows the transaction was well-formed, sufficiently funded
and sufficiently gassed. This isolates the behaviour to the submission path via
the front. It does **not** identify which internal stage — RPC front, admission
queue, bundle construction, or L1 submission — failed to retain or forward it.
Composer logs would be needed for that.

(`absent` = `eth_getTransactionByHash` returned null at that endpoint; it is not a
txpool dump, since `txpool_status`/`txpool_inspect` are not exposed here.)

### Nonce impact on clients that track nonces optimistically

Our client advanced its local nonce after the endpoint returned a hash. Because
the source-chain nonce did not advance, later submissions were refused, each off
by one:

```
invalid nonce 46 for 0x068340Fd9A8Ab7365a2D3D90b016e4CC8772fCF1:
    expected next unreserved nonce 45 (source-chain nonce 45)
```

Recoverable by resyncing to the nonce named in the error, but until the client
does so its subsequent transactions are blocked. This stalled a funding worker at
1 of 12 accounts until we added resync-and-retry.

---

## Part 2 (design question) — how *should* one deposit to a proxy that does not exist yet?

Given that bare transfers are undetectable, the workable shape is an explicit call
that carries `(originalAddress, rollupId)` — e.g. a payable registry entrypoint
along the lines of:

```solidity
function depositTo(address originalAddress, uint64 rollupId) external payable;
```

which deploys the proxy if absent and credits the L2 recipient. The preimage is
supplied rather than inferred, so there is no detection problem, and the deposit
is atomic with creation.

Whether that is worth adding is your call — the current explicit flow
(`createCrossChainProxy`, wait for the receipt, then deposit) works. Our runs
confirm it is the only variant that succeeds. The reason for raising it: the
two-step flow has a **failure mode that permanently destroys funds**, shown below.

### What currently happens to a deposit at an undeployed proxy

Two probes, each from its **own dedicated fresh account at nonce 0**, so neither
could be blocked behind the other's nonce.

| | probe A — via the front | probe B — via plain Chiado |
|---|---|---|
| endpoint | `http://65.109.26.16:18999` | `https://rpc.chiadochain.net` |
| sender | `0xEc3f27220efE28328Da9722a31Bdc70A95A519d4` | `0xDf2a7a819Ec290aAa6E2DCF0d28254C2e90011f6` |
| nonce | 0 | 0 |
| target (computed proxy) | `0x95DCD6bCD0e10Db9228e9854C58076Fd2e4c1E7A` | `0x589E58bC10a87084BcF35C750AfB898bEF92B0bc` |
| value | 1,000,000,000 wei | 1,000,000,000 wei |
| gas limit / price | 300,000 / 10,000,000,007 | 300,000 / 10,000,000,007 |
| calldata | `0x` | `0x` |
| tx hash | `0xe476139ed641db89ccc0842b60f878d3482c135277a35dd9cb686118ed6b69c0` | `0xbcc71c03726bc61324c848b967c429a1114ab6eb1f0777017ea6ef6e39f94817` |
| L1 result | not found on chain — never included | **mined**, block 22313449, status 1, gasUsed **21,000** |
| proxy code after | none | none |
| L2 credit | none | none |

Probe B is the important one: it mined as an **ordinary 21,000-gas value transfer
to an address with no code**. The value now sits at the computed proxy address
with no L2 credit — and, per the constraint above, it cannot be credited later
even if that proxy is subsequently deployed, because no deposit event ever
occurred. **Those funds are permanently lost.**

This is a foot-gun rather than a protocol bug: the user did something the protocol
never promised to support. But it is silent and irreversible, which is why it may
be worth either an explicit entrypoint (Part 2) or a documented warning.

---

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290, ~1 s blocks) |
| L1→L2 ingress front (Inbound) | `http://65.109.26.16:18999` |
| L1 | Gnosis Chiado (chainId 10200), RPC `https://rpc.chiadochain.net` |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| registry deploy block | 22312162 |
| rollupId | `1` — from `ROLLUP_MGR.rollupId()` on `0xca2189a9b1c47e05587c87a5f08e07b628382a1b`; note this is the rollup id, **not** the L2 chain id (6290) |
| observed | 2026-07-29, ~11:05 UTC |

Deployed commit/configuration unknown to me; if any of this is configuration
rather than code, say so and I will retest.

## Questions

1. **Part 1:** if the front accepts a transaction it cannot submit or compose,
   could it reject synchronously, or expose a failure status? Right now the caller
   holds a hash for something that never executed.
2. Should ordinary (non-cross-chain) L1 calls such as `createCrossChainProxy` be
   sent to the front at all? If it only handles cross-chain-shaped transactions,
   rejecting everything else with a clear error would remove this failure mode
   entirely. Documenting which endpoint to use for proxy creation would also help.
3. **Part 2:** is an explicit `depositTo(originalAddress, rollupId)`-style
   entrypoint of interest, given bare transfers are undetectable and the current
   two-step flow can strand funds irrecoverably?

## Reproducing Part 1 without our tooling

```python
import time, requests
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
    b = r.json()
    return b.get("result"), b.get("error")

n = int(rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"])[0], 16)
target = to_checksum_address("0x" + "7e"*20)
data = "0x2dd72120" + encode(["address","uint256"], [target, 1]).hex()
signed = acct.sign_transaction({"to":REG,"value":0,"data":data,"gas":800000,
                                "gasPrice":3*10**9,"nonce":n,"chainId":10200})
raw = "0x" + signed.raw_transaction.hex()
h   = "0x" + keccak(signed.raw_transaction).hex()

print("front:", rpc(FRONT, "eth_sendRawTransaction", [raw]))
for elapsed in range(5, 76, 5):                      # observe the full window
    time.sleep(5)
    print(elapsed,
          rpc(FRONT, "eth_getTransactionByHash", [h])[0],
          rpc(PLAIN, "eth_getTransactionByHash", [h])[0],
          rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"])[0])

print("plain:", rpc(PLAIN, "eth_sendRawTransaction", [raw]))   # same bytes -> mines in ~5 s
```

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).
