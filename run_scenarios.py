"""Deterministic check of the gate, no LLM involved.

Runs envelopes through POST /verify/supplierco and compares each decision and
reason with what the design says it must be, exercises slashing claims (dispute
window, dispute, arbiter), revocation and concurrent orders, and checks every
returned receipt against the gate's public key. Exit code 1 if anything is off.

Usage:
  .venv/bin/python run_scenarios.py           gate must be running on :8402, state from setup_demo.py
  .venv/bin/python run_scenarios.py --spawn   builds a fresh demo world in a temporary directory,
                                              starts its own gate on a free port, stops it at the end
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
SPAWN = "--spawn" in sys.argv
if SPAWN:  # must happen before gate.core is imported, which reads these
    _tmp = Path(tempfile.mkdtemp(prefix="attest-scenarios-"))
    os.environ["ATTEST_STATE_DIR"] = str(_tmp / "state")
    os.environ["ATTEST_LOG_DIR"] = str(_tmp / "logs")

from gate import crypto  # noqa: E402
from gate.core import POW_BASE_BITS, STATE_DIR, solve_pow  # noqa: E402

GATE = "http://127.0.0.1:8402"
AG = STATE_DIR / "agents"
fails = 0
gate_pub = None


def load(name):
    return json.loads((AG / f"{name}.json").read_text())


def envelope(ag, action, amount, message, to="supplierco", strip_credential=False, tamper=False):
    req = crypto.signed(ag["priv"], {
        "type": "kya.request/1", "from": ag["pub"], "to": to, "action": action,
        "amount": amount, "message": message, "ts": time.time(), "nonce": uuid.uuid4().hex})
    if tamper:
        req = {**req, "amount": 1}  # change after signing
    return {"credential": None if strip_credential else ag["credential"],
            "chain": ag["chain"], "request": req}


def stranger():
    """A fresh key with no credential at all, as a spammer would make one per request."""
    priv, pub = crypto.keygen()
    return {"priv": priv, "pub": pub, "credential": None, "chain": []}


def verify(env):
    return httpx.post(f"{GATE}/verify/supplierco", json=env, timeout=15).json()


def check(name, env, expected, reason=None):
    """POST the envelope, compare decision (and reason if given), verify the receipt."""
    global fails
    r = verify(env)
    rec = r["receipt"]
    rec_ok = crypto.verify(gate_pub, crypto.body_of(rec), rec["sig"])
    good = r["decision"] == expected and (reason is None or r["reason"] == reason) and rec_ok
    fails += not good
    print(f"{'ok ' if good else 'BAD'} {name:56} {expected:9} {r['decision']:9} {r['reason']}"
          f"{'' if rec_ok else '  (RECEIPT SIGNATURE INVALID)'}")
    return r


def expect(name, good, got):
    """A check on something other than a verify decision."""
    global fails
    fails += not good
    print(f"{'ok ' if good else 'BAD'} {name:56} {got}")


def post(path, body):
    r = httpx.post(f"{GATE}{path}", json=body, timeout=15)
    return r.status_code, r.json()


def claim(receipt, breach, key):
    body = {"claim": receipt["id"], "receiver": "supplierco", "breach": breach}
    return post("/claims", {"receipt": receipt, "breach": breach, "sig": crypto.sign(key["priv"], body)})


def revoke(document, key, reason):
    body = {"revoke": document["id"], "by": key["pub"], "reason": reason}
    return post("/revoke", {"document": document, "by": key["pub"], "sig": crypto.sign(key["priv"], body),
                            "reason": reason})


def stake_of(name):
    return next(v for v in httpx.get(f"{GATE}/stakes").json().values() if v["name"] == name)


def spawn_gate():
    """Fresh demo world plus a gate of its own, for CI and for runs without a server up."""
    global GATE
    subprocess.run([sys.executable, "setup_demo.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    GATE = f"http://127.0.0.1:{port}"
    log = open(_tmp / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "gate.server:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            httpx.get(f"{GATE}/pubkey", timeout=1)
            return proc
        except httpx.HTTPError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    proc.kill()
    sys.exit(f"gate did not start, see {_tmp / 'server.log'}")


def main():
    global gate_pub, fails
    proc, ok, greedy, imp, flaky = (load(n) for n in
                                    ("acme-procurement", "acme-sub-ok", "acme-sub-greedy", "impostor", "flaky-buyer"))
    acme_op, flaky_op, shady_op = (load(f"op-{n}") for n in ("AcmeCorp", "FlakyCo", "ShadyCo"))
    supplier, arbiter = load("rcv-supplierco"), load("arbiter")
    gate_pub = httpx.get(f"{GATE}/pubkey").json()["gate"]
    print(f"{'scenario':60} {'expected':9} {'got':9} reason")

    # staked issuer, mandate chain, scope, policy
    first = envelope(proc, "request_quote", 4000, "quote 200 units of part X")
    check("1 procurement asks a quote (in scope)", first, "allow", "ok")
    check("2 procurement places a 9,500 order (receiver policy)", envelope(proc, "place_order", 9500, "PO-1001"), "escalate")
    acme_order = check("3 procurement places a 7,500 order (in scope)", envelope(proc, "place_order", 7500, "PO-1002"), "allow", "ok")
    check("4 procurement places a 12,000 order (over limit)", envelope(proc, "place_order", 12000, "PO-1003"), "refuse", "over_limit")
    check("5 then 3,000 more: 10,500 in total (order splitting)", envelope(proc, "place_order", 3000, "PO-1004"), "refuse", "cumulative_over_limit")
    check("5b a negative order to lower the running total", envelope(proc, "place_order", -5000, "PO-1005"), "refuse", "bad_amount")
    with ThreadPoolExecutor(10) as ex:
        rs = list(ex.map(lambda i: verify(envelope(proc, "place_order", 1000, f"PO-11{i:02}")), range(10)))
    allowed = sum(r["decision"] == "allow" for r in rs)
    others = {r["reason"] for r in rs if r["decision"] != "allow"}
    expect("5c ten 1,000 orders at once, 2,500 left under the cap", allowed == 2 and others == {"cumulative_over_limit"},
           f"{allowed} allowed, {10 - allowed} refused {sorted(others)}")
    check("6 sub-agent (narrowed to quotes <= 5k) asks a quote", envelope(ok, "request_quote", 3000, "quote 150 units"), "allow", "ok")
    check("7 sub-agent tries to place an order (out of scope)", envelope(ok, "place_order", 3000, "PO-2001"), "refuse", "out_of_scope")
    check("8 greedy sub-agent, mandate 15k > parent 10k", envelope(greedy, "place_order", 500, "PO-3001"), "refuse", "attenuation_violation")

    # no staked issuer: proof of work for read-only, refusal for money
    env = envelope(imp, "request_quote", 100, "hi, buying for Acme")
    r = check("9 impostor (ShadyCo, no stake) asks a quote", env, "unknown", "pow_required")
    ch = r["challenge"]
    t0 = time.time()
    nonce, tries = solve_pow(ch["seed"], ch["bits"])
    took = time.time() - t0
    print(f"    puzzle: {ch['bits']} bits, seeded by Base Sepolia block {ch['block']['number']}; solved in {tries:,} hashes, {took:.2f} s")
    check("10 impostor resends with the solution",
          {**env, "pow": {"challenge_id": ch["challenge_id"], "nonce": nonce}}, "unknown", "pow_ok")
    r = check("11 impostor asks again: puzzle doubles", envelope(imp, "request_quote", 100, "again"), "unknown", "pow_required")
    bits2 = r["challenge"]["bits"]
    if bits2 != ch["bits"] + 1:
        fails += 1
        print(f"BAD    difficulty went {ch['bits']} -> {bits2}, expected +1 bit (double the work)")
    check("12 impostor tries to place an order", envelope(imp, "place_order", 2000, "PO-9001"), "refuse", "unstaked_money_action")
    with httpx.Client(timeout=15) as c:
        for i in range(40):
            c.post(f"{GATE}/verify/supplierco", json=envelope(stranger(), "request_quote", 10, f"spam {i}"))
    bits3 = verify(envelope(stranger(), "request_quote", 10, "fresh key"))["challenge"]["bits"]
    expect("12b fresh key after 40 requests from fresh keys", bits3 > POW_BASE_BITS,
           f"{bits3} bits (base {POW_BASE_BITS}): new keys do not escape the load")
    check("13 no credential at all, asks a quote", envelope(proc, "request_quote", 100, "trust me", strip_credential=True), "unknown", "pow_required")

    # integrity
    check("14 request body changed after signing", envelope(proc, "request_quote", 900, "tampered", tamper=True), "refuse", "bad_request_signature")
    r = verify(first)
    good = r["decision"] == "refuse" and r["reason"] == "replay"
    fails += not good
    print(f"{'ok ' if good else 'BAD'} {'15 same request presented twice (replay)':56} {'refuse':9} {r['decision']:9} {r['reason']}")

    # slashing: claims are signed by the receiver, wait out a dispute window, can be disputed
    breach = "goods received, payment never sent"
    flaky_order = check("16a FlakyCo agent (staked 20,000) orders 4,000", envelope(flaky, "place_order", 4000, "PO-7001"), "allow", "ok")
    code, c = claim(flaky_order["receipt"], breach, shady_op)
    expect("16b claim not signed by the receiver's key", code == 400, f"{code} {c.get('detail')}")
    code, c = claim(flaky_order["receipt"], breach, supplier)
    expect("16c SupplierCo files a claim with that receipt", code == 200 and c.get("status") == "pending",
           f"{code} {c.get('status')}, dispute window {c.get('deadline', 0) - c.get('filed_at', 0):.0f} s")
    code, c = claim(flaky_order["receipt"], breach, supplier)
    expect("16d the same receipt claimed a second time", code == 400, f"{code} {c.get('detail')}")
    check("16e FlakyCo orders inside the window (not yet slashed)", envelope(flaky, "place_order", 1000, "PO-7002"), "allow", "ok")
    code, acme_claim = claim(acme_order["receipt"], "order cancelled without notice", supplier)
    reason = "order was cancelled within the agreed 24 hours"
    sig = crypto.sign(acme_op["priv"], {"dispute": acme_claim.get("id"), "reason": reason})
    code, c = post(f"/claims/{acme_claim.get('id')}/dispute", {"reason": reason, "sig": sig})
    expect("16f AcmeCorp disputes a claim against it in the window", code == 200 and c.get("status") == "disputed",
           f"{code} {c.get('status')}")
    time.sleep(max(0.0, max(x["deadline"] for x in httpx.get(f"{GATE}/claims").json()) - time.time()) + 0.3)
    check("16g FlakyCo orders after the window closed undisputed", envelope(flaky, "place_order", 500, "PO-7003"), "refuse", "unstaked_money_action")
    s = stake_of("FlakyCo")
    print(f"    FlakyCo slashed: stake left {s['stake']:,.0f}, suspended {s['suspended']}")
    sig = crypto.sign(arbiter["priv"], {"resolve": acme_claim.get("id"), "uphold": False})
    code, c = post(f"/claims/{acme_claim.get('id')}/resolve", {"uphold": False, "sig": sig})
    s = stake_of("AcmeCorp")
    expect("16h arbiter rejects the disputed claim against AcmeCorp",
           code == 200 and c.get("status") == "rejected" and s["stake"] == 25000 and not s["suspended"],
           f"{c.get('status')}, AcmeCorp stake {s['stake']:,.0f}, suspended {s['suspended']}")

    # revocation: only the signer of a document may revoke it, and revocation cascades
    root = proc["chain"][0]
    code, r = revoke(root, flaky_op, "hostile revocation")
    expect("17a FlakyCo tries to revoke AcmeCorp's root mandate", code == 400, f"{code} {r.get('detail')}")
    code, r = revoke(root, acme_op, "procurement key rotated")
    expect("17b AcmeCorp revokes its own root mandate", code == 200, f"{code} {r.get('revoked', r)!s:.16}...")
    check("17c root mandate revoked, sub-agent below it", envelope(ok, "request_quote", 100, "after revoke"), "refuse", "mandate_revoked")

    blk = httpx.get(f"{GATE}/block").json()
    print()
    print(f"puzzles seeded by {blk['source']} block {blk['number']} ({blk['hash'][:18]}...)")
    print("all receipts verified against gate key", gate_pub[:16] + "...")
    print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    server = spawn_gate() if SPAWN else None
    code = 1
    try:
        code = main()
    finally:
        if server:
            server.terminate()
            server.wait(10)
            if code == 0:
                shutil.rmtree(_tmp, ignore_errors=True)
            else:
                print(f"state and server log kept in {_tmp}")
    sys.exit(code)
