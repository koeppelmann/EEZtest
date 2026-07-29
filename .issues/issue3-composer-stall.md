# Composer stops posting batches to L1 while the L2 keeps producing blocks, and does not self-recover

## Summary

At `2026-07-29 11:16:15Z` the composer posted its last `postAndVerifyBatch` to
L1. As of `11:28Z` — **12 minutes later** — it had posted nothing further, while:

- the L2 continued producing blocks at a steady 0.99 blk/s,
- the L2 head timestamp stayed within 1 s of wall-clock (no drift),
- both cross-chain fronts (`:18999`, `:18998`) remained responsive to RPC.

So the L2 kept advancing state that L1 has no record of. The stall began while a
sustained load test was running against the L2, and did **not** recover in the
8 minutes after all load was removed.

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290, ~1 s blocks) |
| L1 x-chain front | `http://65.109.26.16:18999` |
| L2 x-chain front | `http://65.109.26.16:18998` |
| L1 | Gnosis Chiado (chainId 10200) |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| batch poster | `0x63A8eF9c0685767cB6d7B403a1af5b22f64c23d1` |
| `postAndVerifyBatch` selector | `0x8b1a095a` |

Deployed commit/configuration unknown to me.

## Evidence

Confirmed two independent ways, so this is not a Blockscout indexing artifact.

**1. Direct on-chain scan** of Chiado blocks `22313477..22313737` (261 blocks,
~22 minutes), reading each block's transactions and filtering on `to == registry`:

```
registry txs found in range : 88
most recent                 : block 22313612, 11:16:15Z, selector 0x8b1a095a
age at time of scan         : 12.1 minutes
```

A narrower scan of the most recent 60 Chiado blocks (~5 minutes) found **zero**
registry transactions.

**2. Blockscout** inbound transaction list for the registry — newest entry is
also `11:16:15Z` (block 22313612).

**Meanwhile the L2 is healthy.** Six consecutive 5-second samples:

```
+5 blocks in 5.0s = 0.99 blk/s | head timestamp lag behind wall-clock: 1s
+5 blocks in 5.0s = 0.99 blk/s | head timestamp lag behind wall-clock: 1s
+5 blocks in 5.0s = 0.99 blk/s | head timestamp lag behind wall-clock: 1s
+5 blocks in 5.0s = 0.99 blk/s | head timestamp lag behind wall-clock: 1s
+5 blocks in 5.0s = 0.99 blk/s | head timestamp lag behind wall-clock: 1s
```

and both fronts answered `eth_blockNumber` normally throughout
(`:18999` head 22313737, `:18998` head 8930).

## The poster account is healthy — this is not an L1-side resource problem

Checked at 11:33Z, ~17 minutes into the stall:

```
batch poster 0x63A8eF9c0685767cB6d7B403a1af5b22f64c23d1
  balance       : 85.307173 xDAI          (not out of funds)
  nonce latest  : 108186
  nonce pending : 108186                  (nothing stuck in the mempool)
  last tx       : 11:16:15Z postAndVerifyBatch -> status ok
```

So the poster has ample balance, has no pending/stuck transaction, and its final
batch **succeeded**. It did not fail and retry — it simply stopped submitting.
That points at the composer's internal batching path rather than funding, nonce
contention, or L1 inclusion.

## Timeline

```
11:08:33 – 11:15:33   idle measurement window; composer posting normally
                      (50 postAndVerifyBatch observed, ~1 per 8.4 s)
~11:16                sustained L2 load test restarted (ddos + fuzz workers)
11:16:15              ← last postAndVerifyBatch posted to L1
11:20:29              all load stopped
11:27:29              measurement window closes: 421 L2 blocks, 0 L2 txs, 0 L1 posts
11:28:16              still no L1 post; L2 still producing at 0.99 blk/s
```

Immediately before the stall the composer had been posting steadily — 50 batches
in the preceding 7-minute window, gaps predominantly 5 s.

## What this does not establish

- I have not shown that load *caused* the stall. The correlation is suggestive
  (it stopped as sustained load resumed) but I cannot see composer logs, and I
  did not run a controlled load-on/load-off repetition.
- I do not know whether the composer process is alive, crash-looping, stuck on a
  lock, or deliberately backing off. The fronts answering RPC shows the RPC layer
  is up; it does not show the batching path is.
- I do not know whether it eventually recovers. At the time of writing it had not
  after 12 minutes, 8 of them with zero load. (I am continuing to watch and will
  update this issue with the recovery time, or confirmation that it required a
  restart.)

## Why this matters

An L2 that keeps producing blocks while its L1 attestation is stalled is
advancing state that cannot be verified or settled on L1. Cross-chain operations
depending on posted batches cannot complete for the duration. Because the L2 RPC
and both fronts stay responsive, external monitoring that only health-checks
those endpoints would not notice.

## Questions

1. Is there a known condition under which the composer stops posting — a
   back-off, a queue limit, a nonce or funding problem on the poster account
   `0x63A8eF9c…`, or a proving failure?
2. Should it self-recover once load subsides? It did not within 8 minutes here.
3. Would a liveness metric or health endpoint reflecting "time since last
   successful L1 post" be worth exposing? That is the signal that actually
   indicates this failure; L2 block production and RPC availability do not.

## Reproduction

Run sustained L2 load (any high-rate transaction source) against the L2 while
sampling, every ~30 s:

- L2 head and head-timestamp lag via `eth_blockNumber` / `eth_getBlockByNumber`;
- the age of the most recent transaction to the registry, by scanning recent L1
  blocks for `to == EEZ_REGISTRY_ADDRESS` (do not rely on an explorer alone).

The stall shows up as the registry-transaction age growing without bound while
the L2 head continues to advance normally. The collector used here is
[`.issues/measure_idle.py`](https://github.com/koeppelmann/EEZtest/blob/main/.issues/measure_idle.py).
