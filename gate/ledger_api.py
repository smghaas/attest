"""The Attest ledger over HTTP (only when ATTEST_LEDGER=1).

The gate runs four validator nodes in this one process, so this is a demonstration of the protocol,
not independent nodes. Anyone can read an agent's history and submit passports and two-party outcomes;
every record carries its own signatures, and the nodes check them, so the gate cannot invent history.

  GET  /ledger/info                     height, final height, validators, records, puzzle sizes
  POST /ledger/records                  a signed passport or outcome (both parties signed)
  GET  /ledger/agents                   registered agents with their trade counts and a decision
  GET  /ledger/agents/{pub}/history     the agent's records, each with a Merkle branch
  GET  /ledger/agents/{pub}/decision    what a light client decides after asking two nodes
  GET  /ledger/blocks?limit=10          the latest block headers
"""
from __future__ import annotations

import json
import os
import threading

from fastapi import APIRouter, HTTPException

from network import ledger as L
from network.node import LightClient, Node, connect

from .core import STATE_DIR
from . import crypto

router = APIRouter(prefix="/ledger")
PUBLIC_TYPES = ("passport", "outcome")


def enabled() -> bool:
    return os.environ.get("ATTEST_LEDGER") == "1"


class LedgerService:
    VALIDATORS = 4

    def __init__(self, state_dir=None):
        self.dir = state_dir or STATE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keys_file = self.dir / "ledger_keys.json"
        self.records_file = self.dir / "ledger_records.json"
        self.lock = threading.RLock()
        if self.keys_file.exists():
            keys = json.loads(self.keys_file.read_text())
        else:
            keys = [dict(zip(("priv", "pub"), crypto.keygen())) for _ in range(self.VALIDATORS)]
            self.keys_file.write_text(json.dumps(keys))
        vals = [k["pub"] for k in keys]
        stakes = {v: 10000 for v in vals}
        self.log: list = []
        self.nodes = [Node(f"validator-{i + 1}", k["priv"], k["pub"], stakes, vals, self.log)
                      for i, k in enumerate(keys)]
        connect(self.nodes)
        self.light = LightClient(vals)
        self.turn = 0
        self.records: list = []
        if self.records_file.exists():                  # rebuild the chain from the saved records
            saved = json.loads(self.records_file.read_text())
            for i in range(0, len(saved), 50):
                for rec in saved[i:i + 50]:
                    if self.nodes[0].submit(rec)[0]:
                        self.records.append(rec)
                self._mine()

    def _mine(self):
        self.nodes[self.turn % self.VALIDATORS].propose()
        self.turn += 1
        self.log.clear()

    def submit(self, rec: dict) -> tuple[bool, str, int]:
        if rec.get("type") not in PUBLIC_TYPES:
            return False, "only passport and outcome records can be submitted", 0
        with self.lock:
            if len(self.records) >= int(os.environ.get("ATTEST_MAX_LEDGER_RECORDS", 5000)):
                return False, "the ledger sandbox is full", 0
            ok, why = self.nodes[0].submit(rec)
            if not ok:
                return False, why, 0
            if why == "known":
                return True, "already on the ledger", self.height()
            self._mine()
            self.records.append(rec)
            self.records_file.write_text(json.dumps(self.records))
            return True, "ok", self.height()

    def height(self) -> int:
        n = self.nodes[0]
        return n.chain.height(n.chain.tip)

    def info(self) -> dict:
        n = self.nodes[0]
        return {"height": self.height(), "final": n.chain.height(n.chain.finalized),
                "validators": self.VALIDATORS, "records": len(self.records), "block_puzzle_bits": L.POW_BITS,
                "registration_puzzle_bits": L.REG_BITS, "agents": len(n.state.passports),
                "note": "four validators run inside this server: the protocol, not independent nodes"}

    def decision(self, pub: str) -> dict:
        d, why = self.light.decide(self.nodes[:2], pub)
        return {"decision": d, "why": why}


_svc: LedgerService | None = None
_svc_lock = threading.Lock()


def service() -> LedgerService:
    global _svc
    if not enabled():
        raise HTTPException(404, "the ledger is off on this gate (start with ATTEST_LEDGER=1)")
    with _svc_lock:
        if _svc is None or _svc.dir != STATE_DIR:
            _svc = LedgerService()
        return _svc


@router.get("/info")
def info():
    return service().info()


@router.post("/records")
def post_record(rec: dict):
    ok, why, height = service().submit(rec)
    if not ok:
        raise HTTPException(400, why)
    return {"ok": True, "result": why, "height": height, "id": rec.get("id")}


@router.get("/agents")
def agents():
    s = service()
    out = []
    for pub, p in s.nodes[0].state.passports.items():
        h = s.nodes[0].history(pub)
        out.append({"agent": pub, "name": p["name"], "operator": p["operator"], "actions": p["actions"],
                    "max_amount": p["max_amount"], "trades": h["trades"], "accepted": h["accepted"],
                    "disputed": h["disputed"], "volume": h["volume"], **s.decision(pub)})
    return out


@router.get("/agents/{pub}/history")
def history(pub: str):
    return service().nodes[0].history(pub)


@router.get("/agents/{pub}/decision")
def decision(pub: str):
    return service().decision(pub)


@router.get("/blocks")
def blocks(limit: int = 10):
    n = service().nodes[0]
    return [{"height": b["header"]["height"], "hash": L.block_hash(b["header"]), "prev": b["header"]["prev"],
             "records": len(b["records"]), "proposer": b["header"]["proposer"][:12],
             "bits": b["header"]["bits"], "root": b["header"]["root"]}
            for b in n.chain.best_chain()[-max(1, min(limit, 100)):]][::-1]
