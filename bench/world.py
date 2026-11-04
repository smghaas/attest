"""A fresh, isolated Attest world for one benchmark case.

Same actors as setup_demo.py, built in memory with their own temporary gate state,
plus the extra actors the benchmark needs (a forged credential, an expired mandate,
a newcomer with no credential, Sybil operators). setup_demo.py's builders write key
files into ./state/agents, so they are not reused here: a benchmark run must not
touch the demo's keys.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path

from gate import crypto
from gate.core import Gate

DAY = 86400
RECEIVER = "supplierco"
MIN_STAKE = 10000
ESCALATE_ABOVE = 8000


def operator(name: str) -> dict:
    priv, pub = crypto.keygen()
    return {"name": name, "priv": priv, "pub": pub}


def agent(name: str, op: dict, purpose: str, actions: list, max_amount: float, days: float = 30,
          mandate_days: float | None = None, claimed_operator: str | None = None) -> dict:
    """An agent with a credential and root mandate signed by `op`.
    claimed_operator puts a different name in the credential's operator field (forgery);
    mandate_days lets the mandate expire on its own (negative = already expired)."""
    priv, pub = crypto.keygen()
    now = time.time()
    cred = crypto.signed(op["priv"], {
        "type": "kya.credential/1", "agent": pub, "operator": claimed_operator or op["name"],
        "issuer": op["pub"], "purpose": purpose, "issued_at": now, "expires_at": now + days * DAY})
    root = crypto.signed(op["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": op["pub"], "parent": None,
        "scope": {"actions": actions, "max_amount": max_amount, "currency": "USD"},
        "expires_at": now + (days if mandate_days is None else mandate_days) * DAY})
    return {"name": name, "priv": priv, "pub": pub, "operator": op["name"], "credential": cred, "chain": [root]}


def delegate(parent: dict, name: str, actions: list, max_amount: float, days: float = 7) -> dict:
    priv, pub = crypto.keygen()
    child = crypto.signed(parent["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": parent["pub"], "parent": parent["chain"][-1]["id"],
        "scope": {"actions": actions, "max_amount": max_amount, "currency": "USD"},
        "expires_at": time.time() + days * DAY})
    return {"name": name, "priv": priv, "pub": pub, "operator": parent["operator"],
            "delegated_by": parent["name"], "credential": parent["credential"], "chain": parent["chain"] + [child]}


def bare(name: str) -> dict:
    """A key with no credential and no mandate."""
    priv, pub = crypto.keygen()
    return {"name": name, "priv": priv, "pub": pub, "operator": None, "credential": None, "chain": []}


def request(ag: dict, action: str, amount: float, message: str, to: str = RECEIVER,
            presented_amount: float | None = None, signer: dict | None = None) -> dict:
    """A signed request envelope. presented_amount changes the amount after signing
    (tampering); signer signs with a different key than the one named in `from`."""
    req = crypto.signed((signer or ag)["priv"], {
        "type": "kya.request/1", "from": ag["pub"], "to": to, "action": action, "amount": float(amount),
        "message": message, "ts": time.time(), "nonce": uuid.uuid4().hex})
    if presented_amount is not None:
        req = {**req, "amount": float(presented_amount)}
    return {"credential": ag["credential"], "chain": ag["chain"], "request": req}


class World:
    def __init__(self, issuer_cap_ratio: float | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="attest-bench-"))
        self.gate = Gate(self.dir / "state")
        self.gate.receipts_file = self.dir / "receipts.jsonl"
        if issuer_cap_ratio is not None:
            self.gate.configure(issuer_cap_ratio=issuer_cap_ratio)
        self.receiver_key = operator(RECEIVER)
        self.arbiter = operator("Arbiter")
        self.gate.register_receiver(RECEIVER, min_stake=MIN_STAKE,
                                    policy={"escalate_above": {"place_order": ESCALATE_ABOVE}},
                                    pubkey=self.receiver_key["pub"])
        self.gate.configure(dispute_window_s=0, arbiter=self.arbiter["pub"])
        self.ops: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def stake(self, op: dict, amount: float):
        body = {"stake": True, "issuer": op["pub"], "name": op["name"], "amount": float(amount)}
        out = self.gate.stake(op["pub"], op["name"], amount, crypto.sign(op["priv"], body))
        assert out["ok"], out

    def revoke(self, doc: dict, by: dict, reason: str):
        body = {"revoke": doc["id"], "by": by["pub"], "reason": reason}
        out = self.gate.revoke(doc, by["pub"], crypto.sign(by["priv"], body), reason)
        assert out["ok"], out

    def slash(self, ag: dict, amount: float = 1000):
        """Get one allow receipt for `ag`, file an undisputed claim on it, let the window close."""
        r = self.gate.verify(RECEIVER, request(ag, "place_order", amount, "setup: order later disputed"))
        assert r["decision"] == "allow", r
        body = {"claim": r["receipt"]["id"], "receiver": RECEIVER, "breach": "payment never sent"}
        out = self.gate.file_claim(r["receipt"], body["breach"], crypto.sign(self.receiver_key["priv"], body))
        assert out["ok"], out
        self.gate.stakes()  # settles: the window is 0 s


def demo_world(issuer_cap_ratio: float | None = None) -> World:
    """The HW6 demo cast plus the benchmark's extra actors."""
    w = World(issuer_cap_ratio)
    acme, flaky, shady = operator("AcmeCorp"), operator("FlakyCo"), operator("ShadyCo")
    w.ops.update(acme=acme, flaky=flaky, shady=shady)
    proc = agent("acme-procurement", acme, "procurement: quotes and orders for Acme plants",
                 ["request_quote", "place_order"], 10000)
    w.agents.update(
        proc=proc,
        sub_ok=delegate(proc, "acme-sub-ok", ["request_quote"], 5000),
        greedy=delegate(proc, "acme-sub-greedy", ["place_order"], 15000),
        flaky=agent("flaky-buyer", flaky, "procurement for FlakyCo", ["request_quote", "place_order"], 5000),
        impostor=agent("shady-buyer", shady, "buying for Acme (so it says)", ["request_quote", "place_order"], 50000),
        forger=agent("acme-procurement-2", shady, "procurement: quotes and orders for Acme plants",
                     ["request_quote", "place_order"], 10000, claimed_operator="AcmeCorp"),
        expired=agent("acme-procurement-old", acme, "procurement: quotes and orders for Acme plants",
                      ["request_quote", "place_order"], 10000, mandate_days=-1),
        newcomer=bare("newcomer"),
        thief=bare("thief"),
    )
    w.stake(acme, 25000)
    w.stake(flaky, 20000)
    return w
