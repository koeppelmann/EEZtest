Review TWO standalone issue drafts before filing against
https://github.com/eez-association/eez-rollup0.

  .issues/A-feature-auto-proxy.md   — FEATURE REQUEST: one-shot deposit that deploys the proxy
  .issues/B-bug-front-drop.md       — BUG: ingress front returns a tx hash but never forwards the tx

Important context on what changed since your last reviews:

* An earlier draft claimed a cross-chain tx was triggered on L1 but never executed on L2. That was
  WRONG and has been removed. A clean test (proxy pre-created, deposit via the front) showed the
  happy path WORKS: L2 credited at t=10s, L1 mined at t=15s, block 22314436, status 1. There is no
  lost-message bug. Draft B now says so explicitly in a "What this is not" section.
* The two drafts are meant to be strictly separate: A is a feature request only, B is a bug only.
  Neither should smuggle in the other, and neither should mention a separate composer-stall issue.

Review each for:

1. SCOPE PURITY. Does A stay a feature request (no bug claims)? Does B stay a bug report (no
   feature asks beyond a minimal "suggested behaviour")? Flag any bleed between them, and any
   content that belongs in neither.

2. CLAIMS vs EVIDENCE. Every factual claim must be backed by the data shown. Flag overreach,
   hypotheses stated as fact, and missing controls. In B specifically: is the same-raw-bytes
   replay presented accurately, and is the "cannot say which internal stage" disclaimer adequate?
   In A: is the "funds unrecoverable" claim correct, given no deposit event ever occurred?

3. REPRODUCIBILITY. Could a maintainer reproduce B from what is written alone? Check the
   standalone script actually does what the issue describes (a previous draft's script did not
   poll and so could not have observed the failure).

4. ASSUMPTIONS. The goal is issues that "point plainly to the issue" without a lot of assumptions.
   Flag anywhere I speculate about internals, protocol intent, or root cause rather than stating
   observation.

5. TONE/LENGTH. These should be easy to read and act on. Flag padding, repetition, or anything
   that buries the key fact.

Give each a verdict (FILE AS-IS / FILE WITH EDITS / DO NOT FILE) plus exact required edits.
Write to .issues/CODEX_REVIEW_AB.md. Do not modify the drafts.
