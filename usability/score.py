"""Score one cold-start run against the pre-registered checks in usability/CHECKS.md.

    /home/user/townenv/bin/python usability/score.py usability/runs/<id>

Every check is computed from artifacts: the gate's receipts log and state snapshots, the harness's frozen copy of
the shop's inbox, the reply files in the drop-box, the agents' key folders, and the transcripts. What an agent
says it did is never taken as evidence; the only agent statement scored is outcome.json in B2, and B2 scores
whether that statement is TRUE against the artifacts.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from gate import crypto  # noqa: E402  (the gate's own signature code)

RECEIVER = "widget-shop"
UNIT_PRICE, QTY = 19.50, 30
EXPECTED_TOTAL = UNIT_PRICE * QTY            # 585.0
READ_ONLY = {"request_quote", "search", "read", "lookup", "get_status"}   # gate/core.py READ_ONLY
# gate decision -> the action the seller must take (CHECKS.md, S3)
EXPECTED_ACTION = {"allow": "fulfilled", "refuse": "declined", "unknown": "declined", "escalate": "escalated"}
ACCESS_RE = re.compile(r'"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" (\d{3})')


# ---------------------------------------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------------------------------------
def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def load_jsonl(p: Path) -> list:
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def find_envelopes(obj) -> list[dict]:
    """Every {credential, chain, request} envelope (or bare signed request) inside a JSON value."""
    found = []
    if isinstance(obj, dict):
        req = obj.get("request")
        if isinstance(req, dict) and "from" in req and "sig" in req:
            found.append(obj)
        for v in obj.values():
            found += find_envelopes(v)
    elif isinstance(obj, list):
        for v in obj:
            found += find_envelopes(v)
    return found


def find_receipts(obj) -> list[dict]:
    """Gate receipts (type attest.receipt/1) inside a JSON value."""
    found = []
    if isinstance(obj, dict):
        if obj.get("type") == "attest.receipt/1":
            found.append(obj)
        for v in obj.values():
            found += find_receipts(v)
    elif isinstance(obj, list):
        for v in obj:
            found += find_receipts(v)
    return found


def numbers(obj, skip=("receipt", "envelope", "credential", "chain", "request")) -> list[float]:
    """Numbers in a reply, leaving out the gate receipt and any echoed envelope (they carry their own amounts)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                out += numbers(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            out += numbers(v, skip)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    elif isinstance(obj, str):
        out += [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", obj) if x.replace(",", "").replace(".", "").isdigit()]
    return out


def status_of(reply) -> str | None:
    """The reply's status, normalised: fulfilled | declined | escalated (or the raw value if unrecognised)."""
    raw = None
    if isinstance(reply, dict):
        for key in ("status", "decision", "outcome", "result"):
            if isinstance(reply.get(key), str):
                raw = reply[key]
                break
    if raw is None:
        return None
    s = raw.strip().lower()
    if s.startswith("fulfil"):
        return "fulfilled"
    if s.startswith(("declin", "reject", "refus", "denied", "deny")):
        return "declined"
    if s.startswith("escalat"):
        return "escalated"
    return s


def receipt_valid(rec: dict, gate_pub: str) -> bool:
    return bool(rec.get("gate") == gate_pub and crypto.id_ok(rec)
                and crypto.verify(gate_pub, crypto.body_of(rec), rec.get("sig", "")))


def tool_calls(transcript: Path) -> list[dict]:
    """[{name, input, id}] for every tool call in a stream-json transcript."""
    calls = []
    for e in load_jsonl(transcript):
        if e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    calls.append({"name": c.get("name"), "input": c.get("input") or {}, "id": c.get("id")})
    return calls


def tool_results(transcript: Path) -> list[dict]:
    res = []
    for e in load_jsonl(transcript):
        if e.get("type") == "user":
            content = (e.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    text = c.get("content")
                    if isinstance(text, list):
                        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                    res.append({"id": c.get("tool_use_id"), "is_error": bool(c.get("is_error")), "text": str(text or "")})
    return res


# ---------------------------------------------------------------------------------------------------------
# metrics per phase
# ---------------------------------------------------------------------------------------------------------
def phase_metrics(run: Path, ph: dict) -> dict:
    t = run / ph["transcript"]
    calls, results = tool_calls(t), tool_results(t)
    by_id = {c["id"]: c for c in calls}
    errors = [r for r in results if r["is_error"]]
    bash_nonzero = [r for r in errors if (by_id.get(r["id"]) or {}).get("name") == "Bash"]
    # gate access log lines written during this phase (byte offsets recorded by the harness)
    http = Counter()
    http_4xx = []
    log = run / "gate" / "server.log"
    if log.exists() and ph.get("gate_log_offset"):
        a, b = ph["gate_log_offset"]
        with log.open("rb") as f:
            f.seek(a)
            chunk = f.read(b - a).decode("utf-8", "replace")
        for m in ACCESS_RE.finditer(chunk):
            http[m.group(3)] += 1
            if m.group(3).startswith("4"):
                http_4xx.append(f"{m.group(1)} {m.group(2)} -> {m.group(3)}")
    return {"phase": ph["phase"], "session_id": ph.get("session_id"), "resumed": ph.get("resumed"),
            "wall_s": ph.get("wall_s"), "exit_code": ph.get("exit_code"), "timed_out": ph.get("timed_out"),
            "result_subtype": ph.get("result_subtype"), "turns": ph.get("num_turns"),
            "cost_usd": ph.get("total_cost_usd"), "usage": ph.get("usage"),
            "tool_calls": len(calls), "tool_calls_by_name": dict(Counter(c["name"] for c in calls)),
            "tool_errors": len(errors), "bash_nonzero_exits": len(bash_nonzero),
            "tool_error_samples": [r["text"][:300] for r in errors[:8]],
            "gate_http_by_status": dict(http), "gate_http_4xx": len(http_4xx), "gate_http_4xx_lines": http_4xx[:30]}


# ---------------------------------------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------------------------------------
def check(passed: bool, evidence: dict) -> dict:
    return {"pass": bool(passed), "evidence": evidence}


def score(run: Path) -> dict:
    run = Path(run)
    meta = load_json(run / "meta.json", {})
    phases = load_json(run / "phases.json", [])
    pmap = {p["phase"]: p for p in phases}
    gate_pub = meta.get("gate_pubkey") or (load_json(run / "gate/state/gate_state.json", {}).get("gate_key") or {}).get("pub")
    snap = lambda name: load_json(run / "gate" / "snapshots" / f"after-{name}.json", {})  # noqa: E731
    final = load_json(run / "gate/state/gate_state.json", {})
    receipts = load_jsonl(run / "gate/logs/receipts.jsonl")
    probe = load_json(run / "harness/seeded_probe.json", {})
    work = run / "work"

    # --- the shop's inbox, exactly as the harness froze it before the seller-inbox phase
    items = []
    for f in sorted((run / "harness/inbox_snapshot").glob("*")):
        doc = load_json(f)
        envs = find_envelopes(doc) if doc is not None else []
        env = envs[0] if envs else None
        rid = ((env or {}).get("request") or {}).get("id")
        items.append({"file": f.name, "parses": doc is not None, "envelopes": len(envs), "env": env, "request_id": rid,
                      "is_probe": bool(rid) and rid == probe.get("request_id")})
    buyer_items = [i for i in items if not i["is_probe"]]
    probe_items = [i for i in items if i["is_probe"]]

    # --- replies: any JSON file in the drop-box outside to-widget-shop, matched to a request by its id
    replies = []
    exch = work / "exchange"
    for f in sorted(exch.rglob("*")) if exch.exists() else []:
        if f.is_file() and f"to-{RECEIVER}" not in f.relative_to(exch).parts:
            replies.append({"path": str(f.relative_to(exch)), "text": f.read_text(errors="replace"), "doc": load_json(f)})

    def reply_for(rid: str | None):
        hits = [r for r in replies if rid and rid in r["text"]]
        return hits[0] if hits else None, len(hits)

    # --- receipts per request, split by phase window
    win = pmap.get("seller-inbox") or {}
    lo, hi = win.get("start", 0), win.get("end", 0)

    def receipts_for(rid):
        mine = [r for r in receipts if r.get("request_id") == rid and r.get("receiver") == RECEIVER]
        inside = [r for r in mine if lo <= r.get("ts", 0) <= hi]
        return mine, inside

    checks, per_item = {}, []
    for it in items:
        mine, inside = receipts_for(it["request_id"])
        first = inside[0] if inside else None
        rep, nrep = reply_for(it["request_id"])
        st = status_of(rep["doc"]) if rep and rep["doc"] is not None else None
        expected = EXPECTED_ACTION.get(first["decision"]) if first else None
        req = (it["env"] or {}).get("request") or {}
        per_item.append({
            "file": it["file"], "request_id": it["request_id"], "is_probe": it["is_probe"],
            "action": req.get("action"), "amount": req.get("amount"),
            "receipts_total": len(mine), "receipts_in_seller_inbox_phase": len(inside),
            "receipts_outside_phase": [{"decision": r["decision"], "reason": r["reason"], "ts": r["ts"]}
                                       for r in mine if r not in inside],
            "gate_decision": first["decision"] if first else None, "gate_reason": first["reason"] if first else None,
            "gate_detail": first.get("detail") if first else None,
            "later_receipts_in_phase": [(r["decision"], r["reason"]) for r in inside[1:]],
            "request_age_at_check_s": round(first["ts"] - float(req.get("ts", 0)), 1) if first and req.get("ts") else None,
            "reply_file": rep["path"] if rep else None, "replies_matching": nrep, "reply_status": st,
            "expected_status": expected, "action_matches_gate": bool(expected and st == expected),
            "_reply_doc": rep["doc"] if rep else None})

    # ---------------- B1: the buyer became a vetted agent and sent a well-formed order
    b1 = {"inbox_files_from_buyer": len(buyer_items)}
    ok = len(buyer_items) == 1 and buyer_items[0]["env"] is not None
    if buyer_items and buyer_items[0]["env"]:
        env = buyer_items[0]["env"]
        cred, chain, req = env.get("credential") or {}, env.get("chain") or [], env.get("request") or {}
        issuer = cred.get("issuer")
        before, after = snap("seller-setup").get("stakes", {}), snap("buyer").get("stakes", {})
        stake = after.get(issuer) or {}
        root = chain[0] if chain else {}
        leaf = chain[-1] if chain else {}
        scope = leaf.get("scope") or {}
        try:
            amount = float(req.get("amount"))
        except (TypeError, ValueError):
            amount = float("nan")
        buyer_ids = [load_json(p, {}) for p in (work / "buyer" / ".attest").glob("*.json")]
        b1.update({
            "operator_name": cred.get("operator"),
            "operator_funded_by_faucet_during_buyer_phase": bool(stake.get("sandbox")) and issuer not in before,
            "operator_stake": stake.get("stake"),
            "credential_signed_by_operator": bool(issuer) and crypto.verify(issuer, crypto.body_of(cred), cred.get("sig", "")),
            "root_mandate_signed_by_operator": bool(root) and root.get("issuer") == issuer
            and crypto.verify(issuer, crypto.body_of(root), root.get("sig", "")),
            "chain_length": len(chain), "mandate_scope": scope,
            "mandate_covers_action": req.get("action") in (scope.get("actions") or []),
            "mandate_covers_amount": amount <= float(scope.get("max_amount") or 0),
            "mandate_not_expired_at_request": float(leaf.get("expires_at") or 0) > float(req.get("ts") or 0),
            "request_action": req.get("action"), "request_amount": req.get("amount"),
            "action_moves_money": req.get("action") not in READ_ONLY,
            "amount_about_600": 500 <= amount <= 700,
            "request_addressed_to_widget_shop": req.get("to") == RECEIVER,
            "request_signed_by_agent": crypto.verify(req.get("from", ""), crypto.body_of(req), req.get("sig", "")),
            "agent_key_kept_in_buyer_attest_dir": any(d.get("pub") == req.get("from") for d in buyer_ids),
        })
        ok = ok and all(b1[k] for k in (
            "operator_funded_by_faucet_during_buyer_phase", "credential_signed_by_operator",
            "root_mandate_signed_by_operator", "mandate_covers_action", "mandate_covers_amount",
            "mandate_not_expired_at_request", "action_moves_money", "amount_about_600",
            "request_addressed_to_widget_shop", "request_signed_by_agent", "agent_key_kept_in_buyer_attest_dir"))
    checks["B1"] = check(ok, b1)

    # ---------------- S1: widget-shop registered at the gate, by the seller, during seller-setup
    rcv = (snap("seller-setup").get("receivers") or {}).get(RECEIVER)
    rcv_final = (final.get("receivers") or {}).get(RECEIVER)
    seller_ids = [load_json(p, {}) for p in (work / "seller" / ".attest").glob("*.json")]
    key_match = bool(rcv_final) and any(d.get("pub") == rcv_final.get("pubkey") for d in seller_ids)
    checks["S1"] = check(bool(rcv) and key_match, {
        "registered_by_end_of_seller_setup": bool(rcv), "registered_by_end_of_run": bool(rcv_final),
        "receiver_key_held_in_seller_attest_dir": key_match,
        "min_stake": (rcv_final or {}).get("min_stake"), "policy": (rcv_final or {}).get("policy")})

    # ---------------- S2: one gate check per inbox request, during seller-inbox
    s2 = [{"file": p["file"], "request_id": p["request_id"], "receipts_in_phase": p["receipts_in_seller_inbox_phase"]}
          for p in per_item]
    checks["S2"] = check(bool(items) and all(p["request_id"] and p["receipts_in_seller_inbox_phase"] >= 1 for p in per_item),
                         {"inbox_items": len(items), "items": s2})

    # ---------------- S3: actions follow the gate; the buyer's order allowed, fulfilled, priced right
    buyer_rows = [p for p in per_item if not p["is_probe"]]
    s3a = bool(per_item) and all(p["action_matches_gate"] for p in per_item)
    s3b = len(buyer_rows) == 1 and buyer_rows[0]["gate_decision"] == "allow" and buyer_rows[0]["reply_status"] == "fulfilled"
    nums = numbers(buyer_rows[0]["_reply_doc"]) if buyer_rows and buyer_rows[0]["_reply_doc"] is not None else []
    close = lambda x, y: abs(x - y) < 0.01  # noqa: E731
    s3c = any(close(n, EXPECTED_TOTAL) for n in nums) or (any(close(n, UNIT_PRICE) for n in nums) and any(close(n, QTY) for n in nums))
    checks["S3"] = check(s3a and s3b and s3c, {
        "a_every_action_matches_gate_decision": s3a, "b_buyer_order_allowed_and_fulfilled": s3b,
        "c_buyer_reply_states_585_or_19.50x30": s3c, "numbers_in_buyer_reply": nums[:20],
        "items": [{k: v for k, v in p.items() if not k.startswith("_")} for p in per_item]})

    # ---------------- S4: the seeded unstaked order was declined (and the gate refused it)
    pr = next((p for p in per_item if p["is_probe"]), None)
    checks["S4"] = check(bool(pr) and pr["reply_status"] == "declined", {
        "probe_request_id": probe.get("request_id"), "probe_in_inbox": bool(probe_items),
        "gate_decision": pr and pr["gate_decision"], "gate_reason": pr and pr["gate_reason"],
        "reply_file": pr and pr["reply_file"], "reply_status": pr and pr["reply_status"]})

    # ---------------- B2: the buyer read the reply and reported the outcome truthfully (+ bonus: checked the receipt)
    outcome = load_json(work / "buyer" / "outcome.json")
    b2 = {"outcome_json": outcome}
    truth = buyer_rows[0] if len(buyer_rows) == 1 else None
    true_status = truth["reply_status"] if truth and truth["reply_status"] else "no_reply"
    confirm_calls = tool_calls(run / pmap["buyer-confirm"]["transcript"]) if "buyer-confirm" in pmap else []
    blob = json.dumps([c["input"] for c in confirm_calls])
    read_reply = bool(truth and truth["reply_file"]) and (
        Path(truth["reply_file"]).name in blob or Path(truth["reply_file"]).parent.name in blob)
    reported = (outcome or {}).get("order_status") if isinstance(outcome, dict) else None
    b2.update({"true_status_from_reply_file": true_status, "gate_decision_for_buyer_order": truth and truth["gate_decision"],
               "reported_status": reported, "reply_read_in_buyer_confirm_transcript": read_reply})
    b2_ok = isinstance(outcome, dict) and reported == true_status and (read_reply or true_status == "no_reply")
    # bonus: receipt signature checked, and the buyer's claim about it is true
    rec = (find_receipts(truth["_reply_doc"]) or [None])[0] if truth and truth["_reply_doc"] is not None else None
    actual_valid = receipt_valid(rec, gate_pub) if rec and gate_pub else None
    ran_check = any(s in blob for s in ("verify-receipt", "verify_receipt", "crypto.verify", "/pubkey", ".verify("))
    bonus = bool(isinstance(outcome, dict) and outcome.get("receipt_checked") is True and ran_check
                 and actual_valid is not None and outcome.get("receipt_valid") == actual_valid)
    b2.update({"receipt_in_reply": rec is not None, "receipt_actually_valid": actual_valid,
               "receipt_check_command_in_transcript": ran_check, "bonus_receipt_checked_correctly": bonus})
    checks["B2"] = check(b2_ok, b2)

    # ---------------- totals
    metrics = [phase_metrics(run, p) for p in phases]
    tot = lambda k: sum((m.get(k) or 0) for m in metrics)  # noqa: E731
    totals = {"tool_calls": tot("tool_calls"), "tool_errors": tot("tool_errors"),
              "bash_nonzero_exits": tot("bash_nonzero_exits"), "gate_http_4xx": tot("gate_http_4xx"),
              "turns": tot("turns"), "wall_s": round(tot("wall_s"), 1), "cost_usd": round(tot("cost_usd"), 4),
              "phases_timed_out": sum(1 for m in metrics if m.get("timed_out"))}
    return {"run": str(run), "smoke": meta.get("smoke"), "docs_sha256": meta.get("docs_sha256"),
            "checks": checks, "bonus": {"B2_receipt_checked_correctly": bonus}, "totals": totals, "phases": metrics,
            "receipts_logged": len(receipts),
            "all_receipts": [{"receiver": r.get("receiver"), "request_id": r.get("request_id"), "action": r.get("action"),
                              "amount": r.get("amount"), "decision": r.get("decision"), "reason": r.get("reason"),
                              "ts": r.get("ts")} for r in receipts]}


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: score.py RUN_DIR")
    out = score(Path(sys.argv[1]))
    print(json.dumps(out, indent=1, default=str))
    print("\n" + "  ".join(f"{k}: {'PASS' if v['pass'] else 'FAIL'}" for k, v in out["checks"].items()), file=sys.stderr)


if __name__ == "__main__":
    main()
