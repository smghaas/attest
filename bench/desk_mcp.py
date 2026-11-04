#!/usr/bin/env python
"""The supplier's desk for one episode, served over MCP, so a headless Claude Code
session (`claude -p`, running on a Claude subscription) can be the LLM supplier.

  python bench/desk_mcp.py --case A5 --rep 0 --cond C3_llm_attest --cap 1 --out /tmp/ep.json

Same Desk, tools, descriptions and decision recording as the API harness
(bench/llm_supplier.py). check_agent is served only for C3. The budget is
CALLS_PER_REQUEST x requests tool calls; after that every tool refuses. The episode
state is written to --out after every call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from bench.cases import MAIN, SYBIL  # noqa: E402
from bench.llm_supplier import CALLS_PER_REQUEST, CHECK_TOOL, TOOLS, Desk  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--case", required=True)
ap.add_argument("--rep", type=int, default=0)
ap.add_argument("--cond", required=True, choices=["C2_llm", "C3_llm_attest"])
ap.add_argument("--cap", type=float, default=None)
ap.add_argument("--out", required=True)
a = ap.parse_args()

case = next(c for c in MAIN + SYBIL if c.id == a.case)
world = case.world(a.cap)
items = case.build(world)
desk = Desk(world, items, with_attest=a.cond == "C3_llm_attest")
budget = CALLS_PER_REQUEST * len(items)
state = {"case": a.case, "rep": a.rep, "cond": a.cond, "budget": budget, "budget_exhausted": False}
DESC = {t["name"]: t["description"] for t in TOOLS + [CHECK_TOOL]}


def save():
    Path(a.out).write_text(json.dumps({
        **state, "n_items": len(items), "taken": desk.next, "calls": desk.calls,
        "decisions": desk.decisions, "checks": desk.checks}, default=str))


def call(name: str, args: dict) -> dict:
    if len(desk.calls) >= budget:
        state["budget_exhausted"] = True
        save()
        return {"error": "budget exhausted, stop now"}
    out = desk.run(name, args)
    save()
    return out


mcp = MCPServer("desk")


@mcp.tool(description=DESC["inbox_next"])
def inbox_next() -> dict:
    return call("inbox_next", {})


@mcp.tool(description=DESC["view_documents"])
def view_documents(message_id: str) -> dict:
    return call("view_documents", {"message_id": message_id})


@mcp.tool(description=DESC["quote_price"])
def quote_price(part: str, units: int) -> dict:
    return call("quote_price", {"part": part, "units": units})


if desk.with_attest:
    @mcp.tool(description=DESC["check_agent"])
    def check_agent(message_id: str) -> dict:
        return call("check_agent", {"message_id": message_id})


@mcp.tool(description=DESC["fulfil"])
def fulfil(message_id: str, note: str) -> dict:
    return call("fulfil", {"message_id": message_id, "note": note})


@mcp.tool(description=DESC["decline"])
def decline(message_id: str, reason: str) -> dict:
    return call("decline", {"message_id": message_id, "reason": reason})


@mcp.tool(description=DESC["escalate"])
def escalate(message_id: str, reason: str) -> dict:
    return call("escalate", {"message_id": message_id, "reason": reason})


if __name__ == "__main__":
    save()
    try:
        mcp.run()
    finally:
        world.close()
