## What I'd like

A way to deposit to an L2 address whose CrossChainProxy has not been deployed
yet, in **one** L1 transaction, without first sending a separate
`createCrossChainProxy` and waiting for it.

Concretely, a payable entrypoint that carries the preimage:

```solidity
function depositTo(address originalAddress, uint64 rollupId) external payable;
```

The requested behaviour is one L1 transaction that validates the inputs, deploys
the proxy if needed, initiates the credit, and reverts rather than leaving value
at an unintended address if setup cannot be completed.

## Why an explicit preimage-carrying call is preferable to inference

A bare value transfer contains only `to: <20 bytes>, data: 0x`. Without an
external mapping or supplied inputs, the composer cannot feasibly recover
arbitrary `(originalAddress, rollupId)` CREATE2 preimages from the 20-byte
destination alone. (A narrow check such as "is the destination the proxy of
`msg.sender`?" is possible but only covers deposits to one's own proxy, so it
would not generalise.)

An explicit call avoids inference: the caller supplies the preimage, allowing the
implementation to validate the derived proxy address and perform creation and
deposit in one operation.

## Why it's worth considering

The current two-step flow works — `createCrossChainProxy`, wait for the receipt,
then deposit. Verified end-to-end on the deployment below:

```
proxy 0x9C9Ff3397FEf2ce83a92E31dF8c22523DFe4A142 created (917 bytes of code)
deposit 0.002 xDAI -> proxy
  t=10s  L2 credited +2000000000000000 wei
  t=15s  L1 mined block 22314436, status 1, gasUsed 112106
```

The reason to consider a one-shot entrypoint is that getting the order wrong
produces a successful L1 transfer without the intended L2 credit, with no
synchronous warning.

## The failure mode it would remove

Sending value to the computed proxy address **before** the proxy is deployed:

| | |
|---|---|
| intended L2 recipient | `0x771f7eFa2a18Bc1580f412Bf29865FD5930714dE` |
| proxy computation | `computeCrossChainProxyAddress(0x771f7eFa…14dE, 1)` → `0x589E58bC10a87084BcF35C750AfB898bEF92B0bc` (verified by `eth_call`) |
| sender | `0xDf2a7a819Ec290aAa6E2DCF0d28254C2e90011f6` (fresh, nonce 0) |
| value | 1,000,000,000 wei |
| calldata | `0x` |
| tx | `0xbcc71c03726bc61324c848b967c429a1114ab6eb1f0777017ea6ef6e39f94817` |
| L1 result | **mined**, block 22313449, status 1, gasUsed **21,000** |
| proxy code, then and now | none |
| recipient L2 balance, before and 75 min later | 0 → 0 |
| proxy L1 balance now | 1,000,000,000 wei |

It mined as an ordinary 21,000-gas transfer to an address that had no code. No L2
credit followed. The value therefore remained as native L1 balance at the
counterfactual proxy address rather than being processed as a deposit — it is
still sitting there.

I did not test whether deploying the proxy later provides any way to recover or
process that pre-existing balance, so this report does not claim permanent loss.
It is still a foot-gun: the L1 transaction succeeds while the intended deposit
does not occur, and recovery behaviour is not apparent to the user.

## Questions

1. Is a one-shot `depositTo(originalAddress, rollupId)`-style entrypoint of
   interest, or is the two-step flow the intended interface?
2. What is the intended recovery behaviour when value has already been sent to an
   undeployed proxy address? If no recovery path exists after later deployment,
   documenting that warning would be valuable.

## Environment

| item | value |
|---|---|
| L2 execution RPC | `http://65.109.26.16:18688` (chainId 6290) |
| L1 | Gnosis Chiado (chainId 10200) |
| `EEZ_REGISTRY_ADDRESS` | `0xf0656341956d83d047c5e26678130e453952f32c` |
| rollupId | `1` (from `ROLLUP_MGR.rollupId()`; not the L2 chain id) |
| observed | 2026-07-29 |

Found while building an automated test framework
([EEZtest](https://github.com/koeppelmann/EEZtest)).
