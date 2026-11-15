"""Python SDK for the Attest gate. Needs only httpx and the gate's own crypto helpers.

    from attest import Attest, build_request
    a = Attest("http://127.0.0.1:8402")
    d = a.check("supplierco", envelope)
    if d.allowed and d.receipt_verified: ...
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

import httpx

from gate import crypto

DEFAULT_URL = "http://127.0.0.1:8402"


@dataclass
class Decision:
    decision: str                     # allow | refuse | escalate | unknown
    reason: str
    detail: str
    receipt: dict
    receipt_verified: bool            # the receipt's signature checks out against the gate's public key
    challenge: dict | None = None     # a hashcash puzzle, when an unstaked agent must solve one
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def build_request(agent: dict, to: str, action: str, amount: float, message: str = "") -> dict:
    """Sign a request as `agent` (a dict with priv, pub, credential and chain) and wrap it in
    the envelope the gate expects: credential, mandate chain, signed request."""
    req = crypto.signed(agent["priv"], {
        "type": "kya.request/1", "from": agent["pub"], "to": to, "action": action,
        "amount": float(amount), "message": message, "ts": time.time(), "nonce": uuid.uuid4().hex})
    return {"credential": agent["credential"], "chain": agent["chain"], "request": req}


def do_work(envelope: dict, bits: int) -> dict:
    """Proof of work on this request: find a nonce so sha256(request id + ':' + nonce) starts with `bits`
    zero bits. A checker may accept this instead of money put down. Returns the envelope with `work` added."""
    import hashlib
    rid, n = envelope["request"]["id"], 0
    while int.from_bytes(hashlib.sha256(f"{rid}:{n}".encode()).digest(), "big") >> (256 - bits):
        n += 1
    return {**envelope, "work": {"nonce": n, "bits": bits}}


class Attest:
    def __init__(self, url: str | None = None, timeout: float = 10):
        self.url = (url or os.environ.get("ATTEST_URL") or DEFAULT_URL).rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout)
        self._gate_key: str | None = None

    # -- reading ---------------------------------------------------------------------------
    def _get(self, path: str, **params):
        r = self.http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def pubkey(self) -> str:
        if self._gate_key is None:
            self._gate_key = self._get("/pubkey")["gate"]
        return self._gate_key

    def receivers(self) -> dict:
        return self._get("/receivers")

    def stakes(self) -> dict:
        return self._get("/stakes")

    def receipts(self, limit: int = 20) -> list:
        return self._get("/receipts", limit=limit)

    def claims(self) -> list:
        return self._get("/claims")

    def stats(self) -> dict:
        return self._get("/stats")

    # -- the check -------------------------------------------------------------------------
    def verify_receipt(self, receipt: dict) -> bool:
        """True when the receipt was signed by this gate and has not been altered."""
        gate = self.pubkey()
        return bool(receipt.get("gate") == gate and crypto.id_ok(receipt)
                    and crypto.verify(gate, crypto.body_of(receipt), receipt.get("sig", "")))

    def check(self, receiver: str, envelope: dict, receiver_key: dict | None = None) -> Decision:
        """receiver_key: your receiver identity ({name, priv, pub}). A sandbox gate requires it: only the
        receiver may ask about requests sent to it."""
        headers = {}
        if receiver_key:
            rid = (envelope.get("request") or {}).get("id")
            headers["X-Attest-Receiver-Sig"] = crypto.sign(receiver_key["priv"], {"verify": rid, "receiver": receiver})
        r = self.http.post(f"/verify/{receiver}", json=envelope, headers=headers)
        _raise(r)
        out = r.json()
        rec = out["receipt"]
        return Decision(out["decision"], out["reason"], out.get("detail", ""), rec,
                        self.verify_receipt(rec), out.get("challenge"), out)

    def check_task(self, envelope: dict, checker: str, **require) -> dict:
        """Check an agent for one task, choosing what must be true (see gate/checklist.py):
        backer, allowed, min_stake, work_bits, either, min_deals, max_disputed, human_above.
        The envelope's request must be addressed to `checker`. Returns the signed checklist."""
        r = self.http.post("/check", json={"envelope": envelope, "checker": checker, "require": require})
        _raise(r)
        out = r.json()
        out["verified_signature"] = self.verify_receipt(out)
        return out

    # -- writing (each call is signed with the caller's own key) -----------------------------
    def stake(self, operator: dict, amount: float) -> dict:
        body = {"stake": True, "issuer": operator["pub"], "name": operator["name"], "amount": float(amount)}
        r = self.http.post("/stake", json={"issuer": operator["pub"], "name": operator["name"],
                                           "amount": float(amount), "sig": crypto.sign(operator["priv"], body)})
        r.raise_for_status()
        return r.json()

    def file_claim(self, receiver: dict, receipt: dict, breach: str) -> dict:
        body = {"claim": receipt["id"], "receiver": receipt["receiver"], "breach": breach}
        r = self.http.post("/claims", json={"receipt": receipt, "breach": breach,
                                            "sig": crypto.sign(receiver["priv"], body)})
        r.raise_for_status()
        return r.json()

    # -- sandbox onboarding (the gate must run with ATTEST_SANDBOX=1) --------------------------
    def info(self) -> dict:
        return self._get("/info")

    def faucet(self, operator: dict) -> dict:
        """Free test stake for a new operator, once per key."""
        sig = crypto.sign(operator["priv"], {"faucet": True, "issuer": operator["pub"], "name": operator["name"]})
        r = self.http.post("/sandbox/faucet", json={"issuer": operator["pub"], "name": operator["name"], "sig": sig})
        _raise(r)
        return r.json()

    def register_receiver(self, receiver: dict, min_stake: float = 0, escalate_above: dict | None = None,
                          read_only: list | None = None) -> dict:
        """escalate_above: {"*": 8000} asks a human above 8,000 for any action. read_only: actions that move
        no value at this receiver; every other action counts as spending."""
        policy = {"escalate_above": escalate_above} if escalate_above else {}
        if read_only:
            policy["read_only"] = list(read_only)
        body = {"register_receiver": receiver["name"], "pubkey": receiver["pub"],
                "min_stake": float(min_stake), "policy": policy}
        r = self.http.post("/register/receiver", json={**{k: v for k, v in body.items() if k != "register_receiver"},
                                                         "name": receiver["name"], "sig": crypto.sign(receiver["priv"], body)})
        _raise(r)
        return r.json()

    # -- the ledger: passports and agent history (the gate must run with ATTEST_LEDGER=1) ------------
    def ledger_info(self) -> dict:
        return self._get("/ledger/info")

    def ledger_agents(self) -> list:
        return self._get("/ledger/agents")

    def history(self, agent_pub: str) -> dict:
        return self._get(f"/ledger/agents/{agent_pub}/history")

    def ledger_decision(self, agent_pub: str) -> dict:
        return self._get(f"/ledger/agents/{agent_pub}/decision")

    def ledger_blocks(self, limit: int = 10) -> list:
        return self._get("/ledger/blocks", limit=limit)

    def submit_record(self, record: dict) -> dict:
        r = self.http.post("/ledger/records", json=record)
        _raise(r)
        return r.json()

    def register_passport(self, operator: dict, agent: dict) -> dict:
        """The operator and the agent both sign; registering costs a small proof-of-work puzzle."""
        from network import ledger as L
        scope = agent["chain"][0]["scope"]
        return self.submit_record(L.make_passport(operator["priv"], operator["pub"], agent["priv"], agent["pub"],
                                                  agent["name"], scope["actions"], scope["max_amount"]))

    def record_trade(self, provider: dict, buyer: dict, request: str, amount: float, status: str = "accepted") -> dict:
        """Both parties sign the outcome. Here both identities are on this machine; across machines the
        provider sends network.ledger.propose_outcome(...) to the buyer, who calls countersign(...)."""
        from network import ledger as L
        return self.submit_record(L.make_outcome(provider["priv"], provider["pub"], buyer["priv"], buyer["pub"],
                                                 request, amount, status))


def _raise(r: httpx.Response):
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail") or r.text
        except ValueError:
            msg = r.text
        raise httpx.HTTPStatusError(str(msg), request=r.request, response=r)
