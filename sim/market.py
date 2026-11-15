"""A small market that runs by itself: agents shop at stores, Attest is the bouncer.

    ATTEST_URL=http://127.0.0.1:8414 python sim/market.py --rounds 120 --pace 3 [--cast cast.json]

Every agent and every shop is its own program with its own keys. Nothing is scripted except who behaves how:
each round an agent picks a shop and a task, signs the request, and the shop checks it with the real gate
(signed check, real stake, real limits, real ledger). Finished deals are signed by both sides and go on the
ledger, so shops that look at track records start turning away agents that keep getting disputes.

The cast (agents and shops) comes from --cast, else <ATTEST_LOG_DIR>/market_cast.json (the page edits it),
else sim/default_cast.json. Play, pause, one step at a time and speed come from
<ATTEST_LOG_DIR>/market_control.json. Events go to <ATTEST_LOG_DIR>/market.jsonl and full records to
<ATTEST_LOG_DIR>/market/<n>.json. The gate must run with ATTEST_SANDBOX=1 and ATTEST_LEDGER=1.

Behaviours:
  honest    does what it says; deals end well
  shady     deals often end disputed (70%)
  splitter  keeps asking for half its limit, to dodge the limit by splitting
  big       large amounts, mostly at shops where a person approves big amounts
  revoked   halfway through, its backer cancels its ID
  greedy    a helper made by another agent (`parent`) and given more power than that agent has
  impostor  no ID at all
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ATTEST_DIR", tempfile.mkdtemp(prefix="attest-market-"))

from attest import Attest, build_request, identity  # noqa: E402
from gate import crypto  # noqa: E402
from gate.core import LOG_DIR, solve_pow  # noqa: E402

BEHAVIOURS = ("honest", "shady", "splitter", "big", "revoked", "greedy", "impostor")
LOOKING = ("request_quote", "search")


def load_cast(path: str | None) -> dict:
    for p in ([Path(path)] if path else []) + [LOG_DIR / "market_cast.json", ROOT / "sim" / "default_cast.json"]:
        if p.exists():
            return json.loads(p.read_text())
    raise SystemExit("no cast found")


class Market:
    def __init__(self, url: str, pace: float, seed: int, cast: dict):
        self.at = Attest(url)
        self.pace = pace
        self.rng = random.Random(seed)
        self.cast = cast
        self.log = LOG_DIR / "market.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("")
        self.details = self.log.parent / "market"
        self.details.mkdir(exist_ok=True)
        for old in self.details.glob("*.json"):
            old.unlink()
        self.control = LOG_DIR / "market_control.json"
        self.run_id = os.environ.get("ATTEST_MARKET_RUN") or str(int(time.time()))
        self.n = 0
        self.ops, self.agents, self.meta, self.shops = {}, {}, {}, {}

    def emit(self, kind: str, record: dict | None = None, **kw):
        """Log an event; `record` (the full transaction) is kept beside it for the details view."""
        self.n += 1
        ev = {"n": self.n, "run": self.run_id, "t": time.time(), "kind": kind, **kw, "has_detail": record is not None}
        if record is not None:
            (self.details / f"{self.n}.json").write_text(json.dumps({"event": ev, **record}))
        with self.log.open("a") as f:
            f.write(json.dumps(ev) + "\n")

    # ---- setting up ------------------------------------------------------------------------
    def backer(self, name: str, kind: str, money: bool, tag: str) -> dict:
        if name not in self.ops:
            op = identity.new_operator(f"{name} {tag}", backer_kind=kind)
            if money:
                self.at.faucet(op)
            self.ops[name] = op
        return self.ops[name]

    def setup(self):
        tag = str(int(time.time()))[-5:]                          # names must be new on a shared sandbox
        agents = sorted(self.cast["agents"], key=lambda a: a.get("behaviour") == "greedy")   # parents first
        for c in agents:
            key, how = c["key"], c.get("behaviour", "honest")
            acts = [x for x in c.get("actions", []) if x] or ["request_quote"]
            if how == "impostor" or not c.get("backer"):
                priv, pub = crypto.keygen()
                a = {"name": f"{key}-{tag}", "priv": priv, "pub": pub, "credential": None, "chain": []}
                how = "impostor"
            elif how == "greedy" and c.get("parent") in self.agents:
                a = identity.delegate(self.agents[c["parent"]], f"{key}-{tag}", acts, c.get("limit", 1000))
            else:
                if how == "greedy":
                    how = "honest"                                # no parent to be greedier than
                op = self.backer(c["backer"], c.get("backer_kind", "company"), c.get("money", True), tag)
                a = identity.new_agent(f"{key}-{tag}", op, c.get("about", "market agent"), acts, c.get("limit", 1000))
            self.agents[key] = a
            self.meta[key] = {**c, "behaviour": how, "actions": acts, "pub": a["pub"]}
            if a.get("credential") and c.get("backer") in self.ops:
                try:
                    self.at.register_passport(self.ops[c["backer"]], a)
                except Exception:
                    pass
        for c in self.cast["shops"]:
            r = identity.new_receiver(f"{c['key']}-{tag}")
            human = c.get("human_above")
            self.at.register_receiver(r, float(c.get("min_stake") or 0), {"*": human} if human else None,
                                      c.get("free_actions") or None)
            self.shops[c["key"]] = {**c, "id": r}
            self._self_passport(r)
        self.emit("setup", agents=[self.meta[k] for k in self.agents],
                  shops=[{**{k: v for k, v in s.items() if k != "id"}, "pub": s["id"]["pub"]}
                         for s in self.shops.values()])

    def _self_passport(self, r: dict):
        from network import ledger as L
        self.at.submit_record(L.make_passport(r["priv"], r["pub"], r["priv"], r["pub"], r["name"], ["receive"], 0))

    # ---- one round ----------------------------------------------------------------------------
    def task_for(self, key: str) -> tuple[str, float]:
        m, r = self.meta[key], self.rng
        lo, hi = (list(m.get("amount") or [20, 400]) + [400])[:2]
        amount = float(round(r.uniform(float(lo), float(hi)) / 10) * 10) or float(lo)
        acts = m["actions"]
        paying = [a for a in acts if a not in LOOKING] or acts
        if m["behaviour"] == "splitter":
            return paying[0], float(m.get("limit", 900)) / 2
        if m["behaviour"] in ("big", "greedy"):
            return r.choice(paying), amount
        return r.choice(acts), amount

    def round(self, i: int):
        if i == self.revoke_at:
            for k in [k for k in self.agents if self.meta[k]["behaviour"] == "revoked"]:
                self._revoke(k)
        key = self.rng.choice(list(self.agents))
        shops = list(self.shops)
        if self.meta[key]["behaviour"] == "big" and self.rng.random() < 0.7:
            shops = [s for s in shops if self.shops[s].get("human_above")] or shops
        shop_key = self.rng.choice(shops)
        agent, shop = self.agents[key], self.shops[shop_key]
        action, amount = self.task_for(key)
        ev = {"round": i, "agent": key, "shop": shop_key, "action": action, "amount": amount}

        if shop.get("checks_record"):
            try:
                d = self.at.ledger_decision(agent["pub"])
            except Exception:
                d = {"decision": "unknown", "why": ""}
            if d["decision"] == "refuse" and "disputed" in d.get("why", ""):
                self.emit("deal", record={"ledger_decision": d, "agent_pub": agent["pub"]}, **ev,
                          verdict="refuse", reason="bad_track_record",
                          plain=f"its record is bad ({d['why']}), so the shop will not deal with it")
                return

        env = build_request(agent, shop["id"]["name"], action, amount, f"round {i}")
        try:
            out = self.at.check(shop["id"]["name"], env, shop["id"]).raw
            if out["reason"] == "pow_required":                 # no money behind it: it does the puzzle
                ch = out["challenge"]
                nonce, tries = solve_pow(ch["seed"], ch["bits"])
                env = {**env, "pow": {"challenge_id": ch["challenge_id"], "nonce": nonce}}
                out = self.at.check(shop["id"]["name"], env, shop["id"]).raw
                ev["pow"] = {"bits": ch["bits"], "tries": tries}
        except Exception as e:
            self.emit("deal", record={"envelope": env, "error": str(e)}, **ev, verdict="refuse", reason="error",
                      plain=str(e)[:160])
            return
        verdict = {"allow": "allow", "escalate": "wait"}.get(out["decision"],
                                                             "allow" if out["reason"] == "pow_ok" else "refuse")
        ev.update(verdict=verdict, reason=out["reason"], detail=out.get("detail", ""))
        full = {"envelope": env, "gate_answer": {k: v for k, v in out.items() if k != "challenge"},
                "shop": shop["id"]["name"], "agent_pub": agent["pub"]}

        free = action in (shop.get("free_actions") or []) or out["reason"] == "pow_ok"
        if verdict == "allow" and not free and self.meta[key]["behaviour"] != "greedy":
            bad = self.meta[key]["behaviour"] == "shady" and self.rng.random() < 0.7
            status = "disputed" if bad else "accepted"
            try:
                rec = self.at.record_trade(shop["id"], agent, env["request"]["id"], amount, status)
                ev["outcome"] = status
                full["ledger_record_id"] = rec.get("id")
            except Exception as e:
                ev["outcome_error"] = str(e)[:120]
        self.emit("deal", record=full, **ev)

    def _revoke(self, key: str):
        a, op = self.agents[key], self.ops.get(self.meta[key].get("backer"))
        if not a.get("credential") or not op:
            return
        body = {"revoke": a["credential"]["id"], "by": op["pub"], "reason": "agent decommissioned"}
        self.at.http.post("/revoke", json={"document": a["credential"], "by": op["pub"],
                                            "sig": crypto.sign(op["priv"], body), "reason": body["reason"]})
        self.emit("revoked", agent=key, backer=self.meta[key]["backer"],
                  plain=f"{self.meta[key]['backer']} cancels {self.meta[key]['name']}'s ID")

    # ---- play, pause, one step at a time, speed ------------------------------------------------
    def wait_turn(self, done: int) -> bool:
        """Play: wait `pace` seconds. Pause: wait. Step: wait until the page grants one more round."""
        while True:
            try:
                c = json.loads(self.control.read_text())
            except (OSError, ValueError):
                c = {"mode": "play"}
            if c.get("mode") == "stop":
                return False
            if c.get("mode", "play") == "play":
                time.sleep(float(c.get("pace", self.pace)))
                return True
            if int(c.get("granted", 0)) > done:
                return True
            time.sleep(0.2)

    def run(self, rounds: int):
        self.revoke_at = rounds // 2
        self.setup()
        for i in range(1, rounds + 1):
            if not self.wait_turn(i - 1):
                break
            self.round(i)
        self.emit("done", rounds=rounds)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=120)
    p.add_argument("--pace", type=float, default=3.0, help="seconds between rounds when playing")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cast", help="a cast file (agents and shops); see sim/default_cast.json")
    a = p.parse_args()
    Market(os.environ.get("ATTEST_URL", "http://127.0.0.1:8402"), a.pace, a.seed, load_cast(a.cast)).run(a.rounds)
