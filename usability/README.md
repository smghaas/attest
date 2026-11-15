# Cold-start integration test

Can two agents that have never seen this repository do business through Attest, using only the
documentation? Two fresh, headless Claude Code sessions, a buyer and a seller, get a copy of `AGENTS.md`, the URL
of a sandbox gate, a shared drop-box folder and a task prompt. Nothing else: no scripted call sequence and no
expected answer. The harness only takes turns for them and collects evidence.

## Run it

```bash
cd /home/user/attest
# plumbing check (cheap: every phase just asks "what is AGENTS.md's first heading?")
/home/user/townenv/bin/python usability/run_coldstart.py --run-id smoke-1 --smoke

# the real test (run it on a frozen, committed AGENTS.md)
/home/user/townenv/bin/python usability/run_coldstart.py --run-id real-1

# re-score or re-check a finished run
/home/user/townenv/bin/python usability/score.py usability/runs/real-1
/home/user/townenv/bin/python usability/check_isolation.py usability/runs/real-1
```

Options: `--max-turns 40` and `--timeout 1500` (seconds) per phase; `--docs PATH` to give a different
documentation file; `--fresh-followups` to start new sessions for phases 3 and 4 instead of resuming;
`--editable` to install attest into the agents' venv with `-e`; `--work-root` (default `/tmp/attest-coldstart`).
Set `MODEL=<model>` to override the default model, `CLAUDE_BIN` to use another `claude` binary.
A run id is never reused: the harness refuses to overwrite evidence.

## What happens

| # | Phase | Session | What it is asked |
|---|---|---|---|
| 0 | harness | - | Build a venv with attest, start a fresh sandbox gate (`ATTEST_SANDBOX=1`, own state dir, free port, one worker) |
| 1 | `seller-setup` | seller, new | `tasks/seller.md` + `tasks/seller_setup.md`: register `widget-shop` at the gate, nothing else |
| 2 | `buyer` | buyer, new | `tasks/buyer.md`: become a vetted agent, drop ONE order in `exchange/to-widget-shop/` |
| - | harness | - | Seed one probe order (operator with no stake, `place_order` $487.50), freeze a copy of the inbox |
| 3 | `seller-inbox` | seller, resumed (`--resume <id of phase 1>`) | `tasks/seller_inbox.md`: vet every inbox file at the gate, write one reply per file |
| 4 | `buyer-confirm` | buyer, resumed (`--resume <id of phase 2>`) | `tasks/buyer_confirm.md`: read the reply, check the receipt, write `outcome.json` |

The seller registers first because the buyer's request names the receiver and the gate needs it to exist when
the seller checks. Phases 3 and 4 resume the same Claude Code conversation (same session id, same working
folder), so each agent remembers its own earlier work. With `--fresh-followups` they start new sessions given the
full task file again plus a short note saying the earlier part is done.

Replies: the buyer names a `reply_to` folder in its order; the seller writes its reply there
(`exchange/to-<name>/`). The probe asks for `to-brightline-supply`. The scorer finds replies by request id
anywhere in the drop-box, so a seller that picks another folder is still scored.

The probe is the harness's only write into the agents' world. It is a test of the seller (an unstaked order must
not be served), built with the gate's own crypto helpers, and recorded in `harness/seeded_probe.json`. It is not
a relay of anything an agent wrote.

## Session settings (what each `claude -p` gets)

- `--permission-mode bypassPermissions` (works headless here, as root, without prompts),
  `--tools Bash,Read,Write,Edit,Glob,Grep`, `--disallowedTools WebFetch,WebSearch`,
  `--max-turns N`, `--strict-mcp-config` (no MCP servers), `--disable-slash-commands` (no skills),
  `--add-dir <exchange>`, `--output-format stream-json --verbose`.
- Working directory: its own empty folder containing only `AGENTS.md`, OUTSIDE the repo
  (`/tmp/attest-coldstart/<id>/buyer` or `/seller`), so Claude Code cannot discover the repo, its git status or
  any CLAUDE.md from where it starts.
- Environment: `PATH` starts with the agents' venv (`attest` command and SDK installed), `ATTEST_DIR=<cwd>/.attest`.
  Variables that would link the child to the host Claude Code session or hand it extra directories
  (`CLAUDE_ADDITIONAL_DIRECTORIES`, `CLAUDECODE`, session ids, ...) and any `ATTEST_*`/`PYTHONPATH` are removed.
  `ATTEST_URL` is NOT set: the gate URL is only in the prompt.
- The venv gets a regular (non-editable) install of attest by default, so the package source lives inside the
  venv and reading it never leads into the repo. `--editable` switches to `uv pip install -e`.
- Note: Claude Code's built-in `agents-md` plugin loads `AGENTS.md` from the working directory into the session's
  context automatically, so the docs are in context from the first turn.

## Evidence: `usability/runs/<id>/`

| Path | What |
|---|---|
| `meta.json` | run settings, repo commit, docs SHA-256 (and whether it differs from the committed file), claude version, gate URL and key |
| `phases.json` | per phase: session id, resumed or new, start/end, wall time, exit code, timeout, turns, cost, usage, final text, gate-log offsets |
| `summary.json` | headline: check results, totals, isolation verdict |
| `score.json` | full output of `score.py`, with the evidence behind each check |
| `isolation.json` | full output of `check_isolation.py` |
| `docs/AGENTS.md` | the exact documentation the agents got |
| `prompts/<phase>.txt` | the exact prompt each phase got |
| `transcripts/<phase>.jsonl` | full stream-json transcript of each session (plus `.stderr.txt`) |
| `gate/state/gate_state.json` | final gate state; `gate/snapshots/after-<phase>.json` after each phase |
| `gate/logs/receipts.jsonl` | every signed receipt the gate issued |
| `gate/server.log` | gate access log (every HTTP call and status) |
| `harness/seeded_probe.json`, `harness/inbox_snapshot/` | the probe, and the inbox exactly as the seller found it |
| `work/buyer`, `work/seller`, `work/exchange` | copies of the agents' folders after the run (includes their sandbox test keys) |

The root `.gitignore` ignores `runs/`, so add a run you want to keep with `git add -f usability/runs/<id>`.

## Files

| File | Purpose |
|---|---|
| `run_coldstart.py` | the harness: gate, venv, folders, phases, probe, evidence, then scoring |
| `score.py` | computes the pre-registered checks from artifacts |
| `check_isolation.py` | scans transcripts for tool calls touching the repo, the other party's folder, the gate's files, or broad searches |
| `CHECKS.md` | the pre-registered checks (B1, S1-S4, B2) and the metrics counted |
| `tasks/*.md` | the task prompts (`${GATE_URL}`, `${EXCHANGE}`, `${WORKDIR}`, `${ATTEST_DIR}` are filled in at run time); `smoke.md` for `--smoke` |

## Known limits

- The gate refuses a request whose timestamp is more than 10 minutes old (`stale_request`). The buyer signs its
  order in phase 2 and the seller checks it in phase 3, so a slow buyer phase can make the legitimate order fail.
  `score.json` records each request's age when it was checked so this is visible.
- Isolation is instructed, not enforced (`bypassPermissions` lets the sessions read anything the user can);
  `check_isolation.py` reports what they actually touched.
- The probe is seeded after the buyer phase, so the buyer never sees it; the seller sees two orders.
