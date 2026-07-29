# Composer stops posting batches to L1; twice in three hours, once taking the L2 down with it

## Summary

Two independent stalls on 2026-07-29. In both, `postAndVerifyBatch` submissions
from the batch poster stopped completely, having been running on a steady ~5 s
cadence immediately beforehand, and in both the **final batch succeeded** and the
poster account was healthy (funded, no pending/stuck transaction).

| | stall 1 | stall 2 |
|---|---|---|
| last batch | `11:16:15Z` (block 22313612) | `13:07:40Z` (block 22314803) |
| duration | ~76 min, then self-recovered | 27+ min at time of writing |
| L2 block production | kept running, smooth 0.99 blk/s | kept running, but in 10-block bursts every ~5 s (1.09 blk/s average) |
| poster balance | 85.3 xDAI | 133.6 xDAI |
| poster nonce | latest == pending (nothing stuck) | latest == pending (nothing stuck) |
| last batch status | `ok` | `ok` |

Both stalls have the same shape: **L1 attestation stops while the L2 keeps
producing blocks.** In stall 2 the L2's cadence also changed — from a smooth
1 blk/s to bursts of 10 blocks every ~5 s, with the head timestamp drifting to
~7 s behind wall-clock (it was ~1 s during stall 1). Average throughput stayed
normal.

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)) running load against the
chain; see "Correlation with load" below for what that does and does not imply.

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290, ~1 s blocks) |
| L1 | Gnosis Chiado (chainId 10200) |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| batch poster | `0x63A8eF9c0685767cB6d7B403a1af5b22f64c23d1` |
| `postAndVerifyBatch` selector | `0x8b1a095a` |

Deployed commit/configuration unknown to me.

## Stall 2 — evidence

Batches immediately before it stopped, filtered on both the
`postAndVerifyBatch` method **and** the poster address:

```
13:07:40Z  block 22314803  status ok      <- last one
13:07:35Z  block 22314802  status ok
13:07:30Z  block 22314801  status ok
13:07:25Z  block 22314800  status ok
13:07:20Z  block 22314799  status ok
13:07:15Z  block 22314798  status ok

interarrival immediately prior (s): 5,5,5,5,5,5,25,20,5,5,5
```

The L2 at the same time — sampled over 55 s, because a short sample is
misleading here:

```
t= 0.0s head=16615 +0     t=30.2s head=16645 +0
t= 5.1s head=16625 +10    t=35.2s head=16655 +10
t=10.1s head=16625 +0     t=40.2s head=16655 +0
t=15.1s head=16635 +10    t=45.2s head=16665 +10
t=20.1s head=16635 +0     t=50.3s head=16665 +0
t=25.2s head=16645 +10    t=55.3s head=16675 +10

net: +60 blocks in 55 s = 1.09 blk/s
head block timestamp lag behind wall-clock: 7 s
```

So the L2 is still producing at a normal average rate, but in 10-block bursts
rather than smoothly. (An initial 6-second sample showed +0 and I briefly read
that as "stopped"; it is not. Anyone reproducing should sample for at least a
minute.)

Poster account during the stall:

```
balance      133.6342 xDAI
nonce        latest = pending = 108683      (no stuck transaction)
last tx      postAndVerifyBatch, status ok
```

So it is not out of funds, not wedged behind a pending transaction, and its final
submission succeeded. It stopped submitting rather than failing and retrying.

## Stall 1 — evidence

Verified two independent ways so it was not an explorer artifact: a direct scan
of Chiado blocks `22313477..22313737` (261 blocks, ~22 min) filtering
`to == registry` found the most recent registry transaction at `11:16:15Z`, and
Blockscout agreed. A narrower scan of the most recent 60 Chiado blocks found
none.

Throughout, the L2 stayed healthy — six consecutive 5-second samples all showed
0.99 blk/s with the head timestamp within 1 s of wall-clock — and both
cross-chain fronts answered `eth_blockNumber` normally.

It resumed on its own at approximately `12:32Z`, about 76 minutes later, with no
intervention from us.

## Correlation with load

Both stalls occurred while an automated load generator was running against the
L2 (a mixture of value transfers, random-calldata transactions and cross-chain
deposits, tens of transactions per second).

I want to be careful about what that establishes:

- I have **not** run a controlled load-on/load-off repetition, so I cannot claim
  load causes the stall.
- Stall 1 did **not** recover when load was removed — it stayed stalled for a
  further ~35 minutes with zero external load, then recovered on its own. That
  argues against simple back-pressure.
- I have no composer logs, so I cannot tell whether the process is alive,
  crash-looping, blocked on a lock, or deliberately backing off.

## Why it is easy to miss

During stall 1 the L2 RPC and both cross-chain fronts stayed responsive and the
L2 kept producing blocks. Any health check based on RPC availability or L2 block
height would have reported the chain as healthy while nothing was being settled
on L1.

The signal that does catch it is **time since the last successful
`postAndVerifyBatch` from the poster** — which, as far as I can see, is not
exposed anywhere.

Note for anyone reproducing: filter on the method (or the `0x8b1a095a` selector)
**and** the poster address, not merely on transactions to the registry. Our first
recovery detector filtered on the destination address alone and reported a false
recovery, because our own `createCrossChainProxy` calls to the registry were
counted as composer batches.

## Questions

1. Is there a known condition under which the composer stops posting — a
   back-off, a queue or memory limit, a proving failure, or a deadlock?
2. Should it self-recover? Stall 1 did, after ~76 minutes. Is that an intended
   timeout, and is 76 minutes the expected order of magnitude?
3. Why does the L2 keep producing blocks in one case (stall 1) but stop in the
   other (stall 2)? Are L2 block production and L1 attestation independently
   supervised?
4. Would exposing "time since last successful L1 post" as a metric or health
   endpoint be worthwhile? It is the only signal we found that distinguishes this
   state from a healthy chain.

## Reproduction

Run sustained load against the L2 and sample, every ~30 s:

- L2 head via `eth_blockNumber`, to see whether block production continues;
- the age of the most recent `postAndVerifyBatch` **from the poster address**, by
  scanning recent L1 blocks (or an explorer, filtered on method **and** sender).

The stall appears as the batch age growing without bound. Whether the L2 head
also freezes appears to vary between occurrences.
