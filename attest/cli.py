"""Command line for Attest.   attest --help"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from .client import Attest


def _print(obj):
    print(json.dumps(obj, indent=2))


def _table(rows, cols):
    if not rows:
        print("(none)")
        return
    w = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    print("  ".join(c.ljust(n) for c, n in zip(cols, w)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(n) for c, n in zip(cols, w)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="attest", description="Ask the Attest gate whether an agent can be trusted.")
    p.add_argument("--url", help="gate URL (default $ATTEST_URL or http://127.0.0.1:8402)")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is the gate up, its key, counts")
    sub.add_parser("receivers", help="registered receivers and their stake thresholds")
    sub.add_parser("stakes", help="who has stake behind them")
    sub.add_parser("claims", help="slashing claims and their status")
    r = sub.add_parser("receipts", help="recent signed decisions")
    r.add_argument("-n", type=int, default=10)
    c = sub.add_parser("check", help="check an envelope file (use - for stdin) for a receiver")
    c.add_argument("receiver")
    c.add_argument("envelope")
    v = sub.add_parser("verify-receipt", help="check a receipt file was signed by the gate")
    v.add_argument("receipt")
    a = p.parse_args(argv)
    at = Attest(a.url)
    try:
        if a.cmd == "status":
            out = {"url": at.url, "gate_key": at.pubkey(), **at.stats()}
            _print(out) if a.json else [print(f"{k:12} {v}") for k, v in out.items()]
        elif a.cmd == "receivers":
            d = at.receivers()
            _print(d) if a.json else _table([{"receiver": k, "min_stake": v.get("min_stake")} for k, v in d.items()],
                                            ["receiver", "min_stake"])
        elif a.cmd == "stakes":
            d = at.stakes()
            _print(d) if a.json else _table([{"issuer": k, "stake": v.get("stake"), "name": v.get("name"),
                                               "suspended": v.get("suspended")} for k, v in d.items()],
                                             ["issuer", "name", "stake", "suspended"])
        elif a.cmd == "claims":
            d = at.claims()
            _print(d) if a.json else _table(d, ["id", "issuer_name", "receiver", "status", "breach"])
        elif a.cmd == "receipts":
            d = at.receipts(a.n)
            _print(d) if a.json else _table(
                [{"id": x["id"][:10], "receiver": x["receiver"], "action": x.get("action"), "amount": x.get("amount"),
                  "decision": x["decision"], "reason": x["reason"]} for x in d],
                ["id", "receiver", "action", "amount", "decision", "reason"])
        elif a.cmd == "check":
            env = json.load(sys.stdin if a.envelope == "-" else open(a.envelope))
            d = at.check(a.receiver, env)
            if a.json:
                _print(d.raw)
            else:
                print(f"{d.decision.upper()}  {d.reason}")
                if d.detail:
                    print(d.detail)
                print(f"receipt {d.receipt['id'][:12]}  signature {'valid' if d.receipt_verified else 'INVALID'}")
            return 0 if d.allowed else 2
        elif a.cmd == "verify-receipt":
            ok = at.verify_receipt(json.load(open(a.receipt)))
            print("valid" if ok else "INVALID")
            return 0 if ok else 1
    except httpx.ConnectError:
        sys.exit(f"cannot reach a gate at {at.url}. Start one: uvicorn gate.server:app --port 8402")
    except httpx.HTTPStatusError as e:
        sys.exit(f"gate said {e.response.status_code}: {e.response.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
