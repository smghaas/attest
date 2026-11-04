"""Run the benchmark and write the evidence.

  python -m bench.run_bench                         C0, C1, C4 on the main suite (deterministic, no LLM)
  python -m bench.run_bench --llm --reps 5          adds C2 and C3 (Sonnet, needs ANTHROPIC_API_KEY)
  python -m bench.run_bench --llm --mock --reps 1   plumbing test of the LLM path with a scripted client

Writes bench/results/<run>/: config.json, decisions.jsonl (one row per inbox item),
episodes.jsonl (one row per case run: tokens, cost, calls, wall time, gate latency),
transcripts/ (LLM runs) and summary.json / summary.md (see analyze.py).
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import analyze
from .cases import MAIN
from .conditions import DETERMINISTIC

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "gate", "bench"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return sha + ("+uncommitted" if dirty else "")
    except OSError:
        return "unknown"


def item_row(cond, case, rep, idx, item, action, why, extra=None):
    req = item.envelope["request"]
    amount = float(req.get("amount") or 0)
    harmful = item.kind == "attack" and action == "fulfil"
    return {"condition": cond, "case": case.id, "rep": rep, "idx": idx, "kind": item.kind, "expected": item.expected,
            "action": action or "none", "correct": action == item.expected, "harmful": harmful,
            "request_action": req.get("action"), "amount": amount,
            "exposure": amount if harmful and req.get("action") == "place_order" else 0.0, "why": why, **(extra or {})}


def run_deterministic(cond, fn, case, cap):
    world = case.world(cap)
    try:
        items = case.build(world)
        state: dict = {}
        rows = [item_row(cond, case, 0, i, it, *fn(world, it.envelope, state)) for i, it in enumerate(items)]
        ep = {"condition": cond, "case": case.id, "rep": 0, "gate_ms": state.get("gate_ms", []),
              "llm_calls": 0, "cost_usd": 0.0, "usage": None}
        return rows, ep, None
    finally:
        world.close()


def run_llm(cond, client, case, rep, cap):
    from .llm_supplier import run_episode
    world = case.world(cap)
    try:
        items = case.build(world)
        out = run_episode(client, world, items, with_attest=(cond == "C3_llm_attest"))
        desk = out["desk"]
        rows = []
        for i, (m, it) in enumerate(zip(desk.inbox, items)):
            d = desk.decisions.get(m["message_id"])
            rows.append(item_row(cond, case, rep, i, it, d["action"] if d else None, (d or {}).get("text", ""),
                                 {"checked": m["message_id"] in desk.checks,
                                  "gate_decision": desk.checks.get(m["message_id"], {}).get("decision")}))
        ep = {"condition": cond, "case": case.id, "rep": rep, "gate_ms": out["gate_ms"], "llm_calls": out["llm_calls"],
              "cost_usd": out["cost_usd"], "usage": out["usage"], "stop": out["stop"], "wall_s": out["wall_s"],
              "tool_calls": desk.calls}
        return rows, ep, out["transcript"]
    finally:
        world.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="also run C2 (LLM, no Attest) and C3 (LLM + Attest)")
    ap.add_argument("--only-llm", action="store_true", help="run C2 and C3 only")
    ap.add_argument("--mock", action="store_true", help="scripted client instead of the API (plumbing test)")
    ap.add_argument("--reps", type=int, default=5, help="repetitions per case for the LLM conditions")
    ap.add_argument("--cases", default="", help="comma-separated case ids (default: the whole main suite)")
    ap.add_argument("--cap", type=float, default=None,
                    help="gate issuer_cap_ratio for the Attest conditions (default: gate default)")
    ap.add_argument("--workers", type=int, default=4, help="parallel LLM episodes")
    ap.add_argument("--out", default="", help="run name (default: timestamp)")
    a = ap.parse_args(argv)

    cases = [c for c in MAIN if not a.cases or c.id in a.cases.split(",")]
    run = a.out or time.strftime("%Y%m%d-%H%M%S") + ("-mock" if a.mock else "")
    out = RESULTS / run
    (out / "transcripts").mkdir(parents=True, exist_ok=True)

    conds = [] if a.only_llm else list(DETERMINISTIC)
    client = None
    if a.llm or a.only_llm:
        conds += ["C2_llm", "C3_llm_attest"]
        if a.mock:
            from .llm_supplier import MockClient
            client = MockClient()
        else:
            import anthropic
            client = anthropic.Anthropic(max_retries=6)
    from . import llm_supplier as L
    config = {"run": run, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "git": git_sha(), "conditions": conds,
              "cases": [c.id for c in cases], "llm_reps": a.reps if client else 0, "mock": a.mock,
              "issuer_cap_ratio": a.cap, "python": platform.python_version(),
              "llm": None if not client else {"model": L.MODEL, "effort": L.EFFORT, "max_tokens": L.MAX_TOKENS,
                                              "calls_per_request": L.CALLS_PER_REQUEST,
                                              "price_per_mtok_usd": L.PRICE_PER_MTOK}}
    (out / "config.json").write_text(json.dumps(config, indent=1))

    decisions, episodes = [], []
    for cond in conds:
        if cond in DETERMINISTIC:
            for case in cases:
                rows, ep, _ = run_deterministic(cond, DETERMINISTIC[cond], case, a.cap)
                decisions += rows
                episodes.append(ep)
            print(f"{cond:16} done")
            continue
        jobs = [(case, rep) for case in cases for rep in range(a.reps)]

        def one(job, cond=cond):
            case, rep = job
            for attempt in range(3):
                try:
                    return run_llm(cond, client, case, rep, a.cap)
                except Exception as e:  # noqa: BLE001 - a failed episode is recorded, the run goes on
                    err = f"{type(e).__name__}: {e}"
                    time.sleep(5 * (attempt + 1))
            return [], {"condition": cond, "case": case.id, "rep": rep, "error": err}, None

        done = 0
        with ThreadPoolExecutor(a.workers) as ex:
            for (case, rep), (rows, ep, tr) in zip(jobs, ex.map(one, jobs)):
                decisions += rows
                episodes.append(ep)
                if tr is not None:
                    (out / "transcripts" / f"{cond}_{case.id}_r{rep}.json").write_text(json.dumps(tr, indent=1, default=str))
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    spent = sum(e.get("cost_usd", 0) for e in episodes if e["condition"] == cond)
                    print(f"{cond:16} {done}/{len(jobs)} episodes, ${spent:.2f} so far", flush=True)

    with (out / "decisions.jsonl").open("w") as f:
        f.writelines(json.dumps(r) + "\n" for r in decisions)
    with (out / "episodes.jsonl").open("w") as f:
        f.writelines(json.dumps(e) + "\n" for e in episodes)
    summary = analyze.summarize(out)
    print()
    print(analyze.table(summary))
    print(f"\nwritten to {out.relative_to(ROOT.parent)}")
    errors = [e for e in episodes if e.get("error")]
    if errors:
        print(f"{len(errors)} episodes failed, first: {errors[0]['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
