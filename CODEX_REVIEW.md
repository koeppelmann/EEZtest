# Adversarial code review

The framework has several failures that can invalidate its headline EEZ conclusions. Most seriously, the contract caller does not test cross-chain return data despite claiming that it does, and the load workers concurrently reuse the same accounts with independent nonce allocators. The resulting nonce collisions and gaps can manufacture stalls, dropped transactions, and apparent chain failures.

## Findings

### 1. [HIGH] Cross-chain Bug B detection never observes `returnData`

**Files:** `eeztest/workers/contract_caller.py:474-491`, `eeztest/workers/contract_caller.py:647-658`

For both cross-chain directions, the worker labels `Counter`'s stored increment value as a returned value. That value is written by `Counter.increment()` itself (`contracts/Counter.sol:14-17`); it says nothing about whether EEZ propagated the call's ABI return bytes. If EEZ executes the target correctly but drops `returnData` entirely—the stated Bug B—the increment entry and `counter()` still agree and these checks pass. `_assert_return_data()` does enforce non-empty bytes, but it is called only by the same-chain Logger baseline (`contract_caller.py:350-367`), never by either cross-chain combination.

**Failure scenario:** EEZ returns empty bytes for every L1->L2 and L2->L1 call while applying the inner call. The suite records both cross-chain combinations as passes and never reports Bug B.

**Suggested fix:** Route each cross-chain test through a destination-side observer that durably records the actual bytes returned by the inner call, or read an authoritative EEZ execution record/event containing `returnData`. Assert both `len(returnData) > 0` and ABI-decoded equality to the independently read counter. Do not treat target-side state as return-path evidence.

### 2. [HIGH] Fuzzer, DDoS, and congestion independently allocate nonces for the same L2 accounts

**Files:** `eeztest/workers/fuzzer.py:107,222,404-416`, `eeztest/workers/ddos.py:46-54,176-197`, `eeztest/workers/congestion.py:412-420,545-566`

All three workers consume `l2_accounts`. The fuzzer has its own `_nonces` map, DDoS snapshots its own `nonces` array, and congestion reads `"pending"` immediately before signing. There is no cross-worker lock or allocator. Two workers can therefore sign different transactions with the same `(sender, nonce)`. One is rejected/replaced; worse, each local allocator can then advance differently and create gaps. This contaminates the fuzzer's “accepted but never mined,” congestion's spend/deposit conclusions, and DDoS's mempool-refusal and halt signals.

**Failure scenario:** DDoS reserves nonce 10 locally while the fuzzer also reads pending nonce 10. DDoS's tx lands first, the fuzz tx is rejected, the fuzzer resets to the node's pending nonce while DDoS continues issuing 11+. Congestion can race another transaction at that pending nonce. Observed rejection rates and stuck queues are harness artifacts, not EEZ behavior.

**Suggested fix:** Put a per-chain, per-address nonce manager in `WorkerContext` and require every worker using a sub-account to reserve/broadcast/resynchronize through it. Alternatively partition accounts exclusively among workers. Reservations and rejection recovery must be coordinated; merely querying `"pending"` is not sufficient.

### 3. [HIGH] A failed primary-key broadcast permanently consumes a local nonce and can wedge every worker

**File:** `eeztest/rpc.py:103-110,165-180`

`next_nonce()` increments `_local_nonce` before signing/broadcasting, but `send()` does not roll back or resynchronize when `gas_price()`, signing, or `send_raw()` fails. Because the L1/L2 `ChainClient` instances are shared by the funder, contract caller, congestion, and proxy builder, a single transport rejection leaves a missing nonce while subsequent workers keep issuing higher nonces. Those transactions remain blocked and are then misreported as composer stalls or EEZ failures.

**Failure scenario:** an L1 deposit reserves nonce 25 and its HTTP request times out before node acceptance. The next proxy creation uses 26. Pending nonce remains 25, so all later L1 work waits indefinitely behind a harness-created gap.

`reset_nonce()` is not a safe general repair under concurrency either: callers at `congestion.py:127,342` can set the shared allocator to `None` while other threads have already reserved higher nonces, allowing duplicate reservations based on a stale pending count.

**Suggested fix:** Make nonce reservation/broadcast a coordinated operation with explicit reservation states and failure recovery. On ambiguous transport failure, query by signed transaction hash before deciding whether to reuse the nonce. Serialize reset/resync against all outstanding reservations and never blindly reset a shared allocator.

### 4. [HIGH] DDoS dispatch exceeds its configured rate ceiling and has no spend budget

**File:** `eeztest/workers/ddos.py:57-63,113-142`

The per-tick budget is `int(rate_cur * 0.05) + 1`. At the default 200/s this schedules up to 11 every 50 ms, or 220/s. After backpressure reaches 5/s it schedules 2 every tick, or 40/s—eight times the supposed ceiling. Scheduling jitter does not turn this into a valid rate limiter; there is no token accounting across ticks. The worker also has no cumulative gas/spend cap and never checks balances, so a long run can drain every funded account despite describing its settings as ceilings.

**Failure scenario:** transport errors reduce `rate_cur` to 5/s, but the worker continues attempting roughly 40/s, preventing recovery and burning/rejecting far more transactions than configured.

**Suggested fix:** Use a monotonic token bucket (including fractional tokens), consume one token per submission, and cap accumulated burst tokens at `burst`. Add a configured total gas/value budget or per-account reserve floor, stop allocating from depleted accounts, and expose actual attempted spend.

### 5. [MED] Cross-chain identity stability is not tested on L2->L1 within a round

**File:** `eeztest/workers/contract_caller.py:643-685,734-748`

L1->L2 deliberately sends twice, but L2->L1 sends only once per round. `_note_identity()` can detect drift across later rounds, but only if both calls apply successfully. A flaky Bug A response can leave only one successful observation, making the advertised identity-stability assertion vacuous. With the default three rounds, a run with one successful call and two reverts cannot establish stability but can still record that successful hop as a pass.

**Suggested fix:** Require at least two successfully observed identities per direction before declaring identity stability. Track an explicit inconclusive result when that sample count is not reached, and send paired L2->L1 probes just as the L1->L2 test does.

### 6. [MED] Read failures are converted into empty histories, causing false “applied” and race conclusions

**Files:** `eeztest/workers/contract_caller.py:536-559,750-776`, `eeztest/workers/congestion.py:449-462`

`_read_increments()` catches every RPC/ABI error and returns `[]`. Callers then use `len([])` as a trustworthy baseline. If the pre-read fails while the counter already has entries, `_wait_increments(..., pre_len=0)` immediately returns the old history and reports the new cross-chain call as applied. The selected `entries[-1]` may belong to an earlier transaction. Similarly, congestion takes point-in-time balances of an account intentionally shared with fuzzer/DDoS and attributes the entire delta to its own two legs; unrelated transfers and gas charges make its “double credit,” “spend not charged,” and “deposit lost” predicates unsound.

**Failure scenario:** `getIncrements()` transiently fails before an L2->L1 call and succeeds on the first poll, returning an old entry. The worker marks `applied=True` even if the new call never ran, corrupting Bug A and Bug C classification.

**Suggested fix:** Return `None` for unreadable state and retry/mark the probe inconclusive; never substitute a valid empty collection for an error. Correlate observations with a unique call ID/value emitted by that exact probe. Give congestion an exclusive account and account exactly for receipt gas costs.

### 7. [MED] Endpoint “divergence” compares different transactions and is confounded by nonce ordering

**File:** `eeztest/workers/proxy_builder.py:407-415,443-490,575-589`

The matrix claims “identical implicit deposits” behave differently, but each endpoint receives a different random target, proxy, transaction hash, and sequential nonce. Outcomes may differ because of target-specific state or because an earlier endpoint's nonce blocks a later one. The scoring handles higher unmined nonces only after waiting, but still reports divergence among rejection/revert/credit outcomes that are not controlled equivalents.

**Failure scenario:** endpoint A rejects nonce 30 synchronously and endpoint B accepts nonce 31. B cannot mine due to the nonce gap; the matrix reports differing endpoint behavior even though routing was not the cause.

**Suggested fix:** Use independent funded senders with the same starting nonce and otherwise identical calldata/value for each endpoint, or replay the exact same signed transaction in isolated rounds after clearing state. Do not title the finding “identical” unless all transaction inputs except endpoint are controlled.

### 8. [MED] Setup failure handling retries `step()`, not `setup()`

**File:** `eeztest/workers/base.py:123-136`

The default worker loop logs that a failed setup “will retry in loop,” then immediately calls `step()` forever without invoking `setup()` again. Workers whose setup failed before initializing attributes produce repeated `AttributeError`s for the rest of the run. This violates graceful degradation and can flood events/counters while never recovering from a transient chain/compiler failure.

**Suggested fix:** Retry `setup()` with interruptible backoff until it succeeds, or terminate that worker with one durable finding. Do not enter `step()` until initialization has completed.

### 9. [LOW] Shutdown can write an incomplete report while workers are still active

**Files:** `eeztest/runner.py:88-97`, `eeztest/rpc.py:195-202`

Each worker is joined for only ten seconds, while many use non-interruptible `wait_receipt()` calls with 60–240 second timeouts. The runner then stops the dashboard and snapshots the report even though daemon worker threads may still add findings afterward. The delivered report can omit the final outcome and show workers as running.

**Suggested fix:** Make receipt waits stop-event-aware throughout, then join workers to completion (with a global bounded shutdown deadline). If any remain alive, record that explicitly before the report snapshot and prevent later mutation of the reported registry.

## Security notes

No committed private key material was found in the reviewed source, and reports/state snapshots do not currently publish `SubAccount.private_key`. `.gitignore` excludes `config.yaml`, `*.key`, and wallet JSON files. Inline keys remain supported by configuration, so deployments should continue to use environment variables and avoid alternate YAML filenames containing secrets, which are not ignored by the current patterns.
