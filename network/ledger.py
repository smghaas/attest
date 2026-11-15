"""Attest ledger: the Bitcoin ideas applied to agent history.

A small chain of blocks that records what agents have done, with no referee:

  passport   the operator and the agent sign who the agent is and what it may do.
             Registering costs a hashcash puzzle (proof of work), so identities are not free.
  pledge     an operator commits part of its stake to cover one seller. The chain refuses
             a pledge that would take the total above the operator's stake. That is the
             double-spend rule: the same stake cannot back everyone at once.
  outcome    a trade between two agents, signed by BOTH. Status accepted or disputed.
             We record disputes, we do not judge them.
  slash      proof that a validator signed two different blocks at one height.

Blocks need a proof-of-work puzzle and a staked proposer's signature. A block is final
once validators holding more than two thirds of the stake have voted for it, and nobody
may reorganise below a final block. The fork rule is the most cumulative work among chains
that keep the final block.

The stake table (genesis) stands in for a public chain such as Base. Money is not moved here.
"""
from __future__ import annotations

import copy
import hashlib
import time

from gate.crypto import canonical, digest, sign, verify, signed

POW_BITS = 12            # block puzzle, leading zero bits (tests lower it)
REG_BITS = 10            # registration puzzle for a new agent identity
SLASH_FRACTION = 0.5
FINALITY = 2 / 3
STATUSES = ("accepted", "disputed")
GENESIS_PREV = "0" * 64
_SIGS = ("sig", "agent_sig", "buyer_sig")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def zero_bits(hexstr: str) -> int:
    return 256 - int(hexstr, 16).bit_length()


# ---- Merkle tree (paper section 7) ------------------------------------------------------
def merkle_levels(leaves: list[str]) -> list[list[str]]:
    levels = [list(leaves) or [sha("")]]
    while len(levels[-1]) > 1:
        cur, nxt = levels[-1], []
        for i in range(0, len(cur), 2):
            nxt.append(sha(cur[i] + (cur[i + 1] if i + 1 < len(cur) else cur[i])))
        levels.append(nxt)
    return levels


def merkle_root(leaves: list[str]) -> str:
    return merkle_levels(leaves)[-1][0]


def merkle_proof(leaves: list[str], i: int) -> list[tuple[str, int]]:
    proof, idx = [], i
    for level in merkle_levels(leaves)[:-1]:
        sib = idx ^ 1
        proof.append((level[sib] if sib < len(level) else level[idx], idx & 1))
        idx //= 2
    return proof


def verify_proof(leaf: str, proof, root: str) -> bool:
    h = leaf
    for sib, current_is_right in proof:
        h = sha(sib + h) if current_is_right else sha(h + sib)
    return h == root


# ---- documents ---------------------------------------------------------------------------
def _core(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _SIGS}


def _id_ok(d: dict) -> bool:
    return digest({k: v for k, v in _core(d).items() if k != "id"}) == d.get("id")


def solve_registration(agent_pub: str) -> int:
    n = 0
    while zero_bits(sha(f"{agent_pub}:{n}")) < REG_BITS:
        n += 1
    return n


def make_passport(op_priv, op_pub, agent_priv, agent_pub, name, actions, max_amount) -> dict:
    doc = signed(op_priv, {"type": "passport", "agent": agent_pub, "operator": op_pub, "name": name,
                           "actions": sorted(actions), "max_amount": max_amount,
                           "nonce": solve_registration(agent_pub)})
    doc["agent_sig"] = sign(agent_priv, {"id": doc["id"]})
    return doc


def check_passport(d: dict) -> bool:
    return (d.get("type") == "passport" and _id_ok(d)
            and verify(d["operator"], _core(d), d.get("sig", ""))
            and verify(d["agent"], {"id": d["id"]}, d.get("agent_sig", ""))
            and zero_bits(sha(f"{d['agent']}:{d.get('nonce')}")) >= REG_BITS)


def make_pledge(op_priv, op_pub, seller, cover, ts=None) -> dict:
    return signed(op_priv, {"type": "pledge", "operator": op_pub, "seller": seller, "cover": cover,
                            "ts": ts if ts is not None else time.time()})


def make_outcome(provider_priv, provider_pub, buyer_priv, buyer_pub, request, amount, status,
                 ts=None) -> dict:
    body = {"type": "outcome", "request": request, "provider": provider_pub, "buyer": buyer_pub,
            "amount": amount, "status": status, "ts": ts if ts is not None else time.time()}
    body["id"] = digest(body)
    return {**body, "sig": sign(provider_priv, body), "buyer_sig": sign(buyer_priv, body)}


def propose_outcome(provider_priv, provider_pub, buyer_pub, request, amount, status, ts=None) -> dict:
    """Step one of the two-party flow: the provider signs. Send this to the buyer to countersign."""
    body = {"type": "outcome", "request": request, "provider": provider_pub, "buyer": buyer_pub,
            "amount": amount, "status": status, "ts": ts if ts is not None else time.time()}
    body["id"] = digest(body)
    return {**body, "sig": sign(provider_priv, body)}


def countersign(buyer_priv, proposal: dict) -> dict:
    """Step two: the buyer checks the provider's signature, then signs the same body. Returns the record."""
    body = _core(proposal)
    if not verify(proposal["provider"], body, proposal.get("sig", "")):
        raise ValueError("the provider's signature does not match")
    return {**proposal, "buyer_sig": sign(buyer_priv, body)}


def check_outcome(d: dict) -> bool:
    return (d.get("type") == "outcome" and _id_ok(d) and d.get("status") in STATUSES
            and d.get("provider") != d.get("buyer") and float(d.get("amount", -1)) >= 0
            and verify(d["provider"], _core(d), d.get("sig", ""))
            and verify(d["buyer"], _core(d), d.get("buyer_sig", "")))


def vote_body(height: int, block_hash: str) -> dict:
    return {"height": height, "hash": block_hash}


def make_slash(validator, a: dict, b: dict) -> dict:
    body = {"type": "slash", "validator": validator, "a": a, "b": b}
    return {**body, "id": digest(body)}


# ---- chain state -------------------------------------------------------------------------
class State:
    def __init__(self, stakes: dict, validators: list):
        self.stakes = dict(stakes)
        self.validators = list(validators)
        self.suspended: set = set()
        self.passports: dict = {}
        self.pledges: dict = {}
        self.outcomes: dict = {}
        self.slashed_keys: set = set()

    def copy(self) -> "State":
        return copy.deepcopy(self)

    def validator_stake(self) -> float:
        return sum(self.stakes.get(v, 0) for v in self.validators if v not in self.suspended)

    def apply(self, rec: dict) -> str | None:
        """Apply one record. Return None on success or the reason it is invalid."""
        kind = rec.get("type")
        if kind == "passport":
            if not check_passport(rec):
                return "bad passport (signature, id or registration puzzle)"
            if rec["agent"] in self.passports:
                return "agent already registered"
            if rec["operator"] in self.suspended:
                return "operator is suspended"
            self.passports[rec["agent"]] = rec
            return None
        if kind == "pledge":
            if not (_id_ok(rec) and verify(rec.get("operator", ""), _core(rec), rec.get("sig", ""))):
                return "bad pledge signature"
            op, cover = rec["operator"], float(rec.get("cover", 0))
            if cover <= 0 or op in self.suspended:
                return "pledge must be positive from an unsuspended operator"
            committed = sum(self.pledges.get(op, {}).values())
            if committed + cover > self.stakes.get(op, 0):
                return (f"overpledge: {committed + cover:,.0f} committed against a stake of "
                        f"{self.stakes.get(op, 0):,.0f}")
            self.pledges.setdefault(op, {})[rec["seller"]] = \
                self.pledges.get(op, {}).get(rec["seller"], 0) + cover
            return None
        if kind == "outcome":
            if not check_outcome(rec):
                return "bad outcome (both parties must sign)"
            if rec["provider"] not in self.passports or rec["buyer"] not in self.passports:
                return "both agents need a registered passport"
            if rec["request"] in self.outcomes:
                return "request already settled (first record wins)"
            self.outcomes[rec["request"]] = rec
            return None
        if kind == "slash":
            v, a, b = rec.get("validator"), rec.get("a", {}), rec.get("b", {})
            if v not in self.validators:
                return "not a validator"
            if a.get("height") != b.get("height") or a.get("hash") == b.get("hash"):
                return "not a conflicting pair"
            for x in (a, b):
                if not verify(v, vote_body(x["height"], x["hash"]), x.get("sig", "")):
                    return "bad vote signature"
            key = (v, a["height"])
            if key in self.slashed_keys:
                return "already slashed for this height"
            self.slashed_keys.add(key)
            self.stakes[v] = self.stakes.get(v, 0) * (1 - SLASH_FRACTION)
            self.suspended.add(v)
            return None
        return "unknown record type"


# ---- blocks ------------------------------------------------------------------------------
def block_hash(header: dict) -> str:
    return digest(header)


def genesis_block() -> dict:
    return {"header": {"height": 0, "prev": GENESIS_PREV, "root": merkle_root([]), "ts": 0,
                       "proposer": "genesis", "bits": 0, "nonce": 0}, "sig": "", "records": []}


def mine_block(proposer_priv, proposer_pub, prev_hash, height, records: list[dict], bits=None,
               ts=None) -> dict:
    bits = POW_BITS if bits is None else bits
    header = {"height": height, "prev": prev_hash, "root": merkle_root([r["id"] for r in records]),
              "ts": ts if ts is not None else time.time(), "proposer": proposer_pub, "bits": bits,
              "nonce": 0}
    while zero_bits(block_hash(header)) < bits:
        header["nonce"] += 1
    return {"header": header, "sig": sign(proposer_priv, {"hash": block_hash(header)}),
            "records": records}


def check_header(header: dict, sig: str, validators: list) -> str | None:
    """The checks a light client can do alone: puzzle and proposer signature."""
    if header.get("bits", 0) < POW_BITS:
        return "puzzle easier than the network minimum"
    if zero_bits(block_hash(header)) < header["bits"]:
        return "proof of work does not meet the target"
    if header.get("proposer") not in validators:
        return "proposer is not a validator"
    if not verify(header["proposer"], {"hash": block_hash(header)}, sig):
        return "bad proposer signature"
    return None


def validate_block(block: dict, parent_state: State, parent_header: dict) -> tuple[State | None, str]:
    h = block["header"]
    if h["height"] != parent_header["height"] + 1:
        return None, "wrong height"
    why = check_header(h, block.get("sig", ""), parent_state.validators)
    if why:
        return None, why
    if h["proposer"] in parent_state.suspended:
        return None, "proposer is suspended"
    if not all(_id_ok(r) for r in block["records"]):
        return None, "a record's content does not match its id"
    if h["root"] != merkle_root([r.get("id", "") for r in block["records"]]):
        return None, "records do not match the Merkle root"
    state = parent_state.copy()
    for rec in block["records"]:
        why = state.apply(rec)
        if why:
            return None, f"invalid record: {why}"
    return state, "ok"
