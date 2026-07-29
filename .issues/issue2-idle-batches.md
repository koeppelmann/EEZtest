# Question: can L1 batch posting be throttled while L2 blocks contain no transactions?

## Summary

Over a 420-second window in which **416 inspected L2 blocks contained zero
transactions**, the composer submitted **50 successful `postAndVerifyBatch`
transactions** to L1, at ~207.6k gas each. The dominant interarrival is exactly
**5 s** (43 of 49 gaps), giving an aggregate of one post per 8.4 s, or one post
per 8.3 L2 blocks.

If that rate persisted for a day it would consume roughly **2.14 billion L1 gas**
(a projection from a 7-minute sample, not a measured daily total).

Is this cadence required for finality, timestamp progression, forced inclusion,
or another protocol invariant? If not, could the composer fall back to a
configurable slower heartbeat while no L2 transactions have appeared since the
last post, returning to normal policy as soon as activity resumes?

Found by an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290, ~1 s blocks) |
| L1 | Gnosis Chiado (chainId 10200) |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| batch poster | `0x63A8eF9c0685767cB6d7B403a1af5b22f64c23d1` |
| L1 source | `https://gnosis-chiado.blockscout.com` (registry inbound tx list) |
| L2 source | `eth_getBlockTransactionCountByNumber` per block, direct JSON-RPC |
| window | 2026-07-29 11:08:33Z → 11:15:33Z (420 s) |

I do not know the deployed commit or the composer's posting configuration. If
this cadence is configured rather than inherent, please say so — the question
then becomes what the sensible idle default is.

## Measurement

All of our own load generators were stopped for this window, so the L2 was
genuinely idle. Every L2 block in the range was inspected individually; read
failures were counted rather than silently treated as zero (there were none).

```
WINDOW START 2026-07-29T11:08:33Z   L2 block 7735
WINDOW END   2026-07-29T11:15:33Z   L2 block 8150

L2: inspected 416 blocks (7735..8150), read-failures = 0
    blocks containing transactions : 0
    total L2 transactions          : 0

L1 postAndVerifyBatch in the same wall-clock window : 50
    gas: min 207,542   max 208,000   avg 207,577
    interarrival: min 5 s, max 40 s, mean 6.9 s
    interarrivals (s): 5,5,5,5,5,5,5,5,5,5,5,5,40,5,5,5,5,5,5,5,5,5,5,5,5,
                       5,5,5,5,25,5,5,25,5,5,5,5,5,5,5,5,5,5,25,5,5,5,5,5
    aggregate: 50 posts / 420 s = 1 per 8.4 s  (8.3 L2 blocks per post)
```

First and last of the 50 (full list available on request):

```
11:09:15Z  L1blk 22313536  gas 207561  0xd5b994dc4ee7b6a7592e8f1ed04617b0d31fde29a661399e76e42e7b9761606c
11:09:20Z  L1blk 22313537  gas 207561  0x03e7b3181315cd752a4b1e14773d2710092ed774cbb80c35c97c776fc9349d4b
…
11:14:50Z  L1blk 22313599  gas 207552  0x547c28492670de7ee9b25d5668cb8d8fcbac3134390137ec194030ef6a73cc52
11:14:55Z  L1blk 22313600  gas 207552  0x82ec6811e5b9c97f56a8ffd7d0b71a5d2fe6a68c47c0230250a29d97e85d525b
```

The measurement script is
[`.issues/measure_idle.py`](https://github.com/koeppelmann/EEZtest/blob/main/.issues/measure_idle.py)
and needs only `requests`.

## Short-sample extrapolation

Assuming the observed 420-second aggregate rate persists. These are projections,
not measured totals.

| metric | value |
|---|---|
| projected posts / hour | ~429 |
| projected L1 gas / hour | ~89.0M |
| projected L1 gas / day | ~2.14B |
| projected native gas token / day @ 1.5 gwei | ~3.20 |
| projected native gas token / day @ 3 gwei | ~6.41 |

Chiado is a testnet, so this is native-token consumption at assumed gas prices
rather than an economic cost.

With one post per 200 L2 blocks (~200 s at 1 s blocks) this becomes 18 posts/hour
— about a **24× reduction** on the observed rate. `N = 200` is illustrative, not
a proposed universal default.

## What this does not establish

- I did not decode the batch payloads or compare state roots, so "zero L2
  transactions" does not by itself prove the posts carry no useful state
  transition. They may deliberately advance roots, timestamps, finality or
  forced-inclusion windows.
- 420 seconds is a short sample. It is consistent with a separate, less
  instrumented 90-second window earlier the same day (7 posts, 91 L2 blocks, 0
  L2 transactions), but neither establishes long-run behaviour.

## Question and possible optimization

1. What protocol function do these posts serve while the L2 is idle? If they are
   a liveness heartbeat, could the heartbeat interval be decoupled from the busy
   cadence and made configurable?
2. If nothing requires the current rate, could the composer post at most once per
   N L2 blocks when no L2 transaction has appeared since the last post, and
   return to normal cadence immediately on the next transaction so latency for
   real activity is unaffected?
3. Would that interval want to differ between a 1 s/5 s devnet and a 12 s
   network? If so, exposing it as configuration would let both be tuned.
