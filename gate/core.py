"""Attest core: the verify logic, issuer stakes, proof of work, revocation, receipts.

Documents (all JSON, all signed with Ed25519, see crypto.py):

  credential  who deployed this agent. Signed by the operator (issuer).
              {type, agent (pubkey), operator (name), issuer (operator pubkey),
               purpose, issued_at, expires_at, id, sig}
  mandate     what the agent may do. Root mandate signed by the operator;
              a delegated mandate is signed by the delegating agent and names
              its parent. A child may only narrow its parent (attenuation).
              {type, subject (agent pubkey), issuer (signer pubkey), parent (id|null),
               scope {actions [..], max_amount, currency}, expires_at, id, sig}
  request     what the agent wants to do now. Signed by the agent itself,
              which proves it holds the key the credential and mandate name.
              {type, from (pubkey), to (receiver name), action, amount, message,
               ts, nonce, id, sig}
  envelope    {credential, chain [root..leaf mandates], request, pow?}

Trust comes from stake, not from a list the receiver keeps by hand:
  - an operator stakes to have its credentials accepted (stake table below; on chain
    this is one small contract, here it is a table in the gate's state)
  - each receiver sets a stake threshold; an issuer below it, or suspended after a
    slash, counts as unstaked
  - a receiver (signing with its own key) can file a slashing claim backed by an allow
    receipt the gate signed for it. The claim waits out a dispute window. If the issuer
    does not dispute it, it is upheld when the window closes: the issuer loses part of
    its stake and is suspended. A disputed claim goes to the arbiter, who upholds or
    rejects it. One claim per receipt.
  - an agent with no staked issuer gets a hashcash puzzle seeded by the latest Base
    Sepolia block hash and bound to this request and receiver. Difficulty rises by one
    bit per retry from the same key within an hour, and by one bit for every doubling
    of the unstaked load on that receiver, so switching to fresh keys does not escape
    it during a flood. It is answered as unknown, read-only actions only. Anything that
    moves money from an unstaked agent is refused.
  - a credential or mandate can only be revoked by the key that signed it (or the
    gate's own key); the revoker presents the document so the gate can check that.

verify(receiver, envelope) returns a decision and a receipt signed by the gate:
  allow     everything checks out and the action is inside the leaf scope
  refuse    a signature, expiry, revocation, attenuation, scope or spend check failed,
            or an unstaked agent tried to move money
  escalate  checks pass but the receiver's own policy wants a human
  unknown   no staked issuer behind the agent: read-only request, puzzle required
            (pow_required) or solved (pow_ok)

All public methods of Gate take one lock, so concurrent HTTP requests (FastAPI runs
sync endpoints in a thread pool) cannot race on the spend totals, nonces or stakes.
The state lives in one JSON file owned by one process: run the server with a single
worker.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from . import crypto

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("ATTEST_STATE_DIR", ROOT / "state"))
LOG_DIR = Path(os.environ.get("ATTEST_LOG_DIR", ROOT / "logs"))

READ_ONLY = {"request_quote"}           # actions that move no money
MONEY = {"place_order"}

POW_BASE_BITS = 16                      # about 65,000 hashes on average
POW_MAX_BITS = 30
POW_KEY_WINDOW_S = 3600                 # retries from one key add +1 bit (x2 work) each, for an hour
POW_GLOBAL_WINDOW_S = 600               # puzzles issued per receiver in the last 10 minutes...
POW_GLOBAL_FREE = 32                    # ...above this many, each doubling of that load adds +1 bit
POW_TTL_S = 600                         # an issued puzzle must be solved within 10 minutes
POW_MAX_OPEN = 5000                     # open puzzles kept; the oldest is dropped beyond this
POW_MAX_KEYS = 50000                    # keys whose retries are tracked; the oldest is dropped beyond this

REQUEST_SKEW_S = 600                    # a request timestamp may be this far off the gate's clock
NONCE_TTL_S = 2 * REQUEST_SKEW_S        # after this a replayed request is refused as stale anyway
PRUNE_EVERY_S = 60

SLASH_FRACTION = 0.5                    # base penalty; the issuer is also suspended
DISPUTE_WINDOW_S = 72 * 3600            # default; setup_demo.py shortens it for the demo

CHAIN_RPC = "https://sepolia.base.org"  # Base Sepolia testnet, read only
BLOCK_CACHE_S = 60


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _err(code: str) -> dict:
    return {"ok": False, "error": code}


class Gate:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.lock = threading.RLock()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "gate_state.json"
        self.receipts_file = LOG_DIR / "receipts.jsonl"
        self.state = self._load()
        self._block = None  # (fetched_at, number, hash, source)
        self._last_prune = 0.0

    # ---------- state ----------
    def _load(self) -> dict:
        if self.state_file.exists():
            st = json.loads(self.state_file.read_text())
        else:
            priv, pub = crypto.keygen()
            st = {"gate_key": {"priv": priv, "pub": pub}}
        defaults = {
            # receiver name -> {"min_stake": float, "policy": {...}, "pubkey": hex|None}
            "receivers": {},
            # issuer pubkey -> {"name", "stake", "suspended", "slashes": [..]}
            "stakes": {},
            "revoked": {},          # doc id -> {"by", "ts", "reason"}
            "nonces_seen": {},      # nonce -> gate time it was first seen
            "spent": {},            # mandate id -> money already allowed under it
            "pow_attempts": {},     # agent pubkey -> [times a puzzle was issued to it]
            "pow_recent": {},       # receiver -> {minute: puzzles issued}
            "pow_open": {},         # challenge id -> {seed, bits, request_id, receiver, from, block, issued_at}
            "claims": {},           # receipt id -> slashing claim
            "settings": {"dispute_window_s": DISPUTE_WINDOW_S, "arbiter": None},
        }
        for k, v in defaults.items():
            st.setdefault(k, v)
        # states written before retries were time-stamped kept a plain counter
        st["pow_attempts"] = {k: v for k, v in st["pow_attempts"].items() if isinstance(v, list)}
        self._write(st)
        return st

    def _write(self, st: dict):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        os.replace(tmp, self.state_file)    # never leave a half-written state file behind

    def _save(self):
        self._write(self.state)

    @property
    def pubkey(self) -> str:
        return self.state["gate_key"]["pub"]

    # ---------- configuration ----------
    @_locked
    def register_receiver(self, name: str, min_stake: float, policy: dict | None = None,
                          pubkey: str | None = None):
        """pubkey is the receiver's own signing key; it is needed to file slashing claims."""
        self.state["receivers"][name] = {"min_stake": float(min_stake), "policy": policy or {},
                                         "pubkey": pubkey}
        self._save()

    @_locked
    def configure(self, dispute_window_s: float | None = None, arbiter: str | None = None):
        """arbiter is the public key that rules on disputed slashing claims."""
        if dispute_window_s is not None:
            self.state["settings"]["dispute_window_s"] = float(dispute_window_s)
        if arbiter is not None:
            self.state["settings"]["arbiter"] = arbiter
        self._save()

    @_locked
    def stake(self, issuer_pub: str, name: str, amount: float, sig: str) -> dict:
        """An operator adds stake for its issuer key. The operator signs {stake, issuer, name, amount}."""
        body = {"stake": True, "issuer": issuer_pub, "name": name, "amount": float(amount)}
        if float(amount) <= 0:
            return _err("amount_must_be_positive")
        if not crypto.verify(issuer_pub, body, sig):
            return _err("bad_signature")
        s = self.state["stakes"].setdefault(issuer_pub, {"name": name, "stake": 0.0, "suspended": False, "slashes": []})
        s["stake"] += float(amount)
        self._save()
        return {"ok": True, "issuer": name, "stake": s["stake"]}

    @_locked
    def stakes(self) -> dict:
        self._settle(time.time())
        return json.loads(json.dumps(self.state["stakes"]))

    # ---------- slashing claims ----------
    @_locked
    def file_claim(self, receipt: dict, breach: str, sig: str) -> dict:
        """A receiver claims a breach, backed by an allow receipt the gate signed for it.
        The receiver signs {claim: receipt id, receiver, breach} with the key it registered.
        The claim then waits out the dispute window (see dispute, resolve, _settle)."""
        receipt = receipt or {}
        if not (receipt.get("gate") == self.pubkey and crypto.id_ok(receipt)
                and crypto.verify(self.pubkey, crypto.body_of(receipt), receipt.get("sig", ""))):
            return _err("receipt_not_signed_by_this_gate")
        receiver = receipt.get("receiver")
        rcv = self.state["receivers"].get(receiver)
        if not rcv or not rcv.get("pubkey"):
            return _err("receiver_has_no_registered_key")
        body = {"claim": receipt["id"], "receiver": receiver, "breach": breach}
        if not crypto.verify(rcv["pubkey"], body, sig):
            return _err("bad_signature")
        if receipt.get("decision") != "allow":
            return _err("receipt_not_an_allow")
        if receipt["id"] in self.state["claims"]:
            return _err("receipt_already_claimed")
        issuer = receipt.get("issuer")
        s = self.state["stakes"].get(issuer)
        if not s or s["stake"] <= 0:
            return _err("issuer_has_no_stake")
        now = time.time()
        claim = {"id": receipt["id"], "receiver": receiver, "issuer": issuer, "issuer_name": s["name"],
                 "breach": breach, "filed_at": now,
                 "deadline": now + self.state["settings"]["dispute_window_s"], "status": "pending"}
        self.state["claims"][claim["id"]] = claim
        self._save()
        return {"ok": True, **claim}

    @_locked
    def dispute(self, claim_id: str, reason: str, sig: str) -> dict:
        """The accused issuer contests a pending claim before its deadline, signing
        {dispute: claim id, reason}. The claim then waits for the arbiter."""
        now = time.time()
        self._settle(now)
        c = self.state["claims"].get(claim_id)
        if not c:
            return _err("no_such_claim")
        if c["status"] != "pending":
            return _err(f"claim_is_{c['status']}")
        if not crypto.verify(c["issuer"], {"dispute": claim_id, "reason": reason}, sig):
            return _err("bad_signature")
        c.update(status="disputed", disputed_at=now, dispute_reason=reason)
        self._save()
        return {"ok": True, **c}

    @_locked
    def resolve(self, claim_id: str, uphold: bool, sig: str) -> dict:
        """The arbiter rules on a disputed claim, signing {resolve: claim id, uphold}."""
        arbiter = self.state["settings"].get("arbiter")
        if not arbiter:
            return _err("no_arbiter_configured")
        c = self.state["claims"].get(claim_id)
        if not c:
            return _err("no_such_claim")
        if c["status"] != "disputed":
            return _err(f"claim_is_{c['status']}")
        if not crypto.verify(arbiter, {"resolve": claim_id, "uphold": bool(uphold)}, sig):
            return _err("bad_signature")
        now = time.time()
        if uphold:
            self._apply_slash(c, now, "upheld by the arbiter")
        else:
            c.update(status="rejected", resolved_at=now)
        self._save()
        return {"ok": True, **c}

    @_locked
    def claims(self) -> list[dict]:
        self._settle(time.time())
        return json.loads(json.dumps(list(self.state["claims"].values())))

    def _apply_slash(self, c: dict, now: float, how: str):
        s = self.state["stakes"].get(c["issuer"])
        penalty = round(s["stake"] * SLASH_FRACTION, 2) if s else 0.0
        if s:
            s["stake"] -= penalty
            s["suspended"] = True
            s["slashes"].append({"claim": c["id"], "receiver": c["receiver"], "breach": c["breach"],
                                 "penalty": penalty, "ts": now})
        c.update(status="upheld", resolved_at=now, penalty=penalty, how=how)

    def _settle(self, now: float):
        """Uphold every pending claim whose dispute window has closed."""
        changed = False
        for c in self.state["claims"].values():
            if c["status"] == "pending" and c["deadline"] <= now:
                self._apply_slash(c, now, "not disputed within the window")
                changed = True
        if changed:
            self._save()

    # ---------- revocation ----------
    @_locked
    def revoke(self, document: dict, by_pub: str, sig: str, reason: str = "") -> dict:
        """Revoke a credential or mandate. The caller presents the document itself so the
        gate can check that the revoker is the key that signed it: the operator for a
        credential or root mandate, the delegating agent for a delegated mandate. The
        gate's own key may revoke anything. A mandate is revoked implicitly when any
        ancestor is revoked (cascade), which verify() enforces by walking the chain."""
        document = document or {}
        if document.get("type") not in ("kya.credential/1", "kya.mandate/1"):
            return _err("not_a_credential_or_mandate")
        if not (crypto.id_ok(document)
                and crypto.verify(document.get("issuer", ""), crypto.body_of(document), document.get("sig", ""))):
            return _err("bad_document")
        target_id = document["id"]
        body = {"revoke": target_id, "by": by_pub, "reason": reason}
        if not crypto.verify(by_pub, body, sig):
            return _err("bad_signature")
        if by_pub not in (document["issuer"], self.pubkey):
            return _err("revoker_is_not_the_issuer")
        self.state["revoked"][target_id] = {"by": by_pub, "ts": time.time(), "reason": reason}
        self._save()
        return {"ok": True, "revoked": target_id}

    # ---------- chain ----------
    def latest_block(self) -> dict:
        """Latest Base Sepolia block, cached 60 s. Falls back to a local value if the RPC is
        unreachable, and says so (the receipt carries the source)."""
        now = time.time()
        if self._block and now - self._block[0] < BLOCK_CACHE_S:
            _, num, h, src = self._block
            return {"number": num, "hash": h, "source": src}
        try:
            r = httpx.post(CHAIN_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                                            "params": ["latest", False]}, timeout=4).json()["result"]
            num, h, src = int(r["number"], 16), r["hash"], "base-sepolia"
        except Exception:
            num, h, src = 0, "0x" + hashlib.sha256(str(int(now // BLOCK_CACHE_S)).encode()).hexdigest(), "local-fallback"
        self._block = (now, num, h, src)
        return {"number": num, "hash": h, "source": src}

    # ---------- housekeeping ----------
    def _prune(self, now: float):
        """Drop state that can no longer matter, so it does not grow without bound."""
        if now - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = now
        st = self.state
        st["nonces_seen"] = {n: t for n, t in st["nonces_seen"].items() if now - t < NONCE_TTL_S}
        st["pow_open"] = {k: v for k, v in st["pow_open"].items() if now - v.get("issued_at", 0) < POW_TTL_S}
        st["pow_attempts"] = {k: kept for k, v in st["pow_attempts"].items()
                              if (kept := [t for t in v if now - t < POW_KEY_WINDOW_S])}
        for rcv, buckets in st["pow_recent"].items():
            st["pow_recent"][rcv] = {m: c for m, c in buckets.items() if now - int(m) * 60 < POW_GLOBAL_WINDOW_S}
        self._save()

    # ---------- verify ----------
    @_locked
    def verify(self, receiver: str, envelope: dict) -> dict:
        now = time.time()
        self._settle(now)
        self._prune(now)
        extra: dict = {}
        decision, reason, detail = self._decide(receiver, envelope or {}, extra)
        req = (envelope or {}).get("request") or {}
        chain = (envelope or {}).get("chain") or []
        cred = (envelope or {}).get("credential") or {}
        receipt_body = {
            "type": "attest.receipt/1",
            "receiver": receiver,
            "request_id": req.get("id"),
            "from": req.get("from"),
            "action": req.get("action"),
            "amount": req.get("amount"),
            "decision": decision,
            "reason": reason,
            "detail": detail,
            "credential_id": cred.get("id"),
            "issuer": cred.get("issuer"),
            "chain_ids": [m.get("id") for m in chain],
            "ts": time.time(),
            "gate": self.pubkey,
            **{k: v for k, v in extra.items() if k != "challenge"},
        }
        receipt = crypto.signed(self.state["gate_key"]["priv"], receipt_body)
        with self.receipts_file.open("a") as f:
            f.write(json.dumps(receipt) + "\n")
        out = {"decision": decision, "reason": reason, "detail": detail, "receipt": receipt}
        if "challenge" in extra:
            out["challenge"] = extra["challenge"]
        return out

    def _issuer_status(self, rcv: dict, cred: dict | None) -> tuple[bool, str]:
        """(staked enough for this receiver, explanation)."""
        if not cred:
            return False, "no credential presented"
        s = self.state["stakes"].get(cred.get("issuer"))
        if not s:
            return False, f"issuer {cred.get('operator', '?')} has no stake"
        if s["suspended"]:
            return False, f"issuer {s['name']} is suspended after a slash (stake left {s['stake']:,.0f})"
        if s["stake"] < rcv["min_stake"]:
            return False, f"issuer {s['name']} stake {s['stake']:,.0f} is below this receiver's threshold {rcv['min_stake']:,.0f}"
        return True, f"issuer {s['name']} staked {s['stake']:,.0f} (threshold {rcv['min_stake']:,.0f})"

    def _check_request_sig(self, receiver: str, req: dict, now: float) -> tuple[str, str] | None:
        if not crypto.verify(req.get("from", ""), crypto.body_of(req), req.get("sig", "")):
            return "bad_request_signature", "request signature does not verify"
        if req.get("to") != receiver:
            return "wrong_receiver", "request is addressed to a different receiver"
        try:
            ts = float(req.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0.0
        if abs(now - ts) > REQUEST_SKEW_S:
            return "stale_request", f"request timestamp is more than {REQUEST_SKEW_S // 60} minutes off"
        if req.get("nonce") in self.state["nonces_seen"]:
            return "replay", "this request was already presented"
        return None

    def _pow_bits(self, receiver: str, pub: str, now: float) -> int:
        tries = [t for t in self.state["pow_attempts"].get(pub, []) if now - t < POW_KEY_WINDOW_S]
        buckets = self.state["pow_recent"].get(receiver, {})
        recent = sum(c for m, c in buckets.items() if now - int(m) * 60 < POW_GLOBAL_WINDOW_S)
        load = int(math.log2(1 + recent / POW_GLOBAL_FREE))
        return min(POW_MAX_BITS, POW_BASE_BITS + len(tries) + load)

    def _record_puzzle(self, receiver: str, pub: str, now: float):
        att = self.state["pow_attempts"]
        tries = [t for t in att.pop(pub, []) if now - t < POW_KEY_WINDOW_S] + [now]
        att[pub] = tries[-(POW_MAX_BITS - POW_BASE_BITS + 1):]   # re-inserted, so oldest keys come first
        while len(att) > POW_MAX_KEYS:
            del att[next(iter(att))]
        buckets = self.state["pow_recent"].setdefault(receiver, {})
        minute = str(int(now // 60))
        buckets[minute] = buckets.get(minute, 0) + 1

    def _unstaked(self, receiver: str, env: dict, req: dict, why: str, now: float, extra: dict) -> tuple[str, str, str]:
        """No staked issuer behind this agent: refuse money, puzzle for read-only."""
        bad = self._check_request_sig(receiver, req, now)
        if bad:
            return "refuse", *bad
        action = req.get("action")
        if action not in READ_ONLY:
            self.state["nonces_seen"][req["nonce"]] = now
            self._save()
            return "refuse", "unstaked_money_action", f"{why}; {action} moves money, so it is refused"
        sol = env.get("pow")
        if sol:
            ch = self.state["pow_open"].get(sol.get("challenge_id", ""))
            if not ch or ch["request_id"] != req.get("id") or ch["receiver"] != receiver:
                return "refuse", "bad_pow", "proof of work does not match an open challenge for this request"
            if now - ch.get("issued_at", 0) > POW_TTL_S:
                del self.state["pow_open"][sol["challenge_id"]]
                self._save()
                return "refuse", "bad_pow", f"puzzle expired (must be solved within {POW_TTL_S // 60} minutes)"
            digest = hashlib.sha256(f"{ch['seed']}:{sol.get('nonce')}".encode()).digest()
            if int.from_bytes(digest, "big") >> (256 - ch["bits"]) != 0:
                return "refuse", "bad_pow", f"solution does not have {ch['bits']} leading zero bits"
            del self.state["pow_open"][sol["challenge_id"]]
            self.state["nonces_seen"][req["nonce"]] = now
            self._save()
            extra.update({"pow_bits": ch["bits"], "block": ch["block"]})
            return "unknown", "pow_ok", (f"{why}; puzzle of {ch['bits']} bits solved, answer as to the public "
                                         f"(read-only, nothing that moves money)")
        # issue a puzzle bound to the latest block, this request and this receiver
        blk = self.latest_block()
        bits = self._pow_bits(receiver, req["from"], now)
        seed = hashlib.sha256(f"{blk['hash']}:{req['id']}:{receiver}".encode()).hexdigest()
        cid = seed[:16]
        self._record_puzzle(receiver, req["from"], now)
        opened = self.state["pow_open"]
        opened.pop(cid, None)
        opened[cid] = {"seed": seed, "bits": bits, "request_id": req["id"], "receiver": receiver,
                       "from": req["from"], "block": blk, "issued_at": now}
        while len(opened) > POW_MAX_OPEN:
            del opened[next(iter(opened))]
        self._save()
        extra["challenge"] = {"challenge_id": cid, "seed": seed, "bits": bits, "block": blk,
                              "rule": "find nonce so that sha256(seed + ':' + nonce) starts with `bits` zero bits"}
        extra.update({"pow_bits": bits, "block": blk})
        return "unknown", "pow_required", (f"{why}; solve a {bits}-bit puzzle seeded by Base Sepolia block "
                                           f"{blk['number']} ({blk['source']}) and resend")

    def _decide(self, receiver: str, env: dict, extra: dict) -> tuple[str, str, str]:
        rcv = self.state["receivers"].get(receiver)
        if rcv is None:
            return "refuse", "unknown_receiver", f"no receiver named {receiver} is registered"
        cred = env.get("credential")
        chain = env.get("chain") or []
        req = env.get("request")
        if not req:
            return "refuse", "no_request", "envelope has no request"
        now = time.time()

        # 1. is a staked issuer behind this agent?
        staked, why = self._issuer_status(rcv, cred)
        if not staked:
            return self._unstaked(receiver, env, req, why, now, extra)
        if not crypto.verify(cred["issuer"], crypto.body_of(cred), cred.get("sig", "")):
            return "refuse", "bad_credential_signature", "credential signature does not verify"
        if not crypto.id_ok(cred):
            return "refuse", "bad_credential_id", "credential id does not match its body"
        if cred.get("expires_at", 0) < now:
            return "refuse", "credential_expired", "credential expired"
        if cred["id"] in self.state["revoked"]:
            return "refuse", "credential_revoked", "credential has been revoked by its issuer"

        # 2. mandate chain: root signed by the operator, each child narrows its parent
        if not chain:
            return "refuse", "no_mandate", "no mandate presented"
        root = chain[0]
        if root.get("issuer") != cred["issuer"] or root.get("parent") is not None:
            return "refuse", "bad_root_mandate", "root mandate must be issued by the credential's operator"
        if root.get("subject") != cred["agent"]:
            return "refuse", "root_subject_mismatch", "root mandate is for a different agent than the credential"
        prev = None
        for depth, m in enumerate(chain):
            if not crypto.verify(m.get("issuer", ""), crypto.body_of(m), m.get("sig", "")):
                return "refuse", "bad_mandate_signature", f"mandate at depth {depth} has a bad signature"
            if not crypto.id_ok(m):
                return "refuse", "bad_mandate_id", f"mandate at depth {depth} id does not match its body"
            if m.get("expires_at", 0) < now:
                return "refuse", "mandate_expired", f"mandate at depth {depth} expired"
            if m["id"] in self.state["revoked"]:
                return "refuse", "mandate_revoked", f"mandate at depth {depth} was revoked (everything below it is revoked too)"
            if prev is not None:
                if m.get("parent") != prev["id"]:
                    return "refuse", "broken_chain", f"mandate at depth {depth} does not name the previous mandate as its parent"
                if m.get("issuer") != prev["subject"]:
                    return "refuse", "wrong_delegator", f"mandate at depth {depth} was not signed by the holder of its parent"
                ok, whyn = self._attenuates(m["scope"], prev["scope"], m.get("expires_at", 0), prev.get("expires_at", 0))
                if not ok:
                    return "refuse", "attenuation_violation", f"delegated mandate exceeds its parent: {whyn}"
            prev = m
        leaf = prev

        # 3. the request is signed by the leaf subject
        if req.get("from") != leaf["subject"]:
            return "refuse", "request_from_mismatch", "request is not from the holder of the leaf mandate"
        bad = self._check_request_sig(receiver, req, now)
        if bad:
            return "refuse", *bad
        self.state["nonces_seen"][req["nonce"]] = now
        self._save()

        # 4. action inside the leaf scope, and running spend inside every mandate of the chain
        scope = leaf["scope"]
        if req.get("action") not in scope.get("actions", []):
            return "refuse", "out_of_scope", f"action {req.get('action')} is not in the mandate (allowed: {scope.get('actions')})"
        try:
            amt = float(req.get("amount") or 0)
        except (TypeError, ValueError):
            amt = math.nan
        if not math.isfinite(amt) or amt < 0:
            # a negative or NaN amount would lower or poison the running spend totals
            return "refuse", "bad_amount", "amount must be a finite number, not negative"
        if amt > float(scope.get("max_amount", 0)):
            return "refuse", "over_limit", f"amount {amt:,.0f} exceeds mandate limit {scope.get('max_amount'):,}"
        if req.get("action") in MONEY:
            for m in chain:
                spent = self.state["spent"].get(m["id"], 0.0)
                cap = float(m["scope"].get("max_amount", 0))
                if spent + amt > cap:
                    return "refuse", "cumulative_over_limit", (f"{spent:,.0f} already spent under this mandate; "
                                                               f"{amt:,.0f} more would exceed {cap:,.0f} (order splitting)")

        # 5. the receiver's own policy
        pol = rcv.get("policy") or {}
        esc = pol.get("escalate_above", {})
        if req.get("action") in esc and amt > float(esc[req["action"]]):
            return "escalate", "policy_escalation", f"{req['action']} above {esc[req['action']]:,} needs a human at {receiver}"

        if req.get("action") in MONEY:
            for m in chain:
                self.state["spent"][m["id"]] = self.state["spent"].get(m["id"], 0.0) + amt
            self._save()
        return "allow", "ok", f"{why}, chain depth {len(chain)}, {req.get('action')} {amt:,.0f} within limit {scope.get('max_amount'):,}"

    @staticmethod
    def _attenuates(child: dict, parent: dict, child_exp: float, parent_exp: float) -> tuple[bool, str]:
        if not set(child.get("actions", [])) <= set(parent.get("actions", [])):
            return False, f"actions {child.get('actions')} not within {parent.get('actions')}"
        if child.get("currency") != parent.get("currency"):
            return False, f"currency {child.get('currency')} differs from parent {parent.get('currency')}"
        if float(child.get("max_amount", 0)) > float(parent.get("max_amount", 0)):
            return False, f"max_amount {child.get('max_amount')} > parent {parent.get('max_amount')}"
        if child_exp > parent_exp:
            return False, "child expires after its parent"
        return True, ""

    @_locked
    def receipts(self, limit: int = 50) -> list[dict]:
        if not self.receipts_file.exists() or limit <= 0:
            return []
        with self.receipts_file.open() as f:
            tail = deque((line for line in f if line.strip()), maxlen=limit)
        return [json.loads(line) for line in tail]


def solve_pow(seed: str, bits: int) -> tuple[str, int]:
    """Brute force a hashcash puzzle. Returns (nonce, tries). Used by unknown agents."""
    i = 0
    while True:
        d = hashlib.sha256(f"{seed}:{i}".encode()).digest()
        if int.from_bytes(d, "big") >> (256 - bits) == 0:
            return str(i), i + 1
        i += 1
