"""HTTP interface of Attest (FastAPI).

Gate endpoints (the product):
  GET  /pubkey                       the gate's signing key, so receipts can be checked
  POST /verify/{receiver}            body = envelope -> decision + signed receipt
  POST /revoke                       {document, by, sig, reason}   by must be the document's signer
  GET  /receipts?limit=20            recent signed receipts
  GET  /receivers                    registered receivers and their stake thresholds
  GET  /stakes                       issuer stake table (stands in for the on-chain contract)
  POST /stake                        {issuer, name, amount, sig}   operator adds stake
  GET  /claims                       slashing claims and their status
  POST /claims                       {receipt, breach, sig}        receiver files a claim backed by a receipt
  POST /claims/{id}/dispute          {reason, sig}                 accused issuer contests it in the window
  POST /claims/{id}/resolve          {uphold, sig}                 arbiter rules on a disputed claim
  GET  /block                        latest Base Sepolia block the puzzles are seeded with
  GET  /stats                        counts for the dashboard
  GET  /                             the web dashboard (gate/static/index.html)

Demo mode (only when ATTEST_DEMO=1; the gate signs on behalf of the demo agents in state/agents):
  GET  /demo/agents                  names of the demo agents
  POST /demo/try                     {agent, receiver, action, amount} -> the same answer as /verify

Demo plumbing (not the product): a tiny message inbox so two agents on one laptop
can talk to each other without a messaging network.
  POST /inbox/{receiver}             drop an envelope for a receiver
  GET  /inbox/{receiver}/next        pop the oldest pending envelope
  GET  /inbox/{receiver}/get/{id}    fetch an envelope by request id
  POST /inbox/{receiver}/reply       {request_id, text}
  GET  /replies/{agent_pub}          replies addressed to an agent

Run with a single worker: the gate's state is one JSON file guarded by an in-process lock.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .core import Gate, STATE_DIR

app = FastAPI(title="Attest", version="0.3")
gate = Gate()
INBOX_FILE = STATE_DIR / "inbox.json"
_inbox_lock = threading.Lock()


def _inbox() -> dict:
    if INBOX_FILE.exists():
        return json.loads(INBOX_FILE.read_text())
    return {"pending": {}, "all": {}, "replies": []}


def _save_inbox(d: dict):
    INBOX_FILE.write_text(json.dumps(d, indent=1))


def _ok(out: dict) -> dict:
    if not out.get("ok"):
        raise HTTPException(400, out.get("error"))
    return out


STATIC = Path(__file__).parent / "static"
DEMO = os.environ.get("ATTEST_DEMO") == "1"


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/stats")
def stats():
    rs = gate.receipts(1000)
    st, cl = gate.stakes(), gate.claims()
    return {"decisions": len(rs), "by_decision": dict(Counter(r["decision"] for r in rs)),
            "staked_total": sum(v["stake"] for v in st.values()), "issuers": len(st),
            "suspended": sum(1 for v in st.values() if v.get("suspended")),
            "claims": dict(Counter(c["status"] for c in cl)), "demo": DEMO}


@app.get("/pubkey")
def pubkey():
    return {"gate": gate.pubkey, "alg": "ed25519"}


@app.get("/receivers")
def receivers():
    with gate.lock:
        return json.loads(json.dumps(gate.state["receivers"]))


@app.get("/stakes")
def stakes():
    return {k[:16]: v for k, v in gate.stakes().items()}


@app.get("/block")
def block():
    with gate.lock:
        return gate.latest_block()


class Stake(BaseModel):
    issuer: str
    name: str
    amount: float
    sig: str


@app.post("/stake")
def stake(s: Stake):
    return _ok(gate.stake(s.issuer, s.name, s.amount, s.sig))


class Claim(BaseModel):
    receipt: dict
    breach: str
    sig: str


class Dispute(BaseModel):
    reason: str
    sig: str


class Resolve(BaseModel):
    uphold: bool
    sig: str


@app.get("/claims")
def claims():
    return gate.claims()


@app.post("/claims")
def file_claim(c: Claim):
    return _ok(gate.file_claim(c.receipt, c.breach, c.sig))


@app.post("/claims/{claim_id}/dispute")
def dispute(claim_id: str, d: Dispute):
    return _ok(gate.dispute(claim_id, d.reason, d.sig))


@app.post("/claims/{claim_id}/resolve")
def resolve(claim_id: str, r: Resolve):
    return _ok(gate.resolve(claim_id, r.uphold, r.sig))


@app.post("/verify/{receiver}")
def verify(receiver: str, envelope: dict):
    return gate.verify(receiver, envelope)


class Revoke(BaseModel):
    document: dict
    by: str
    sig: str
    reason: str = ""


@app.post("/revoke")
def revoke(r: Revoke):
    return _ok(gate.revoke(r.document, r.by, r.sig, r.reason))


@app.get("/receipts")
def receipts(limit: int = 20):
    return gate.receipts(limit)


# ---------------- demo mode ----------------
class Try(BaseModel):
    agent: str
    receiver: str = "supplierco"
    action: str = "request_quote"
    amount: float = 0


def _demo_agents() -> list[str]:
    d = STATE_DIR / "agents"
    return sorted(f.stem for f in d.glob("*.json") if "credential" in json.loads(f.read_text())) if d.exists() else []


@app.get("/demo/agents")
def demo_agents():
    if not DEMO:
        raise HTTPException(404, "demo mode is off (start with ATTEST_DEMO=1)")
    return _demo_agents()


@app.post("/demo/try")
def demo_try(t: Try):
    if not DEMO:
        raise HTTPException(404, "demo mode is off (start with ATTEST_DEMO=1)")
    if t.agent not in _demo_agents():
        raise HTTPException(404, "no such demo agent")
    from attest.client import build_request
    me = json.loads((STATE_DIR / "agents" / f"{t.agent}.json").read_text())
    out = gate.verify(t.receiver, build_request(me, t.receiver, t.action, t.amount, "sent from the dashboard"))
    return {**out, "agent": t.agent, "agent_key": me["pub"][:16], "chain_depth": len(me["chain"])}


# ---------------- demo inbox ----------------
@app.post("/inbox/{receiver}")
def inbox_post(receiver: str, envelope: dict):
    req = envelope.get("request") or {}
    rid = req.get("id")
    if not rid:
        raise HTTPException(400, "envelope.request.id missing")
    with _inbox_lock:
        d = _inbox()
        d["pending"].setdefault(receiver, []).append(rid)
        d["all"][rid] = {"receiver": receiver, "envelope": envelope, "ts": time.time()}
        _save_inbox(d)
    return {"ok": True, "request_id": rid}


@app.get("/inbox/{receiver}/next")
def inbox_next(receiver: str):
    with _inbox_lock:
        d = _inbox()
        q = d["pending"].get(receiver, [])
        if not q:
            return {"empty": True}
        rid = q.pop(0)
        _save_inbox(d)
    return {"request_id": rid, **d["all"][rid]}


@app.get("/inbox/{receiver}/get/{rid}")
def inbox_get(receiver: str, rid: str):
    with _inbox_lock:
        item = _inbox()["all"].get(rid)
    if not item or item["receiver"] != receiver:
        raise HTTPException(404, "no such request")
    return item


class Reply(BaseModel):
    request_id: str
    text: str


@app.post("/inbox/{receiver}/reply")
def inbox_reply(receiver: str, r: Reply):
    with _inbox_lock:
        d = _inbox()
        item = d["all"].get(r.request_id)
        if not item or item["receiver"] != receiver:
            raise HTTPException(404, "no such request")
        to = item["envelope"].get("request", {}).get("from")
        d["replies"].append({"request_id": r.request_id, "from": receiver, "to": to, "text": r.text, "ts": time.time()})
        _save_inbox(d)
    return {"ok": True}


@app.get("/replies/{agent_pub}")
def replies(agent_pub: str):
    with _inbox_lock:
        d = _inbox()
    return [r for r in d["replies"] if r["to"] == agent_pub]
