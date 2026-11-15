"""HTTP interface of Attest (FastAPI).

Gate endpoints (the product):
  GET  /pubkey                       the gate's signing key, so receipts can be checked
  POST /check                        {envelope, checker, require} -> a checklist the asker chose, signed
  POST /verify/{receiver}            body = envelope -> decision + signed receipt
                                     header X-Attest-Receiver-Sig (required on a sandbox): see verify()
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

from fastapi import FastAPI, Header, HTTPException, Request
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
WRITE_PATHS = ("/sandbox/", "/register/", "/claims", "/revoke", "/ledger/records")  # /check counts as a check


@app.middleware("http")
async def sandbox_guards(request: Request, call_next):
    if sandbox():
        path = request.url.path
        if path.startswith(("/inbox", "/replies", "/demo")) and not (demo_on() and path.startswith("/demo")):
            return JSONResponse({"detail": "not available on the public sandbox"}, status_code=404)
        if int(request.headers.get("content-length") or 0) > _num("ATTEST_MAX_BODY", 65536):
            return JSONResponse({"detail": "request too large"}, status_code=413)
        local = (request.client and request.client.host in ("127.0.0.1", "::1")
                 and "x-forwarded-for" not in request.headers)      # jobs on this machine, e.g. sim/market.py
        if request.method == "POST" and not local:
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
app.mount("/static", StaticFiles(directory=STATIC, html=True), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
def home():
    """Everything on one page: tabs for the live town, between people, checking an agent, NANDA Town."""
    return FileResponse(STATIC / "app.html")


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


def receiver_sig_required() -> bool:
    """On a public sandbox only the receiver may ask about requests sent to it: a check spends the
    agent's budget, so a stranger must not be able to run checks in a receiver's name."""
    return sandbox() or os.environ.get("ATTEST_REQUIRE_RECEIVER_SIG") == "1"


@app.post("/verify/{receiver}")
def verify(receiver: str, envelope: dict, x_attest_receiver_sig: str | None = Header(default=None)):
    """X-Attest-Receiver-Sig: the receiver's signature over {"verify": <request id>, "receiver": <name>}.
    The request id is single-use, so a signature cannot be replayed for another request."""
    with gate.lock:
        rcv = gate.state["receivers"].get(receiver)
    if rcv and rcv.get("pubkey") and (x_attest_receiver_sig or receiver_sig_required()):
        from . import crypto
        rid = ((envelope or {}).get("request") or {}).get("id")
        body = {"verify": rid, "receiver": receiver}
        if not (x_attest_receiver_sig and rid and crypto.verify(rcv["pubkey"], body, x_attest_receiver_sig)):
            raise HTTPException(401, f"only {receiver} can ask about requests sent to it: sign "
                                     '{"verify": <request id>, "receiver": <name>} and send it as X-Attest-Receiver-Sig')
    return gate.verify(receiver, envelope)


class Check(BaseModel):
    envelope: dict
    checker: str
    require: dict = {}


@app.post("/check")
def check(c: Check):
    """Anyone checks an agent for a task, choosing what must be true. Nothing is spent; the answer is a
    checklist signed by the gate. See gate/checklist.py for the requirements."""
    if not 0 < len(c.checker) <= 64:
        raise HTTPException(400, "checker: 1 to 64 characters (who is asking; the request must be addressed to it)")
    from . import checklist
    from .core import LOG_DIR
    from .ledger_api import enabled as ledger_on, service as ledger
    with gate.lock:
        out = checklist.run(gate, c.envelope, c.checker, c.require, ledger() if ledger_on() else None)
    with (LOG_DIR / "checks.jsonl").open("a") as f:
        f.write(json.dumps(out) + "\n")
    return out


_market: dict = {"proc": None}
BEHAVIOURS = ("honest", "shady", "splitter", "big", "revoked", "greedy", "impostor")


def _need_demo():
    if not demo_on():
        raise HTTPException(404, "demo mode is off (start the gate with ATTEST_DEMO=1)")


def _control(**kw):
    from .core import LOG_DIR
    f = LOG_DIR / "market_control.json"
    try:
        c = json.loads(f.read_text())
    except (OSError, ValueError):
        c = {}
    c.update(kw)
    f.write_text(json.dumps(c))
    return c


def _rounds_done() -> int:
    from .core import LOG_DIR
    f = LOG_DIR / "market.jsonl"
    done = 0
    if f.exists():
        for line in f.read_text().splitlines():
            if '"kind": "deal"' in line:
                done = max(done, json.loads(line).get("round", 0))
    return done


@app.get("/market/cast")
def market_cast():
    """The agents and shops the next market run will use (sim/default_cast.json until someone edits it)."""
    from .core import LOG_DIR, ROOT
    f = LOG_DIR / "market_cast.json"
    return json.loads((f if f.exists() else ROOT / "sim" / "default_cast.json").read_text())


@app.put("/market/cast")
def market_cast_save(cast: dict):
    """Demo mode: replace the cast. Agents: key, name, backer, backer_kind, money, actions, limit, amount,
    behaviour, about, parent. Shops: key, name, sells, min_stake, human_above, checks_record, free_actions."""
    _need_demo()
    from .core import LOG_DIR
    agents, shops = cast.get("agents") or [], cast.get("shops") or []
    if not (1 <= len(agents) <= 30 and 1 <= len(shops) <= 30):
        raise HTTPException(400, "between 1 and 30 agents, and between 1 and 30 shops")
    slug = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
    keys = set()
    for x in agents + shops:
        x["key"] = re.sub(r"[^a-z0-9-]+", "-", str(x.get("key") or x.get("name") or "").lower()).strip("-")[:31]
        if not slug.match(x["key"]) or x["key"] in keys:
            raise HTTPException(400, f"every agent and shop needs a different short name ({x.get('name')!r})")
        keys.add(x["key"])
        x["name"] = str(x.get("name") or x["key"])[:60]
    for a in agents:
        if a.get("behaviour", "honest") not in BEHAVIOURS:
            raise HTTPException(400, f"behaviour must be one of {', '.join(BEHAVIOURS)}")
        a["actions"] = [re.sub(r"[^a-z0-9_]+", "_", str(x).lower()).strip("_")[:40] for x in a.get("actions") or []][:12]
        a["actions"] = [x for x in a["actions"] if x] or ["request_quote"]
        a["limit"] = max(0.0, min(float(a.get("limit") or 0), 1e7))
        lo, hi = (list(a.get("amount") or [20, 400]) + [400])[:2]
        a["amount"] = [max(0.0, float(lo)), max(float(lo), float(hi))]
    for sh in shops:
        sh["min_stake"] = max(0.0, float(sh.get("min_stake") or 0))
        sh["human_above"] = float(sh["human_above"]) if sh.get("human_above") not in (None, "", 0) else None
        sh["free_actions"] = [re.sub(r"[^a-z0-9_]+", "_", str(x).lower()).strip("_")[:40]
                              for x in sh.get("free_actions") or []][:12]
    (LOG_DIR / "market_cast.json").write_text(json.dumps({"agents": agents, "shops": shops}, indent=1))
    return {"ok": True, "agents": len(agents), "shops": len(shops)}


@app.delete("/market/cast")
def market_cast_reset():
    _need_demo()
    from .core import LOG_DIR
    (LOG_DIR / "market_cast.json").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/market/start")
def market_start(request: Request, rounds: int = 120, pace: float = 3.0, mode: str = "play"):
    """Demo mode: start sim/market.py against this gate. A run already going is stopped first."""
    _need_demo()
    import subprocess
    import sys
    from .core import LOG_DIR, ROOT
    p = _market["proc"]
    if p is not None and p.poll() is None:
        _control(mode="stop")
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    (LOG_DIR / "market.jsonl").write_text("")               # the page reads the new run from the start
    _control(mode="step" if mode == "step" else "play", pace=max(0.3, min(pace, 10)), granted=0)
    run_id = str(int(time.time() * 1000))
    env = {**os.environ, "ATTEST_URL": str(request.base_url).rstrip("/"), "ATTEST_LOG_DIR": str(LOG_DIR),
           "ATTEST_MARKET_RUN": run_id}
    env.pop("ATTEST_DIR", None)
    _market["proc"] = subprocess.Popen([sys.executable, str(ROOT / "sim" / "market.py"),
                                        "--rounds", str(max(5, min(rounds, 400)))],
                                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "run": run_id}


class Control(BaseModel):
    mode: str                      # play, pause, step (one more transaction), stop
    pace: float | None = None


@app.post("/market/control")
def market_control(c: Control):
    _need_demo()
    if c.mode not in ("play", "pause", "step", "stop"):
        raise HTTPException(400, "mode: play, pause, step or stop")
    kw = {"mode": c.mode}
    if c.pace is not None:
        kw["pace"] = max(0.3, min(c.pace, 10))
    if c.mode == "step":
        kw["granted"] = _rounds_done() + 1
    if c.mode == "pause":
        kw["granted"] = _rounds_done()
    return _control(**kw)


@app.get("/card/{pub}")
def agent_card(pub: str):
    """An agent's card, for anyone: its passport (who vouches for it, what it may do), whether its backer has
    money down, what a stranger should conclude, and every deal on its record, each one in a ledger block."""
    from .ledger_api import enabled as ledger_on, service
    if not ledger_on():
        raise HTTPException(404, "this gate keeps no ledger")
    svc = service()
    node = svc.nodes[0]
    pp = node.state.passports.get(pub)
    names = {k: v["name"] for k, v in node.state.passports.items()}
    h = node.history(pub)
    with gate.lock:
        stake = gate.state["stakes"].get(pp["operator"]) if pp else None
    deals = []
    for e in h["entries"]:
        r = e["record"]
        other = r["buyer"] if r["provider"] == pub else r["provider"]
        deals.append({"block": e["height"], "request": r["request"], "status": r["status"], "amount": r["amount"],
                      "with": names.get(other, other[:12]), "role": "seller" if r["provider"] == pub else "buyer",
                      "ts": r.get("ts")})
    return {"agent": pub, "passport": {"name": pp["name"], "backer_key": pp["operator"], "actions": pp["actions"],
                                       "max_amount": pp["max_amount"], "puzzle_nonce": pp["nonce"],
                                       "self_vouched": pp["operator"] == pub} if pp else None,
            "backer_money_down": stake["stake"] if stake else 0, "backer_name": stake["name"] if stake else None,
            "backer_suspended": bool(stake and stake.get("suspended")),
            "summary": {k: h[k] for k in ("trades", "accepted", "disputed", "volume")},
            "verdict": svc.decision(pub), "deals": deals[::-1]}


@app.get("/market/deal/{n}")
def market_deal(n: int):
    """Everything about one deal from sim/market.py, with every signature and proof checked again now."""
    from . import crypto
    from .core import LOG_DIR
    f = LOG_DIR / "market" / f"{n}.json"
    if not f.exists():
        raise HTTPException(404, "no details for that event")
    d = json.loads(f.read_text())
    env = d.get("envelope") or {}
    req, cred, chain = env.get("request") or {}, env.get("credential"), env.get("chain") or []
    checks = []

    def add(what, ok, note=""):
        checks.append({"what": what, "ok": bool(ok), "note": note})

    if req:
        add("The agent signed the request", crypto.verify(req.get("from", ""), crypto.body_of(req), req.get("sig", "")),
            f"agent key {req.get('from', '')[:16]}…")
        add("The request has not been changed since (its id is the hash of its content)", crypto.id_ok(req))
    if cred:
        add(f"{cred.get('operator')} ({cred.get('backer_kind', 'company')}) signed the agent's ID",
            crypto.verify(cred.get("issuer", ""), crypto.body_of(cred), cred.get("sig", "")) and crypto.id_ok(cred),
            f"backer key {cred.get('issuer', '')[:16]}…")
        with gate.lock:
            revoked = cred.get("id") in gate.state["revoked"]
            stake = gate.state["stakes"].get(cred.get("issuer"))
        add("The ID is still valid today (not cancelled)", not revoked, "cancelled by its backer" if revoked else "")
        add("Its backer has money down at the gate", bool(stake and stake["stake"] > 0),
            f"${stake['stake']:,.0f}" if stake else "none")
    elif req:
        add("Someone signed an ID for this agent", False, "no ID was presented")
    for depth, m in enumerate(chain):
        ok = crypto.verify(m.get("issuer", ""), crypto.body_of(m), m.get("sig", "")) and crypto.id_ok(m)
        sc = m.get("scope", {})
        who = "its backer" if depth == 0 else "the agent above it"
        add(f"Permission {depth + 1}: signed by {who}: may {', '.join(sc.get('actions', []))} up to ${float(sc.get('max_amount', 0)):,.0f}", ok)
    if env.get("pow"):
        add("It solved the puzzle (proof of work)", True, f"answer {env['pow'].get('nonce')}, accepted by the gate")
    ans = d.get("gate_answer")
    if ans and ans.get("receipt"):
        r = ans["receipt"]
        add("Attest signed its answer, and the answer was not changed",
            r.get("gate") == gate.pubkey and crypto.id_ok(r) and crypto.verify(gate.pubkey, crypto.body_of(r), r.get("sig", "")),
            f"gate key {gate.pubkey[:16]}…")
        add("The answer is about this very request", r.get("request_id") == req.get("id"))
    ledger = None
    rid = d.get("ledger_record_id")
    if rid:
        from network import ledger as L
        from .ledger_api import enabled as ledger_on, service
        if ledger_on():
            node = service().nodes[0]
            for blk in node.chain.best_chain():
                ids = [x["id"] for x in blk["records"]]
                if rid in ids:
                    i, rec = ids.index(rid), blk["records"][ids.index(rid)]
                    proof_ok = L.verify_proof(rid, L.merkle_proof(ids, i), blk["header"]["root"])
                    ledger = {"block": blk["header"]["height"], "block_hash": L.block_hash(blk["header"]),
                              "final": node.chain.height(node.chain.finalized) >= blk["header"]["height"],
                              "record": rec}
                    add(f"The outcome ({rec['status']}) is signed by both the shop and the agent", L.check_outcome(rec))
                    add(f"It is in ledger block {blk['header']['height']} (Merkle proof checks out)", proof_ok)
                    add("That block is final (more than 2/3 of validators voted for it)", ledger["final"])
                    break
            else:
                add("The outcome is on the ledger", False, "not found")
    return {**d, "checks": checks, "ledger": ledger, "gate_pubkey": gate.pubkey}


@app.get("/market/events")
def market_events(after: int = 0):
    """Events from sim/market.py (a market that runs by itself), for the page at /static/market/."""
    from .core import LOG_DIR
    f = LOG_DIR / "market.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            if e["n"] > after:
                out.append(e)
    return out[:400]


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
