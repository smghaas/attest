"""A check where the asker decides what must be true.

Anyone, a person or an agent, sends an agent's signed request (the envelope), the task it is about, and a
list of requirements. The gate answers each one and signs the whole checklist. Nothing is spent and no
state changes, so no one needs permission to ask; the request is bound to the asker (`to`) and must be
fresh, so an old envelope cannot be shown to someone else.

Requirements (all optional except the first):
  signed       the agent signed this request, just now, for this asker          always on
  backer       a person, company or organization other than the agent signed its ID and permission,
               valid, not revoked (an ID marked backer_kind "self" fails this; its permission still counts)
  allowed      the task (action, amount) is inside what the backer allowed
  stake        the backer put down at least `min_stake`
  work         the agent did proof of work of at least `work_bits` on this request
  either       with both stake and work asked: one of the two is enough
  history      on the ledger: at least `min_deals` confirmed deals, at most `max_disputed` of them disputed
  human_above  above this amount, a person must approve (answer: wait)
"""
from __future__ import annotations

import hashlib
import math
import time

from . import crypto
from .core import REQUEST_SKEW_S

MAX_WORK_BITS = 24


def work_ok(request_id: str, nonce, bits: int) -> bool:
    d = hashlib.sha256(f"{request_id}:{nonce}".encode()).digest()
    return int.from_bytes(d, "big") >> (256 - bits) == 0 if bits > 0 else True


def _chain(gate, env: dict, now: float) -> tuple[bool, str, dict | None]:
    """The backer's ID and the permission chain: signatures, expiry, revocation, delegation."""
    cred, chain, req = env.get("credential"), env.get("chain") or [], env.get("request") or {}
    if not cred:
        return False, "no one has signed an ID for this agent", None
    if not (crypto.verify(cred.get("issuer", ""), crypto.body_of(cred), cred.get("sig", "")) and crypto.id_ok(cred)):
        return False, "the ID is not genuinely signed by its backer", None
    if cred.get("expires_at", 0) < now:
        return False, "the ID has expired", None
    if cred["id"] in gate.state["revoked"]:
        return False, "the backer cancelled this ID", None
    if not chain or chain[0].get("issuer") != cred["issuer"] or chain[0].get("parent") is not None \
            or chain[0].get("subject") != cred.get("agent"):
        return False, "no valid permission from the backer", None
    prev = None
    for m in chain:
        if not (crypto.verify(m.get("issuer", ""), crypto.body_of(m), m.get("sig", "")) and crypto.id_ok(m)):
            return False, "a permission in the chain is not genuinely signed", None
        if m.get("expires_at", 0) < now:
            return False, "a permission has expired", None
        if m["id"] in gate.state["revoked"]:
            return False, "a permission was cancelled", None
        if prev is not None:
            ok, why = gate._attenuates(m["scope"], prev["scope"], m.get("expires_at", 0), prev.get("expires_at", 0))
            if m.get("parent") != prev["id"] or m.get("issuer") != prev["subject"] or not ok:
                return False, "a helper agent was given more power than the agent that made it", None
        prev = m
    if req.get("from") != prev["subject"]:
        return False, "the request is not from the agent the permission is for", None
    kind = cred.get("backer_kind", "company")
    if kind == "self":
        # a valid, signed ID, but nobody else answers for it: the permission still counts, the backing does not
        return False, f"{cred.get('operator', '?')} vouches only for themself", prev["scope"]
    return True, f"{cred.get('operator', '?')} ({kind}) signed its ID and permission", prev["scope"]


def _history(ledger, pub: str, cap: int = 2) -> dict | None:
    if ledger is None:
        return None
    h = ledger.nodes[0].history(pub)
    peers: dict = {}
    for e in h["entries"]:
        r = e["record"]
        if r["status"] == "accepted":
            p = r["buyer"] if r["provider"] == pub else r["provider"]
            peers[p] = peers.get(p, 0) + 1
    return {"accepted": h["accepted"], "disputed": h["disputed"], "trades": h["trades"],
            "credit": sum(min(cap, c) for c in peers.values()), "partners": len(peers)}


def run(gate, env: dict, checker: str, require: dict, ledger=None) -> dict:
    now = time.time()
    env = env or {}
    req = env.get("request") or {}
    items: list[dict] = []

    def add(key, label, ok, why, status=None):
        items.append({"key": key, "label": label, "status": status or ("pass" if ok else "fail"), "why": why})

    # always: the agent itself signed this request, just now, for this asker
    fresh = abs(now - float(req.get("ts") or 0)) <= REQUEST_SKEW_S
    sig_ok = crypto.verify(req.get("from", ""), crypto.body_of(req), req.get("sig", "")) and crypto.id_ok(req)
    if not sig_ok:
        why = "the request is not signed by the agent's key (forged or changed)"
    elif req.get("to") != checker:
        why = f"the request was made for {req.get('to')!r}, not for you"
    elif not fresh:
        why = f"the request is more than {REQUEST_SKEW_S // 60} minutes old"
    else:
        why = "the sender holds its key and signed this request for you just now"
    add("signed", "Signed by the sender itself", sig_ok and req.get("to") == checker and fresh, why)

    try:
        amount = float(req.get("amount") or 0)
    except (TypeError, ValueError):
        amount = math.nan
    action = req.get("action")

    chain_ok, chain_why, scope = _chain(gate, env, now)
    if require.get("backer", True):
        add("backer", "Someone else vouches for it", chain_ok, chain_why)
    if require.get("allowed"):
        if scope is None:
            add("allowed", "It may do this task", False, "no valid permission to check against")
        elif action not in scope.get("actions", []):
            add("allowed", "It may do this task", False, f"its backer never allowed {action!r} (allowed: "
                                                              f"{', '.join(scope.get('actions', []))})")
        elif not math.isfinite(amount) or amount < 0 or amount > float(scope.get("max_amount", 0)):
            add("allowed", "It may do this task", False, f"${amount:,.0f} is above its limit of "
                                                              f"${float(scope.get('max_amount', 0)):,.0f}")
        else:
            add("allowed", "It may do this task", True, f"{action} for ${amount:,.0f} is within what its backer allowed")

    min_stake = require.get("min_stake")
    bits = min(int(require.get("work_bits") or 0), MAX_WORK_BITS)
    stake_res = work_res = None
    if min_stake is not None:
        s = gate.state["stakes"].get((env.get("credential") or {}).get("issuer")) if scope is not None else None
        if not s:
            stake_res = (False, "its backer has put no money down")
        elif s.get("suspended"):
            stake_res = (False, f"its backer was penalised for misconduct and is suspended")
        elif s["stake"] < float(min_stake):
            stake_res = (False, f"its backer put down ${s['stake']:,.0f}, less than ${float(min_stake):,.0f}")
        else:
            stake_res = (True, f"its backer put down ${s['stake']:,.0f}")
    if bits:
        nonce = (env.get("work") or {}).get("nonce")
        if nonce is None:
            work_res = (False, f"it did no proof of work ({bits} bits asked)")
        elif sig_ok and work_ok(req.get("id", ""), nonce, bits):
            work_res = (True, f"it did {bits} bits of proof of work on this very request")
        else:
            work_res = (False, f"its proof of work does not reach {bits} bits")
    if stake_res and work_res and require.get("either"):
        ok = stake_res[0] or work_res[0]
        add("backing", "Money down, or proof of work", ok,
            stake_res[1] if stake_res[0] else work_res[1] if work_res[0] else f"{stake_res[1]}; and {work_res[1]}")
    else:
        if stake_res:
            add("stake", "Its backer put money down", *stake_res)
        if work_res:
            add("work", "It did proof of work", *work_res)

    if require.get("min_deals") is not None or require.get("max_disputed") is not None:
        h = _history(ledger, req.get("from", ""))
        need = int(require.get("min_deals") or 0)
        ratio = float(require.get("max_disputed") if require.get("max_disputed") is not None else 1)
        if h is None:
            add("history", "Good track record", False, "this gate keeps no ledger")
        else:
            bad = h["disputed"] / h["trades"] if h["trades"] else 0
            if h["credit"] < need:
                add("history", "Good track record", False,
                    f"{h['accepted']} confirmed deals, {h['credit']} counted (at most 2 per partner); {need} needed")
            elif bad > ratio:
                add("history", "Good track record", False, f"{h['disputed']} of {h['trades']} deals disputed")
            else:
                add("history", "Good track record", True,
                    f"{h['accepted']} confirmed deals with {h['partners']} partners, {h['disputed']} disputed")

    if require.get("human_above") is not None and math.isfinite(amount):
        limit = float(require["human_above"])
        if amount > limit:
            add("human", "A person approves", False, f"${amount:,.0f} is above ${limit:,.0f}: a person must approve",
                status="wait")

    statuses = {i["status"] for i in items}
    verdict = "fail" if "fail" in statuses else "wait" if "wait" in statuses else "pass"
    body = {"type": "attest.check/1", "checker": checker, "request_id": req.get("id"), "from": req.get("from"),
            "action": action, "amount": req.get("amount"), "require": require, "items": items,
            "verdict": verdict, "ts": time.time(), "gate": gate.pubkey}
    return crypto.signed(gate.state["gate_key"]["priv"], body)
