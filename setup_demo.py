"""Create the demo world: two operators, three agents, credentials and mandates.

  AcmeCorp      operator (buyer side). Issues a credential and a root mandate to
                its procurement agent: actions [request_quote, place_order], max 10,000.
  acme-procurement   agent of AcmeCorp (Hermes, role buyer).
  acme-sub-ok        sub-agent delegated by acme-procurement: [request_quote], max 5,000.
  acme-sub-greedy    sub-agent delegated by acme-procurement: [place_order], max 15,000
                     (exceeds its parent: the gate must refuse it).
  ShadyCo       an operator with no stake. Issues a credential to 'impostor'.
  impostor      agent claiming to buy for Acme, credential from ShadyCo.
  FlakyCo       a staked operator whose agent misbehaves and gets slashed.
  flaky-buyer   agent of FlakyCo: [request_quote, place_order], max 5,000.
  SupplierCo    receiver (Hermes, role supplier). Accepts any issuer staked at
                10,000 or more (at least the value of what it protects).
                Policy: place_order above 8,000 escalates to a human.
                Has its own key (rcv-supplierco), used to sign slashing claims.
  Arbiter       key that rules on disputed slashing claims.

Stakes (stand-in for the on-chain stake contract): AcmeCorp 25,000, FlakyCo 20,000,
ShadyCo none. The dispute window for slashing claims is shortened to a few seconds
so the scenarios can show it closing (the gate's default is 72 hours).

Everything is written to state/agents/<name>.json. Private keys stay on this
laptop; this is a demo, not a key management system.
"""
import json
import time

from gate import crypto
from gate.core import Gate, STATE_DIR

AG = STATE_DIR / "agents"
AG.mkdir(parents=True, exist_ok=True)
DAY = 86400
DEMO_DISPUTE_WINDOW_S = 3


def save(name, doc):
    (AG / f"{name}.json").write_text(json.dumps(doc, indent=1))


def operator(name, file=None):
    priv, pub = crypto.keygen()
    doc = {"name": name, "priv": priv, "pub": pub}
    save(file or f"op-{name}", doc)
    return doc


def agent(name, op, purpose, actions, max_amount, days=30):
    priv, pub = crypto.keygen()
    now = time.time()
    cred = crypto.signed(op["priv"], {
        "type": "kya.credential/1", "agent": pub, "operator": op["name"], "issuer": op["pub"],
        "purpose": purpose, "issued_at": now, "expires_at": now + days * DAY})
    root = crypto.signed(op["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": op["pub"], "parent": None,
        "scope": {"actions": actions, "max_amount": max_amount, "currency": "USD"},
        "expires_at": now + days * DAY})
    doc = {"name": name, "priv": priv, "pub": pub, "operator": op["name"],
           "credential": cred, "chain": [root]}
    save(name, doc)
    return doc


def delegate(parent, name, actions, max_amount, days=7):
    """parent agent signs a child mandate for a new sub-agent key."""
    priv, pub = crypto.keygen()
    now = time.time()
    child = crypto.signed(parent["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": parent["pub"],
        "parent": parent["chain"][-1]["id"],
        "scope": {"actions": actions, "max_amount": max_amount, "currency": "USD"},
        "expires_at": now + days * DAY})
    doc = {"name": name, "priv": priv, "pub": pub, "operator": parent["operator"],
           "delegated_by": parent["name"],
           "credential": parent["credential"], "chain": parent["chain"] + [child]}
    save(name, doc)
    return doc


if __name__ == "__main__":
    acme = operator("AcmeCorp")
    shady = operator("ShadyCo")
    proc = agent("acme-procurement", acme, "procurement: quotes and orders for Acme plants",
                 ["request_quote", "place_order"], 10000)
    delegate(proc, "acme-sub-ok", ["request_quote"], 5000)
    delegate(proc, "acme-sub-greedy", ["place_order"], 15000)
    agent("impostor", shady, "buying for Acme (so it says)", ["request_quote", "place_order"], 50000)
    flaky = operator("FlakyCo")
    agent("flaky-buyer", flaky, "procurement for FlakyCo", ["request_quote", "place_order"], 5000)

    supplier_key = operator("supplierco", file="rcv-supplierco")
    arbiter = operator("Arbiter", file="arbiter")

    g = Gate()
    g.register_receiver("supplierco", min_stake=10000,
                        policy={"escalate_above": {"place_order": 8000}}, pubkey=supplier_key["pub"])
    g.configure(dispute_window_s=DEMO_DISPUTE_WINDOW_S, arbiter=arbiter["pub"])
    for op, amount in ((acme, 25000), (flaky, 20000)):
        body = {"stake": True, "issuer": op["pub"], "name": op["name"], "amount": float(amount)}
        print(g.stake(op["pub"], op["name"], amount, crypto.sign(op["priv"], body)))
    print("gate pubkey:", g.pubkey)
    for f in sorted(AG.glob("*.json")):
        print("wrote", f.name)
