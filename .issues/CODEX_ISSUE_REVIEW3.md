# Review of issue 3 — composer posting stall

**Verdict: DO NOT FILE in the current form.**

The draft contains a useful and concerning observation: no transaction to the stated registry was found after 11:16:15Z while the queried L2 kept producing blocks. But it promotes that observation into “the composer stopped posting,” “did not self-recover,” and protocol settlement consequences without establishing the composer's complete set of valid L1 posting routes. The direct scan is also not reproducible or auditable as written: there is no collector, no read-failure count, and no evidence that all returned transactions were classified by selector, sender, and receipt status. Those are fixable. Once fixed, this should be a narrowly framed liveness report or operational question.

## Blocking evidence gaps

### 1. The evidence proves absence at one registry, not absence of every composer post

Scanning all transactions in Chiado blocks and filtering `to == 0xf065…f32c` can establish that no transaction targeted that address during the scanned suffix, provided every block read succeeded. It cannot by itself rule out:

- a posting transaction to a different registry or verifier contract;
- a deployment/configuration change that switched the destination;
- a different batch-posting method routed through another contract;
- a private/bundled transaction that was attempted but never included;
- batches being produced or persisted off-chain without an L1 submission.

A different sender posting to the **same** registry would have appeared in this scan, so poster rotation alone does not explain zero registry transactions. A different destination would.

Before filing, add one of the following:

- deployed configuration or composer logs showing that this exact registry was the sole configured L1 batch destination throughout the window; or
- a broader L1 scan keyed to all known batch destination addresses/events/calldata and all known poster accounts, with the routing assumptions explicitly stated.

Until then, replace every global “composer stopped posting” claim with the observed fact:

> no transaction to the configured registry was observed on Chiado during the stated window

### 2. The direct block scan has no failure accounting

The issue says each block's transactions were read, but does not identify the script, RPC, request mode, error handling, or number of successful and failed block reads. A common faulty scanner catches an RPC exception or `null` block and treats it as an empty block. Blockscout agreement is useful corroboration, but it is not an independent substitute for proving that the direct scan completed.

The scan output must include:

> requested blocks: 261  
> successfully decoded blocks: 261  
> null responses: 0  
> RPC/HTTP/JSON failures: 0  
> transactions inspected: N  
> transactions to registry: N  
> matching selector `0x8b1a095a`: N  
> successful / reverted / unreadable matching calls: N / N / N  
> latest matching call: hash, sender, block, timestamp, receipt status

The scanner should call `raise_for_status()`, reject JSON-RPC error objects and `null` blocks, retry bounded transient failures, and fail the result rather than treating an unreadable block as empty. Publish or link that exact scanner and its raw output.

The draft should also provide the hash of the alleged last successful call, not only its block/time, and list the exact RPC used for the direct scan.

### 3. “Most recent registry transaction” is not always equivalent to “most recent successful batch”

The broad scan filters only on destination according to the description. It must separately verify input selector `0x8b1a095a`, sender, and receipt status. The line at block 22313612 includes a selector and the account section says status OK, but the issue does not show how either was obtained or whether later calls to other batch-related destinations were considered.

Replace the opening of the Evidence section with:

> Within the observed deployment route, I found no successful call with selector `0x8b1a095a` to registry `0xf065…f32c` after transaction `<full hash>` in block 22313612 at 11:16:15Z. This was checked by a direct Chiado block scan with `<N>` successful block reads and zero read failures, and corroborated by Blockscout. The deployed commit and full composer routing configuration are unknown, so this does not rule out an alternate posting destination.

Do not file until the placeholders are populated.

## Required framing edits

### Title and duration

“Does not self-recover” is not established by a bounded 12- or 20-minute observation. It establishes only “had not recovered by time T.” The task context says the gap exceeded 20 minutes, while the draft alternates among 12 minutes at 11:28, 17 minutes at 11:33, and a title with no bound. Pick a single final observation cutoff and update every duration, block range, chain head, and timeline entry from the same snapshot. If posting later resumed, report the exact recovery time; if a restart was required, report that.

Replace the title with:

> # No successful batch call to the configured L1 registry observed for `<duration>` while L2 blocks continued

Replace summary lines 5–14 with:

> The latest successful `postAndVerifyBatch` call I found to registry `0xf065…f32c` was `<hash>`, included at 2026-07-29 11:16:15Z. As of `<cutoff UTC>` (`<duration>` later), a direct Chiado scan with zero block-read failures found no later matching call to that registry. During the observation window, the queried L2 endpoint continued producing blocks at approximately 0.99 blocks/s and its reported head timestamp remained close to wall clock.
>
> The gap began around the time an L2 load test was running and persisted for `<duration after load stop>` after our load generators stopped. This is temporal correlation only; no controlled repetition or composer logs establish that load caused the gap.

### Causal language

The explicit disclaimer at lines 99–101 is good, but the reproduction section undermines it by instructing maintainers to “run sustained load” and saying “the stall shows up,” as though load were a known trigger. The timeline's “stopped as sustained load resumed” is also stronger than the timestamps permit: load restarted only approximately at 11:16, while the last inclusion was 11:16:15; L1 inclusion time is not composer creation/submission time.

Replace the first bullet under “What this does not establish” with:

> - I have not shown that load caused the posting gap. The approximate load-restart time overlaps the last observed L1 inclusion, but inclusion time is not the time the composer created or submitted that transaction. I have no controlled load-on/load-off repetition or composer logs.

Rename `## Reproduction` to:

> ## Observation procedure and requested maintainer diagnostics

Replace lines 130–139 with:

> I do not yet have a deterministic reproduction. To detect the observed condition, sample L2 head number/timestamp and independently scan L1 for successful matching batch calls to every configured destination. Treat every failed RPC/block read as an error, not as an empty result.
>
> For diagnosis, please correlate the UTC window above with composer logs and metrics for batch construction, proving/verification, queue depth, backoff state, configured destination, signer selection, L1 RPC/bundle submission, and process restarts. The current `.issues/measure_idle.py` is not the collector used for the direct L1 block scan and does not reproduce this event, so it should not be cited here.

### Poster-account inference

The checks support only two narrow conclusions at the queried snapshot:

- the account was not exhausted of native token; and
- the queried public RPC reported no difference between latest and pending nonce.

They do **not** establish that the whole L1 submission side was healthy. `latest == pending` does not reveal a transaction held in a private relay/builder, an attempted transaction rejected before mempool admission, an L1 RPC/auth/rate-limit failure, fee-estimation failure, bundle simulation failure, a rotated signer, a changed destination, or a transaction never constructed. The last transaction succeeding rules out failure of that transaction; it says nothing about the next batching attempt. “Ample” balance also requires a fee/budget comparison, although 85 native tokens makes simple exhaustion unlikely.

Replace the section heading and conclusion at lines 65–80 with:

> ## Poster-account checks rule out simple balance exhaustion and a public pending-nonce gap
>
> At 11:33Z, the known poster held 85.307173 native tokens, and the queried Chiado RPC returned `latest == pending == 108186`. Its last observed transaction, the 11:16:15Z batch call, succeeded. These checks make exhausted balance and a transaction visibly pending at that RPC unlikely.
>
> They do not identify the failing stage or rule out other L1-side submission failures, including private bundle state, RPC rejection, fee estimation, signer/destination rotation, or a failure before transaction broadcast. Composer logs and configuration are needed to distinguish batching/proving from signing and submission.

Delete:

> It did not fail and retry — it simply stopped submitting.

No evidence observes internal retry attempts, so that sentence is unsupported.

## Consequence and monitoring claims

The first sentence of “Why this matters” assumes the configured registry call is the protocol's only way to make L2 state verifiable or settled. The second asserts that cross-chain operations cannot complete, but the draft presents no cross-chain probe during the gap and no protocol reference proving that dependency. Both may be true, but neither follows from the supplied observations alone.

Replace lines 110–116 with:

> ## Why this may matter
>
> If successful calls to this registry are the deployment's mechanism for anchoring or verifying L2 batches on L1, a prolonged gap increases the amount of L2 activity not yet represented through that mechanism. Please clarify the resulting finality, verification, settlement, and cross-chain-call behavior for this deployment. I did not directly test those consequences during the window.
>
> The L2 RPC and both front endpoints remained responsive, so a monitor limited to RPC availability and L2 head advancement would not detect this specific absence of registry calls. A “time since last successful configured L1 batch post” metric would detect it.

This preserves the useful monitoring recommendation without claiming what all “external monitoring” does or that endpoint responsiveness proves system health.

## Questions that would better help maintainers

Replace the current Questions section with:

> ## Questions / requested diagnostics
>
> 1. Was registry `0xf065…f32c` the sole configured batch destination throughout `<start>..<end>`, and was `0x63A8…23d1` the active signer throughout?
> 2. Do composer logs show batch construction, proving/verification, queue backpressure, deliberate backoff, signer/nonce/fee errors, L1 RPC errors, private-bundle failures, or process restarts during that UTC window?
> 3. Is a prolonged absence of successful `postAndVerifyBatch` calls expected under any configured condition, and what is the expected recovery policy?
> 4. What user-visible finality, settlement, or cross-chain behavior should be expected while these calls are absent?
> 5. Would the composer expose last successful L1 post time, current batch/proving state, queue depth, destination, and last submission error in a health/metrics endpoint?

## Additional reproducibility requirements

Before filing:

- identify the direct Chiado RPC and publish the direct-scan script/raw output;
- report attempted/successful/failed block reads explicitly;
- give the full last-batch hash, calldata selector, sender, receipt status, gas data, block hash, and timestamp;
- record start and end L1/L2 heads from one final observation snapshot;
- state how wall-clock times were synchronized and whether block timestamps or local receipt times were used;
- confirm the relevant blocks were stable/final enough that an L1 reorg did not change the result;
- replace the stale 12-minute snapshot with the final known recovery/restart outcome;
- identify the exact load generators, configured rates/concurrency, accepted transaction rate, and precise start/stop times as context—not as a reproduced cause;
- if possible, verify the deployment's configured registry, poster, and batch method from configuration or on-chain state.

With those changes, the issue would be a credible report of an externally observed posting gap without pretending to know its cause or internal failure stage.
