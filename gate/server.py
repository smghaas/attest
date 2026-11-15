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

Public sandbox (only when ATTEST_SANDBOX=1; stake is free test credit, nothing real is at risk):
  GET  /info                         mode, faucet amount, limits
  POST /sandbox/faucet               {issuer, name, sig}  test stake for a new operator, once per key
  POST /register/receiver            {name, pubkey, min_stake, policy, sig}  create your own receiver
  (/stake is closed in sandbox mode, and the demo inbox is off)

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
import re
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import Gate, STATE_DIR

app = FastAPI(title="Attest", version="0.4.0")
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


def sandbox() -> bool:
    return os.environ.get("ATTEST_SANDBOX") == "1"


def demo_on() -> bool:
    return os.environ.get("ATTEST_DEMO") == "1"


def _num(var: str, default: float) -> float:
    return float(os.environ.get(var, default))


# Sandbox guards: body size, per-IP rate limits, no demo inbox, open CORS so browser agents can call.
_hits: dict = defaultdict(deque)
WRITE_PATHS = ("/sandbox/", "/register/", "/claims", "/revoke", "/ledger/records")


@app.middleware("http")
async def sandbox_guards(request: Request, call_next):
    if sandbox():
        path = request.url.path
        if path.startswith(("/inbox", "/replies", "/demo")) and not (demo_on() and path.startswith("/demo")):
            return JSONResponse({"detail": "not available on the public sandbox"}, status_code=404)
        if int(request.headers.get("content-length") or 0) > _num("ATTEST_MAX_BODY", 65536):
            return JSONResponse({"detail": "request too large"}, status_code=413)
        if request.method == "POST":
            ip = request.client.host if request.client else "?"
            if os.environ.get("ATTEST_TRUST_PROXY") == "1":
                ip = request.headers.get("x-forwarded-for", ip).split(",")[0].strip()
            kind = "write" if path.startswith(WRITE_PATHS) else "check"
            limit = _num("ATTEST_RATE_WRITE", 20) if kind == "write" else _num("ATTEST_RATE_CHECK", 120)
            q, now = _hits[(ip, kind)], time.time()
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= limit:
                return JSONResponse({"detail": f"rate limit: {int(limit)} {kind} requests per minute"},
                                    status_code=429, headers={"Retry-After": "60"})
            q.append(now)
    return await call_next(request)


from .ledger_api import router as ledger_router  # noqa: E402  (endpoints live in their own file)

app.include_router(ledger_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/info")
def info():
    return {"service": "attest", "version": app.version, "mode": "sandbox" if sandbox() else "private",
            "stake_is_real": False if sandbox() else None, "faucet_amount": _num("ATTEST_SANDBOX_STAKE", 10000),
            "demo_agents": demo_on(), "self_registration": sandbox(),
            "limits": {"write_per_min": _num("ATTEST_RATE_WRITE", 20), "check_per_min": _num("ATTEST_RATE_CHECK", 120)}}


@app.get("/stats")
def stats():
    rs = gate.receipts(1000)
    st, cl = gate.stakes(), gate.claims()
    return {"decisions": len(rs), "by_decision": dict(Counter(r["decision"] for r in rs)),
            "staked_total": sum(v["stake"] for v in st.values()), "issuers": len(st),
            "suspended": sum(1 for v in st.values() if v.get("suspended")), "sandbox": sandbox(),
            "claims": dict(Counter(c["status"] for c in cl)), "demo": demo_on()}


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
    if sandbox():
        raise HTTPException(403, "stake is free test credit on the sandbox: use POST /sandbox/faucet")
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


# ---------------- public sandbox: self-registration ----------------
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")


def _need_sandbox():
    if not sandbox():
        raise HTTPException(403, "self-registration is off on this gate (it is not a sandbox)")


class Faucet(BaseModel):
    issuer: str
    name: str
    sig: str


@app.post("/sandbox/faucet")
def faucet(f: Faucet):
    """An operator proves it holds its key, and gets free test stake once."""
    _need_sandbox()
    if not re.fullmatch(r"[0-9a-f]{64}", f.issuer):
        raise HTTPException(400, "issuer must be a 64 hex character Ed25519 public key")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,31}", f.name):
        raise HTTPException(400, "name: 2 to 32 letters, digits, space, dot, dash or underscore")
    from . import crypto
    if not crypto.verify(f.issuer, {"faucet": True, "issuer": f.issuer, "name": f.name}, f.sig):
        raise HTTPException(400, "bad_signature")
    if len(gate.stakes()) >= _num("ATTEST_MAX_ISSUERS", 500):
        raise HTTPException(503, "the sandbox is full")
    return _ok(gate.grant_stake(f.issuer, f.name, _num("ATTEST_SANDBOX_STAKE", 10000)))


class NewReceiver(BaseModel):
    name: str
    pubkey: str
    min_stake: int | float = 0             # kept as sent: the signature covers the number as written
    policy: dict = {}
    sig: str


@app.post("/register/receiver")
def register_receiver(r: NewReceiver):
    """A receiver proves it holds its key and creates itself. Names are first come, first served."""
    _need_sandbox()
    if not NAME_RE.match(r.name):
        raise HTTPException(400, "name: 3 to 32 chars, lowercase letters, digits and dashes")
    if not re.fullmatch(r"[0-9a-f]{64}", r.pubkey):
        raise HTTPException(400, "pubkey must be a 64 hex character Ed25519 public key")
    if not 0 <= r.min_stake <= 1e9:
        raise HTTPException(400, "min_stake out of range")
    esc, ro = r.policy.get("escalate_above", {}), r.policy.get("read_only", [])
    if set(r.policy) - {"escalate_above", "read_only"} or not isinstance(esc, dict) or not all(
            isinstance(k, str) and isinstance(v, (int, float)) and v >= 0 for k, v in esc.items()) or not (
            isinstance(ro, list) and len(ro) <= 50 and all(isinstance(a, str) and 0 < len(a) <= 64 for a in ro)):
        raise HTTPException(400, 'policy may only be {"escalate_above": {"action" or "*": amount}, '
                                 '"read_only": ["action", ...]}')
    from . import crypto
    body = {"register_receiver": r.name, "pubkey": r.pubkey, "min_stake": r.min_stake, "policy": r.policy}
    if not crypto.verify(r.pubkey, body, r.sig):
        raise HTTPException(400, "bad_signature")
    with gate.lock:
        known = gate.state["receivers"]
        if r.name in known:
            raise HTTPException(409, "that receiver name is taken")
        if len(known) >= _num("ATTEST_MAX_RECEIVERS", 500):
            raise HTTPException(503, "the sandbox is full")
        gate.register_receiver(r.name, r.min_stake, r.policy, r.pubkey)
    return {"ok": True, "receiver": r.name, "min_stake": r.min_stake}


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
    if not demo_on():
        raise HTTPException(404, "demo mode is off (start with ATTEST_DEMO=1)")
    return _demo_agents()


@app.post("/demo/try")
def demo_try(t: Try):
    if not demo_on():
        raise HTTPException(404, "demo mode is off (start with ATTEST_DEMO=1)")
    if t.agent not in _demo_agents():
        raise HTTPException(404, "no such demo agent")
    from attest.client import build_request
    me = json.loads((STATE_DIR / "agents" / f"{t.agent}.json").read_text())
    out = gate.verify(t.receiver, build_request(me, t.receiver, t.action, t.amount, "sent from the dashboard"))
    return {**out, "agent": t.agent, "agent_key": me["pub"][:16], "chain_depth": len(me["chain"])}


# ---------------- demo inbox ----------------
@app.post("/inbox/{receiver}", include_in_schema=False)
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


@app.get("/inbox/{receiver}/next", include_in_schema=False)
def inbox_next(receiver: str):
    with _inbox_lock:
        d = _inbox()
        q = d["pending"].get(receiver, [])
        if not q:
            return {"empty": True}
        rid = q.pop(0)
        _save_inbox(d)
    return {"request_id": rid, **d["all"][rid]}


@app.get("/inbox/{receiver}/get/{rid}", include_in_schema=False)
def inbox_get(receiver: str, rid: str):
    with _inbox_lock:
        item = _inbox()["all"].get(rid)
    if not item or item["receiver"] != receiver:
        raise HTTPException(404, "no such request")
    return item


class Reply(BaseModel):
    request_id: str
    text: str


@app.post("/inbox/{receiver}/reply", include_in_schema=False)
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


@app.get("/replies/{agent_pub}", include_in_schema=False)
def replies(agent_pub: str):
    with _inbox_lock:
        d = _inbox()
    return [r for r in d["replies"] if r["to"] == agent_pub]
