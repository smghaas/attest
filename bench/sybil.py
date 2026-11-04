"""Limit probe (Part 4): how much money can one staked operator move through the gate,
compared with the stake that is supposed to back it?

  python -m bench.sybil [--cap RATIO] [--out bench/results/<run>/sybil.json]

An operator stakes S. It mints agents with their own root mandates (or delegated
sub-agents) and has them place orders below the receiver's escalation threshold.
Measured under C4 (the gate's decision is the action):
  exposure   dollars of orders the gate allowed
  recovered  what slashing claims on every allowed order can take back (at most S)
  uncovered  max(0, exposure - recovered): the receivers' loss if the operator walks away
             (slashing takes a share of the stake per claim, not the damage, so it can
             also take more than the loss: over_recovered)

TUNING cases are the sweep that exposed the limit. FRESH cases were written and
committed together with them, before any change to the gate, and were not used to
design the change.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gate import crypto

from .world import RECEIVER, MIN_STAKE, World, agent, delegate, operator, request


def spec(id, stake, groups, receivers=(RECEIVER,), honest=False, note=""):
    """groups: list of (n_agents, kind 'root'|'sub', mandate_max, [order amounts per agent])."""
    return {"id": id, "stake": stake, "groups": groups, "receivers": list(receivers), "honest": honest, "note": note}


TUNING = [spec(f"T-k{k}", 20000, [(k, "root", 10000, [5000, 5000])], note=f"{k} agent(s), own root mandates")
          for k in (1, 2, 4, 8)]

FRESH = [
    spec("F1-subagents", 20000, [(6, "sub", 10000, [5000, 5000])],
         note="one root agent delegates to 6 sub-agents (10,000 each)"),
    spec("F2-two-receivers", 20000, [(6, "root", 10000, [4000, 4000])], receivers=(RECEIVER, "partsco"),
         note="6 root agents, orders alternate between two receivers sharing the gate"),
    spec("F3-stake-15k", 15000, [(5, "root", 9000, [3000, 3000, 3000])],
         note="stake 15,000, 5 root agents, three 3,000 orders each"),
    spec("F4-honest-volume", 30000, [(5, "root", 10000, [4000, 4000])], honest=True,
         note="honest high-volume operator: 40,000 of real orders on a 30,000 stake"),
]


def run(sp: dict, cap: float | None) -> dict:
    w = World(cap)
    try:
        for extra in sp["receivers"][1:]:
            w.gate.register_receiver(extra, min_stake=MIN_STAKE, policy={"escalate_above": {"place_order": 8000}},
                                     pubkey=w.receiver_key["pub"])
        op = operator("SybilCo" if not sp["honest"] else "BigBuyer")
        w.stake(op, sp["stake"])
        agents = []
        for n, kind, mx, orders in sp["groups"]:
            if kind == "root":
                agents += [(agent(f"a{i}", op, "procurement", ["place_order"], mx), orders) for i in range(n)]
            else:
                root = agent("root", op, "procurement", ["place_order"], mx)
                agents += [(delegate(root, f"s{i}", ["place_order"], mx), orders) for i in range(n)]
        allowed, refused, receipts, i = 0.0, [], [], 0
        for ag, orders in agents:
            for amt in orders:
                rcv = sp["receivers"][i % len(sp["receivers"])]
                i += 1
                r = w.gate.verify(rcv, request(ag, "place_order", amt, f"PO-{i}", to=rcv))
                if r["decision"] == "allow":
                    allowed += amt
                    receipts.append(r["receipt"])
                else:
                    refused.append(r["reason"])
        requested = sum(sum(o) * n for n, _, _, o in sp["groups"])
        recovered = 0.0
        if not sp["honest"]:   # receivers claim every allowed order; the window is 0 s
            for rec in receipts:
                body = {"claim": rec["id"], "receiver": rec["receiver"], "breach": "goods shipped, never paid"}
                out = w.gate.file_claim(rec, body["breach"], crypto.sign(w.receiver_key["priv"], body))
                if out.get("ok"):
                    w.gate.stakes()                          # settle this claim before the next
            recovered = sp["stake"] - w.gate.stakes()[op["pub"]]["stake"]
        return {"id": sp["id"], "note": sp["note"], "honest": sp["honest"], "stake": sp["stake"],
                "requested": requested, "exposure": allowed, "exposure_over_stake": allowed / sp["stake"],
                "recovered": round(recovered, 2),
                "uncovered": None if sp["honest"] else round(max(0.0, allowed - recovered), 2),
                "over_recovered": None if sp["honest"] else round(max(0.0, recovered - allowed), 2),
                "refused_reasons": sorted(set(refused)), "refused_amount": requested - allowed}
    finally:
        w.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=None, help="gate issuer_cap_ratio (default: gate default)")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    res = {"issuer_cap_ratio": a.cap, "tuning": [run(s, a.cap) for s in TUNING], "fresh": [run(s, a.cap) for s in FRESH]}
    print(f"{'case':18} {'stake':>7} {'requested':>9} {'allowed':>8} {'x stake':>7} {'recovered':>9} {'uncovered':>9}  refused")
    for group in ("tuning", "fresh"):
        for r in res[group]:
            unc = "-" if r["uncovered"] is None else f"{r['uncovered']:,.0f}"
            print(f"{r['id']:18} {r['stake']:7,.0f} {r['requested']:9,.0f} {r['exposure']:8,.0f} "
                  f"{r['exposure_over_stake']:7.2f} {r['recovered']:9,.0f} {unc:>9}  {', '.join(r['refused_reasons'])}")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    main()
