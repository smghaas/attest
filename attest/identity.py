"""Your own keys and documents, kept on your machine (default ~/.attest, override with $ATTEST_DIR).

An operator is who stands behind agents. An agent holds a credential from its operator and a mandate
(what it may do, up to what amount). A receiver is who asks the gate before acting. Private keys never
leave this folder: the gate only ever sees signatures.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from gate import crypto

DAY = 86400


def home() -> Path:
    d = Path(os.environ.get("ATTEST_DIR") or Path.home() / ".attest")
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(doc: dict) -> Path:
    p = home() / f"{doc['name']}.json"
    p.write_text(json.dumps(doc, indent=1))
    p.chmod(0o600)
    return p


def load(name: str) -> dict:
    p = home() / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"no identity called {name!r} in {home()} (see: attest ids)")
    return json.loads(p.read_text())


def ids() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(home().glob("*.json"))]


def _fresh(name: str):
    if (home() / f"{name}.json").exists():
        raise FileExistsError(f"{name!r} already exists in {home()}")


def new_operator(name: str, persist: bool = True) -> dict:
    _fresh(name)
    priv, pub = crypto.keygen()
    doc = {"kind": "operator", "name": name, "priv": priv, "pub": pub}
    if persist:
        save(doc)
    return doc


def new_receiver(name: str, persist: bool = True) -> dict:
    _fresh(name)
    priv, pub = crypto.keygen()
    doc = {"kind": "receiver", "name": name, "priv": priv, "pub": pub}
    if persist:
        save(doc)
    return doc


def new_agent(name: str, operator: dict, purpose: str, actions: list[str], max_amount: float,
              days: float = 30) -> dict:
    """The operator signs a credential and a root mandate for a fresh agent key."""
    _fresh(name)
    priv, pub = crypto.keygen()
    now = time.time()
    cred = crypto.signed(operator["priv"], {
        "type": "kya.credential/1", "agent": pub, "operator": operator["name"], "issuer": operator["pub"],
        "purpose": purpose, "issued_at": now, "expires_at": now + days * DAY})
    root = crypto.signed(operator["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": operator["pub"], "parent": None,
        "scope": {"actions": actions, "max_amount": float(max_amount), "currency": "USD"},
        "expires_at": now + days * DAY})
    doc = {"kind": "agent", "name": name, "priv": priv, "pub": pub, "operator": operator["name"],
           "credential": cred, "chain": [root]}
    save(doc)
    return doc


def delegate(parent: dict, name: str, actions: list[str], max_amount: float, days: float = 7) -> dict:
    """The parent agent signs a narrower mandate for a new sub-agent. The gate refuses it if it is wider."""
    _fresh(name)
    priv, pub = crypto.keygen()
    now = time.time()
    # A child may not outlive its parent (the gate refuses that as an attenuation violation), so the
    # requested lifetime is cut at the parent's expiry.
    expires = min(now + days * DAY, float(parent["chain"][-1]["expires_at"]))
    child = crypto.signed(parent["priv"], {
        "type": "kya.mandate/1", "subject": pub, "issuer": parent["pub"], "parent": parent["chain"][-1]["id"],
        "scope": {"actions": actions, "max_amount": float(max_amount), "currency": "USD"},
        "expires_at": expires})
    doc = {"kind": "agent", "name": name, "priv": priv, "pub": pub, "operator": parent["operator"],
           "delegated_by": parent["name"], "credential": parent["credential"], "chain": parent["chain"] + [child]}
    save(doc)
    return doc
