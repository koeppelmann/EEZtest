# Re-review of the rewritten issue drafts

Issue 1 now has a valid same-bytes replay and has addressed most of the prior overreach. It still needs several wording and reproduction corrections before filing. Issue 2's displayed projection arithmetic is correct, but the measurement script does not paginate a 50-item Blockscout response and does not verify either the method or transaction status. The exact count of 50 is therefore a likely page-limit artifact, not a demonstrated complete population of successful `postAndVerifyBatch` calls.

## Issue 1 — front submission and undeployed proxy

**Verdict: FILE WITH EDITS.**

### What is now sound

- The endpoint replay is genuinely controlled with respect to the signed transaction: the same raw bytes and canonical hash were submitted to both endpoints. The 800,000 gas limit versus 263,431 gas used rules out insufficient transaction gas for this call.
- The draft now correctly localizes the observation to the submission path while disclaiming knowledge of the failing internal stage.
- Implicit proxy creation is framed as a protocol question with synchronous rejection as a valid alternative.
- The nonce section correctly limits the problem to clients with optimistic local nonce tracking and explains that recovery is possible.
- The draft explicitly says that `eth_getTransactionByHash -> null` is not a txpool dump. It no longer claims global mempool absence.

### Remaining required edits

1. **The title still overclaims both “never executed” and “unsupported.”** The transaction was eventually executed after direct submission, and the front was observed for 75 seconds—not forever. The undeployed-proxy probe suggests that current behavior does not deliver a credit, but does not establish a protocol-level support decision.

   Replace the title with:

   > # X-chain front accepts an L1 transaction but does not make it observable within 75 seconds

   If maintainers prefer one issue per behavior, split the undeployed-proxy design question into a separate discussion. The controlled replay tests `createCrossChainProxy`; it does not itself test a deposit to an undeployed proxy.

2. **“The only variable is the submission endpoint” is not literally true in a sequential replay.** The signed transaction is fixed, but submission time and endpoint state also differ. The replay still establishes the important point: the transaction is well-formed, sufficiently funded/gassed, and executable on Chiado.

   Replace lines 94–101 with:

   > The signed transaction is byte-identical in both submissions. The submissions were sequential, so endpoint and submission time differ; nevertheless, the direct Chiado result demonstrates that this transaction was well-formed, sufficiently gassed, and executable. During the preceding 75-second observation period, the front path returned the canonical hash but neither queried endpoint returned the transaction and the source-chain nonce remained 88.
   >
   > This isolates the observed behavior to the submission path and/or its state at that time. It does not identify which internal stage—RPC front, admission queue, bundle construction, or L1 submission—failed to retain or forward the transaction. Composer logs would be needed for that.

3. **Replace residual unbounded language.** “Never observed again,” “never happened,” “never observed,” and the script comment “then nothing happens” all turn a bounded observation into a permanent conclusion.

   Make these exact replacements:

   - Summary line 7: replace “but is then never observed again” with “but was not returned by either queried endpoint during the following 75 seconds”.
   - Summary line 16: replace “never happened” with “was not observed during that window”.
   - Reproduction 2 line 109: replace “then never observed” with “then not observed through either queried endpoint for more than 120 seconds”.
   - Script line 190 comment: replace `# returns h, then nothing happens` with `# record h, then poll for the full observation window before direct replay`.

4. **The advertised standalone script does not reproduce the experiment as written.** It submits to the front and then immediately submits to plain Chiado. It neither polls for 75 seconds nor checks the front, receipt, or pending nonce. Running it verbatim therefore prevents observation of the alleged front-path failure and can make the plain submission mine before any meaningful control window.

   Replace the final four lines with actual polling logic equivalent to:

   ```python
   import time

   print("front:", rpc(FRONT, "eth_sendRawTransaction", [raw]))
   for elapsed in range(5, 76, 5):
       time.sleep(5)
       front_tx = rpc(FRONT, "eth_getTransactionByHash", [h])
       plain_tx = rpc(PLAIN, "eth_getTransactionByHash", [h])
       receipt = rpc(PLAIN, "eth_getTransactionReceipt", [h])
       pending = rpc(PLAIN, "eth_getTransactionCount", [acct.address, "pending"])
       print(elapsed, front_tx, plain_tx, receipt, pending)

   print("plain:", rpc(PLAIN, "eth_sendRawTransaction", [raw]))
   ```

   The script should also make `rpc()` call `raise_for_status()` and preserve/report malformed JSON-RPC responses; the current helper can turn an HTTP failure or unexpected body into misleading `(None, None)`.

5. **Reproduction 2 remains materially under-specified.** It gives proxy and transaction hashes, but not target address, sender, nonce, value, gas limit, gas price, calldata/raw transaction, exact observation duration for both probes, L1 receipt block/gas for the direct probe, or L2 balance before/after. That prevents an engineer from reproducing or ruling out under-gassing and insufficient value in the deposit case.

   Add a table for each deposit probe containing:

   > target address; computed proxy; sender; nonce; value; gas limit; gas price; chain ID; raw calldata; canonical transaction hash; submission endpoint; observation start/end; L1 receipt/status/gas used (if any); proxy code before/after; and target L2 balance before/after.

6. **The CREATE2 statement should say “not feasibly recoverable,” not mathematically “cannot.”** The conclusion is cryptographically sound in practice, but this is a preimage-resistance claim, not proof of information-theoretic impossibility. There might also be an external mapping known to the implementation.

   Replace both occurrences of:

   > cannot be recovered from a 160-bit address

   with:

   > are not feasibly recoverable from the proxy address alone without an external mapping or supplied preimage

7. **The future-nonce paragraph is a separate behavioral claim without a clean reproduction.** The two errors were produced after harness-local nonce drift, not by a fresh controlled test of pipelining. “Normal mempool” is also too broad; nodes vary in future-nonce policy.

   Replace lines 142–145 with:

   > In this run the front rejected the shown higher-nonce submissions with `expected next unreserved nonce N` rather than retaining them for later execution. If the endpoint intentionally requires strictly contiguous nonce submission, documenting that policy would help clients choose an appropriate nonce strategy.

## Issue 2 — idle L1 batch posting

**Verdict: DO NOT FILE in the current form. Fix and rerun the collector first.**

### Arithmetic recheck

Using the draft's stated 50 posts, 420 seconds, and average gas of 207,577:

- Posts/hour: `50 / 420 × 3600 = 428.571`, correctly rounded to **429**.
- Gas/hour: `428.571 × 207,577 = 88,961,571`, correctly rounded to **89.0M**.
- Gas/day: `88,961,571.4 × 24 = 2,135,077,714`, correctly rounded to **2.14B**.
- Native token/day at 1.5 gwei: approximately **3.203**, correctly rounded to **3.20**.
- Native token/day at 3 gwei: approximately **6.405**, correctly rounded to **6.41**.
- Proposed 18 posts/hour: `428.571 / 18 = 23.81`, correctly rounded to a **24×** reduction.

The arithmetic is fine. The input count is not yet trustworthy.

### Blocking measurement defects

1. **The Blockscout query retrieves only one page, and Blockscout pages contain 50 results.** The collector at `.issues/measure_idle.py:60-68` makes one request and ignores `next_page_params`. Blockscout's API documents keyset pagination in groups of 50. Receiving exactly 50 items is therefore a strong sign that the dataset was truncated. The 42-second gap from the window start (11:08:33) to the first listed transaction (11:09:15) makes missing earlier in-window posts plausible. The draft cannot claim “50 L1 posts in the window,” a complete interarrival distribution, or projections based on the complete rate until all pages covering the start boundary have been fetched. See [Blockscout REST pagination documentation](https://blockscout.mintlify.app/devs/apis/rest).

   Required fix: follow `next_page_params` until the oldest returned transaction predates `t0`, deduplicate by hash, and only then filter to `[t0, t1]`. Rerun the measurement and regenerate every count, gap, average, projection, and example from that complete dataset.

2. **The collector does not test that transactions are `postAndVerifyBatch`.** It requests all inbound transactions to the registry and labels every in-window item `postAndVerifyBatch` without checking method selector, decoded method, destination semantics beyond the address filter, or calldata. Any unrelated registry call is counted as a batch.

   Required fix: filter on the exact `postAndVerifyBatch` selector/calldata (and report that selector), then include counts of excluded inbound methods. The issue should state how overloads, if any, were handled.

3. **The collector does not verify success.** It reads `gas_used` but never inspects transaction status or receipt status, yet the draft calls all 50 transactions “successful.”

   Required fix: fetch/inspect receipt status for each candidate and report successful, reverted, pending/unreadable, and read-failure counts separately. Only successful matching calls belong in the headline unless the issue explicitly analyzes attempts.

4. **The draft's “43 of 49 gaps” is arithmetically false.** The displayed list has 49 gaps: one 40-second gap, three 25-second gaps, and **45** five-second gaps. Their sum is 340 seconds and mean is 6.94 seconds, consistent with the displayed 6.9-second mean and the first/last timestamps.

   After rerunning the paginated collector, recompute the distribution. If the displayed list somehow remains unchanged, replace both occurrences of:

   > 43 of 49 gaps

   with:

   > 45 of 49 gaps

5. **“Full list available on request” weakens an otherwise evidence-driven issue.** The linked script cannot reconstruct a historical result unless the remote deployment and Blockscout retention/order remain unchanged, and the current script is defective. Attach the complete raw result or include a stable artifact containing every matching hash, timestamp, block, input selector, status, and gas used.

### Assessment after those fixes

The rewritten framing is otherwise appropriately cautious:

- The seven-minute duration and daily extrapolation are prominent and honest.
- Every hourly/daily figure is labeled as projected.
- The title is a question rather than a defect assertion.
- “What this does not establish” correctly acknowledges that empty L2 transaction lists do not prove useless batches and that the sample does not establish long-run behavior.
- The unknown deployment commit/configuration is disclosed.

If a corrected, paginated, selector-filtered, status-checked rerun produces materially the same pattern, the issue can be filed with the recalculated numbers. Add one further limitation to “What this does not establish”:

> - This observation is from one deployment with an unknown composer commit and posting configuration. It does not establish that the cadence is a repository default or protocol requirement.
