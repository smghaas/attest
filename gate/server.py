"""HTTP interface of Attest (FastAPI).

Gate endpoints (the product):
  GET  /pubkey                       the gate's signing key, so receipts can be checked
  POST /verify/{receiver}            body = envelope -> decision + signed receipt
  POST /revoke                       {target_id, by, sig, reason}
  GET  /receipts?limit=20            recent signed receipts
  GET  /receivers                    registered receivers and their stake thresholds
  GET  /stakes                       issuer stake table (stands in for the on-chain contract)
  POST /stake                        {issuer, name, amount, sig}   operator adds stake
  POST /slash/{receiver}             {receipt_id, breach}          breach claim backed by a receipt
  GET  /block                        latest Base Sepolia block the puzzles are seeded with

Demo plumbing (not the product): a tiny message inbox so two agents on one laptop
can talk to each other without a messaging network.
  POST /inbox/{receiver}             drop an envelope for a receiver
  GET  /inbox/{receiver}/next        pop the oldest pending envelope
  GET  /inbox/{receiver}/get/{id}    fetch an envelope by request id
  POST /inbox/{receiver}/reply       {request_id, text}
  GET  /replies/{agent_pub}          replies addressed to an agent
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .core import Gate, STATE_DIR

app = FastAPI(title="Attest", version="0.2")
gate = Gate()
INBOX_FILE = STATE_DIR / "inbox.json"


def _inbox() -> dict:
    if INBOX_FILE.exists():
        return json.loads(INBOX_FILE.read_text())
    return {"pending": {}, "all": {}, "replies": []}


def _save_inbox(d: dict):
    INBOX_FILE.write_text(json.dumps(d, indent=1))


@app.get("/pubkey")
def pubkey():
    return {"gate": gate.pubkey, "alg": "ed25519"}


@app.get("/receivers")
def receivers():
    return gate.state["receivers"]


@app.get("/stakes")
def stakes():
    return {k[:16]: v for k, v in gate.state["stakes"].items()}


@app.get("/block")
def block():
    return gate.latest_block()


class Stake(BaseModel):
    issuer: str
    name: str
    amount: float
    sig: str


@app.post("/stake")
def stake(s: Stake):
    out = gate.stake(s.issuer, s.name, s.amount, s.sig)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error"))
    return out


class Slash(BaseModel):
    receipt_id: str
    breach: str


@app.post("/slash/{receiver}")
def slash(receiver: str, s: Slash):
    out = gate.slash(receiver, s.receipt_id, s.breach)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error"))
    return out


@app.post("/verify/{receiver}")
def verify(receiver: str, envelope: dict):
    return gate.verify(receiver, envelope)


class Revoke(BaseModel):
    target_id: str
    by: str
    sig: str
    reason: str = ""


@app.post("/revoke")
def revoke(r: Revoke):
    out = gate.revoke(r.target_id, r.by, r.sig, r.reason)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error"))
    return out


@app.get("/receipts")
def receipts(limit: int = 20):
    return gate.receipts(limit)


# ---------------- demo inbox ----------------
@app.post("/inbox/{receiver}")
def inbox_post(receiver: str, envelope: dict):
    req = envelope.get("request") or {}
    rid = req.get("id")
    if not rid:
        raise HTTPException(400, "envelope.request.id missing")
    d = _inbox()
    d["pending"].setdefault(receiver, []).append(rid)
    d["all"][rid] = {"receiver": receiver, "envelope": envelope, "ts": time.time()}
    _save_inbox(d)
    return {"ok": True, "request_id": rid}


@app.get("/inbox/{receiver}/next")
def inbox_next(receiver: str):
    d = _inbox()
    q = d["pending"].get(receiver, [])
    if not q:
        return {"empty": True}
    rid = q.pop(0)
    _save_inbox(d)
    return {"request_id": rid, **d["all"][rid]}


@app.get("/inbox/{receiver}/get/{rid}")
def inbox_get(receiver: str, rid: str):
    d = _inbox()
    item = d["all"].get(rid)
    if not item or item["receiver"] != receiver:
        raise HTTPException(404, "no such request")
    return item


class Reply(BaseModel):
    request_id: str
    text: str


@app.post("/inbox/{receiver}/reply")
def inbox_reply(receiver: str, r: Reply):
    d = _inbox()
    item = d["all"].get(r.request_id)
    if not item:
        raise HTTPException(404, "no such request")
    to = item["envelope"].get("request", {}).get("from")
    d["replies"].append({"request_id": r.request_id, "from": receiver, "to": to, "text": r.text, "ts": time.time()})
    _save_inbox(d)
    return {"ok": True}


@app.get("/replies/{agent_pub}")
def replies(agent_pub: str):
    d = _inbox()
    return [r for r in d["replies"] if r["to"] == agent_pub]
