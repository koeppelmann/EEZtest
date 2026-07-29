# Review of proposed public issues

Neither draft should be filed as written. Issue 1 has a broken control (transactions called “identical” have different hashes), attributes the failure to the front more strongly than the evidence permits, and mixes a likely intended prerequisite with a proposed new feature. Issue 2 has correct arithmetic, but describes a highly irregular seven-event sample as a stable cadence and cites an hour-long observation without presenting it.

## Issue 1 — undeployed proxy / silent drop

**Verdict: DO NOT FILE in the current form. Retest, then FILE WITH EDITS if the corrected control still reproduces.**

### Required edits

1. **Remove the claim that the control transactions were identical.** An identical signed raw Ethereum transaction necessarily has the same transaction hash. The two hashes at lines 74 and 76 differ, so at least one signed field differs (or the front is returning a noncanonical hash, which would itself need to be demonstrated).

   Replace section C with this until the raw transactions have been recovered and decoded:

   > ### C — comparison with direct L1 submission
   >
   > A `createCrossChainProxy` transaction submitted to the front returned a hash but was not observed mined during the 60-second observation window. A separately signed transaction with the same signer and calldata was submitted directly to Chiado and mined successfully (block 22313258, status 1, gasUsed 263431).
   >
   > These transactions had different hashes, so this comparison establishes that the call can succeed on Chiado, but it is not a controlled replay of the same signed transaction. A conclusive endpoint comparison still requires decoding and listing every signed field, or submitting the exact same raw transaction to both endpoints from a fresh nonce.

   Before filing, preferably replace that caveat with a new reproduction that records:

   - the complete raw signed transaction or decoded `from`, `to`, nonce, value, input, gas limit, gas price/fee fields, chain ID, and hash;
   - the exact JSON-RPC response from each endpoint;
   - `eth_getTransactionByHash`, `eth_getTransactionReceipt`, and sender pending/latest nonce checks over a stated period;
   - the transaction hash returned by the front compared with the locally computed canonical hash;
   - composer logs or operator confirmation, if available.

2. **Do not say the front is proven to be the component that “loses” the transaction.** The observations distinguish submission paths, but do not locate the loss inside the front, composer admission queue, bundle construction, upstream L1 submission, or an ordinary mempool eviction. `eth_getTransactionByHash -> null` also does not prove absence from “any mempool”; it proves only that the queried RPC did not return the transaction.

   Replace lines 57–58 with:

   > After 120 seconds, neither the queried front nor the queried Chiado RPC returned the transaction via `eth_getTransactionByHash`; no receipt was observed, and the proxy still had no code. The submission endpoint had returned success, but this external evidence does not identify which downstream stage failed to retain or submit the transaction.

   Replace lines 80–81 with:

   > The direct-L1 comparison shows that this method can execute successfully on Chiado. Because the two recorded hashes differ, it does not yet isolate the front as the failing component.

3. **Reframe undeployed-proxy auto-creation as a feature question, not expected protocol behavior.** The draft itself says pre-creation is documented as required. A bare transfer contains only the proxy address; if the CREATE2 derivation depends on `(originalAddress, rollupId)`, recovering arbitrary preimage inputs from the final 160-bit address is not feasible. That supports “the composer cannot infer the inputs from the bare transfer alone,” but it does not prove that a hint is the required protocol design. Explicit pre-creation, an off-chain/address registry, a wrapper call carrying inputs, or synchronous rejection are alternatives.

   Replace summary lines 10–15 with:

   > The documented flow appears to require creating the proxy before depositing. A bare value transfer to an undeployed deterministic address does not carry the `(originalAddress, rollupId)` inputs needed to create that proxy, and those inputs cannot feasibly be recovered from the address alone. The immediate issue is therefore not necessarily lack of auto-creation: it is that the front returned success instead of clearly rejecting a request it did not process. If implicit creation is intended, the protocol would need an additional mechanism that supplies or records those inputs.

   Replace expected-behavior item 1 with:

   > 1. Please clarify whether deposits to an undeployed deterministic proxy are supported. If they are unsupported, reject them synchronously with an actionable “create the proxy first” error. If implicit creation is intended, document and validate the mechanism by which `(originalAddress, rollupId)` is supplied.

4. **Correct the nonce impact.** Receiving a hash does not force a standards-compliant client to increment forever; the EEZtest harness's local allocator did so. Later higher-nonce transactions can be blocked behind a missing nonce, but the account is not “bricked”: resubmitting the missing nonce directly, replacing it, or resynchronizing the client can recover it. The shown errors also do not demonstrate that *every* subsequent transaction is rejected indefinitely.

   Replace lines 83–101 with:

   > ## Nonce impact on clients with optimistic local tracking
   >
   > EEZtest advanced its local nonce after the endpoint returned a transaction hash. Because the source-chain nonce did not advance, its next submissions used higher nonces and received the following errors:
   >
   > [retain the two error lines]
   >
   > Thus an accepted-but-unsubmitted transaction can create a nonce gap for clients that optimistically advance local nonce state. Such clients must resynchronize or resubmit the missing nonce before later transactions can proceed. In this run, that behavior stalled the funding worker at 1 of 12 accounts until it resynchronized and retried.

5. **Narrow the RPC guarantee.** Returning a transaction hash is not a promise that a transaction will be mined or retained forever; public mempools may evict or replace accepted transactions. The actionable claim is that this specialized front apparently accepted a transaction it could not process and provided no later error/status channel.

   Replace lines 123–125 with:

   > 3. If the front accepts a transaction that it cannot submit or compose, it should expose a failure status or actionable error. Ideally, requests known to be unsupported should be rejected synchronously rather than acknowledged and then becoming unobservable.

6. **Resolve the internal contradiction between sections B and C.** Section B says pre-create then deposit works, while C says `createCrossChainProxy` is dropped through the front. State exactly which endpoint was used for the successful pre-creation. If it was plain Chiado, say so. If it was the front, the draft must explain why the apparently same operation behaved differently.

7. **Add missing reproduction parameters.** Include the fresh `target` address (not just the computed proxy), sender, nonce, value, gas limit, gas price/fee fields, exact calldata for `computeCrossChainProxyAddress` and `createCrossChainProxy`, registry ABI/commit, L2 balance before/after, queried Chiado RPC URL/provider, and transaction/receipt polling commands. Explain why `rollupId=1` is correct for a deployment whose L2 chain ID is 6290; readers must not be left to infer that rollup ID equals or differs from chain ID.

8. **Include gas as a control.** A [previous EEZ deployment report](https://gist.github.com/koeppelmann/6d41f3bf3c33d6df451173ff20573003) attributed accepted-but-unmined transactions to an insufficient gas limit after a higher-gas retry succeeded. The draft gives only successful `gasUsed=263431`, not the failed transaction's gas limit. Report both and repeat with comfortably higher gas before attributing the behavior to proxy hints or endpoint routing.

### Cut

- Cut “silently discarded,” “the front is what loses it,” “bricks the account,” and “my wallet stopped working on this chain.”
- Cut the proposed hint from the title and primary diagnosis unless maintainers confirm implicit proxy creation is intended.
- Cut “never appears in any mempool” unless actual txpool inspection is available from every relevant node.

## Issue 2 — idle batch posting

**Verdict: FILE WITH EDITS.**

The displayed arithmetic is internally correct:

- `7 / 90 × 3600 = 280` batches/hour.
- `280 × 207,643 = 58,140,040` gas/hour.
- Daily gas is `1,395,360,960`, reasonably rounded to `1.40B`.
- At 1.5 and 3 gwei, that is approximately `2.093` and `4.186` native tokens/day.
- One post per 200 seconds is 18/hour; `280 / 18 = 15.56`, reasonably rounded to a 16× reduction.

Those are rate estimates from a short sample, not established daily costs or a demonstrated steady cadence.

### Required edits

1. **Present the sample honestly and stop calling it a regular 13-block cadence.** The listed interarrival times are 5, 5, 20, 5, 5, and 45 seconds—far from “roughly every 13 blocks.” Dividing a 90-second exposure by seven events estimates one event per 12.9 seconds, but it is not the observed interval distribution (the six listed intervals average 14.2 seconds).

   Replace summary lines 5–12 with:

   > During one 90-second window in which 91 inspected L2 blocks contained zero transactions, we observed seven successful L1 `postAndVerifyBatch` transactions. They used about 207.6k gas each. The observed timestamps were irregular (interarrival times from 5 to 45 seconds); over this short window the aggregate rate was equivalent to about one post per 12.9 seconds.
   >
   > If that short-window rate persisted for a full day, it would use approximately 1.40 billion L1 gas. This is an extrapolation, not a measured daily total.

2. **Either substantiate or remove the hour-long claim.** “The same cadence” and “the L2 had no transactions at all” over 60 minutes need the start/end L2 block numbers, count of inspected blocks and transactions, number of batch posts, gas total/average, and query output or a reproducible script. “Every ~5 s” also conflicts with the seven-event sample.

   If that evidence is not available, replace lines 56–58 with:

   > We also noticed frequent `postAndVerifyBatch` transactions earlier that day, but did not retain a synchronized L1/L2 dataset for that period; the quantitative claims in this issue are based only on the 90-second sample above.

3. **Avoid asserting that the batches attest to “nothing.”** Empty batches may intentionally advance roots, timestamps, finality/liveness state, forced-inclusion windows, or protocol bookkeeping. The issue does not decode the transactions or compare batch contents/state roots, so zero user transactions does not prove zero useful state transition.

   Replace the title with:

   > Question: can L1 batch posting be throttled while L2 blocks contain no transactions?

   Replace “to attest to nothing” with:

   > while L2 blocks contain no transactions; please clarify what protocol/liveness function these idle posts serve.

4. **Make the proposal conditional on protocol requirements.**

   Replace the Expected behaviour section with:

   > ## Question and possible optimization
   >
   > Is this idle posting cadence required for finality, timestamp progression, forced inclusion, or another protocol invariant? If not, could the composer use a configurable slower heartbeat while no L2 transactions have appeared since the last post, then return immediately to its normal policy when activity resumes? `N = 200` is an illustrative value, not a proposed universal default.

5. **Fix reproducibility.** Provide the full batch-poster address (not `0x63A8eF9c06…`), all seven L1 transaction hashes and block numbers, the registry transaction/function identification method, exact UTC start/end boundaries, L1 RPC/explorer source, L2 block hashes/numbers/timestamps, and executable JSON-RPC commands or a minimal script. Explain whether blocks 7070 through 7160 inclusive are 91 blocks and how their timestamps were aligned to the L1 inclusion timestamps. Also identify the deployed software commit/configuration; cadence may be configuration or intended behavior rather than a repository defect.

6. **Label all projected figures.**

   Rename the table heading to:

   > ## Short-sample extrapolation (assuming the observed 90-second aggregate rate persists)

   Change “batches / hour,” “L1 gas / hour,” and “L1 gas / day” to “projected batches / hour,” “projected L1 gas / hour,” and “projected L1 gas / day.”

7. **Use the correct unit framing.** xDAI is the native gas token on Gnosis-family networks, but Chiado is a testnet and the displayed “2–4 xDAI/day” is not evidence of a real economic cost. Phrase this as native-token consumption at assumed gas prices, and avoid implying mainnet cost without choosing a target chain and its measured gas price.

   Replace the two cost rows with:

   > | projected native gas token / day @ 1.5 gwei | ~2.09 |
   > | projected native gas token / day @ 3 gwei | ~4.19 |

### Cut

- Cut “continuously,” “roughly every 13 L2 blocks,” “burns,” and “to attest to nothing.”
- Cut “materially more on a chain with real gas prices”; it is vague and unsupported.
- Cut the unconditional acceptance criteria until maintainers clarify the required idle/liveness behavior. Present them as a possible design after that clarification.
