# Attest for agents

Attest answers one question before you act on a request from another agent: is the sender who it
claims, is a staked operator behind it, and is this action inside its mandate?

## If you are the receiver (you got a request from another agent)

1. Take the envelope you received: `{credential, chain, request}`.
2. Call the gate: `POST {ATTEST_URL}/verify/{your_receiver_name}` with the envelope as the JSON body.
   Or use the SDK (`attest.Attest(url).check(receiver, envelope)`) or the MCP tool `check_agent`.
3. Read `decision`:
   - `allow`: act, within the request's action and amount.
   - `refuse`: do not act. `reason` says why (for example `attenuation_violation`, `unstaked_money_action`, `revoked`).
   - `escalate`: ask a human. Your policy requires it for this action or amount.
   - `unknown`: no staked operator is behind the sender. Give public, read-only answers at most.
4. Check `receipt_verified` (the SDK and MCP tool do it for you). Never act on a decision whose receipt
   does not verify. Keep the receipt: it is your evidence if you later file a slashing claim.

## If you are the sender

Attach your credential and mandate chain to every request and sign the request with your own key.
`attest.build_request(agent, to, action, amount, message)` builds the envelope. You never call the gate.

## If you are new and have no credentials (public sandbox)

Check `GET {ATTEST_URL}/info`. If `mode` is `sandbox`, you can register yourself, and `stake_is_real` is false:
stake is free test credit. Generate an Ed25519 key, then `POST /sandbox/faucet` `{issuer, name, sig}` where `sig` signs
`{"faucet": true, "issuer": <pubkey>, "name": <name>}`. Receivers register with `POST /register/receiver`
(see `attest/client.py` for the exact signed bodies). Or just use the CLI: `attest operator create`, `attest receiver create`,
`attest agent create`. Rate limits are per IP; a 429 means wait a minute. Never send a private key to the gate.

## Setup

- Gate URL: `ATTEST_URL`, default `http://127.0.0.1:8402`.
- Start a gate: `python setup_demo.py && uvicorn gate.server:app --port 8402` (one worker).
- MCP server: `python -m attest.mcp_server`.
- Do not paste private keys into prompts. Demo keys live in `state/agents/` and are test keys only.
- Run the tests: `pytest tests/test_interface.py`, and `python run_scenarios.py --spawn` for the 17 scenarios.
