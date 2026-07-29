## What I'd like

Let me send a **plain, ordinary L1 transaction** — unchanged format, any wallet,
any tooling — and have the composer deal with a CrossChainProxy that does not
exist yet.

The mechanism that makes this possible without changing the transaction:

1. **I register a hint with the composer**, out of band: "address `P` is the
   CrossChainProxy of `(originalAddress, rollupId)`".
2. When the composer processes my transaction, it **checks whether the
   transaction touches any hinted-at proxy**.
3. If it does, the composer submits my transaction as a **bundle**:
   `[deploy proxy, register incoming call, my tx]`.

The hint is what makes step 2 possible at all. A bare transfer carries only
`to: <20 bytes>, data: 0x`, and `(originalAddress, rollupId)` cannot feasibly be
recovered from a CREATE2-derived address. But the composer does not need to
*derive* anything if it has been *told* the mapping in advance — it only needs to
match against a set of addresses it already knows.

## Why this shape rather than a special entrypoint

I originally proposed a payable `depositTo(originalAddress, rollupId)` on the
registry. The hint approach is better:

- **My transaction does not change.** No new ABI, no wallet support needed, works
  with transactions I do not control the construction of.
- **It is not limited to deposits.** Any transaction that happens to touch a
  hinted proxy gets the proxy deployed and the incoming call registered ahead of
  it, not just plain value transfers.
- **The composer already builds bundles**, so `[deploy, register, tx]` is the
  natural unit of work rather than a new contract-level primitive.

## Open questions on the design

1. **Hint registration** — what is the right channel? An RPC method on the ingress
   front, a config entry, or something on-chain that the composer watches?
2. **Lifetime and scope** — are hints per-sender or global? Do they expire once
   the proxy is deployed (at which point they are unnecessary)?
3. **Abuse** — can anyone register a hint for any `(address, rollupId)` pair?
   Registering a hint is a claim the composer will act on, so it presumably wants
   to verify the derivation itself before accepting one, which is cheap: given the
   preimage, recomputing the CREATE2 address is a single hash.
4. **Failure mode** — if the bundle cannot be built, is my transaction rejected,
   or submitted without the proxy deployment (reproducing the failure below)?

## The failure this removes

Sending value to a computed proxy address before the proxy exists:

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

It mined as an ordinary 21,000-gas transfer to a codeless address. No L2 credit
followed; the value is still sitting at the counterfactual proxy address.

I did not test whether deploying the proxy later provides any way to recover that
pre-existing balance, so this does not claim permanent loss — but recovery
behaviour is not apparent to the user, and the L1 transaction succeeds either way.

**What is the intended recovery behaviour here?** If none exists after later
deployment, that is worth documenting.

## Meanwhile: a helper contract, which we can build ourselves

Independent of the above, the same UX gap can be closed today at the contract
level without any composer change — a helper you send funds to, which deploys the
proxy and forwards the value in one transaction:

```solidity
function depositTo(address originalAddress, uint256 rollupId) external payable returns (address proxy) {
    proxy = registry.computeCrossChainProxyAddress(originalAddress, rollupId);
    if (proxy.code.length == 0) {
        registry.createCrossChainProxy(originalAddress, rollupId);
    }
    (bool ok, ) = proxy.call{value: msg.value}("");
    if (!ok) revert ForwardFailed(proxy);
}
```

Because creation and forwarding are in one call, a failure reverts the whole
transaction and the caller keeps their funds, instead of stranding value at a
codeless address.

We are deploying this ourselves and will add the address here once it is verified
— raising it only so it is visible, and in case a canonical version belongs in
this repo rather than living in ours. It is a workaround, not a substitute for the
hint mechanism: it only helps for plain deposits through the helper, whereas the
hint approach covers any transaction touching a hinted proxy.

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
