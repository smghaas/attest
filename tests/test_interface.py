"""The human and agent interfaces: SDK, CLI, dashboard, demo mode and MCP tools."""
import json
from pathlib import Path

import conftest

_tmp = conftest.TMP
from fastapi.testclient import TestClient  # noqa: E402

from attest import Attest, build_request  # noqa: E402
from attest.cli import main as cli  # noqa: E402
from gate.server import app  # noqa: E402

client = TestClient(app)


def agent(name):
    return json.loads((Path(_tmp) / "state" / "agents" / f"{name}.json").read_text())


def sdk():
    at = Attest("http://testserver")
    at.http = client
    return at


def test_sdk_allows_a_staked_agent_and_verifies_the_receipt():
    d = sdk().check("supplierco", build_request(agent("acme-procurement"), "supplierco", "request_quote", 2000))
    assert d.allowed and d.receipt_verified


def test_sdk_refuses_the_greedy_sub_agent_and_the_unstaked_impostor():
    at = sdk()
    greedy = at.check("supplierco", build_request(agent("acme-sub-greedy"), "supplierco", "place_order", 12000))
    assert greedy.decision == "refuse" and greedy.receipt_verified
    imp = at.check("supplierco", build_request(agent("impostor"), "supplierco", "place_order", 100))
    assert imp.decision in ("refuse", "unknown")


def test_a_tampered_receipt_does_not_verify():
    at = sdk()
    d = at.check("supplierco", build_request(agent("acme-procurement"), "supplierco", "request_quote", 10))
    forged = dict(d.receipt, decision="allow", amount=1)
    assert at.verify_receipt(d.receipt) and not at.verify_receipt(forged)


def test_large_order_escalates_to_a_human():
    d = sdk().check("supplierco", build_request(agent("acme-procurement"), "supplierco", "place_order", 9000))
    assert d.decision == "escalate"


def test_dashboard_and_stats_are_served():
    page = client.get("/")
    assert page.status_code == 200 and "Attest" in page.text
    s = client.get("/stats").json()
    assert s["decisions"] >= 1 and s["demo"] is True and s["issuers"] >= 2


def test_demo_mode_signs_on_behalf_of_a_demo_agent_and_never_leaks_keys():
    assert "acme-procurement" in client.get("/demo/agents").json()
    r = client.post("/demo/try", json={"agent": "acme-procurement", "action": "request_quote", "amount": 50})
    assert r.status_code == 200 and r.json()["decision"] == "allow"
    assert agent("acme-procurement")["priv"] not in r.text
    assert client.post("/demo/try", json={"agent": "op-AcmeCorp"}).status_code == 404   # operator keys are not agents


def test_cli_check_exit_codes(tmp_path, monkeypatch, capsys):
    import attest.cli as c
    monkeypatch.setattr(c, "Attest", lambda url=None: sdk())
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps(build_request(agent("acme-procurement"), "supplierco", "request_quote", 5)))
    bad.write_text(json.dumps(build_request(agent("acme-sub-greedy"), "supplierco", "place_order", 12000)))
    assert cli(["check", "supplierco", str(ok)]) == 0
    assert cli(["check", "supplierco", str(bad)]) == 2
    assert "REFUSE" in capsys.readouterr().out
    assert cli(["receipts", "-n", "3"]) == 0


def test_mcp_tools_return_a_decision_an_agent_can_act_on(monkeypatch):
    mcp_server = __import__("attest.mcp_server", fromlist=["x"])
    monkeypatch.setattr(mcp_server, "_at", sdk())
    out = mcp_server.check_agent("supplierco", build_request(agent("acme-procurement"), "supplierco",
                                                               "request_quote", 5))
    assert out["decision"] == "allow" and out["receipt_verified"] is True
    assert mcp_server.list_receivers()["supplierco"]
