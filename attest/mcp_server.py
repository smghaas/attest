#!/usr/bin/env python
"""MCP server: lets any MCP client (Claude, Hermes, Cursor) use Attest.

    ATTEST_URL=http://127.0.0.1:8402 python -m attest.mcp_server

Tools: check_agent, verify_receipt, list_receivers, get_stakes, recent_decisions.
A receiving agent calls check_agent with the envelope it got from the sender before it acts.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .client import Attest

mcp = MCPServer("attest")
_at = Attest()


@mcp.tool()
def check_agent(receiver: str, envelope: dict) -> dict:
    """Ask Attest whether the agent that sent this envelope is who it claims, is backed by a staked
    operator, and is allowed to do what it asks. `envelope` is {credential, chain, request} exactly as
    received. `receiver` is your own registered name at the gate.
    Returns decision (allow, refuse, escalate or unknown), the reason, and receipt_verified.
    allow: act. refuse: do not act. escalate: ask a human. unknown: no staked operator behind the
    sender, treat as public and read-only. Never act on a decision whose receipt_verified is false.
    repeat true: the gate already answered this exact request for you (same receipt); act on a request at most once."""
    d = _at.check(receiver, envelope)
    out = {"decision": d.decision, "reason": d.reason, "detail": d.detail, "receipt_id": d.receipt["id"],
           "receipt_verified": d.receipt_verified, "repeat": d.repeat}
    if d.challenge:
        out["puzzle_required_bits"] = d.challenge.get("bits")
    return out


@mcp.tool()
def verify_receipt(receipt: dict) -> dict:
    """Check that a receipt was signed by this gate and not altered."""
    return {"valid": _at.verify_receipt(receipt)}


@mcp.tool()
def list_receivers() -> dict:
    """Receivers registered at the gate and the stake threshold each one requires."""
    return _at.receivers()


@mcp.tool()
def get_stakes() -> dict:
    """Which operators have stake behind them, and how much."""
    return _at.stakes()


@mcp.tool()
def recent_decisions(limit: int = 10) -> list:
    """The latest signed decisions the gate made."""
    return [{"id": r["id"], "receiver": r["receiver"], "action": r.get("action"), "amount": r.get("amount"),
             "decision": r["decision"], "reason": r["reason"]} for r in _at.receipts(limit)]


if __name__ == "__main__":
    mcp.run()
