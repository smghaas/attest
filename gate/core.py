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
  - a receiver can file a slashing claim backed by a gate receipt; the issuer loses
    part of its stake and is suspended
  - an agent with no staked issuer gets a hashcash puzzle seeded by the latest Base
    Sepolia block hash and bound to this request and receiver; difficulty doubles on
    every retry from the same key. It is answered as unknown, read-only actions only.
    Anything that moves money from an unstaked agent is refused.

verify(receiver, envelope) returns a decision and a receipt signed by the gate:
  allow     everything checks out and the action is inside the leaf scope
  refuse    a signature, expiry, revocation, attenuation, scope or spend check failed,
            or an unstaked agent tried to move money
  escalate  checks pass but the receiver's own policy wants a human
  unknown   no staked issuer behind the agent: read-only request, puzzle required
            (pow_required) or solved (pow_ok)
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from . import crypto

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

READ_ONLY = {"request_quote"}           # actions that move no money
MONEY = {"place_order"}
POW_BASE_BITS = 16                      # about 65,000 hashes on average; +1 bit (x2 work) per retry
SLASH_FRACTION = 0.5                    # base penalty; the issuer is also suspended
CHAIN_RPC = "https://sepolia.base.org"  # Base Sepolia testnet, read only
BLOCK_CACHE_S = 60


class Gate:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "gate_state.json"
        self.receipts_file = LOG_DIR / "receipts.jsonl"
        self.state = self._load()
        self._block = None  # (fetched_at, number, hash, source)

    # ---------- state ----------
    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        priv, pub = crypto.keygen()
        st = {
            "gate_key": {"priv": priv, "pub": pub},
            # receiver name -> {"min_stake": float, "policy": {...}}
            "receivers": {},
            # issuer pubkey -> {"name", "stake", "suspended", "slashes": [..]}
            "stakes": {},
            "revoked": {},          # doc id -> {"by", "ts", "reason"}
            "nonces_seen": {},      # nonce -> ts
            "spent": {},            # mandate id -> money already allowed under it
            "pow_attempts": {},     # agent pubkey -> puzzles issued so far
            "pow_open": {},         # challenge id -> {seed, bits, request_id, receiver, from}
        }
        self.state_file.write_text(json.dumps(st, indent=1))
        return st

    def _save(self):
        self.state_file.write_text(json.dumps(self.state, indent=1))

    @property
    def pubkey(self) -> str:
        return self.state["gate_key"]["pub"]

    # ---------- configuration ----------
    def register_receiver(self, name: str, min_stake: float, policy: dict | None = None):
        self.state["receivers"][name] = {"min_stake": float(min_stake), "policy": policy or {}}
        self._save()

    def stake(self, issuer_pub: str, name: str, amount: float, sig: str) -> dict:
        """An operator adds stake for its issuer key. The operator signs {stake, issuer, name, amount}."""
        body = {"stake": True, "issuer": issuer_pub, "name": name, "amount": float(amount)}
        if not crypto.verify(issuer_pub, body, sig):
            return {"ok": False, "error": "bad_signature"}
        s = self.state["stakes"].setdefault(issuer_pub, {"name": name, "stake": 0.0, "suspended": False, "slashes": []})
        s["stake"] += float(amount)
        self._save()
        return {"ok": True, "issuer": name, "stake": s["stake"]}

    def slash(self, receiver: str, receipt_id: str, breach: str) -> dict:
        """A receiver claims a breach, backed by a receipt the gate signed for that receiver.
        MVP rule: the receipt must exist, be addressed to this receiver, be an allow, and
        name a credential from a staked issuer. No dispute window yet."""
        rec = next((r for r in self.receipts(10_000) if r["id"] == receipt_id), None)
        if rec is None:
            return {"ok": False, "error": "no_such_receipt"}
        if rec["receiver"] != receiver or rec["decision"] != "allow":
            return {"ok": False, "error": "receipt_not_an_allow_for_this_receiver"}
        issuer = rec.get("issuer")
        s = self.state["stakes"].get(issuer)
        if not s or s["stake"] <= 0:
            return {"ok": False, "error": "issuer_has_no_stake"}
        penalty = round(s["stake"] * SLASH_FRACTION, 2)
        s["stake"] -= penalty
        s["suspended"] = True
        s["slashes"].append({"receipt": receipt_id, "receiver": receiver, "breach": breach,
                             "penalty": penalty, "ts": time.time()})
        self._save()
        return {"ok": True, "issuer": s["name"], "penalty": penalty, "stake_left": s["stake"], "suspended": True}

    def revoke(self, target_id: str, by_pub: str, sig: str, reason: str = "") -> dict:
        """Revoke a credential or mandate. MVP: accepted from any staked issuer key or the
        gate operator (the gate does not keep every document, so it cannot check who issued
        the target). A delegated mandate is revoked implicitly when any ancestor is revoked
        (cascade), which verify() enforces by walking the chain."""
        body = {"revoke": target_id, "by": by_pub, "reason": reason}
        if not crypto.verify(by_pub, body, sig):
            return {"ok": False, "error": "bad_signature"}
        trusted = {self.pubkey} | {k for k, v in self.state["stakes"].items() if v["stake"] > 0}
        if by_pub not in trusted:
            return {"ok": False, "error": "revoker_not_trusted"}
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

    # ---------- verify ----------
    def verify(self, receiver: str, envelope: dict) -> dict:
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
        if abs(now - float(req.get("ts", 0))) > 600:
            return "stale_request", "request timestamp is more than 10 minutes off"
        if req.get("nonce") in self.state["nonces_seen"]:
            return "replay", "this request was already presented"
        return None

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
        n = self.state["pow_attempts"].get(req["from"], 0)
        bits = POW_BASE_BITS + n
        seed = hashlib.sha256(f"{blk['hash']}:{req['id']}:{receiver}".encode()).hexdigest()
        cid = seed[:16]
        self.state["pow_attempts"][req["from"]] = n + 1
        self.state["pow_open"][cid] = {"seed": seed, "bits": bits, "request_id": req["id"],
                                       "receiver": receiver, "from": req["from"], "block": blk}
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
        amt = float(req.get("amount") or 0)
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
        if float(child.get("max_amount", 0)) > float(parent.get("max_amount", 0)):
            return False, f"max_amount {child.get('max_amount')} > parent {parent.get('max_amount')}"
        if child_exp > parent_exp:
            return False, "child expires after its parent"
        return True, ""

    def receipts(self, limit: int = 50) -> list[dict]:
        if not self.receipts_file.exists():
            return []
        lines = self.receipts_file.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-limit:]]


def solve_pow(seed: str, bits: int) -> tuple[str, int]:
    """Brute force a hashcash puzzle. Returns (nonce, tries). Used by unknown agents."""
    i = 0
    while True:
        d = hashlib.sha256(f"{seed}:{i}".encode()).digest()
        if int.from_bytes(d, "big") >> (256 - bits) == 0:
            return str(i), i + 1
        i += 1
