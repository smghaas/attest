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

## Use it

Start a gate and open the dashboard (demo mode lets you try the demo agents from the page):

```
.venv/bin/python setup_demo.py
ATTEST_DEMO=1 .venv/bin/uvicorn gate.server:app --port 8402     # http://127.0.0.1:8402/
```

**Human, no install:** on a sandbox gate the page at `/` has "Use Attest in your browser": create a company
(free test stake), an agent or sub-agent, your own receiver, send a request and see allow, refuse or escalate, then
register a passport and record trades on the ledger. Keys are made and kept in the browser (Ed25519 via WebCrypto);
the gate only sees signatures. Keys can be downloaded, and keys made with the CLI can be imported.

**Human:** the dashboard at `/` also shows live decisions, stake behind each operator, receivers and slashing
claims, and has a "Check an agent" box (a demo agent, or a pasted envelope).

**Terminal:** `pip install git+https://github.com/smghaas/attest` (or `pip install -e .` in a checkout) gives an
`attest` command. Add `[mcp]` (`pip install "attest[mcp] @ git+https://github.com/smghaas/attest"`) for the MCP server.

```
attest status
attest receipts -n 10
attest check supplierco envelope.json      # exit code 0 allow, 2 anything else
attest verify-receipt receipt.json
```

**Python:**

```python
from attest import Attest
d = Attest("http://127.0.0.1:8402").check("supplierco", envelope)   # envelope = {credential, chain, request}
if d.allowed and d.receipt_verified:
    ...                                                              # d.decision is allow, refuse, escalate or unknown
```

**Any agent (MCP):** `python -m attest.mcp_server` exposes `check_agent`, `verify_receipt`,
`list_receivers`, `get_stakes` and `recent_decisions`. Set `ATTEST_URL` to point at your gate.
Agents reading this repo should start with [AGENTS.md](AGENTS.md). The raw HTTP API is described at `/openapi.json`.

## Check any agent for any task (people and agents alike)

`POST /check` lets anyone, a person or an agent, check an agent for one task and choose what must be true:
that it signed the request itself, that a person or company stands behind it, that it may do this task at
this amount, that its backer put money down, or that it did proof of work instead, its track record, and
whether a person must approve. The answer is a checklist signed by the gate. Nothing is spent, so no one
needs permission to ask. In Python: `Attest(url).check_task(envelope, "bob", allowed=True, work_bits=16)`.
On a web page, `gate/static/widget.js` draws it as a reverse CAPTCHA: a box that confirms the agent.

## Attest Town: a market that runs by itself

`sim/market.py` runs agents and shops as separate programs with their own keys, trading on their own through
the real gate: honest buyers, a person with no money down who does proof of work, an impostor, a helper with
more power than its maker, an order splitter, a big spender whose orders go to a person, a reseller that keeps
getting disputed (and gets turned away once its record is bad), and one whose ID is cancelled halfway.
Open `/static/market/` on a gate started with `ATTEST_DEMO=1 ATTEST_SANDBOX=1 ATTEST_LEDGER=1`: start it,
pause, step one transaction at a time, change the speed, edit the agents, shops and actions, click any
transaction to see every signature checked again, and click any agent for its card and full history
(`GET /card/<agent key>` works for any agent on the ledger).

## Try it on a public sandbox (anyone, no setup from the gate owner)

A gate started with `ATTEST_SANDBOX=1` lets strangers register themselves. Stake there is free test credit,
and every decision is public. Your keys are created and kept on your machine (`~/.attest`); the gate only sees signatures.

```
export ATTEST_URL=https://<the-gate>
attest operator create MyCompany                 # 10,000 test stake, once per key
attest receiver create my-desk --min-stake 5000 --escalate place_order=8000
attest agent create my-buyer --operator MyCompany --actions request_quote,place_order --max 10000
attest agent delegate my-sub --parent my-buyer --actions place_order --max 50000    # wider than its parent
attest request my-buyer my-desk place_order 2000 --check      # ALLOW
attest request my-sub my-desk place_order 100 --check         # REFUSE: attenuation_violation
```

The same steps exist in Python (`attest.identity`, `Attest.faucet`, `Attest.register_receiver`) and as plain HTTP
(`POST /sandbox/faucet`, `POST /register/receiver`, `POST /verify/{receiver}`; see `/openapi.json`).
To run your own sandbox, see [docs/DEPLOY.md](docs/DEPLOY.md) (Dockerfile and a Render blueprint are included).
The sandbox proves the decision logic. It does not prove an operator has money, because anyone can mint test stake.

## NANDA Town

How agents in NANDA Town find the gate through the seller, the Town tests, and Town's catalog entry:
[docs/NANDATOWN.md](docs/NANDATOWN.md).

## Agent history ledger (Bitcoin's ideas applied to agent history)

`network/` is a small chain that records what agents have done, with no referee. A **passport** (operator and
agent both sign, plus a registration puzzle) says who an agent is. An **outcome** is a trade signed by both sides,
recorded as accepted or disputed: we record, we do not judge. Blocks need a hashcash puzzle and a staked validator's
signature, and become final once validators holding more than 2/3 of stake voted for them. Nobody can reorganise below
a final block. A validator that signs two blocks at one height is slashed. A light client keeps only block headers,
checks each record against a Merkle branch, and asks several nodes so one lying node cannot hide a bad record.

```
python network_demo.py                       # 4 nodes, 11 steps, writes docs/network_demo.json
ATTEST_LEDGER=1 uvicorn gate.server:app      # serve it: /ledger/info, /ledger/agents, /ledger/agents/{pub}/history
attest passport create my-buyer              # then: attest trade SELLER BUYER REQ AMOUNT, attest history, attest ledger
```

Limits, stated plainly: the gate runs all four validators in one process, so this shows the protocol, not independent
nodes. The chain proves two parties signed an outcome, not that the service was good. Wash trading is capped
(2 credits per counterparty), not solved. A buyer can mark honest trades disputed and drag a seller's record down;
this is the next thing to design. The stake table is a stand-in for a public chain.

## Layout

```
gate/crypto.py       Ed25519 keys, canonical JSON, sign and verify
gate/core.py         the verify logic, revocation, receipts (read this first)
attest/              SDK (client.py), CLI (cli.py) and MCP server (mcp_server.py)
gate/static/         the web dashboard, served at /
network/             the history ledger: ledger.py (records, blocks, Merkle), node.py (nodes, finality, light client)
gate/ledger_api.py   the ledger over HTTP
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
