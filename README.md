# EEZtest

An autonomous test framework for **EEZ sync-rollup chains**.

Point it at an L1 / L2 pair, and it launches a set of independent **workers** that
each exercise one facet of the chain — funding, fuzzing, cross-chain contract
calls, congestion races, DoS load, and proxy-creation routing — shows their live
state on a **dashboard**, and writes a **report** after a fixed run (e.g. 1 hour).

```
          ┌──────────── EEZ instance (L1 + L2 + composer fronts) ───────────┐
          │                                                                 │
  config.yaml ──▶ Runner ──▶ workers ──▶ shared state ──▶ dashboard (:8799) │
                                │                     └──▶ report (.md/.json)│
                                └── funder ▸ fuzzer ▸ contract_caller        │
                                    ▸ congestion ▸ ddos ▸ proxy_builder      │
          └─────────────────────────────────────────────────────────────────┘
```

## What it tests

| worker            | what it does |
|-------------------|--------------|
| **funder**        | Funds L2 sub-accounts by depositing to their **L1 proxies**; the funded pool feeds the fuzzer and DoS workers. Flags deposits that mine on L1 but never credit L2 (lost-deposit / silent stall). |
| **fuzzer**        | Fires randomized L2 transactions from the funded accounts (random value / calldata / gas / edge targets) and asserts the L2 keeps producing blocks under fuzz. |
| **contract_caller** | Deploys **Counter + Logger** on **both** L1 and L2, calls them in cross-chain combinations, and **verifies the deployments against Blockscout**. Encodes the known regression bugs as assertions (see below). |
| **congestion**    | Races an **L2→L1** state write against a **parallel L1** write to the *same* state and classifies the winner / detects lost writes. |
| **ddos**          | Sustained high-rate load on the L2 to find the throughput ceiling, mempool cap, and any liveness break (halt / mempool refusal). |
| **proxy_builder** | Sends cross-chain txs that **explicitly create** a proxy vs **explicitly don't**, across different **mempools** (inbound front / plain RPC / extras), to see whether the builder lazily creates the proxy — and whether the endpoint you use changes the answer. |

### Regression bugs it watches for

- **Bug A** — an L2→L1 return path that reverts with `ExecutionNotFound` *after* the inner call already ran.
- **Bug B** — a cross-chain call that reports success with **empty** return data (assert `len(returnData) > 0` and the decoded value, never just `status == 1`).
- **Bug C** — the cross-chain derived sender must equal `computeCrossChainProxyAddress` and be **stable** across transactions.

> **Scope assumption:** this build assumes the L2 permits only *simple* L1↔L2 calls
> — an outer cross-chain call may **not** trigger an inner cross-chain call. No
> worker builds nested cross-chain actions.

## Quick start

```bash
git clone https://github.com/koeppelmann/EEZtest.git
cd EEZtest
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e .

cp config.example.yaml config.yaml       # then edit for your chain
export EEZTEST_PRIVATE_KEY=0x...          # a key funded on L1 (and ideally L2)

python -m eeztest check   --config config.yaml     # connectivity + config sanity (no txs)
python -m eeztest run     --config config.yaml     # full autonomous run + report
```

Open the dashboard at **http://localhost:8799** while it runs.

A short run for a quick signal:

```bash
python -m eeztest run --config config.yaml --duration 120     # 2-minute smoke run
make smoke                                                     # same, via Makefile
```

## Configure

Everything is in `config.yaml` (start from `config.example.yaml`). The parameters
you actually need to set:

- `l1.rpc`, `l1.chain_id`, `l1.xchain_front` — the L1 chain + the composer's **inbound** cross-chain front.
- `l2.rpc`, `l2.chain_id`, `l2.xchain_front` — the L2 chain + the composer's **outbound** front.
- `eez.rollup_id`, `eez.registry`, `eez.ccm_l2` — this rollup's id and the proxy contracts.
- `wallet.private_key_env` — name of the env var holding your funded key (default `EEZTEST_PRIVATE_KEY`).
- `blockscout.l1_api`, `blockscout.l2_api` — for contract verification (optional).
- `workers.*` — enable/disable each worker and tune its knobs.
- `run.duration_seconds` — how long a run lasts before the report is written (default `3600`).

Each worker can be independently toggled; a disabled worker still shows up on the
dashboard as `disabled`.

## Output

- **Dashboard** (`http://<host>:<port>`) — live chain heads, per-worker status /
  counters / metrics / recent events, and a findings feed. Polls `/api/state` and
  `/api/findings` every 2 s.
- **Report** — written to `reports/` at the end of a run as both Markdown and JSON:
  a verdict, a severity table, chain state, every finding (ranked), and per-worker
  activity.

## Commands

```
eeztest run     --config config.yaml [--duration N] [--report-dir DIR]
eeztest check   --config config.yaml     # verify connectivity + config, send nothing
eeztest report  --config config.yaml     # serve the dashboard only (monitor, no workers)
```

## Layout

```
eeztest/
  config.py      rpc.py       eez.py        # config, JSON-RPC + signing, EEZ mechanics
  contracts.py   verify.py                  # solc compile/deploy, Blockscout verification
  state.py       monitor.py   report.py     # shared state, chain monitor, report writer
  runner.py      cli.py                      # orchestration + CLI
  dashboard/     server.py  static/index.html
  workers/       base.py + one file per worker
contracts/       Counter.sol Logger.sol …    # the probe contracts (deployed on L1 and L2)
```

## Safety

The **ddos** worker generates real load and the fuzzer sends real transactions —
point EEZtest at a **test chain** you control or are authorized to test. All value
transfers are dust-sized, but transaction volume is deliberately high.

## License

MIT
