# Review of standalone drafts A and B

The separation is substantially successful: A is a feature request about a one-shot explicit entrypoint; B is an ingress-admission bug report and does not ask for implicit proxy creation. Both still need edits before filing.

## A — one-shot proxy creation and deposit

**Verdict: FILE WITH EDITS.**

### Scope and framing

A stays within feature-request scope. It explicitly says the current two-step flow works and calls the mistaken-order behavior a foot-gun rather than a protocol defect. It does not import the front-drop or composer-stall bugs.

The main remaining problem is not scope but certainty: “irreversible” and “funds are unrecoverable” are not established by the displayed evidence.

### Required edits

1. **Do not claim the funds are unrecoverable.** The transaction proves that value was transferred on L1 to an address with no code and that no L2 credit occurred. The lack of a deposit event does not by itself prove permanent loss. The native balance remains at that address; whether a later contract deployed there can expose, forward, or otherwise recover it depends on the eventual proxy bytecode and protocol behavior, neither of which is tested here.

   Replace lines 43–44 with:

   > The reason to consider a one-shot entrypoint is that getting the order wrong produces a successful L1 transfer without the intended L2 credit, with no synchronous warning.

   Replace lines 61–68 with:

   > It mined as an ordinary 21,000-gas transfer to an address that had no code. No L2 credit followed. The value therefore remained as native L1 balance at the counterfactual proxy address rather than being processed as a deposit.
   >
   > I did not test whether deploying the proxy later provides any way to recover or process that pre-existing balance, so this report does not claim permanent loss. It is still a foot-gun: the L1 transaction succeeds while the intended deposit does not occur, and recovery behavior is not apparent to the user.

2. **Soften the absolute claim that auto-detection is “not on the table.”** Inverting a CREATE2-derived address over arbitrary inputs is infeasible, but an implementation might possess an external mapping, a finite configured rollup set, or another registration mechanism. The argument supports carrying the preimage as a clean design, not the impossibility of every alternative.

   Replace the heading at line 15 with:

   > ## Why an explicit preimage-carrying call is preferable to inference

   Replace lines 17–20 with:

   > A bare value transfer contains only `to: <20 bytes>, data: 0x`. Without an external mapping or supplied inputs, the composer cannot feasibly recover arbitrary `(originalAddress, rollupId)` CREATE2 preimages from the 20-byte destination alone.

3. **Cut the “one narrow case” heuristic paragraph.** Lines 22–26 are speculative, require the composer somehow to choose/know `rollupId`, and distract from the simple proposal. They also say it “silently fails” for other recipients, which sounds like a defect claim in a feature request. Delete that paragraph in full.

   Join the surrounding text with:

   > An explicit call avoids inference: the caller supplies the preimage, allowing the implementation to validate the derived proxy address and perform creation and deposit in one operation.

4. **Present atomicity as a requested property, not an already established consequence.** A payable entrypoint does not automatically guarantee that cross-chain credit is atomic with L1 proxy deployment; that depends on EEZ settlement semantics.

   Replace line 29 with:

   > The requested behavior is one L1 transaction that validates the inputs, deploys the proxy if needed, initiates the credit, and reverts rather than leaving value at an unintended address if setup cannot be completed.

5. **Make the failed-order example independently verifiable.** The table omits the `originalAddress` used to compute the proxy and the method/result proving the computation. Add:

   > | original L2 recipient | `<full address>` |
   > | proxy computation | `computeCrossChainProxyAddress(<recipient>, 1) -> 0x589E…B0bc` |
   > | L2 balance before / after and observation period | `<before> / <after>, observed for <duration>` |

   Do not file with those placeholders. A maintainer should be able to confirm that the destination really is the claimed proxy and that “no credit” is a bounded measured result.

6. **Ask explicitly about later recovery instead of answering it without evidence.** Add under the proposal:

   > Separately, what is the intended recovery behavior when value has already been sent to an undeployed proxy address? If no recovery path exists after later deployment, documenting that warning would be valuable.

### Cut

- Cut “silent, irreversible failure mode.”
- Cut “The funds are unrecoverable.”
- Cut the partial auto-detection heuristic at lines 22–26.

These edits keep A short and focused on the requested API and observable UX failure.

## B — ingress front acknowledges but does not produce observable L1 inclusion

**Verdict: FILE WITH EDITS.**

### What is sound

- The control uses one signed transaction and byte-identical raw bytes, evidenced by the same canonical hash.
- The sequential nature of the replay is disclosed. The direct submission proves that this particular transaction was well-formed, funded, executable, and sufficiently gassed.
- The draft correctly says it cannot identify whether the RPC front, admission queue, bundle construction, or L1 submission stage is responsible.
- `absent` is carefully limited to `eth_getTransactionByHash == null`, with no claim of a full txpool inspection.
- The feature request is kept out. Asking whether this transaction class is supported by the ingress endpoint is directly relevant to whether the endpoint should accept or reject it.
- “What this is not” clearly prevents recurrence of the disproven lost-message claim. It is useful context and concise enough to keep.

### Required edits

1. **Bound every “never” and “dropped” claim to the actual observation.** The front path was observed for 75 seconds. Directly submitting the same nonce afterward caused the transaction to mine, which also prevents learning whether the front path might eventually have submitted it. The evidence supports “not observable or included within 75 seconds,” not permanent loss.

   Replace summary lines 3–6 with:

   > `eth_sendRawTransaction` to the L1→L2 ingress front (`:18999`) returned the correct canonical transaction hash, but for the following 75 seconds neither queried endpoint returned the transaction, no L1 receipt appeared, and the sender's pending nonce did not advance. Submitting the exact same raw bytes to a plain Chiado RPC afterward produced an L1 receipt in about five seconds.

   Replace lines 61–63 with:

   > I cannot identify which internal stage—RPC front, admission queue, bundle construction, or L1 submission—prevented observable inclusion during that window. Composer logs would be needed to locate it.

   Replace suggested-behavior lines 82–85 with:

   > If this transaction class is unsupported, the front should reject it synchronously with an actionable error rather than return a hash. If it is supported, an accepted transaction should either progress to submission or expose a later failure state; during this test the caller could not distinguish queued work from a request the ingress path would not process.

   Replace the last sentence at line 149 with:

   > This issue is only about an accepted request that produced no observable L1 submission during the measured window.

2. **Do not describe the call as “not cross-chain-shaped” without defining that protocol term.** That wording assumes the front's admission rules and may itself explain intended rejection. The useful fact is simply its exact destination and calldata.

   Replace lines 8–10 with:

   > The tested transaction calls `createCrossChainProxy(address,uint256)` on the L1 registry. It is not a bare value transfer; its destination and calldata are shown below. I do not know whether this method is within the ingress front's supported transaction set.

3. **The standalone script now polls the front path, but it still does not reproduce the successful direct leg.** It prints the second `eth_sendRawTransaction` response and merely comments that the transaction mines. It never polls `eth_getTransactionReceipt`, asserts status 1, or prints gas used. It also does not poll the receipt during the first 75 seconds even though the issue claims no receipt appeared.

   During the first loop, add:

   ```python
   rpc(PLAIN, "eth_getTransactionReceipt", [h])
   ```

   After direct submission, replace the final line with:

   ```python
   print("plain:", rpc(PLAIN, "eth_sendRawTransaction", [raw]))
   receipt = None
   for elapsed in range(1, 31):
       time.sleep(1)
       receipt = rpc(PLAIN, "eth_getTransactionReceipt", [h])[0]
       if receipt is not None:
           print("mined after", elapsed, "s", receipt)
           break
   if receipt is None:
       raise RuntimeError("no receipt within 30 seconds after direct submission")
   if int(receipt["status"], 16) != 1:
       raise RuntimeError(f"direct replay reverted: {receipt}")
   ```

4. **Make JSON-RPC errors fail loudly.** The current helper returns `(None, error)`, and most callers discard the error element. A rate limit, unsupported method, or JSON-RPC failure can therefore be printed as if it were an absent transaction.

   Replace `rpc()` with:

   ```python
   def rpc(url, method, params):
       response = requests.post(
           url,
           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
           timeout=15,
       )
       response.raise_for_status()
       body = response.json()
       if body.get("error") is not None:
           raise RuntimeError(f"{url} {method}: {body['error']}")
       if "result" not in body:
           raise RuntimeError(f"{url} {method}: missing result: {body}")
       return body["result"]
   ```

   Adjust call sites to stop indexing `[0]`. For the original historical transcript, retain the raw `result/error` display; this change is for reliable reproduction.

5. **Use a fresh target or explain idempotency.** The script hard-codes `0x7e…7e`. After the reported direct replay, its proxy may already exist. A rerun may therefore exercise an already-created proxy or revert for a different reason. Generate a fresh target, verify the predicted proxy has no code if freshness matters, or explicitly state that `createCrossChainProxy` behavior is identical for an existing proxy.

   A minimal replacement is:

   ```python
   from eth_account import Account
   target = Account.create().address
   ```

   If proxy absence is part of the intended test, also call `computeCrossChainProxyAddress(target, 1)` and assert `eth_getCode == "0x"` before signing.

6. **Label the nonce side effect as a separate run.** The displayed error uses nonce 46/45, while the controlled replay uses nonce 88. As written, readers can mistake it for evidence from the same transaction.

   Replace the opening of that section with:

   > In a separate EEZtest run, the same accept-without-observable-submission behavior caused a client-side nonce desynchronization. EEZtest optimistically advanced its local nonce after receiving a hash; the source-chain nonce had not advanced, so a later submission received:

   Replace “each off by one” with the singular wording supported by the one displayed error:

   > its next submission was one nonce ahead:

7. **Avoid saying the front definitively failed to “forward” the transaction.** An external observer cannot distinguish no forwarding from attempted private submission, bundle rejection, or other failure before inclusion. Change the final link text and any issue title that uses “never forwards” to:

   > ingress front returns a canonical hash but no L1 inclusion is observed within 75 seconds

### Cut

- Cut unbounded “never included,” “then dropped,” and “gone” wording.
- Do not add proxy auto-creation, undeployed-proxy loss, or composer-stall material; those are correctly absent now.

After these edits, B points plainly to the actionable defect: the ingress endpoint acknowledges a request without either observable progress or a clear unsupported-transaction error.
