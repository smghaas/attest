# HW9: agent usability work (technical notes)

What was built and changed for HW9 (agent usability), why, where it lives, and how to reproduce it. This file
documents the work; it is not the homework write-up. Branch `claude/wonderful-tesla-8o7z43`, built on `main` at
`ecde861`.

| Part | What | Main files |
|---|---|---|
| 1 | Interface documentation for agents, plus two bugs found by following it | `AGENTS.md`, `attest/identity.py`, `pyproject.toml` |
| 2 | Cold-start test: fresh agents use Attest from `AGENTS.md` alone | `usability/` |
| 3 | Crash-and-retry test, and the improvement it motivated (repeat checks) | `gate/core.py`, `tests/test_repeat.py`, `usability/runs/crash-*` |

## Part 1: the interface document

`AGENTS.md` was rewritten so an agent can use Attest without reading the code. It covers:
- what Attest offers and the three roles (operator, sender agent, receiver);
- access: HTTP, CLI, Python SDK and MCP;
- a quick start with real output;
- the full request and response, every field, all reason codes and what to do with each decision;
- sandbox sign-up with the exact signed bodies;
- the puzzle for unstaked senders;
- canonical JSON and signing rules, so other languages can take part;
- limits and permissions, every endpoint, HTTP errors, and how to run a gate.

The README now points agents to it in its first lines.

Following the document step by step on a fresh sandbox gate found two bugs a newcomer would hit. Both are fixed
in commit `3850b2d`.

**Sub-agents made with the CLI defaults were always refused** (`attest/identity.py`, `delegate`).
- `attest agent delegate` gave the child mandate a fresh lifetime (`--days`, default 30). That outlived its
  parent, so the gate refused every such sub-agent as `attenuation_violation` ("child expires after its parent").
- The child's expiry is now cut at the parent's.
- Regression test: `tests/test_onboarding.py::test_a_delegated_sub_agent_made_with_the_cli_defaults_is_accepted`.
  It fails before the fix and passes after.

**A pip install could not start the gate** (`pyproject.toml`).
- The package left out `network/`, which `gate/ledger_api.py` imports, and the `gate/static/town/` pages.
  `uvicorn gate.server:app` then failed with `ModuleNotFoundError: network`.
- Checked by installing into a clean virtualenv, then starting the gate, serving `/static/town/index.html` and
  signing up an operator.

Smaller corrections to the document:
- `--json` goes before the CLI command.
- With a pip install, `ATTEST_STATE_DIR` and `ATTEST_LOG_DIR` must be set, or the state goes next to the package.
- The MCP server needs `mcp>=2.0`.
- The dispute window is 3 s in the seeded demo world.

## Part 2: the cold-start test (`usability/`)

### Design

Two fresh Claude Code sessions run headless (`claude -p`) and have never seen the repository: a buyer and a shop
(seller). Each gets only:
- `AGENTS.md`,
- the URL of a fresh sandbox gate,
- a shared drop-box folder,
- a task.

There is no call sequence and no expected answer. Unless noted, the sessions run Claude Code's default model, which
was Opus 5.5 for every default run here (read from the transcripts; earlier commit messages say "Sonnet" in error). The harness only takes turns; it never relays or edits a
message. Full details are in `usability/README.md`.

| Phase | Session | Task |
|---|---|---|
| `seller-setup` | seller, new | register the shop `widget-shop` at the gate, choosing its own stake threshold and policy |
| `buyer` | buyer, new | buy 30 × X-200 for about $600 from `widget-shop`, which only deals with agents it can vet through Attest; drop the order in `exchange/to-widget-shop/` |
| harness | — | seed one probe order from an operator with **no stake** (`place_order`, $487.50) |
| `seller-inbox` | seller, resumed | vet every inbox file with Attest, reply fulfilled, declined or escalated, keep the receipts |
| `buyer-confirm` | buyer, resumed | read the reply, check the receipt, write `outcome.json` |

How the sessions are isolated:
- Each runs in its own folder under `/tmp/attest-coldstart/<run>/`, outside the repository.
- Attest is installed as a regular, non-editable package.
- The only tools are Bash, Read, Write, Edit, Glob and Grep: no web, no MCP servers, no skills.
- Environment variables from the host session are stripped.

`usability/check_isolation.py` scans every tool call for access to the repository, the other party's folder or
the gate's files. Isolation is instructed and checked, not enforced.

### Pre-registered checks

The checks are in `usability/CHECKS.md`, committed in `20655be` before the real run. `usability/score.py`
computes them from artifacts only: the gate's signed receipts log, gate state snapshots, the drop-box files and
the transcripts. An agent's own claim never counts as evidence.

| Check | Pass when |
|---|---|
| B1 | the buyer got faucet stake, created an agent whose mandate covers the order, and sent a signed order of $500–700 addressed to `widget-shop` |
| S1 | the seller registered `widget-shop` with a key it holds |
| S2 | every inbox item has a gate receipt for `widget-shop` from the inbox phase |
| S3 | every reply follows the gate decision; the buyer's order was `allow` and fulfilled at $585 (30 × $19.50) |
| S4 | the unstaked probe was declined |
| B2 | the buyer's `outcome.json` matches the real reply (bonus: it checked the receipt signature correctly) |

### Result: run `real-1`

All checks and the bonus passed (docs at `3850b2d`).
- The seller registered `widget-shop`: minimum stake 5,000, escalate anything above $1,000.
- The buyer created the operator "BrightforgeIndustries" and an agent allowed up to $700, then ordered $600.
- The gate allowed the order; the seller fulfilled it at $585.
- The gate refused the probe as `unstaked_money_action`; the seller declined it.
- The buyer reported "fulfilled, $585" and verified the gate's receipt.
- Totals: 12 tool calls, 0 tool errors, 0 gate 4xx, 86 s, $0.73.
- Isolation: clean. The first scan reported 7 warnings, all URLs read as file paths; the checker was fixed and
  the original verdict is kept in `summary.json`.

## Part 3: crash and retry, and repeat checks

### The scenario

`run_coldstart.py --crash-retry` adds a phase, `seller-retry`. After `seller-inbox`:
- the harness simulates a crash of the shop between checking and replying;
- it moves the shop's replies and saved receipts to `harness/crash_removed/` (keys are kept);
- a **fresh** seller session handles the same inbox, told only that it was restarted after a crash and lost its work.

Check R1 was added to `CHECKS.md` before any crash run (commit `1576ca9`). It passes when, at the end, the buyer's
order has a `fulfilled` reply carrying a genuine `allow` receipt, and the probe is declined.

### Before the fix (gate `1576ca9`)

Prediction: the gate refuses a second check of the same request as `replay`, so the shop declines a good order.
What happened, in both runs: the restarted shop had read in `AGENTS.md` that a request can be checked only once.
It did not re-check. Instead it searched the gate's **public log of every decision** (`GET /receipts`) for its old
receipts, verified them and replied.

| Run | Model | Public-log reads | Re-checks | Tool calls | Turns | Wall time | Buyer's order |
|---|---|---|---|---|---|---|---|
| `crash-before-1` | Opus 5.5 | 2 | 0 | 4 | 5 | 38.0 s | fulfilled (R1 pass) |
| `crash-before-haiku-1` | Haiku | 2 | 0 | 7 | 8 | 31.5 s | escalated |

The work-around succeeds, but recovery depends on a public log of everyone's decisions, which `AGENTS.md` itself
lists as a privacy limit. A client that simply re-checks gets `replay`.

### The improvement: repeat checks

Commit `df78644`, in `gate/core.py`:
- `Gate.verify` first looks for an earlier answer to the same request, via `Gate._earlier_answer`.
- If the same unchanged request is checked again at the same receiver within 20 minutes, the gate returns the
  original decision and receipt with `"repeat": true`. An unchanged request has the same id, a valid signature,
  and the same credential and mandate chain.
- Nothing is counted again and no new receipt is written.
- A different or altered request that reuses the nonce is still refused as `replay`.
- Answers are stored in the gate state under `answered` (request id → receipt) and pruned with the nonces.
- Every response now has `repeat: false|true`.

Side effect: a third party that presents a copy of a request first can no longer lock the receiver out. The
receiver gets the same answer, marked as a repeat.

Follow-on changes:
- **`tests/test_repeat.py`:** 8 focused tests. 7 fail on the old gate; all pass now. The one that already passed
  checks that nothing is counted twice, which also held before because the old gate refused repeats.
- **`run_scenarios.py`:** scenario 15 is split into 15a (a repeat returns the original answer) and 15b (reusing a
  nonce is still `replay`).
- **`bench/conditions.py`:** the benchmark's C4 condition treats a repeat as a duplicate and declines it. The
  deterministic rerun in `bench/results/hw9-repeat-check` is unchanged: C4 blocks 100% with 0% false refusals.
- **SDK and MCP:** `Decision.repeat` in the SDK; the MCP tool `check_agent` returns `repeat`.
- **`AGENTS.md`:** a new section, "Checking the same request again". Act on a request at most once: if your
  records show you acted, don't act again; if you have none, act now. The `replay` reason and the limits were
  updated to match.
- **NANDA Town seller:** it treats `repeat` as a duplicate. With the journal off, a double delivery is now
  applied once, where before it got `replay`. Its test and README row were updated.

### After the fix (gate `df78644`)

| Run | Model | Public-log reads | Re-checks | Tool calls | Turns | Wall time | Buyer's order |
|---|---|---|---|---|---|---|---|
| `crash-after-1` | Opus 5.5 | 0 | 1 (2 `/verify` requests) | 3 | 4 | 29.7 s | fulfilled; all checks pass |
| `crash-after-haiku-1` | Haiku | 0 | 2 (4 `/verify` requests) | 4 | 5 | 27.6 s | escalated |

In both after-runs the gate answered with the original receipts marked `repeat: true`, and no new receipts were
written.

### What did not improve, or remains unproven

- **Haiku's shops escalated the order.** Haiku's shops chose to escalate orders above $200 (before) and $500
  (after), so the $600 order was escalated both times. The replies follow the gate, but R1 and S3(b) assume a
  shop that accepts $600, so they fail by the shop's own policy choice, not by error.
- **The gate cannot tell who asked first.** Whether an order was already fulfilled is still the receiver's job.
- **Few runs.** One run per condition, all self-run on this machine. The documentation was handed to the agents;
  they did not have to find it.

### How the counts are made

Over the retry phase's tool calls:
- **Public-log reads:** calls that query the gate's `/receipts` (curl or `attest receipts`).
- **Re-checks:** calls containing `/verify/`, `attest … check` or `.check(`.
- **`/verify` requests:** counted from the gate's access log, within the phase.

## Confirmation on the merged code (signed checks)

`main` at `b3d5a5c` merged Sam's signed checks: on a sandbox a receiver must sign every `/verify` call, or the gate
answers HTTP 401. Two more runs, registered in `CHECKS.md` before they ran (commit `ab40abb`), use the merged code and
its `AGENTS.md`:

| Run | Checks | Tool calls | Errors | Gate 4xx (incl. 401) | Wall time | Cost |
|---|---|---|---|---|---|---|
| `final-1` | B1, S1–S4, B2 pass | 11 | 0 | 0 | 79.6 s | $0.77 |
| `final-crash-1` | B1, S1–S4, B2, R1 pass | 14 | 0 | 0 | 113.9 s | $0.99 |

The fresh shop agents signed their checks from the documentation alone (no 401s). In `final-crash-1` the restarted
shop re-checked both orders and got `repeat: true` (0 public-log reads, 2 `/verify` requests, no new receipts).
Isolation warnings in both runs are benign: the sessions' own Claude Code scratchpad folders and the endpoint name
`/pubkey` inside a string.

## Reproduce

```bash
# environment: a venv with attest (pip install -e .) and pytest; the harness uses the `claude` CLI
python -m pytest -q tests                                   # repo tests
python run_scenarios.py --spawn                             # desk scenarios
python usability/run_coldstart.py --run-id <new id>         # Part 2 cold-start run (about 1.5 min)
python usability/run_coldstart.py --run-id <new id> --crash-retry                     # Part 3, default model
MODEL=claude-haiku-5-5 python usability/run_coldstart.py --run-id <new id> --crash-retry
python usability/score.py usability/runs/<id>               # re-score a run
```

Run ids are never reused. The evidence for each run is in `usability/runs/<id>/`; `usability/README.md` lists what
each file is. `runs/` is in `.gitignore`, so add new runs with `git add -f`. NANDA Town tests:
`integrations/nandatown/README.md`.

## Commits

| Commit | What |
|---|---|
| `3850b2d` | Part 1: `AGENTS.md`, delegate fix, packaging fix |
| `20655be` | Part 2: harness and pre-registered checks |
| `41c6ddc` | Part 2: run `real-1` |
| `1576ca9` | Part 3: crash-retry scenario and check R1 (before the crash runs) |
| `29f5a26` | Part 3: runs before the fix |
| `df78644` | Part 3: repeat checks (gate, tests, docs, scenarios, bench, Town seller) |
| `511e813` | Part 3: runs after the fix |
| `b3d5a5c` | Sam's merge (signed checks in `AGENTS.md`, MCP tool signs) |
| `ab40abb`, `3d239a5` | confirmation runs on the merged code, registered first |
