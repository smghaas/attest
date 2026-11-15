"""Turn recorded NANDA Town runs into the data behind the gate's /static/town/ page.

    python integrations/nandatown/show/make_showcase.py RUN_DIR:RECEIPTS:SLUG ...

RUN_DIR is a Town run bundle (events.jsonl, result.json, town.html). RECEIPTS is the gate's receipts.jsonl from
the same test, or '-' if the gate never signed one. Each run becomes a sequence of steps: what the buyer and
seller sent each other through Town, what the seller asked the gate and the gate's answer, and what the buyer
noted. Nothing is written by hand: the steps come from the recorded events and the gate's signed receipts.
Town's raw viewer for each run is copied next to it as proof.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[3] / "gate" / "static" / "town"
STORIES = {
    "stock": ("A robot with no ID",
              "An ordinary NANDA Town buyer asks the shop for a price. It has no ID. Attest says no, and the shop "
              "tells it where to get one."),
    "stranger": ("A stranger figures it out alone",
                 "This buyer was told nothing. It reads the shop's 'no', finds Attest by itself, does the small "
                 "puzzle that strangers must do, and gets the public price."),
    "quote": ("A company that signed up by itself",
              "An outside company signed up on the Attest website, got test credit and made its own robot. "
              "Nobody from our team helped. In NANDA Town, its robot gets served."),
    "order": ("A big order needs a human",
              "The same robot orders $1,800 of goods. The shop set a rule: above $1,500, a person must approve. "
              "Attest enforces it, so the shop does not act alone."),
}


def steps_for(run: Path, receipts: dict) -> list[dict]:
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]
    acks = {(e["observer"], e["subject"]): e["detail"].get("note") or {}
            for e in events if e["kind"] == "ack_recorded"}
    out = []
    for e in events:
        if e["kind"] != "message_accepted":
            continue
        d = e["detail"]
        reply_to = d.get("request_id")
        if d["sender"] == "seller" and reply_to:
            note = acks.get(("seller", reply_to), {})
            if "attest_decision" in note:
                rec = receipts.get(note.get("attest_receipt_id") or "", {})
                out.append({"from": "seller", "to": "gate", "kind": "check",
                            "decision": note["attest_decision"], "reason": note.get("attest_reason", ""),
                            "receipt": (note.get("attest_receipt_id") or "")[:12],
                            "action": rec.get("action"), "amount": rec.get("amount"),
                            "hint_sent": note.get("attest_hint_sent", False)})
        out.append({"from": d["sender"], "to": d["to"], "kind": d["kind"], "id": e["subject"]})
        if d["sender"] == "seller":
            note = acks.get(("buyer", e["subject"]), {})
            keep = {k: v for k, v in note.items()
                    if k in ("learned_gate", "learned_receiver", "pow_solved_bits", "correct", "total_cents",
                             "attest_receipt_verified", "reason", "attest_decision")}
            if keep:
                out.append({"from": "buyer", "to": "buyer", "kind": "note", **keep})
    return out


def main(specs: list[str]):
    OUT.mkdir(parents=True, exist_ok=True)
    runs = []
    for spec in specs:
        run_dir, rec_file, slug = spec.split(":")
        run = Path(run_dir)
        receipts = {}
        if rec_file != "-":
            for line in Path(rec_file).read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    receipts[r["id"]] = r
        result = json.loads((run / "result.json").read_text())
        title, story = STORIES[slug]
        shutil.copy(run / "town.html", OUT / f"{slug}.html")
        runs.append({"slug": slug, "title": title, "story": story, "run_id": result["run_id"],
                     "town_stages": {s["name"]: s["status"] for s in result["stages"]},
                     "steps": steps_for(run, receipts), "viewer": f"{slug}.html"})
    (OUT / "runs.json").write_text(json.dumps(runs, indent=1))
    print(f"wrote {OUT / 'runs.json'} with {len(runs)} runs")


if __name__ == "__main__":
    main(sys.argv[1:])
