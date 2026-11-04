"""Deterministic conditions: C0 accept everything, C1 static allowlist rule, C4 Attest alone."""
from __future__ import annotations

import time

from gate.core import READ_ONLY

from .world import ESCALATE_ABOVE, RECEIVER

ALLOWLIST = {"AcmeCorp"}


def accept_all(world, envelope, state):
    return "fulfil", "accepts everything"


def static_rule(world, envelope, state):
    """C1: what a receiver can do without Attest. It trusts the presented documents as
    written: an allowlisted operator name and the leaf mandate's scope. No signature,
    expiry, revocation, stake or history checks."""
    req = envelope.get("request") or {}
    action, amount = req.get("action"), float(req.get("amount") or 0)
    if action in READ_ONLY:
        return "fulfil", "quotes are public"
    cred = envelope.get("credential") or {}
    if cred.get("operator") not in ALLOWLIST:
        return "decline", f"operator {cred.get('operator')!r} is not on the allowlist"
    chain = envelope.get("chain") or []
    scope = chain[-1]["scope"] if chain else {}
    if action not in scope.get("actions", []) or amount > float(scope.get("max_amount", 0)):
        return "decline", "outside the presented mandate"
    if amount > ESCALATE_ABOVE:
        return "escalate", "above the escalation threshold"
    return "fulfil", "allowlisted operator, inside the presented mandate"


def attest_only(world, envelope, state):
    """C4: the gate's decision is the action."""
    t0 = time.perf_counter()
    r = world.gate.verify(RECEIVER, envelope)
    state.setdefault("gate_ms", []).append((time.perf_counter() - t0) * 1000)
    d = r["decision"]
    action = {"allow": "fulfil", "refuse": "decline", "escalate": "escalate"}.get(d)
    if d == "unknown":
        is_read_only = (envelope.get("request") or {}).get("action") in READ_ONLY
        action = "fulfil" if is_read_only else "decline"
    return action, f"{d} / {r['reason']}"


DETERMINISTIC = {"C0_accept_all": accept_all, "C1_static_rule": static_rule, "C4_attest_only": attest_only}
