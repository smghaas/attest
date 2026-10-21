"""Deterministic check of the gate, no LLM involved.

Runs envelopes through POST /verify/supplierco and compares each decision and
reason with what the design says it must be. Then checks every returned receipt
against the gate's public key. Exit code 1 if anything is off.

Usage: .venv/bin/python run_scenarios.py   (gate must be running on :8402)
"""
import json
import sys
import time
import uuid

import httpx

from gate import crypto
from gate.core import STATE_DIR, solve_pow

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


def check(name, env, expected, reason=None):
    """POST the envelope, compare decision (and reason if given), verify the receipt."""
    global fails
    r = httpx.post(f"{GATE}/verify/supplierco", json=env, timeout=15).json()
    rec = r["receipt"]
    rec_ok = crypto.verify(gate_pub, crypto.body_of(rec), rec["sig"])
    good = r["decision"] == expected and (reason is None or r["reason"] == reason) and rec_ok
    fails += not good
    print(f"{'ok ' if good else 'BAD'} {name:56} {expected:9} {r['decision']:9} {r['reason']}"
          f"{'' if rec_ok else '  (RECEIPT SIGNATURE INVALID)'}")
    return r


def main():
    global gate_pub, fails
    proc, ok, greedy, imp, flaky = (load(n) for n in
                                    ("acme-procurement", "acme-sub-ok", "acme-sub-greedy", "impostor", "flaky-buyer"))
    gate_pub = httpx.get(f"{GATE}/pubkey").json()["gate"]
    print(f"{'scenario':60} {'expected':9} {'got':9} reason")

    # staked issuer, mandate chain, scope, policy
    first = envelope(proc, "request_quote", 4000, "quote 200 units of part X")
    check("1 procurement asks a quote (in scope)", first, "allow", "ok")
    check("2 procurement places a 9,500 order (receiver policy)", envelope(proc, "place_order", 9500, "PO-1001"), "escalate")
    check("3 procurement places a 7,500 order (in scope)", envelope(proc, "place_order", 7500, "PO-1002"), "allow")
    check("4 procurement places a 12,000 order (over limit)", envelope(proc, "place_order", 12000, "PO-1003"), "refuse", "over_limit")
    check("5 then 3,000 more: 10,500 in total (order splitting)", envelope(proc, "place_order", 3000, "PO-1004"), "refuse", "cumulative_over_limit")
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
    check("13 no credential at all, asks a quote", envelope(proc, "request_quote", 100, "trust me", strip_credential=True), "unknown", "pow_required")

    # integrity
    check("14 request body changed after signing", envelope(proc, "request_quote", 900, "tampered", tamper=True), "refuse", "bad_request_signature")
    r = httpx.post(f"{GATE}/verify/supplierco", json=first, timeout=10).json()
    good = r["decision"] == "refuse" and r["reason"] == "replay"
    fails += not good
    print(f"{'ok ' if good else 'BAD'} {'15 same request presented twice (replay)':56} {'refuse':9} {r['decision']:9} {r['reason']}")

    # slashing: FlakyCo's agent is allowed, SupplierCo proves a breach, FlakyCo is slashed and suspended
    r = check("16a FlakyCo agent (staked 20,000) orders 4,000", envelope(flaky, "place_order", 4000, "PO-7001"), "allow", "ok")
    s = httpx.post(f"{GATE}/slash/supplierco", json={"receipt_id": r["receipt"]["id"],
                                                     "breach": "goods received, payment never sent"}).json()
    print(f"    slashing claim with that receipt: FlakyCo loses {s['penalty']:,.0f}, stake left {s['stake_left']:,.0f}, suspended")
    check("16b FlakyCo agent orders again after the slash", envelope(flaky, "place_order", 1000, "PO-7002"), "refuse", "unstaked_money_action")

    # revocation cascade: AcmeCorp revokes the procurement root mandate, then the ok sub-agent must fail
    acme = load("op-AcmeCorp")
    target = proc["chain"][0]["id"]
    body = {"revoke": target, "by": acme["pub"], "reason": "procurement key rotated"}
    httpx.post(f"{GATE}/revoke", json={"target_id": target, "by": acme["pub"],
                                       "sig": crypto.sign(acme["priv"], body), "reason": body["reason"]})
    check("17 root mandate revoked, sub-agent below it", envelope(ok, "request_quote", 100, "after revoke"), "refuse", "mandate_revoked")

    blk = httpx.get(f"{GATE}/block").json()
    print()
    print(f"puzzles seeded by {blk['source']} block {blk['number']} ({blk['hash'][:18]}...)")
    print("all receipts verified against gate key", gate_pub[:16] + "...")
    print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
