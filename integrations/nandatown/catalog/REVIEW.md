# Attest in Town's service catalog: answers for the reviewer

`attest/` is what Town's on-ramp (`nandatown onramp`, Town 6f9a7c0) generated from the gate's own
OpenAPI document (`attest/openapi.json`). Regenerate it with `make_candidate.sh`. The on-ramp checks
passed except two it cannot answer from the document. The answers are below, from the Attest team.

## Open questions

**Live endpoint (`https-servers`: not_enough_evidence).** None yet. The public sandbox is ready to
deploy (`docs/DEPLOY.md`, `render.yaml`, `Dockerfile`) but not running anywhere. Once it is, its URL
goes into the gate's OpenAPI `servers` and this candidate is regenerated.

**Authentication (`auth-declared`: not_enough_evidence).** No HTTP authentication, on purpose.
- Reads (`/pubkey`, `/info`, `/receivers`, `/stakes`, `/receipts`, `/claims`, `/block`, `/stats`) are public.
- Every write that changes state carries a signature from the caller's own Ed25519 key in the body:
  the operator for `/sandbox/faucet` and `/stake`, the receiver for `/register/receiver` and `/claims`,
  the accused issuer for a dispute, the arbiter for a ruling, the document's signer for `/revoke`.
  Private keys never reach the gate.
- `/verify/{receiver}` checks the signatures inside the envelope (credential, mandates, request).
  The caller itself is not authenticated. Known gap: anyone holding a copy of a signed request can
  present it first, which uses up its nonce, so the intended receiver then gets `replay`.

## Which operations belong in a listing

| Operations | Status |
| --- | --- |
| `/verify/{receiver}`, `/pubkey`, `/info`, `/receivers`, `/receipts`, `/block` | the service |
| `/sandbox/faucet`, `/register/receiver` | self-registration; only on a gate started with `ATTEST_SANDBOX=1` |
| `/stake` | private gates only; closed (403) on the sandbox |
| `/claims`, `/claims/{id}/dispute`, `/claims/{id}/resolve`, `/revoke`, `/stakes`, `/stats` | the service (slashing, revocation, dashboard) |
| `/demo/agents`, `/demo/try` | demo only (`ATTEST_DEMO=1`); act as seeded test agents |
| `/inbox/...`, `/replies/...` | HW6 demo plumbing; off (404) on the sandbox; not part of the service |

## Side effects of the writes

`/verify` is declared a write because it records the request's nonce (replay protection), adds an
allowed order to the mandate's and the issuer's running spend, and issues proof-of-work puzzles.
No money moves anywhere: stakes are numbers in the gate's state, and on the sandbox they are free test credit.

## What Town tests show

`tests/test_attest_integration.py` (22 tests) runs the real gate behind a Town seller, including a buyer
that finds the gate from the seller's refusal and an operator that registered itself on a sandbox gate.
