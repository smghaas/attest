"""Command line for Attest.   attest --help"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from . import identity
from .client import Attest, build_request


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
    sub.add_parser("ids", help="identities (operators, agents, receivers) kept on this machine")
    sub.add_parser("info", help="what kind of gate this is: sandbox or private, limits, faucet")
    o = sub.add_parser("operator", help="create an operator and get free sandbox stake")
    o.add_argument("action", choices=["create"])
    o.add_argument("name")
    rc = sub.add_parser("receiver", help="create a receiver, the one who asks the gate before acting")
    rc.add_argument("action", choices=["create"])
    rc.add_argument("name")
    rc.add_argument("--min-stake", type=float, default=0, help="operators must have at least this much stake")
    rc.add_argument("--escalate", action="append", default=[], metavar="ACTION=AMOUNT",
                    help="ask a human above this amount: *=8000 for every action, or place_order=8000 for one")
    ag = sub.add_parser("agent", help="create an agent for an operator, or delegate a narrower one")
    ag.add_argument("action", choices=["create", "delegate"])
    ag.add_argument("name")
    ag.add_argument("--operator", help="operator name (for create)")
    ag.add_argument("--parent", help="parent agent name (for delegate)")
    ag.add_argument("--purpose", default="agent")
    ag.add_argument("--actions", default="request_quote", help="comma separated, e.g. request_quote,place_order")
    ag.add_argument("--max", type=float, default=1000, dest="max_amount", help="largest amount per mandate, USD")
    ag.add_argument("--days", type=float, default=30)
    sub.add_parser("ledger", help="the ledger: height, agents with passports and their history")
    pp = sub.add_parser("passport", help="register an agent's passport on the ledger")
    pp.add_argument("action", choices=["create"])
    pp.add_argument("agent")
    hs = sub.add_parser("history", help="what an agent has done, from the ledger (local name or public key)")
    hs.add_argument("agent")
    tr = sub.add_parser("trade", help="record a trade both sides sign (both identities must be on this machine)")
    tr.add_argument("provider")
    tr.add_argument("buyer")
    tr.add_argument("request_id")
    tr.add_argument("amount", type=float)
    tr.add_argument("--disputed", action="store_true", help="record it as disputed (we record, we do not judge)")
    rq = sub.add_parser("request", help="sign a request as an agent; --check also asks the gate")
    rq.add_argument("agent")
    rq.add_argument("receiver")
    rq.add_argument("action")
    rq.add_argument("amount", type=float)
    rq.add_argument("--message", default="")
    rq.add_argument("--check", action="store_true")
    a = p.parse_args(argv)
    at = Attest(a.url)
    try:
        if a.cmd == "ids":
            _table([{"name": d["name"], "kind": d.get("kind", "?"), "key": d["pub"][:16],
                     "operator": d.get("operator", ""), "parent": d.get("delegated_by", "")}
                    for d in identity.ids()], ["name", "kind", "operator", "parent", "key"])
        elif a.cmd == "ledger":
            info = at.ledger_info()
            print(f"height {info['height']}, final {info['final']}, {info['records']} records, "
                  f"{info['validators']} validators\n{info['note']}\n")
            _table([{"agent": x["name"], "trades": x["trades"], "accepted": x["accepted"], "disputed": x["disputed"],
                     "volume": f"{x['volume']:,.0f}", "decision": x["decision"]} for x in at.ledger_agents()],
                   ["agent", "trades", "accepted", "disputed", "volume", "decision"])
        elif a.cmd == "passport":
            ag = identity.load(a.agent)
            out = at.register_passport(identity.load(ag["operator"]), ag)
            print(f"passport for {a.agent} is on the ledger (block {out['height']})")
        elif a.cmd == "history":
            pub = identity.load(a.agent)["pub"] if (identity.home() / f"{a.agent}.json").exists() else a.agent
            h = at.history(pub)
            _print(h) if a.json else print(f"{a.agent}: passport {'yes' if h['passport'] else 'no'}, {h['trades']} trades, "
                                           f"{h['accepted']} accepted, {h['disputed']} disputed, volume ${h['volume']:,.0f}")
            if not a.json:
                _table([{"block": e["height"], "request": e["record"]["request"], "status": e["record"]["status"],
                         "amount": e["record"]["amount"]} for e in h["entries"]], ["block", "request", "status", "amount"])
        elif a.cmd == "trade":
            out = at.record_trade(identity.load(a.provider), identity.load(a.buyer), a.request_id, a.amount,
                                  "disputed" if a.disputed else "accepted")
            print(f"recorded in block {out['height']} (signed by both {a.provider} and {a.buyer})")
        elif a.cmd == "info":
            _print(at.info())
        elif a.cmd == "operator":
            op = identity.new_operator(a.name, persist=False)
            out = at.faucet(op)
            identity.save(op)                      # keep the key only once the gate accepted it
            print(f"operator {a.name} created, key kept in {identity.home()}/{a.name}.json")
            print(f"funded with {out['stake']:,.0f} test stake (sandbox credit, not money)")
        elif a.cmd == "receiver":
            rcv = identity.new_receiver(a.name, persist=False)
            esc = {k: float(v) for k, v in (x.split("=", 1) for x in a.escalate)}
            at.register_receiver(rcv, a.min_stake, esc or None)
            identity.save(rcv)
            print(f"receiver {a.name} registered (min stake {a.min_stake:,.0f}); key kept in {identity.home()}/{a.name}.json")
            print(f"check an agent with:  attest request AGENT {a.name} request_quote 100 --check")
        elif a.cmd == "agent":
            acts = [x.strip() for x in a.actions.split(",") if x.strip()]
            if a.action == "create":
                if not a.operator:
                    sys.exit("--operator is required")
                d = identity.new_agent(a.name, identity.load(a.operator), a.purpose, acts, a.max_amount, a.days)
            else:
                if not a.parent:
                    sys.exit("--parent is required")
                d = identity.delegate(identity.load(a.parent), a.name, acts, a.max_amount, a.days)
            print(f"agent {a.name} created: {', '.join(acts)} up to ${a.max_amount:,.0f}, chain depth {len(d['chain'])}")
        elif a.cmd == "request":
            env = build_request(identity.load(a.agent), a.receiver, a.action, a.amount, a.message)
            if not a.check:
                _print(env)
                return 0
            d = at.check(a.receiver, env)
            if a.json:
                _print(d.raw)
            else:
                print(f"{d.decision.upper()}  {d.reason}")
                if d.detail:
                    print(d.detail)
                print(f"receipt {d.receipt['id'][:12]}  signature {'valid' if d.receipt_verified else 'INVALID'}")
            return 0 if d.allowed else 2
        elif a.cmd == "status":
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
        try:
            why = e.response.json().get("detail", e.response.text)
        except ValueError:
            why = e.response.text
        sys.exit(f"gate said {e.response.status_code}: {why}")
    except (FileNotFoundError, FileExistsError) as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
