# Run a public Attest sandbox

A sandbox lets strangers (people and agents) try Attest without asking you for anything. Stake is free
test credit, nothing real is at risk, and the page says so. The decisions, signatures and receipts are the
real logic.

## What a stranger can do

- Open `/` and check the seeded demo agents (`ATTEST_DEMO=1`), or paste an envelope.
- Create their own operator, receiver and agents with the `attest` CLI or the Python SDK. Keys stay on their
  machine; the gate only sees signatures. Each new operator gets 10,000 test stake once, per key.
- Call `POST /verify/{receiver}` from any language, or use the MCP server.

## Deploy (you do this under your own account)

**Render:** New, Blueprint, pick this repo. `render.yaml` sets a Docker service and a 1 GB disk for state.

**Anywhere with Docker:**

```
docker build -t attest .
docker run -p 8402:8402 -v attest-data:/data attest
```

First boot seeds the demo world (receiver `supplierco`, demo agents). State lives in `/data`; keep the disk
or the gate forgets everyone on restart.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ATTEST_SANDBOX` | `1` in the Docker image | Turns on self-registration and the guards below. Off means a private gate. |
| `ATTEST_DEMO` | `1` in the Docker image | Lets the page act as the seeded demo agents. They hold test keys only. |
| `ATTEST_SANDBOX_STAKE` | 10000 | Free stake per new operator |
| `ATTEST_RATE_WRITE` / `ATTEST_RATE_CHECK` | 20 / 120 | Requests per minute per IP, for registration and for checks |
| `ATTEST_MAX_ISSUERS` / `ATTEST_MAX_RECEIVERS` | 500 | Caps so the state file cannot grow without limit |
| `ATTEST_MAX_BODY` | 65536 | Largest request body, bytes |
| `ATTEST_TRUST_PROXY` | `1` in the Docker image | Read the client IP from `X-Forwarded-For` (needed behind a host's proxy) |

## What the sandbox is not

- Not real security. Anyone can mint stake, so a sandbox decision proves the logic works, not that an operator
  has money behind it. Real stake means reading it from a chain (Base) or an escrow.
- Everything is public. `/receipts` lists every decision from every user. Do not put anything private in it.
- One worker only. The state is one JSON file behind a lock, which is fine for a class, not for scale.
- The arbiter for disputed slashing claims is the demo key. A production gate needs its own.
