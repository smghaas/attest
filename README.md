# Attest

A Know-Your-Agent (KYA) check that one agent calls before it acts on a request from another agent.
MAS.665 AI Studio, MIT, fall 2026. Samuel Haas and Eduard Alsina.

## What it does

Before a receiving agent trusts a request, it asks the gate three questions about the sender:
who deployed you (a credential signed by the operator), what are you allowed to do (a mandate,
possibly delegated down a chain where each child may only narrow its parent), and is that still
valid (expiry, revocation with cascade). The gate answers `allow`, `refuse`, `escalate` or
`unknown`, with a reason and a receipt signed by the gate's key.

Trust comes from stake. An operator stakes to have its credentials accepted, and each receiver
sets a stake threshold. A receiver can file a slashing claim, signed with its own key and backed
by an `allow` receipt the gate issued to it (one claim per receipt). The claim waits out a dispute
window (72 h by default, 3 s in the demo): undisputed, it slashes the stake and suspends the issuer;
disputed by the issuer, it goes to an arbiter key that upholds or rejects it.
An agent with no staked issuer behind it must solve a hashcash puzzle seeded by the latest Base
Sepolia block hash and bound to its request. Difficulty doubles on each retry from the same key
within an hour, and also doubles each time the unstaked load on that receiver doubles, so fresh
keys do not escape it during a flood. It then gets `unknown`, i.e. read-only answers. Anything
that moves money from an unstaked agent is refused. The gate also keeps a running spend total per
mandate, so an order cannot be split into pieces under the limit (negative or non-numeric amounts
are refused).

A credential or mandate can only be revoked by the key that signed it (or the gate's own key); the
revoker sends the document itself so the gate can check. Revoking a mandate revokes everything
delegated below it.

## Layout

```
gate/crypto.py       Ed25519 keys, canonical JSON, sign and verify
gate/core.py         the verify logic, revocation, receipts (read this first)
gate/server.py       FastAPI: /pubkey /verify/{receiver} /revoke /claims /receipts, plus a demo inbox
agents/kya_tools.py  MCP tools for Hermes agents (buyer role and supplier role)
setup_demo.py        creates operators, agents, receiver and arbiter keys, credentials, mandates in state/agents/
run_scenarios.py     scripted scenarios, decisions compared with the design, receipts verified
demo.py              the class demo (HW7 story, live gate calls, recorded LLM results)
demo.sh              the Homework 6 demo: reset, two Hermes agents, scenario table
```

## Run

```
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python demo.py                 # class demo in the terminal: no server, LLM or network needed
.venv/bin/python run_scenarios.py --spawn  # no LLM needed; own temporary state and gate, as CI runs it
.venv/bin/python setup_demo.py
.venv/bin/uvicorn gate.server:app --port 8402 &
.venv/bin/python run_scenarios.py          # against the running gate and ./state
./demo.sh                                  # needs Hermes with the kya_* MCP servers in ~/.hermes/config.yaml
```

Run the server with one worker: the gate's state is a single JSON file guarded by an in-process
lock. `ATTEST_STATE_DIR` and `ATTEST_LOG_DIR` move the state and logs away from `./state` and `./logs`.

Hermes config (add under `mcp_servers`): three servers pointing at `agents/kya_tools.py` with env
`KYA_ROLE=buyer KYA_AGENT=acme-procurement`, `KYA_ROLE=buyer KYA_AGENT=acme-sub-greedy`,
`KYA_ROLE=supplier KYA_RECEIVER=supplierco`.

## Benchmark (HW7)

Claim, cases and metrics are pre-registered in `bench/PREREG.md`. The report is `docs/hw7_report.pdf`.

```
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-bench.txt
.venv/bin/python -m bench.run_bench          # C0 accept-all, C1 static allowlist, C4 Attest alone (no LLM)
ANTHROPIC_API_KEY=... bench/run_llm.sh       # C2 LLM supplier without Attest, C3 with Attest (Sonnet)
bench/run_llm_subscription.sh --smoke      # same, on a Claude subscription via headless Claude Code
.venv/bin/python -m bench.sybil --cap 0      # limit probe: one operator, many agents (cap off / --cap 1 on)
.venv/bin/python -m bench.latency            # gate latency, in-process and over HTTP
.venv/bin/python -m bench.report.make_report # rebuild docs/hw7_report.pdf from bench/results
```

Every run writes its config, per-request decisions, per-episode costs, LLM transcripts and a
summary to `bench/results/<run>/`.

## Documents

credential, mandate and request are JSON objects signed with Ed25519. `id` is the SHA-256 of the
body without `id` and `sig`. A request envelope is `{credential, chain: [root..leaf], request}`.
See the docstring at the top of `gate/core.py`.

## Not done yet

single local process with one worker; stakes, slashing claims, revocations and nonces live in the
gate's JSON state, not yet on chain (planned: ERC-8004 identity registry on Base Sepolia, one
contract for issuer stakes, slashing claims and the revocation root, daily log hash); only the
block hash is read from Base Sepolia today. The issuer exposure cap holds per gate, not across
gates. The arbiter is a single key. An operator can revoke a
deep delegated mandate only by revoking an ancestor it signed. No correlation penalty yet.
`escalate` only logs; one receiver policy rule.
