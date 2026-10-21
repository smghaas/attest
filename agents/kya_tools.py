#!/usr/bin/env python
"""MCP tools that let a Hermes agent use Attest.

One file, two roles, chosen by environment variables set in ~/.hermes/config.yaml:

  KYA_ROLE=buyer     KYA_AGENT=<agent file name in state/agents/>
      whoami, send_request, read_replies
  KYA_ROLE=supplier  KYA_RECEIVER=<receiver name registered at the gate>
      inbox_next, check_agent, quote_price, reply

The supplier side is the customer of the gate: before it acts on a request from
another agent it calls check_agent, which is POST /verify/<receiver> on the gate,
and gets allow / refuse / escalate / unknown plus a receipt signed by the gate.
The tool also checks that receipt signature locally against GET /pubkey, so the
agent does not have to take the gate's word for it.

The buyer side only needs to present its credential and mandate chain with
every request and sign the request with its own key. It never talks to the
gate directly.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from gate import crypto  # noqa: E402

GATE = os.environ.get("KYA_GATE_URL", "http://127.0.0.1:8402")
ROLE = os.environ.get("KYA_ROLE", "buyer")
AGENT = os.environ.get("KYA_AGENT", "acme-procurement")
RECEIVER = os.environ.get("KYA_RECEIVER", "supplierco")
AG_DIR = ROOT / "state" / "agents"
AUDIT = ROOT / "logs" / "tools_audit.log"

mcp = MCPServer(f"kya_{ROLE}")


def _audit(tool: str, args: dict, out):
    AUDIT.parent.mkdir(exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "role": ROLE, "agent": AGENT if ROLE == "buyer" else RECEIVER,
                            "tool": tool, "args": args, "out": out}, default=str) + "\n")


def _me() -> dict:
    return json.loads((AG_DIR / f"{AGENT}.json").read_text())


PRICE_LIST = {"X-200": 19.50, "X-300": 27.25, "Y-10": 4.10, "Z-FLANGE": 112.00}

# ----------------------------------------------------------------- buyer tools
if ROLE == "buyer":

    @mcp.tool()
    def whoami() -> dict:
        """Who this agent is: operator, credential issuer and the scope of its mandate (what it may do and up to what amount)."""
        me = _me()
        leaf = me["chain"][-1]
        out = {"agent": me["name"], "operator": me["operator"], "public_key": me["pub"][:16] + "...",
               "credential_issuer": me["credential"]["operator"], "chain_depth": len(me["chain"]),
               "delegated_by": me.get("delegated_by"), "allowed_actions": leaf["scope"]["actions"],
               "max_amount_usd": leaf["scope"]["max_amount"]}
        _audit("whoami", {}, out)
        return out

    @mcp.tool()
    def send_request(action: str, amount: float, message: str, to: str = "supplierco") -> dict:
        """Send a signed request to another agent, with this agent's credential and mandate chain attached.
        action: request_quote or place_order. amount: the dollar amount involved. message: free text for the other agent.
        Returns the request id to quote when reading replies."""
        me = _me()
        req = crypto.signed(me["priv"], {
            "type": "kya.request/1", "from": me["pub"], "to": to, "action": action,
            "amount": float(amount), "message": message, "ts": time.time(), "nonce": uuid.uuid4().hex})
        env = {"credential": me["credential"], "chain": me["chain"], "request": req}
        r = httpx.post(f"{GATE}/inbox/{to}", json=env, timeout=10).json()
        out = {"sent": True, "request_id": req["id"], "to": to, "action": action, "amount": amount,
               "attached": f"credential from {me['credential']['operator']}, mandate chain depth {len(me['chain'])}, request signed by {me['name']}"}
        _audit("send_request", {"action": action, "amount": amount, "to": to}, out)
        return out

    @mcp.tool()
    def read_replies() -> list:
        """Replies other agents have sent to this agent, newest last."""
        me = _me()
        out = httpx.get(f"{GATE}/replies/{me['pub']}", timeout=10).json()
        _audit("read_replies", {}, out)
        return out

# -------------------------------------------------------------- supplier tools
else:

    @mcp.tool()
    def inbox_next() -> dict:
        """Take the next pending request from other agents. Shows who claims to send it, the action, amount and message.
        Nothing here is verified yet: call check_agent with the request_id before acting on it."""
        r = httpx.get(f"{GATE}/inbox/{RECEIVER}/next", timeout=10).json()
        if r.get("empty"):
            out = {"empty": True}
        else:
            env = r["envelope"]
            req = env["request"]
            cred = env.get("credential") or {}
            out = {"request_id": r["request_id"], "claimed_sender_key": req["from"][:16] + "...",
                   "claimed_operator": cred.get("operator", "(no credential attached)"),
                   "action": req["action"], "amount": req["amount"], "message": req["message"],
                   "verified": False}
        _audit("inbox_next", {}, out)
        return out

    @mcp.tool()
    def check_agent(request_id: str) -> dict:
        """Ask Attest whether the agent behind a request is who it claims, is backed by a staked operator, and is allowed to do this.
        Returns decision (allow / refuse / escalate / unknown), the reason, and a receipt signed by the gate.
        unknown means no staked operator is behind the sender: treat it as the public, read-only answers at most.
        receipt_verified is true when the receipt's signature checks out against the gate's public key."""
        item = httpx.get(f"{GATE}/inbox/{RECEIVER}/get/{request_id}", timeout=10)
        if item.status_code != 200:
            out = {"error": "no such request_id"}
            _audit("check_agent", {"request_id": request_id}, out)
            return out
        env = item.json()["envelope"]
        r = httpx.post(f"{GATE}/verify/{RECEIVER}", json=env, timeout=10).json()
        gate_pub = httpx.get(f"{GATE}/pubkey", timeout=10).json()["gate"]
        rec = r["receipt"]
        ok = crypto.verify(gate_pub, crypto.body_of(rec), rec["sig"]) and rec["gate"] == gate_pub
        out = {"decision": r["decision"], "reason": r["reason"], "detail": r["detail"],
               "receipt_id": rec["id"], "receipt_verified": ok,
               "chain_depth": len(rec["chain_ids"]), "gate_key": gate_pub[:16] + "..."}
        if r.get("challenge"):
            out["puzzle_required_bits"] = r["challenge"]["bits"]
        _audit("check_agent", {"request_id": request_id}, out)
        return out

    @mcp.tool()
    def quote_price(part: str, units: int) -> dict:
        """Price a quote from SupplierCo's price list. Parts: X-200, X-300, Y-10, Z-FLANGE. Volume discount 5% above 100 units."""
        part = part.upper().strip()
        if part not in PRICE_LIST:
            out = {"error": f"unknown part {part}", "parts": list(PRICE_LIST)}
        else:
            unit = PRICE_LIST[part]
            disc = 0.05 if units > 100 else 0.0
            total = round(unit * units * (1 - disc), 2)
            out = {"part": part, "units": units, "unit_price": unit, "discount": disc, "total_usd": total,
                   "valid_days": 14}
        _audit("quote_price", {"part": part, "units": units}, out)
        return out

    @mcp.tool()
    def reply(request_id: str, text: str) -> dict:
        """Send a reply to the agent that made the request."""
        r = httpx.post(f"{GATE}/inbox/{RECEIVER}/reply", json={"request_id": request_id, "text": text}, timeout=10)
        out = {"ok": r.status_code == 200, "request_id": request_id}
        _audit("reply", {"request_id": request_id, "text": text}, out)
        return out


if __name__ == "__main__":
    mcp.run()
