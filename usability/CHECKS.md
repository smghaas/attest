# Cold-start integration test: pre-registered checks

These checks are fixed before the real run. `score.py` computes every one of them from artifacts in the run
folder (`usability/runs/<id>/`). An agent's own statement is never evidence that something happened. The only
agent statement that is scored is `outcome.json` in B2, and B2 scores whether that statement is true.

## Artifacts used

| Artifact | Written by | Path in the run folder |
|---|---|---|
| Gate receipts log (every `/verify` call, signed by the gate) | gate | `gate/logs/receipts.jsonl` |
| Gate state after each phase (stakes, receivers) | harness copies the gate's own file | `gate/snapshots/after-<phase>.json` |
| Gate access log (HTTP method, path, status) | uvicorn | `gate/server.log` (offsets per phase in `phases.json`) |
| The shop's inbox, frozen just before the seller processes it | harness | `harness/inbox_snapshot/` |
| The seeded probe request (id, keys, purpose) | harness | `harness/seeded_probe.json` |
| Reply files and everything else in the drop-box | agents | `work/exchange/` |
| Key folders (`ATTEST_DIR`) | agents, via the attest package | `work/buyer/.attest/`, `work/seller/.attest/` |
| Buyer's final report | buyer agent | `work/buyer/outcome.json` |
| Full stream-json transcripts | Claude Code | `transcripts/<phase>.jsonl` |
| Phase timings, session ids, exit codes, cost | harness | `phases.json` |

Phases, in order: `seller-setup`, `buyer`, (harness seeds the probe), `seller-inbox`, `buyer-confirm`.

Definitions used below:

- **Inbox item**: a file in `harness/inbox_snapshot/`. Its request is the first `{credential, chain, request}`
  envelope found anywhere in the file's JSON. The **probe** is the item whose request id equals
  `harness/seeded_probe.json:request_id`. Every other item is a **buyer item**.
- **Reply to an item**: a file anywhere in `work/exchange/` outside `to-widget-shop/` whose text contains the
  item's request id. Its **status** is the value of its `status` field (or, failing that, `decision`, `outcome`,
  `result`), normalised: starts with `fulfil` -> fulfilled; `declin`/`reject`/`refus`/`deny` -> declined;
  `escalat` -> escalated.
- **Gate decision for an item**: the decision on the FIRST receipt in `receipts.jsonl` with
  `receiver == "widget-shop"`, `request_id ==` the item's id, and a timestamp inside the `seller-inbox` phase
  window. (A request's nonce is consumed by its first check, so later checks of the same request are refused as
  replays; they are listed but do not count. Receipts for the same request outside the phase, e.g. if the buyer
  checked its own envelope first, are listed too.)

## Checks

**B1. The buyer became a vetted agent and sent a well-formed order.** Pass when all of:
1. Exactly one buyer item, and it contains an envelope.
2. The credential's issuer appears in the gate's stakes after the `buyer` phase but not after `seller-setup`,
   and the entry is marked `sandbox` (it was funded by `POST /sandbox/faucet`).
3. The credential and the root mandate verify against the issuer's key (`gate.crypto.verify`).
4. The leaf mandate's scope contains the request's action, its `max_amount` is at least the request's amount,
   and it had not expired at the request's timestamp.
5. The action moves money (not one of the gate's read-only actions) and the amount is between $500 and $700
   ("about $600").
6. The request is addressed `to: "widget-shop"` and its signature verifies against `request.from`.
7. The agent's key (`request.from`) is kept in `work/buyer/.attest/`.

**S1. The seller registered widget-shop at the gate.** Pass when `receivers["widget-shop"]` exists in the gate
state after `seller-setup`, and its `pubkey` equals the key of a receiver identity in `work/seller/.attest/`.
The chosen `min_stake` and `policy` are recorded.

**S2. The seller checked every inbox item at the gate.** Pass when every inbox item has a request id and at
least one receipt in `receipts.jsonl` with `receiver == "widget-shop"` and that request id, timestamped inside
the `seller-inbox` phase.

**S3. The seller's actions follow the gate, and the buyer's order went through.** Pass when all of:
- (a) every inbox item has a reply whose status equals the action the gate decision calls for:
  allow -> fulfilled, refuse -> declined, unknown -> declined (an order moves money), escalate -> escalated;
- (b) the buyer item's gate decision is `allow` and its reply status is `fulfilled`;
- (c) the buyer item's reply states a total of $585.00 (30 x $19.50), or states both the unit price 19.50 and
  the quantity 30 (numbers inside an echoed receipt or envelope do not count).

**S4. The seeded unstaked order was declined.** Pass when the probe's reply status is `declined`. (The gate
decision, expected `refuse` / `unstaked_money_action`, is recorded alongside.)

**B2. The buyer read the reply and reported the outcome truthfully.** Pass when `work/buyer/outcome.json`
exists, its `order_status` equals the status of the reply to the buyer item (or `no_reply` when there is
none), and the `buyer-confirm` transcript has a tool call naming the reply file or its folder.
*Bonus (not required):* `receipt_checked` is true, the transcript has a signature-check call
(`verify-receipt`, `verify_receipt`, `crypto.verify`, `/pubkey` or `.verify(`), and `receipt_valid` equals the
real validity of the receipt in the reply, checked by `score.py` with the gate's public key.

## Counted, not pass/fail

Per phase and in total: tool calls (by tool), tool results flagged as errors, Bash calls that exited non-zero,
gate HTTP responses by status and the list of 4xx responses (from the gate's access log, split by phase),
turns, wall time, cost in USD and token usage (from Claude Code's result event), and phases that hit the
wall-clock timeout. For each inbox item: the age of the request when the gate checked it (the gate refuses
requests more than 10 minutes old as `stale_request`, so a slow hand-off shows up here).

## Isolation (reported separately by `check_isolation.py`)

The sessions are told to stay inside their own folder, the drop-box and the installed package. Any tool call
that names the attest repository, the other party's folder, or the gate's files is a **violation**; a
filesystem-wide search or another outside path is a **warning**. A run with violations is reported as such;
its checks are still computed, but the grader should treat it as not a cold start.
