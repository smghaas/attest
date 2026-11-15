# Attest: interface for agents

Attest is a **Know-Your-Agent gate**. When your agent receives a request from another agent ("send me a
quote", "place this $2,000 order", "book this trip"), it asks Attest before acting. Attest checks:

- **who** deployed the sender: a credential signed by an operator (a company), and
- **whether** that operator has stake behind it, at least as much as you require, and is not suspended, and
- **what** the sender may do: a mandate listing allowed actions and a maximum amount, possibly delegated
  down a chain where each step can only narrow the one above, and
- **whether all of that is still valid**: not expired, not revoked, the request is signed by the sender's key,
  it is addressed to you, it is fresh and has not been used before, and it does not exceed the running
  spend under the mandate.

It answers `allow`, `refuse`, `escalate` (a human should decide) or `unknown` (no staked operator behind the
sender: give public, read-only answers at most), with a reason and a **receipt signed by the gate**.

Attest does not carry messages between agents. You send the request over whatever channel you already use
(HTTP, a mailbox, A2A, a file), and the receiver hands it to the gate.

Contents: [Roles](#roles) · [Access](#access) · [Quick start](#quick-start) · [Receiver: check a request](#receiver-check-a-request) ·
[Sender: build a request](#sender-build-a-request) · [Signing up](#signing-up-sandbox) · [Unknown senders and the puzzle](#unknown-senders-and-the-puzzle) ·
[Document formats](#document-formats-and-signing) · [Limits and permissions](#limits-and-permissions) · [All endpoints](#all-endpoints) · [Errors](#errors) · [Run your own gate](#run-your-own-gate)

## Roles

| Role | Who | What it does with Attest |
|---|---|---|
| **Operator** | a company or person that deploys agents | gets stake, signs a credential and a mandate for each of its agents |
| **Sender agent** | the agent asking for something | attaches its credential and mandate chain to every request and signs the request with its own key |
| **Receiver** | the agent or service being asked | registers at the gate with its own policy, then checks every incoming request before acting |

One program can play several roles. All keys are Ed25519 and stay with their owner: the gate only sees
public keys and signatures. Never send a private key to the gate or paste one into a prompt.

## Access

| | |
|---|---|
| Gate URL | the `ATTEST_URL` you were given. Public sandbox: not deployed yet, so run your own (two commands, [below](#run-your-own-gate)). |
| Is it a sandbox? | `GET {ATTEST_URL}/info`: `mode` is `sandbox` (anyone may self-register; stake is free test credit) or `private` |
| HTTP | JSON over HTTP, no login. Machine-readable schema at `GET {ATTEST_URL}/openapi.json` |
| Command line | `pip install git+https://github.com/smghaas/attest`, then `attest --help`. Reads `ATTEST_URL` |
| Python SDK | `from attest import Attest, build_request, identity` (same package) |
| MCP server | `ATTEST_URL=... python -m attest.mcp_server` (needs `pip install "mcp>=2.0"`). Tools: `check_agent`, `verify_receipt`, `list_receivers`, `get_stakes`, `recent_decisions` |

The command line and SDK keep your keys and documents in `~/.attest/<name>.json` (override with `ATTEST_DIR`).
Those files hold private keys: keep them private.

## Quick start

On a sandbox gate, from nothing to a checked request (real output):

```bash
export ATTEST_URL=http://127.0.0.1:8402           # your gate

attest operator create AcmeCorp                     # an operator key + 10,000 free test stake
# operator AcmeCorp created, key kept in ~/.attest/AcmeCorp.json
# funded with 10,000 test stake (sandbox credit, not money)

attest receiver create acme-supplies --min-stake 5000 --escalate place_order=2000
# receiver acme-supplies registered (min stake 5,000)

attest agent create acme-buyer --operator AcmeCorp --actions request_quote,place_order --max 3000
# agent acme-buyer created: request_quote, place_order up to $3,000, chain depth 1

attest request acme-buyer acme-supplies place_order 1200 --message "PO-1: 40 units of X-200" --check
# ALLOW  ok
# issuer AcmeCorp staked 10,000 (threshold 5,000), chain depth 1, place_order 1,200 within limit 3,000.0
# receipt 34232599e5c6  signature valid

attest request acme-buyer acme-supplies place_order 2500 --check
# REFUSE  cumulative_over_limit
# 1,200 already spent under this mandate; 2,500 more would exceed 3,000 (order splitting)
```

In real use the sender and receiver are different parties:
- the **sender** runs `attest request AGENT RECEIVER ACTION AMOUNT --message "..." > envelope.json` (without `--check`
  it prints the signed envelope) and sends that JSON to the receiver;
- the **receiver** runs `attest check RECEIVER envelope.json` (or calls the HTTP endpoint) and acts on the answer.

`attest request --check` and `attest check` exit with code 0 on `allow` and 2 on any other decision. For the full JSON response put
`--json` before the command: `attest --json check acme-supplies envelope.json`.

## Receiver: check a request

### Request

`POST {ATTEST_URL}/verify/{receiver}` with the envelope you received as the JSON body. `{receiver}` is your own
registered receiver name; the request inside must be addressed to it.

```json
{
  "credential": { "type": "kya.credential/1", "agent": "e258af50…", "operator": "AcmeCorp", "issuer": "0b6d0b6a…",
                  "purpose": "agent", "issued_at": 1794772282.19, "expires_at": 1797364282.19, "id": "50026aff…", "sig": "41e1ca46…" },
  "chain": [
    { "type": "kya.mandate/1", "subject": "e258af50…", "issuer": "0b6d0b6a…", "parent": null,
      "scope": { "actions": ["request_quote", "place_order"], "max_amount": 3000.0, "currency": "USD" },
      "expires_at": 1797364282.19, "id": "27d91369…", "sig": "0cdaaa83…" }
  ],
  "request": { "type": "kya.request/1", "from": "e258af50…", "to": "acme-supplies", "action": "request_quote",
               "amount": 400.0, "message": "quote: 20 units of X-200", "ts": 1794772291.47,
               "nonce": "0fa49050544548809d75cc44811b1c4a", "id": "dc223673…", "sig": "96acc5c5…" }
}
```

Other ways to make the same call: `attest check acme-supplies envelope.json`, `Attest(url).check("acme-supplies", envelope)`
(returns a `Decision` with `.decision`, `.reason`, `.receipt`, `.receipt_verified`, `.allowed`), or the MCP tool
`check_agent(receiver, envelope)`.

### Response

```json
{
  "decision": "allow",
  "reason": "ok",
  "detail": "issuer AcmeCorp staked 10,000 (threshold 5,000), chain depth 1, request_quote 400 within limit 3,000.0",
  "receipt": {
    "type": "attest.receipt/1", "receiver": "acme-supplies", "request_id": "dc223673…", "from": "e258af50…",
    "action": "request_quote", "amount": 400.0, "decision": "allow", "reason": "ok", "detail": "…",
    "credential_id": "50026aff…", "issuer": "0b6d0b6a…", "chain_ids": ["27d91369…"], "ts": 1794772291.48,
    "gate": "67044160…", "id": "d8d751ee…", "sig": "7a4000d5…"
  },
  "repeat": false
}
```

| Field | Meaning |
|---|---|
| `decision` | `allow`, `refuse`, `escalate` or `unknown` |
| `reason` | a stable code, listed below. Branch on this, not on `detail` |
| `detail` | one sentence for humans and logs |
| `receipt` | the decision signed by the gate. `gate` is the gate's public key (also at `GET /pubkey`). Keep it: a slashing claim needs it |
| `challenge` | only for `unknown` / `pow_required`: the puzzle to solve ([below](#unknown-senders-and-the-puzzle)) |
| `repeat` | `true` when the gate had already answered this exact request for you: the same decision and the same receipt come back ([below](#checking-the-same-request-again)) |

**Check the receipt before acting.** It is genuine when `receipt.gate` equals `GET /pubkey`'s `gate`, `receipt.id`
is the SHA-256 of the receipt without `id` and `sig`, and `receipt.sig` is a valid Ed25519 signature by `gate` over the
receipt without `sig` ([signing rules](#document-formats-and-signing)). The SDK (`receipt_verified`), the CLI
(`signature valid`, `attest verify-receipt receipt.json`) and the MCP tool do this for you. Also check that
`receipt.request_id` is the request you are about to act on.

### What to do with each decision

| decision | do |
|---|---|
| `allow` | act, but only the action and amount in the request |
| `refuse` | do not act; tell the sender `reason` |
| `escalate` | do not act on your own; a human at your side should decide (your own policy asked for this) |
| `unknown` | no staked operator is behind the sender. Give public, read-only answers at most. `pow_ok` means it solved the puzzle; it still gets nothing that moves value |

### Checking the same request again

You may check the same envelope again: after a crash, a retried tool call, or a message delivered twice. The gate
then returns its **original** answer and receipt with `"repeat": true`. Nothing is counted again and no new receipt is
written. A repeat is the same request, not a new one: **act on a request at most once.** If your own records show you
already acted on it, do not act again. If you have no record of acting (you crashed before you did), act on the
decision now. Only an unchanged request (same id, valid signature, same credential and chain) at the same receiver is
answered this way, within 20 minutes of the first check; anything else reusing its `nonce` is refused as `replay`.

### Reason codes

| reason | decision | meaning |
|---|---|---|
| `ok` | allow | every check passed |
| `policy_escalation` | escalate | the amount is above your own `escalate_above` threshold |
| `pow_required` / `pow_ok` | unknown | unstaked sender, read-only action: puzzle set / puzzle solved |
| `unstaked_money_action` | refuse | unstaked, suspended or under-staked operator, and the action moves value |
| `bad_pow` | refuse | puzzle answer wrong, expired, or not for this request |
| `bad_request_signature` | refuse | the request was changed after signing, or signed by a different key than `from` |
| `wrong_receiver` | refuse | the request is addressed to someone else |
| `stale_request` | refuse | the request's `ts` is more than 10 minutes from the gate's clock |
| `replay` | refuse | a different or altered request reuses a `nonce` the gate has already seen (the same unchanged request gets `repeat: true` instead) |
| `bad_credential_signature`, `bad_credential_id` | refuse | the credential was forged or altered |
| `credential_expired`, `credential_revoked` | refuse | the credential is no longer valid |
| `no_mandate`, `bad_root_mandate`, `root_subject_mismatch` | refuse | no mandate, or the first mandate is not from the credential's operator for this agent |
| `bad_mandate_signature`, `bad_mandate_id`, `broken_chain`, `wrong_delegator` | refuse | a mandate in the chain is forged, altered or out of order |
| `attenuation_violation` | refuse | a delegated mandate is wider than its parent (more actions, higher amount, other currency, later expiry) |
| `mandate_expired`, `mandate_revoked` | refuse | a mandate in the chain expired or was revoked (revoking one revokes everything below it) |
| `request_from_mismatch` | refuse | the request is not from the holder of the last mandate |
| `out_of_scope` | refuse | the action is not in the mandate |
| `over_limit` | refuse | the amount is above the mandate's `max_amount` |
| `cumulative_over_limit` | refuse | earlier allowed requests plus this one exceed a mandate's `max_amount` (stops order splitting) |
| `issuer_exposure_over_stake` | refuse | the operator's agents together would move more than its stake in 30 days |
| `bad_amount` | refuse | the amount is negative or not a number |
| `unknown_receiver` | refuse | no receiver with that name is registered |
| `no_request` | refuse | the envelope has no `request` |

## Sender: build a request

Attach your credential and mandate chain to every request and sign the request with your own agent key.
You never need to call the gate yourself.

```bash
attest request acme-buyer acme-supplies place_order 1200 --message "PO-1: 40 units of X-200" > envelope.json
```

```python
from attest import build_request, identity
env = build_request(identity.load("acme-buyer"), to="acme-supplies", action="place_order",
                    amount=1200, message="PO-1: 40 units of X-200")
# send env (a dict, JSON-serialisable) to the receiver over your own channel
```

Rules the receiver's gate will apply:
- `to` must be the receiver's registered name at the gate.
- Build a new envelope for every new request: each carries a random `nonce`. Sending the same envelope again is
  safe; the receiver's gate recognises it as the same request (`repeat`), so it is never counted twice.
- It must be checked within 10 minutes of being built (`ts`).
- `amount` is in the mandate's currency (USD). Use `0` for actions without an amount.
- Any action name is allowed if it is in your mandate. Actions count as moving value (running totals,
  stake rules) unless they are read-only: `request_quote`, `search`, `read`, `lookup`, `get_status`, or ones the
  receiver lists in its own `read_only` policy.

## Signing up (sandbox)

On a sandbox gate (`/info` shows `"mode": "sandbox"`) anyone can sign up. Stake is free test credit, not money.

```bash
attest operator create MyCompany                    # key pair + 10,000 test stake, once per key
attest agent create my-buyer --operator MyCompany --actions request_quote,place_order --max 2000 --days 30
attest agent delegate my-sub --parent my-buyer --actions request_quote --max 500   # a narrower sub-agent
attest receiver create my-shop --min-stake 5000 --escalate place_order=1500 --read-only check_stock
attest ids                                          # what is stored locally
```

Creating an agent and delegating happen on your machine (your operator key signs); only the operator's stake and
the receiver's registration go to the gate. Over HTTP:

- `POST /sandbox/faucet` with `{"issuer": <operator pubkey hex>, "name": <name>, "sig": <sig>}`, where `sig` signs
  `{"faucet": true, "issuer": <pubkey>, "name": <name>}`. Name: 2–32 letters, digits, space, `.`, `_` or `-`; unique.
  One grant per key.
- `POST /register/receiver` with `{"name", "pubkey", "min_stake", "policy", "sig"}`, where `sig` signs
  `{"register_receiver": <name>, "pubkey": <pubkey>, "min_stake": <min_stake>, "policy": <policy>}` with the
  receiver's own key (numbers exactly as sent). Name: 3–32 lowercase letters, digits and dashes, first come first
  served, cannot be changed later. `policy` may contain only
  `"escalate_above": {"<action>" or "*": <amount>}` and `"read_only": ["<action>", ...]` (at most 50).

On a private gate, the gate's owner registers receivers and records stake (`POST /stake` is signed by the operator).

## Unknown senders and the puzzle

A sender with no staked operator behind it (no credential, an unstaked operator, or too little stake for
this receiver) gets `unknown` / `pow_required` for read-only actions, with a `challenge`:

```json
{"decision": "unknown", "reason": "pow_required",
 "detail": "no credential presented; solve a 16-bit puzzle seeded by Base Sepolia block 59252115 (base-sepolia) and resend",
 "challenge": {"challenge_id": "8735ebfd93e5d405", "seed": "8735ebfd…", "bits": 16, "block": {"number": 59252115, "hash": "0x621d…"},
               "rule": "find nonce so that sha256(seed + ':' + nonce) starts with `bits` zero bits"}}
```

Find a string `nonce` (for example a decimal counter) so that `sha256(seed + ":" + nonce)` starts with `bits` zero
bits (`from gate.core import solve_pow; solve_pow(seed, bits)` does this). Then present **the same envelope** again
with `"pow": {"challenge_id": ..., "nonce": ...}` added. The answer is `unknown` / `pow_ok`: public, read-only answers.
Puzzles expire after 10 minutes and get harder with retries from the same key and with load on the receiver.
Anything that moves value from an unstaked sender is refused outright (`unstaked_money_action`).

## Document formats and signing

Everything is JSON signed with Ed25519, so any language can take part.

- **Canonical form**: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8.
- **id**: SHA-256 (hex) of the canonical form of the document **without** `id` and `sig`.
- **sig**: Ed25519 signature (hex) over the canonical form of the document **with** `id` and **without** `sig`.
- Keys are raw 32-byte Ed25519 keys, hex-encoded (64 characters).

| Document | Fields (besides `id`, `sig`) | Signed by |
|---|---|---|
| credential | `type: "kya.credential/1"`, `agent` (agent pubkey), `operator` (name), `issuer` (operator pubkey), `purpose`, `issued_at`, `expires_at` (Unix seconds) | operator |
| mandate | `type: "kya.mandate/1"`, `subject` (agent pubkey), `issuer` (signer pubkey), `parent` (id of the mandate above, or `null`), `scope: {actions: [...], max_amount, currency: "USD"}`, `expires_at` | the operator for the first mandate; the parent mandate's holder for a delegated one |
| request | `type: "kya.request/1"`, `from` (agent pubkey), `to` (receiver name), `action`, `amount`, `message`, `ts` (Unix seconds), `nonce` (random, unique) | the agent |
| envelope | `{credential, chain: [first mandate … last mandate], request, pow?}` (not signed itself) | — |

## Limits and permissions

- **Sandbox stake is not money.** Anyone can mint test stake, so a sandbox `allow` shows the checks pass, not that a
  real company stands behind the agent. Real stake would come from a chain or an escrow (not built).
- **Everything is public.** `GET /receipts` lists every decision from everyone. Do not put secrets in `message`.
- **Checking again** returns the first answer (`repeat: true`) for 20 minutes; after that the request is stale anyway.
  Keeping track of what you already acted on is your job: the gate only says the request is the same.
- **Clock**: requests are accepted within ±10 minutes of the gate's clock.
- **Rate limits on the sandbox**, per IP and per minute: 120 checks, 20 writes (sign-up, claims, revocations).
  HTTP 429 means wait a minute (`Retry-After`). Bodies over 64 KB get 413. The sandbox holds at most 500 operators
  and 500 receivers.
- **Who may change what**: only a document's signer (or the gate) can revoke it (`POST /revoke`); only a receiver's
  key can file a slashing claim, and only with an `allow` receipt the gate issued to it; only the accused operator can
  dispute; only the arbiter key can rule.
- **One gate**: the gate is one process with its state in one file. Limits such as running spend hold per gate.
- **Not checked**: who calls `/verify`. Anyone holding a copy of an envelope can present it first; the receiver then
  gets the same answer marked `repeat`, so it cannot be locked out, but it cannot tell who asked first.

## All endpoints

| Method and path | Use |
|---|---|
| `GET /info` | mode (sandbox or private), faucet amount, limits |
| `GET /pubkey` | the gate's public key, to check receipts |
| `POST /verify/{receiver}` | **the check** |
| `GET /receivers` | registered receivers, their thresholds and policies |
| `GET /stakes` | operators' stake and status |
| `GET /receipts?limit=20` | recent signed decisions (public) |
| `POST /sandbox/faucet`, `POST /register/receiver` | sandbox sign-up ([above](#signing-up-sandbox)) |
| `POST /stake` | add stake (private gates only) |
| `POST /revoke` | `{document, by, sig, reason}`; `sig` by `by` over `{"revoke": <document id>, "by": <pubkey>, "reason": <reason>}` |
| `POST /claims` | slashing claim: `{receipt, breach, sig}`; `sig` by the receiver over `{"claim": <receipt id>, "receiver": <name>, "breach": <text>}` |
| `POST /claims/{id}/dispute`, `POST /claims/{id}/resolve` | the accused operator disputes; the arbiter rules |
| `GET /claims`, `GET /stats`, `GET /block` | claims, counts, the chain block puzzles are seeded with |
| `GET /` | web page for people |
| `GET /ledger/...` | agent history ledger, only when the gate runs with `ATTEST_LEDGER=1` (see `gate/ledger_api.py`) |

A slashing claim waits out a dispute window (72 hours by default; the seeded demo world sets 3 seconds). If undisputed it takes half the operator's stake
and suspends it; after that its agents count as unstaked everywhere.

## Errors

A decision is always HTTP 200, even `refuse`. Other status codes mean the call itself failed:

| Status | Meaning |
|---|---|
| 400 | malformed or badly signed sign-up, claim or revocation (the body's `detail` says which, e.g. `bad_signature`) |
| 403 | not allowed on this gate (e.g. `/stake` on a sandbox, sign-up on a private gate) |
| 404 | unknown path, or a demo route switched off |
| 409 | that receiver name is taken |
| 413 / 429 / 503 | body too large / rate limit / the sandbox is full |

## Run your own gate

```bash
pip install git+https://github.com/smghaas/attest
ATTEST_SANDBOX=1 ATTEST_STATE_DIR=./state ATTEST_LOG_DIR=./logs uvicorn gate.server:app --port 8402   # one worker only
export ATTEST_URL=http://127.0.0.1:8402
```

`ATTEST_STATE_DIR` and `ATTEST_LOG_DIR` say where the gate keeps its state (including its own signing key) and its
receipts; set them, or they default to folders next to the installed package. Without `ATTEST_SANDBOX=1` it is a
private gate. Docker and hosting: `docs/DEPLOY.md`. A seeded demo world with test agents: `python setup_demo.py` from a
checkout. Tests: `pytest tests` and `python run_scenarios.py --spawn`.
