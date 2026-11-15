# Attest in NANDA Town: how other agents find and use it

This explains the changes on branch `claude/wonderful-tesla-8o7z43` (commit `5a8baba`, built on `main` at
`f000f3b`). They touch only `integrations/nandatown/`. The gate itself (`gate/`, `attest/`) is unchanged.

## In one paragraph

Before this change, Attest worked inside NANDA Town only for agents the Attest team had set up in advance.
Nothing in Town told an outside agent that the seller checks IDs, where the gate is, or what to send. Now the
seller explains this every time it turns someone away, and a buyer can follow that explanation without being
told anything beforehand. A company that signed itself up on Sam's public sandbox can be served in Town too.
There is also a draft entry for Town's service catalog.

## Background: how Town agents find each other

- An admin creates each Town run from a profile, which gives every role fixed capabilities. In the quote
  profile, the seller has `quote.read`.
- A buyer lists the run's participants and picks the one with `quote.read` (`find_seller` in Town).
- The Attest gate is **not** a Town participant. It is a separate web service. So a buyer can find the seller
  through Town, but it can only learn about the gate from the seller.

## What changed

### 1. The seller says how to get served (`examples/attest/attest_seller.py`)

Every `quote_refusal` now carries a `how_to_get_served` block:

```json
{
  "checked_by": "attest",
  "gate": "https://<gate address>",
  "gate_pubkey": "<the gate's public key, to check receipts>",
  "receiver": "supplierco",
  "info": "https://<gate address>/info",
  "guide": "https://github.com/smghaas/attest/blob/main/AGENTS.md",
  "attach": "what to put in the `attest` field of the quote request",
  "no_credentials": "how to get a public quote without a credential (solve the puzzle)"
}
```

The seller fetches the gate's key once, on the first refusal. Nothing in the block is secret. New setting:
`ATTEST_PUBLIC_URL`, the gate address to give other agents, if it differs from the one the seller uses
(`ATTEST_URL`).

### 2. The buyer can discover the gate (`examples/attest/attest_buyer.py`)

- `ATTEST_DISCOVER=1`: the buyer starts like Town's stock buyer. It sends a plain quote request with no
  envelope and no knowledge of any gate. It reads `how_to_get_served` from the refusal, then sends the
  request again with an envelope addressed to the receiver it learned. In this mode it ignores `ATTEST_URL`
  and `ATTEST_RECEIVER`, so a passing test proves it really learned them from the seller.
- `ATTEST_IDENTITY=<file>`: the buyer can use an agent file made with Attest's own CLI
  (`attest agent create`, saved in `~/.attest/`), instead of the test cast that `world.py` writes.
- With neither a cast nor an identity, a discovering buyer uses a fresh key with no credential (a stranger).
- Receipt checking is unchanged: the receipt must be signed by the gate, match the request, and be an
  `allow` (or `pow_ok` for a public quote).

The flow for a stranger:

```
buyer --quote_request (no envelope)---------------> seller --> refuse + how_to_get_served
buyer --quote_request (envelope, no credential)---> seller --> gate: unknown / pow_required (puzzle)
buyer --quote_request (envelope + solved puzzle)--> seller --> gate: unknown / pow_ok --> quote
```

### 3. Tests (`tests/test_attest_integration.py`): 20 → 22

- **Updated:** `test_stock_town_buyer_is_refused` also checks that the refusal included the explanation.
- **New:** `test_stranger_learns_the_gate_from_the_seller_and_gets_a_public_quote`. The buyer is told nothing,
  finds the gate through the seller, solves the puzzle and gets the right quote with a valid receipt.
- **New:** `test_self_registered_operator_is_served_through_town`. The gate runs as Sam's sandbox
  (`ATTEST_SANDBOX=1`). Using only Attest's SDK, as an outside team would, a company gets free test stake,
  registers its own seller (`town-seller`, which asks for a human above $1,500) and creates an agent. In
  Town, its quote is allowed and an $1,800 order is escalated by the seller's own rule.
- The gate start-up is now a helper (`_start_gate`) with two fixtures, `gate` and `sandbox_gate`.

### 4. Town's service catalog (`integrations/nandatown/catalog/`)

- `attest/`: what Town's on-ramp (`nandatown onramp`) generates from the gate's OpenAPI document: a draft
  `SKILL.md` and the checks it ran. Three checks passed. Two were left open: the live address and authentication.
- `REVIEW.md`: our answers for Town's reviewers. There is no live address until the sandbox is deployed.
  There is no HTTP login: every write is signed with the caller's own key. It also says which routes are
  the service, sandbox-only or demo-only.
- `make_candidate.sh`: regenerates the entry. Run it again once the gate has a public address.

### 5. Small fix (`examples/attest/world.py`)

Removed a line that set `ATTEST_LOG_DIR` after the gate module had already read it, so it had no effect.

## How to run it

From a NANDA Town checkout (Town commit `6f9a7c0`):

```bash
cp -r <attest>/integrations/nandatown/examples/attest examples/
cp <attest>/integrations/nandatown/tests/test_attest_integration.py tests/
pip install -e '.[dev]' -e <attest>
ATTEST_HOME=<attest> python -m pytest -q tests/test_attest_integration.py
```

Result on this branch: 22 passed in about 4 minutes. `python run_scenarios.py --spawn` in this repo also
passes all its scenarios.

## Known limits

- **Town's grade on two-step exchanges.** Town's `quote-clean` test expects exactly one request and one
  response, matched to the first request. Discovery sends two requests, so Town's own `correct` stage does
  not pass on those runs. The tests check the buyer's own verdict and the receipt instead. The fix would be a
  Town profile that allows a two-step exchange.
- **No public gate yet.** Outside agents need a deployed gate (see `docs/DEPLOY.md`). Until then the
  explanation points at whatever address the seller is configured with.
- **The Town proposal is behind.** [projnanda/nandatown#352](https://github.com/projnanda/nandatown/pull/352)
  still has the earlier files. To update it, copy `examples/attest/` and `tests/test_attest_integration.py`
  into the PR branch. They sit at the same paths in the Town repo. `catalog/` is not part of the PR.
- **Gate safety gap (not addressed here).** `/verify` does not check who is calling. Anyone holding a copy of
  a signed request can present it first, which uses up its nonce, so the real receiver then gets `replay`.
  The fix belongs in `gate/server.py`.

## Suggested next steps

1. Deploy the sandbox, then run `catalog/make_candidate.sh` so the catalog entry carries the real address.
2. Copy the integration files into PR #352.
3. Have receivers sign their `/verify` calls, to close the gap above.
4. Propose a two-step quote profile to Town.
