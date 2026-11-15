"""Nodes, fork choice, finality and a light client for the Attest ledger."""
from __future__ import annotations

from gate.crypto import sign, verify
from . import ledger as L


class Chain:
    """Every block this node has seen, the state after each, and the heaviest valid tip."""

    def __init__(self, stakes: dict, validators: list):
        g = L.genesis_block()
        gh = L.block_hash(g["header"])
        self.blocks = {gh: g}
        self.states = {gh: L.State(stakes, validators)}
        self.work = {gh: 0}
        self.genesis = self.tip = self.finalized = gh

    def height(self, h: str) -> int:
        return self.blocks[h]["header"]["height"]

    def descends_from(self, h: str, ancestor: str) -> bool:
        while True:
            if h == ancestor:
                return True
            if h == self.genesis:
                return False
            h = self.blocks[h]["header"]["prev"]

    def add(self, block: dict) -> tuple[bool, str]:
        h = L.block_hash(block["header"])
        if h in self.blocks:
            return False, "known"
        parent = block["header"]["prev"]
        if parent not in self.blocks:
            return False, "orphan"
        state, why = L.validate_block(block, self.states[parent], self.blocks[parent]["header"])
        if state is None:
            return False, why
        self.blocks[h], self.states[h] = block, state
        self.work[h] = self.work[parent] + 2 ** block["header"]["bits"]
        if self._better(h):
            self.tip = h
        return True, "ok"

    def _better(self, h: str) -> bool:
        if not self.descends_from(h, self.finalized):
            return False          # stake finality beats work: no reorg below a final block
        return (self.work[h], -int(h, 16)) > (self.work[self.tip], -int(self.tip, 16))

    def best_chain(self) -> list[dict]:
        out, h = [], self.tip
        while True:
            out.append(self.blocks[h])
            if h == self.genesis:
                return out[::-1]
            h = self.blocks[h]["header"]["prev"]


class Node:
    def __init__(self, name: str, priv: str, pub: str, stakes: dict, validators: list, log=None):
        self.name, self.priv, self.pub = name, priv, pub
        self.chain = Chain(stakes, validators)
        self.peers: list["Node"] = []
        self.online = True
        self.mempool: dict[str, dict] = {}
        self.votes: dict[str, dict] = {}          # block hash -> {validator: sig}
        self.voted: dict[tuple, dict] = {}        # (validator, height) -> first vote seen
        self.seen_records: set = set()
        self.log = log if log is not None else []

    # -- plumbing -------------------------------------------------------------------------
    def _note(self, kind: str, detail: str):
        self.log.append({"node": self.name, "kind": kind, "detail": detail})

    def _gossip(self, fn: str, *args):
        if not self.online:
            return
        for p in self.peers:
            if p.online:
                getattr(p, fn)(*args)

    @property
    def state(self) -> L.State:
        return self.chain.states[self.chain.tip]

    # -- records --------------------------------------------------------------------------
    def submit(self, rec: dict) -> tuple[bool, str]:
        """Check a record against the tip, queue it and tell the peers."""
        if rec.get("id") in self.seen_records:
            return True, "known"
        pending = self.state.copy()
        for queued in self.mempool.values():          # judge it after everything already queued
            pending.apply(queued)
        why = pending.apply(rec)
        if why:
            self._note("reject_record", f"{rec.get('type')}: {why}")
            return False, why
        self.seen_records.add(rec["id"])
        self.mempool[rec["id"]] = rec
        self._note("record", f"{rec['type']} queued")
        self._gossip("submit", rec)
        return True, "ok"

    # -- blocks ---------------------------------------------------------------------------
    def propose(self) -> dict | None:
        if self.pub not in self.state.validators or self.pub in self.state.suspended:
            return None
        st, recs = self.state.copy(), []
        for rec in list(self.mempool.values()):
            if st.apply(rec) is None:
                recs.append(rec)
        tip = self.chain.tip
        block = L.mine_block(self.priv, self.pub, tip, self.chain.height(tip) + 1, recs)
        self._note("mined", f"block {block['header']['height']} with {len(recs)} records, "
                            f"puzzle {block['header']['bits']} bits")
        self.receive_block(block, None)
        return block

    def receive_block(self, block: dict, sender: "Node | None"):
        if not self.online:
            return
        h = L.block_hash(block["header"])
        ok, why = self.chain.add(block)
        if why == "known":
            return
        if why == "orphan" and sender is not None:
            self.sync(sender)
            ok, why = self.chain.add(block)
        if not ok:
            if why != "known":
                self._note("reject_block", why)
            return
        for r in block["records"]:
            self.mempool.pop(r["id"], None)
            self.seen_records.add(r["id"])
        self._note("block", f"height {block['header']['height']} accepted")
        if self.pub in self.chain.states[h].validators:
            self.vote(block)
        for p in self.peers:
            if p.online and self.online:
                p.receive_block(block, self)

    def sync(self, peer: "Node"):
        """Pull every block the peer has that we lack, in height order, plus its votes."""
        for blk in sorted(peer.chain.blocks.values(), key=lambda b: b["header"]["height"]):
            if L.block_hash(blk["header"]) not in self.chain.blocks:
                self.chain.add(blk)
        for h, votes in peer.votes.items():
            for v, sig in votes.items():
                if h in self.chain.blocks:
                    self.receive_vote({"validator": v, "height": self.chain.height(h),
                                       "hash": h, "sig": sig}, gossip=False)

    def come_online(self):
        self.online = True
        for p in self.peers:
            if p.online:
                self.sync(p)
        self._note("online", f"synced to height {self.chain.height(self.chain.tip)}")

    # -- votes and finality ---------------------------------------------------------------
    def vote(self, block: dict, force: bool = False):
        h, height = L.block_hash(block["header"]), block["header"]["height"]
        if (self.pub, height) in self.voted and not force:
            return
        vote = {"validator": self.pub, "height": height, "hash": h,
                "sig": sign(self.priv, L.vote_body(height, h))}
        self.receive_vote(vote)

    def receive_vote(self, vote: dict, gossip: bool = True):
        if not self.online:
            return
        v, height, h = vote["validator"], vote["height"], vote["hash"]
        if v not in self.chain.states[self.chain.genesis].validators:
            return
        if not verify(v, L.vote_body(height, h), vote["sig"]):
            return
        if v in self.votes.get(h, {}):
            return                                    # this exact vote is already known
        key = (v, height)
        first = self.voted.get(key)
        if first and first["hash"] != h:
            slash = L.make_slash(v, {"height": height, "hash": first["hash"], "sig": first["sig"]},
                                 {"height": height, "hash": h, "sig": vote["sig"]})
            self._note("equivocation", f"validator {v[:8]} signed two blocks at height {height}")
            self.submit(slash)
        if first and first["hash"] == h:
            return
        self.voted.setdefault(key, {"hash": h, "sig": vote["sig"]})
        self.votes.setdefault(h, {})[v] = vote["sig"]
        self._update_finality(h)
        if gossip:
            self._gossip("receive_vote", vote)

    def _update_finality(self, h: str):
        if h not in self.chain.states:
            return
        st = self.chain.states[h]
        got = sum(st.stakes.get(v, 0) for v in self.votes.get(h, {}) if v not in st.suspended)
        if st.validator_stake() and got / st.validator_stake() > L.FINALITY:
            if self.chain.descends_from(h, self.chain.finalized) and \
                    self.chain.height(h) > self.chain.height(self.chain.finalized):
                self.chain.finalized = h
                self._note("final", f"block {self.chain.height(h)} is final "
                                    f"({got / st.validator_stake():.0%} of stake voted)")

    # -- reading the ledger ---------------------------------------------------------------
    def entries(self, agent: str, kinds=("passport", "outcome")) -> list[dict]:
        """Everything on the best chain about this agent, each with a Merkle branch."""
        out = []
        for blk in self.chain.best_chain():
            ids = [r["id"] for r in blk["records"]]
            for i, rec in enumerate(blk["records"]):
                if rec["type"] in kinds and agent in (rec.get("agent"), rec.get("provider"),
                                                      rec.get("buyer")):
                    out.append({"record": rec, "block": L.block_hash(blk["header"]),
                                "height": blk["header"]["height"],
                                "proof": L.merkle_proof(ids, i)})
        return out

    def history(self, agent: str) -> dict:
        """The agent's ledger as a full node sees it."""
        outs = [e for e in self.entries(agent, ("outcome",))]
        acc = [e for e in outs if e["record"]["status"] == "accepted"]
        return {"agent": agent, "passport": agent in self.state.passports, "trades": len(outs),
                "accepted": len(acc), "disputed": len(outs) - len(acc),
                "volume": sum(e["record"]["amount"] for e in acc),
                "entries": outs}


class LightClient:
    """Keeps block headers only (about 200 bytes each) and checks branches against them."""

    def __init__(self, validators: list):
        g = L.genesis_block()
        gh = L.block_hash(g["header"])
        self.validators = list(validators)
        self.headers = {gh: g["header"]}
        self.work = {gh: 0}
        self.genesis = self.tip = gh

    def sync_headers(self, node: Node):
        for blk in node.chain.best_chain()[1:]:
            h = L.block_hash(blk["header"])
            if h in self.headers or blk["header"]["prev"] not in self.headers:
                continue
            if L.check_header(blk["header"], blk["sig"], self.validators):
                continue
            self.headers[h] = blk["header"]
            self.work[h] = self.work[blk["header"]["prev"]] + 2 ** blk["header"]["bits"]
            if self.work[h] > self.work[self.tip]:
                self.tip = h

    def on_best_chain(self, h: str) -> bool:
        cur = self.tip
        while True:
            if cur == h:
                return True
            if cur == self.genesis:
                return False
            cur = self.headers[cur]["prev"]

    def verify_entry(self, e: dict) -> bool:
        hdr = self.headers.get(e["block"])
        rec = e["record"]
        valid = L.check_passport(rec) if rec["type"] == "passport" else L.check_outcome(rec)
        return bool(hdr and valid and self.on_best_chain(e["block"])
                    and L.verify_proof(rec["id"], e["proof"], hdr["root"]))

    def decide(self, nodes: list[Node], agent: str, *, min_credit=3, pair_cap=2,
               max_dispute_ratio=0.25) -> tuple[str, str]:
        """A seller's policy. Asks several nodes, keeps only what verifies, takes the union,
        so one lying node cannot hide a bad record unless every node it asks lies too."""
        seen: dict[str, dict] = {}
        for n in nodes:
            if not n.online:
                continue
            self.sync_headers(n)
            for e in n.entries(agent):
                if self.verify_entry(e):
                    seen[e["record"]["id"]] = e["record"]
        passports = [r for r in seen.values() if r["type"] == "passport"]
        if not passports:
            return "refuse", "no passport on the chain"
        outs = [r for r in seen.values() if r["type"] == "outcome"]
        disputed = [r for r in outs if r["status"] == "disputed"]
        if outs and len(disputed) / len(outs) > max_dispute_ratio:
            return "refuse", f"{len(disputed)} of {len(outs)} trades disputed"
        per_peer: dict[str, int] = {}
        for r in outs:
            if r["status"] == "accepted":
                peer = r["buyer"] if r["provider"] == agent else r["provider"]
                per_peer[peer] = per_peer.get(peer, 0) + 1
        credit = sum(min(pair_cap, c) for c in per_peer.values())
        if credit < min_credit:
            return "unknown", (f"history credit {credit} of {min_credit} needed "
                               f"(at most {pair_cap} per counterparty)")
        return "allow", f"{credit} credit from {len(per_peer)} counterparties, {len(disputed)} disputed"


def connect(nodes: list[Node]):
    for n in nodes:
        n.peers = [p for p in nodes if p is not n]
