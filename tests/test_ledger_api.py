"""The ledger through the gate's HTTP API, the SDK and the CLI."""
import pytest
from fastapi.testclient import TestClient

import attest.cli as cli_mod
from attest import Attest, identity
from gate.server import app
from network import ledger as L


@pytest.fixture(autouse=True)
def easy_puzzles(monkeypatch):
    monkeypatch.setattr(L, "POW_BITS", 8)
    monkeypatch.setattr(L, "REG_BITS", 6)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTEST_LEDGER", "1")
    monkeypatch.setenv("ATTEST_SANDBOX", "1")
    monkeypatch.setenv("ATTEST_DIR", str(tmp_path / "me"))
    import gate.ledger_api as api
    import gate.server as srv
    monkeypatch.setattr(api, "STATE_DIR", tmp_path / "ledger")
    api._svc = None
    srv._hits.clear()
    monkeypatch.setenv("ATTEST_RATE_WRITE", "1000")
    c = TestClient(app)
    at = Attest("http://testserver")
    at.http = c
    monkeypatch.setattr(cli_mod, "Attest", lambda url=None: at)
    return c, at


def mk(name, op):
    return identity.new_agent(name, op, "t", ["request_quote"], 1000)


def test_a_passport_and_cosigned_trades_build_a_history(world):
    c, at = world
    op = identity.new_operator("Initrode")
    seller = mk("seller", op)
    buyers = [mk(f"b{i}", op) for i in range(3)]
    for a in (seller, *buyers):
        at.register_passport(op, a)
    for i, b in enumerate(buyers):
        at.record_trade(seller, b, f"r{i}", 400)
    h = at.history(seller["pub"])
    assert (h["trades"], h["accepted"], h["volume"]) == (3, 3, 1200)
    assert at.ledger_decision(seller["pub"])["decision"] == "allow"
    assert at.ledger_info()["agents"] == 4 and at.ledger_info()["final"] >= 1
    assert at.ledger_blocks(3)[0]["height"] == at.ledger_info()["height"]


def test_forged_and_one_sided_records_are_refused(world):
    c, at = world
    op = identity.new_operator("Hooli2")
    s, b = mk("s", op), mk("b", op)
    at.register_passport(op, s)
    at.register_passport(op, b)
    one_sided = L.propose_outcome(s["priv"], s["pub"], b["pub"], "x", 100, "accepted")   # no buyer signature
    assert c.post("/ledger/records", json=one_sided).status_code == 400
    rec = L.countersign(b["priv"], one_sided)
    rec["amount"] = 9999                                   # edited after both signed
    assert c.post("/ledger/records", json=rec).status_code == 400
    assert c.post("/ledger/records", json={"type": "slash"}).status_code == 400   # internal types are not public
    good = L.countersign(b["priv"], one_sided)
    assert c.post("/ledger/records", json=good).status_code == 200
    assert c.post("/ledger/records", json=good).json()["result"] == "already on the ledger"
    dup = L.countersign(b["priv"], L.propose_outcome(s["priv"], s["pub"], b["pub"], "x", 100, "disputed"))
    assert c.post("/ledger/records", json=dup).status_code == 400                # first record wins


def test_history_survives_a_restart(world, monkeypatch):
    c, at = world
    op = identity.new_operator("Soylent")
    s, b = mk("s", op), mk("b", op)
    at.register_passport(op, s)
    at.register_passport(op, b)
    at.record_trade(s, b, "r1", 250)
    before = at.ledger_info()
    import gate.ledger_api as api
    api._svc = None                                        # a fresh process reading the saved blocks
    assert at.history(s["pub"])["volume"] == 250
    after = at.ledger_info()
    assert (after["height"], after["final"]) == (before["height"], before["final"])   # the same chain, not re-mined


def test_cli_ledger_flow(world, capsys):
    c, at = world
    identity.new_operator("Wonka")
    for n in ("s", "b"):
        cli_mod.main(["agent", "create", n, "--operator", "Wonka"])
        cli_mod.main(["passport", "create", n])
    assert cli_mod.main(["trade", "s", "b", "req1", "300"]) == 0
    assert cli_mod.main(["trade", "s", "b", "req2", "300", "--disputed"]) == 0
    capsys.readouterr()
    assert cli_mod.main(["history", "s"]) == 0
    assert "2 trades, 1 accepted, 1 disputed" in capsys.readouterr().out
    assert cli_mod.main(["ledger"]) == 0


def test_the_ledger_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("ATTEST_LEDGER", raising=False)
    assert TestClient(app).get("/ledger/info").status_code == 404
