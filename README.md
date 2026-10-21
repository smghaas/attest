# Attest

A Know-Your-Agent (KYA) check that one agent calls before it acts on a request from another agent.
MAS.665 AI Studio, MIT, fall 2026. Samuel Haas and Eduard Alsina.

## What it does

Before a receiving agent trusts a request, it asks the gate three questions about the sender:
who deployed you (a credential signed by the operator), what are you allowed to do (a mandate,
possibly delegated down a chain where each child may only narrow its parent), and is that still
valid (expiry, revocation with cascade). The gate answers `allow`, `refuse`, `escalate` or
`unknown`, with a reason and a receipt signed by the gate's key.

Trust comes from stake. An operator stakes to have its credentials accepted, each receiver sets a
stake threshold, and a breach proven with a gate receipt slashes the stake and suspends the issuer.
An agent with no staked issuer behind it must solve a hashcash puzzle seeded by the latest Base
Sepolia block hash and bound to its request (difficulty doubles on each retry from the same key);
it then gets `unknown`, i.e. read-only answers. Anything that moves money from an unstaked agent
is refused. The gate also keeps a running spend total per mandate, so an order cannot be split
into pieces under the limit.

## Layout

```
gate/crypto.py       Ed25519 keys, canonical JSON, sign and verify
gate/core.py         the verify logic, revocation, receipts (read this first)
gate/server.py       FastAPI: /pubkey /verify/{receiver} /revoke /receipts, plus a demo inbox
agents/kya_tools.py  MCP tools for Hermes agents (buyer role and supplier role)
setup_demo.py        creates operators, agents, credentials, mandates in state/agents/
run_scenarios.py     17 scripted scenarios, decisions compared with the design, receipts verified
demo.sh              the Homework 6 demo: reset, two Hermes agents, scenario table
```

## Run

```
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python fastapi uvicorn httpx cryptography mcp pydantic
.venv/bin/python setup_demo.py
.venv/bin/uvicorn gate.server:app --port 8402 &
.venv/bin/python run_scenarios.py          # no LLM needed
./demo.sh                                  # needs Hermes with the kya_* MCP servers in ~/.hermes/config.yaml
```

Hermes config (add under `mcp_servers`): three servers pointing at `agents/kya_tools.py` with env
`KYA_ROLE=buyer KYA_AGENT=acme-procurement`, `KYA_ROLE=buyer KYA_AGENT=acme-sub-greedy`,
`KYA_ROLE=supplier KYA_RECEIVER=supplierco`.

## Documents

credential, mandate and request are JSON objects signed with Ed25519. `id` is the SHA-256 of the
body without `id` and `sig`. A request envelope is `{credential, chain: [root..leaf], request}`.
See the docstring at the top of `gate/core.py`.

## Not done yet (as of HW6)

single local process; stakes, slashing, revocations and nonces live in the gate's JSON state, not
yet on chain (planned: ERC-8004 identity registry on Base Sepolia, one contract for issuer stakes,
slashing claims and the revocation root, daily log hash); only the block hash is read from Base
Sepolia today. No dispute window before a slash, no correlation penalty yet. `escalate` only logs;
one receiver policy rule.
