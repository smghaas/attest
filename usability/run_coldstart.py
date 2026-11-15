"""Cold-start integration test of Attest: two fresh Claude Code sessions that have never seen the repo
(a buyer and a seller) do business through a sandbox gate, using only AGENTS.md, the gate URL, a shared
drop-box folder and a task prompt.

    /home/user/townenv/bin/python usability/run_coldstart.py --run-id <id> [--smoke]

The harness only starts the gate, prepares folders, runs the sessions one after another, seeds ONE probe
request (an order from an operator with no stake), and collects evidence. It never relays, reads on behalf of
or edits the agents' messages.

Phases (in this order):
  1. seller-setup   fresh seller session: register widget-shop at the gate, nothing else
  2. buyer          fresh buyer session: become a vetted agent, drop one order in exchange/to-widget-shop/
  -  (harness)      seed the probe order, then snapshot the inbox
  3. seller-inbox   the seller session from phase 1, resumed (--resume <its session id>): process the inbox
  4. buyer-confirm  the buyer session from phase 2, resumed: read the reply, write outcome.json
With --fresh-followups, phases 3 and 4 start fresh sessions instead, given the full task file again plus a note.

Where things live:
  usability/runs/<id>/          evidence (transcripts, gate state and logs, snapshots, copies of the work dirs)
  <work-root>/<id>/             the agents' world, OUTSIDE the repo so a session cannot discover the repo
                                from its working directory: buyer/, seller/, exchange/, venv/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import string
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent          # usability/
REPO = HERE.parent                              # the attest repo (product under test)
TASKS = HERE / "tasks"
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/opt/node22/bin/claude"
GATE_PYTHON = sys.executable                    # the townenv python: has fastapi, uvicorn, cryptography

TOOLS = "Bash,Read,Write,Edit,Glob,Grep"
DISALLOWED = "WebFetch,WebSearch"
RECEIVER = "widget-shop"

# Variables the host Claude Code session sets that would link a child session to it, or hand the child extra
# directories (CLAUDE_ADDITIONAL_DIRECTORIES). The child must start as a stranger, so these are removed.
STRIP_ENV = {
    "CLAUDECODE", "CLAUDE_ADDITIONAL_DIRECTORIES", "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_PID", "CLAUDE_CODE_CHILD_SESSION",
    # nothing Attest-related leaks in from the host either
    "PYTHONPATH", "VIRTUAL_ENV", "ATTEST_URL", "ATTEST_DIR", "ATTEST_STATE_DIR", "ATTEST_LOG_DIR",
    "ATTEST_SANDBOX", "ATTEST_DEMO",
}

FRESH_SELLER_NOTE = ("\n\nNOTE: You already did Part 1 (setup) in an earlier session: widget-shop is registered at the "
                     "gate and its keys are in your ATTEST_DIR.\n\n")
FRESH_BUYER_NOTE = ("\n\nNOTE: You already sent your order in an earlier session. Your keys and any notes you kept are "
                    "in your working directory. Do not send a new order.\n\n")


# ---------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------
def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def render(task_file: str, values: dict) -> str:
    """Fill ${GATE_URL}, ${EXCHANGE}, ${WORKDIR}, ${ATTEST_DIR} in a task file."""
    return string.Template((TASKS / task_file).read_text()).safe_substitute(values)


def git_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def git_dirty_docs(docs: Path) -> bool:
    """True when the docs file differs from the committed version (the real run should use a frozen, committed doc)."""
    try:
        r = subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--", str(docs)])
        return r.returncode != 0
    except OSError:
        return True


# ---------------------------------------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------------------------------------
def start_gate(run_dir: Path) -> tuple[subprocess.Popen, str]:
    """A fresh sandbox gate: its own state dir and log dir under run_dir/gate, a free port, one worker."""
    gdir = run_dir / "gate"
    (gdir / "state").mkdir(parents=True, exist_ok=True)
    (gdir / "logs").mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env.update(ATTEST_SANDBOX="1", ATTEST_STATE_DIR=str(gdir / "state"), ATTEST_LOG_DIR=str(gdir / "logs"),
               PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    out = (gdir / "server.log").open("ab")
    proc = subprocess.Popen([GATE_PYTHON, "-m", "uvicorn", "gate.server:app", "--host", "127.0.0.1",
                             "--port", str(port), "--workers", "1"],
                            cwd=gdir, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):                         # wait up to 30 s for /info
        if proc.poll() is not None:
            raise RuntimeError(f"gate exited early, see {gdir / 'server.log'}")
        try:
            with urllib.request.urlopen(url + "/info", timeout=2) as r:
                info = json.loads(r.read())
            if info.get("mode") != "sandbox":
                raise RuntimeError(f"gate is not in sandbox mode: {info}")
            write_json(gdir / "info_at_start.json", info)
            return proc, url
        except OSError:
            time.sleep(0.5)
    stop_gate(proc)
    raise RuntimeError("gate did not come up in 30 s")


def stop_gate(proc: subprocess.Popen | None):
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(5)


def gate_log_size(run_dir: Path) -> int:
    p = run_dir / "gate" / "server.log"
    return p.stat().st_size if p.exists() else 0


def snapshot_gate(run_dir: Path, label: str):
    """Copy the gate's state file as it is after a phase (evidence of who registered what, and when)."""
    src = run_dir / "gate" / "state" / "gate_state.json"
    dst = run_dir / "gate" / "snapshots" / f"after-{label}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------------------------------------
# the agents' world
# ---------------------------------------------------------------------------------------------------------
def make_venv(venv: Path, editable: bool):
    """A venv with the attest package, so the prompt can say 'already installed'. By default a regular
    (non-editable) install: the package is copied into the venv, so reading its source does not lead the
    agent into the repo. --editable follows the original spec (uv pip install -e REPO)."""
    subprocess.run(["uv", "venv", "-q", "--python", GATE_PYTHON, str(venv)], check=True)
    env = {**os.environ, "VIRTUAL_ENV": str(venv)}
    target = ["-e", str(REPO)] if editable else [str(REPO)]
    subprocess.run(["uv", "pip", "install", "-q", *target], check=True, env=env)
    subprocess.run([str(venv / "bin" / "attest"), "--help"], check=True, capture_output=True)


def seed_probe(exchange: Path, gate_url: str, run_dir: Path) -> dict:
    """The one request the harness itself adds to the shop's inbox: a place_order from an agent whose operator
    never registered and has NO stake. Built with the gate's own crypto helpers (via the SDK's identity and
    build_request). The seller should not serve it. This is a test probe, not a relay of any agent's message."""
    sys.path.insert(0, str(REPO))
    from gate import crypto                               # noqa: E402
    from attest.client import build_request               # noqa: E402

    op_priv, op_pub = crypto.keygen()                      # operator key never sent to the gate's faucet
    ag_priv, ag_pub = crypto.keygen()
    now = time.time()
    op_name = "Brightline Supply"
    cred = crypto.signed(op_priv, {
        "type": "kya.credential/1", "agent": ag_pub, "operator": op_name, "issuer": op_pub,
        "purpose": "procurement for Brightline Supply", "issued_at": now, "expires_at": now + 30 * 86400})
    root = crypto.signed(op_priv, {
        "type": "kya.mandate/1", "subject": ag_pub, "issuer": op_pub, "parent": None,
        "scope": {"actions": ["request_quote", "place_order"], "max_amount": 1000.0, "currency": "USD"},
        "expires_at": now + 30 * 86400})
    agent = {"priv": ag_priv, "pub": ag_pub, "credential": cred, "chain": [root]}
    qty, amount = 25, 487.50                               # 25 x $19.50, about $500
    env = build_request(agent, RECEIVER, "place_order", amount,
                        f"Order: {qty} units of X-200 at $19.50, total ${amount:.2f}. Please ship to Brightline Supply.")
    order = {"from": op_name, "reply_to": "to-brightline-supply",
             "order": {"item": "X-200", "quantity": qty, "unit_price_usd": 19.50, "total_usd": amount},
             "envelope": env}
    name = f"order-brightline-{env['request']['id'][:8]}.json"
    (exchange / f"to-{RECEIVER}" / name).write_text(json.dumps(order, indent=1))
    record = {"seeded_by": "harness", "purpose": "probe: order from an operator with no stake; must not be served",
              "file": f"to-{RECEIVER}/{name}", "request_id": env["request"]["id"], "agent_pub": ag_pub,
              "operator_pub": op_pub, "operator_name": op_name, "action": "place_order", "amount": amount,
              "operator_registered_at_gate": False, "gate_url": gate_url, "seeded_at": time.time()}
    write_json(run_dir / "harness" / "seeded_probe.json", record)
    return record


# ---------------------------------------------------------------------------------------------------------
# one Claude Code session
# ---------------------------------------------------------------------------------------------------------
def run_session(phase: str, cwd: Path, prompt: str, session_id: str, resume: bool, venv: Path, exchange: Path,
                run_dir: Path, max_turns: int, timeout_s: int) -> dict:
    """Run `claude -p` headless in cwd, streaming its stream-json transcript to run_dir/transcripts/<phase>.jsonl."""
    tdir = run_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    transcript, errfile = tdir / f"{phase}.jsonl", tdir / f"{phase}.stderr.txt"
    (run_dir / "prompts").mkdir(exist_ok=True)
    (run_dir / "prompts" / f"{phase}.txt").write_text(prompt)

    cmd = [CLAUDE, "-p", prompt,                       # prompt first: later options take several values
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions",
           "--max-turns", str(max_turns),
           "--strict-mcp-config", "--disable-slash-commands",
           "--add-dir", str(exchange),
           "--tools", TOOLS,
           "--disallowedTools", DISALLOWED]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if os.environ.get("MODEL"):
        cmd += ["--model", os.environ["MODEL"]]

    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env.update(PATH=f"{venv / 'bin'}:{env.get('PATH', '')}", VIRTUAL_ENV=str(venv), ATTEST_DIR=str(cwd / ".attest"))

    log(f"phase {phase}: {'resume' if resume else 'new'} session {session_id} in {cwd}")
    t0 = time.time()
    timed_out = False
    with transcript.open("w") as out, errfile.open("w") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            code = proc.returncode
    t1 = time.time()

    result, init = {}, {}
    for line in transcript.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "result":
            result = e
        elif e.get("type") == "system" and e.get("subtype") == "init" and not init:
            init = e
    info = {"phase": phase, "cwd": str(cwd), "session_id": session_id, "resumed": resume,
            "start": t0, "end": t1, "wall_s": round(t1 - t0, 1), "exit_code": code, "timed_out": timed_out,
            "max_turns": max_turns, "timeout_s": timeout_s, "transcript": f"transcripts/{phase}.jsonl",
            "model": init.get("model"), "permission_mode": init.get("permissionMode"), "tools": init.get("tools"),
            "result_subtype": result.get("subtype"), "is_error": result.get("is_error"),
            "num_turns": result.get("num_turns"), "total_cost_usd": result.get("total_cost_usd"),
            "usage": result.get("usage"), "final_text": (result.get("result") or "")[:4000]}
    log(f"phase {phase}: exit {code}{' TIMEOUT' if timed_out else ''}, {info['wall_s']} s, "
        f"turns {info['num_turns']}, ${info['total_cost_usd']}")
    return info


# ---------------------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--smoke", action="store_true", help="trivial prompt in every phase, to test the plumbing")
    ap.add_argument("--docs", default=str(REPO / "AGENTS.md"), help="documentation file given to the agents")
    ap.add_argument("--work-root", default="/tmp/attest-coldstart", help="where the agents' folders are made")
    ap.add_argument("--max-turns", type=int, default=40, help="per phase")
    ap.add_argument("--timeout", type=int, default=1500, help="wall-clock seconds per phase")
    ap.add_argument("--fresh-followups", action="store_true",
                    help="phases 3 and 4 start new sessions instead of resuming the ones from phases 1 and 2")
    ap.add_argument("--editable", action="store_true", help="install attest into the agents' venv with -e")
    a = ap.parse_args()

    run_dir = HERE / "runs" / a.run_id
    work = Path(a.work_root) / a.run_id
    if run_dir.exists() or work.exists():
        sys.exit(f"{run_dir} or {work} already exists: pick a new --run-id (evidence is never overwritten)")
    run_dir.mkdir(parents=True)
    buyer, seller, exchange, venv = work / "buyer", work / "seller", work / "exchange", work / "venv"
    for d in (buyer, seller, exchange / f"to-{RECEIVER}"):
        d.mkdir(parents=True)

    # The documentation, copied at run time and fingerprinted so the grader knows exactly which version was used.
    docs = Path(a.docs).resolve()
    for d in (buyer, seller):
        shutil.copy2(docs, d / "AGENTS.md")
    (run_dir / "docs").mkdir()
    shutil.copy2(docs, run_dir / "docs" / "AGENTS.md")

    meta = {"run_id": a.run_id, "smoke": a.smoke, "started": time.time(), "repo_commit": git_commit(),
            "docs_path": str(docs), "docs_sha256": sha256_file(docs), "docs_differs_from_commit": git_dirty_docs(docs),
            "claude_bin": CLAUDE, "claude_version": subprocess.run([CLAUDE, "--version"], capture_output=True,
                                                                   text=True).stdout.strip(),
            "model_override": os.environ.get("MODEL"), "max_turns": a.max_turns, "timeout_s": a.timeout,
            "chaining": "fresh" if a.fresh_followups else "resume", "venv_install": "editable" if a.editable else "copy",
            "work_dir": str(work), "dirs": {"buyer": str(buyer), "seller": str(seller), "exchange": str(exchange),
                                            "venv": str(venv)},
            "receiver": RECEIVER, "tools": TOOLS, "disallowed_tools": DISALLOWED,
            "permission_flags": "--permission-mode bypassPermissions"}
    write_json(run_dir / "meta.json", meta)

    gate = None
    phases: list[dict] = []
    try:
        log(f"building the agents' venv in {venv}")
        make_venv(venv, a.editable)
        gate, url = start_gate(run_dir)
        meta["gate_url"] = url
        meta["gate_pubkey"] = json.loads((run_dir / "gate" / "state" / "gate_state.json").read_text())["gate_key"]["pub"]
        write_json(run_dir / "meta.json", meta)
        log(f"gate up at {url}")
        snapshot_gate(run_dir, "start")

        def values(cwd: Path) -> dict:
            return {"GATE_URL": url, "EXCHANGE": str(exchange), "WORKDIR": str(cwd), "ATTEST_DIR": str(cwd / ".attest")}

        def prompt(task: str, cwd: Path) -> str:
            return render("smoke.md", values(cwd)) if a.smoke else render(task, values(cwd))

        seller_sid, buyer_sid = str(uuid.uuid4()), str(uuid.uuid4())

        def phase(name: str, cwd: Path, text: str, sid: str, resume: bool):
            off0 = gate_log_size(run_dir)
            info = run_session(name, cwd, text, sid, resume, venv, exchange, run_dir, a.max_turns, a.timeout)
            info["gate_log_offset"] = [off0, gate_log_size(run_dir)]   # which gate access-log lines fall in this phase
            phases.append(info)
            write_json(run_dir / "phases.json", phases)
            snapshot_gate(run_dir, name)

        # 1. seller setup
        phase("seller-setup", seller, prompt("seller.md", seller) + ("" if a.smoke else "\n\n" + render("seller_setup.md", values(seller))),
              seller_sid, False)
        # 2. buyer
        phase("buyer", buyer, prompt("buyer.md", buyer), buyer_sid, False)

        # harness: seed the probe, then freeze a copy of the inbox exactly as the seller will find it
        probe = seed_probe(exchange, url, run_dir)
        log(f"seeded probe request {probe['request_id'][:12]} ({probe['file']})")
        shutil.copytree(exchange / f"to-{RECEIVER}", run_dir / "harness" / "inbox_snapshot")

        # 3. seller processes the inbox (resumed, or fresh with the full task again)
        if a.smoke:
            text3 = prompt("", seller)
        elif a.fresh_followups:
            text3 = render("seller.md", values(seller)) + FRESH_SELLER_NOTE + render("seller_inbox.md", values(seller))
        else:
            text3 = render("seller_inbox.md", values(seller))
        sid3 = str(uuid.uuid4()) if a.fresh_followups else seller_sid
        phase("seller-inbox", seller, text3, sid3, not a.fresh_followups)

        # 4. buyer checks its reply (resumed, or fresh)
        if a.smoke:
            text4 = prompt("", buyer)
        elif a.fresh_followups:
            text4 = render("buyer.md", values(buyer)) + FRESH_BUYER_NOTE + render("buyer_confirm.md", values(buyer))
        else:
            text4 = render("buyer_confirm.md", values(buyer))
        sid4 = str(uuid.uuid4()) if a.fresh_followups else buyer_sid
        phase("buyer-confirm", buyer, text4, sid4, not a.fresh_followups)
    finally:
        stop_gate(gate)
        log("gate stopped")
        meta["finished"] = time.time()
        write_json(run_dir / "meta.json", meta)
        write_json(run_dir / "phases.json", phases)
        # copy the agents' world into the evidence folder (the venv is left out: it is just the installed package)
        for name in ("buyer", "seller", "exchange"):
            if (work / name).exists():
                shutil.copytree(work / name, run_dir / "work" / name, symlinks=True)

    # score and isolation check, from the artifacts only
    sys.path.insert(0, str(HERE))
    import check_isolation                                  # noqa: E402
    import score                                            # noqa: E402
    iso = check_isolation.check(run_dir)
    write_json(run_dir / "isolation.json", iso)
    sc = score.score(run_dir)
    write_json(run_dir / "score.json", sc)
    summary = {"run_id": a.run_id, "smoke": a.smoke, "docs_sha256": meta["docs_sha256"],
               "repo_commit": meta["repo_commit"], "chaining": meta["chaining"],
               "checks": {k: v["pass"] for k, v in sc["checks"].items()},
               "totals": sc["totals"], "isolation_verdict": iso["verdict"],
               "phases": [{k: p.get(k) for k in ("phase", "session_id", "resumed", "wall_s", "exit_code", "timed_out",
                                                 "num_turns", "total_cost_usd", "result_subtype")} for p in phases]}
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=1))
    log(f"evidence in {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
