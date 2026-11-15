"""A stranger with no access to the gate's files uses the public sandbox end to end."""
import json

import pytest
from fastapi.testclient import TestClient

import attest.cli as cli_mod
from attest import Attest, identity
from gate import crypto
from gate.server import app


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTEST_SANDBOX", "1")
    monkeypatch.setenv("ATTEST_DIR", str(tmp_path / "me"))
    import gate.server as srv
    srv._hits.clear()
    c = TestClient(app)
    at = Attest("http://testserver")
    at.http = c
    monkeypatch.setattr(cli_mod, "Attest", lambda url=None: at)
    return c, at


def run(*argv):
    return cli_mod.main(list(argv))


def test_a_stranger_goes_from_nothing_to_an_allow_and_a_refuse(sandbox, capsys):
    c, at = sandbox
    assert at.info()["mode"] == "sandbox"
    assert run("operator", "create", "Globex") == 0
    assert run("receiver", "create", "outsider-desk", "--min-stake", "5000", "--escalate", "place_order=8000") == 0
    assert run("agent", "create", "globex-buyer", "--operator", "Globex", "--actions", "request_quote,place_order",
               "--max", "10000") == 0
    capsys.readouterr()
    assert run("request", "globex-buyer", "outsider-desk", "request_quote", "500", "--check") == 0
    assert "ALLOW" in capsys.readouterr().out
    assert run("request", "globex-buyer", "outsider-desk", "place_order", "9000", "--check") == 2   # escalate
    assert "ESCALATE" in capsys.readouterr().out
    # a sub-agent wider than its parent is refused, with a receipt that verifies
    assert run("agent", "delegate", "greedy", "--parent", "globex-buyer", "--actions", "place_order",
               "--max", "50000") == 0
    capsys.readouterr()
    assert run("request", "greedy", "outsider-desk", "place_order", "100", "--check") == 2
    out = capsys.readouterr().out
    assert "REFUSE" in out and "signature valid" in out
    assert any(d["name"] == "globex-buyer" for d in identity.ids())


def test_an_operator_that_never_took_test_stake_is_not_trusted(sandbox):
    c, at = sandbox
    run("receiver", "create", "strict-desk", "--min-stake", "1")
    op = identity.new_operator("Ghost")            # never calls the faucet
    agent = identity.new_agent("ghost-bot", op, "x", ["place_order"], 100)
    from attest import build_request
    d = at.check("strict-desk", build_request(agent, "strict-desk", "place_order", 50), identity.load("strict-desk"))
    assert d.decision == "refuse" and d.receipt_verified


def test_faucet_is_once_per_key_and_names_cannot_be_copied(sandbox):
    c, at = sandbox
    op = identity.new_operator("Initech")
    assert at.faucet(op)["stake"] == 10000
    with pytest.raises(Exception, match="already_funded"):
        at.faucet(op)
    twin = identity.new_operator("initech-twin")
    twin["name"] = "INITECH"                          # same name, different key
    with pytest.raises(Exception, match="name_taken"):
        at.faucet(twin)


def test_forged_signatures_are_rejected(sandbox):
    c, at = sandbox
    op = identity.new_operator("Hooli")
    r = c.post("/sandbox/faucet", json={"issuer": op["pub"], "name": "Hooli", "sig": "00" * 64})
    assert r.status_code == 400
    other_priv, other_pub = crypto.keygen()
    body = {"register_receiver": "mine-desk", "pubkey": other_pub, "min_stake": 0.0, "policy": {}}
    bad = c.post("/register/receiver", json={"name": "mine-desk", "pubkey": other_pub, "min_stake": 0,
                                             "policy": {}, "sig": crypto.sign(op["priv"], body)})
    assert bad.status_code == 400                     # signed with the wrong key


def test_receiver_names_are_first_come_first_served_and_cannot_hijack_the_demo(sandbox):
    c, at = sandbox
    rcv = identity.new_receiver("taken-desk")
    at.register_receiver(rcv)
    again = identity.new_receiver("taken-desk-b")
    again["name"] = "taken-desk"
    with pytest.raises(Exception, match="taken"):
        at.register_receiver(again)
    hijack = identity.new_receiver("supplierco")      # the seeded demo receiver
    with pytest.raises(Exception, match="taken"):
        at.register_receiver(hijack)


def test_the_open_stake_endpoint_and_demo_inbox_are_closed_in_sandbox(sandbox):
    c, at = sandbox
    op = identity.new_operator("Umbrella")
    body = {"stake": True, "issuer": op["pub"], "name": "Umbrella", "amount": 1e9}
    r = c.post("/stake", json={"issuer": op["pub"], "name": "Umbrella", "amount": 1e9,
                               "sig": crypto.sign(op["priv"], body)})
    assert r.status_code == 403
    assert c.get("/inbox/supplierco/next").status_code == 404
    assert c.post("/inbox/supplierco", json={}).status_code == 404


def test_rate_limits_and_size_limits(sandbox, monkeypatch):
    c, at = sandbox
    monkeypatch.setenv("ATTEST_RATE_WRITE", "3")
    codes = [c.post("/sandbox/faucet", json={"issuer": "x", "name": "x", "sig": "x"}).status_code for _ in range(5)]
    assert codes[3:] == [429, 429] and 429 not in codes[:3]
    monkeypatch.setenv("ATTEST_MAX_BODY", "100")
    r = c.post("/verify/supplierco", content=b"{" + b" " * 500 + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_a_private_gate_does_not_allow_self_registration(monkeypatch):
    monkeypatch.delenv("ATTEST_SANDBOX", raising=False)
    c = TestClient(app)
    r = c.post("/register/receiver", json={"name": "abc-desk", "pubkey": "0" * 64, "sig": "0"})
    assert r.status_code == 403
    assert c.get("/info").json()["mode"] == "private"


def test_a_failed_registration_leaves_no_key_behind(sandbox):
    c, at = sandbox
    with pytest.raises(SystemExit):
        run("receiver", "create", "supplierco")           # the seeded demo name is taken
    assert not any(d["name"] == "supplierco" for d in identity.ids())


def test_a_human_threshold_for_every_action_also_catches_large_quotes(sandbox, capsys):
    assert run("operator", "create", "Vandelay2") == 0
    assert run("receiver", "create", "careful-desk", "--min-stake", "5000", "--escalate", "*=8000") == 0
    assert run("agent", "create", "v-buyer", "--operator", "Vandelay2", "--actions", "request_quote,place_order",
               "--max", "10000") == 0
    capsys.readouterr()
    assert run("request", "v-buyer", "careful-desk", "request_quote", "9000", "--check") == 2
    assert "ESCALATE" in capsys.readouterr().out
    assert run("request", "v-buyer", "careful-desk", "place_order", "9000", "--check") == 2
    assert "ESCALATE" in capsys.readouterr().out
    assert run("request", "v-buyer", "careful-desk", "request_quote", "500", "--check") == 0


def test_any_action_name_works_and_counts_as_spending_unless_read_only(sandbox, capsys):
    """Not only quotes and orders: a travel agent books flights, and the running total still applies."""
    assert run("operator", "create", "Skyways") == 0
    assert run("receiver", "create", "airline-desk", "--min-stake", "1000", "--read-only", "check_availability") == 0
    assert run("agent", "create", "travel-bot", "--operator", "Skyways",
               "--actions", "check_availability,book_flight", "--max", "1000") == 0
    capsys.readouterr()
    assert run("request", "travel-bot", "airline-desk", "book_flight", "600", "--check") == 0
    assert run("request", "travel-bot", "airline-desk", "book_flight", "600", "--check") == 2   # 1,200 > 1,000
    assert "cumulative_over_limit" in capsys.readouterr().out
    assert run("request", "travel-bot", "airline-desk", "check_availability", "900", "--check") == 0  # read-only: no total
    # an agent with no staked operator may look, but not book
    ghost_op = identity.new_operator("GhostAir")
    identity.new_agent("ghost-bot", ghost_op, "t", ["check_availability", "book_flight"], 1000)
    capsys.readouterr()
    assert run("request", "ghost-bot", "airline-desk", "book_flight", "50", "--check") == 2
    assert "unstaked_money_action" in capsys.readouterr().out


def test_only_the_receiver_can_ask_about_requests_sent_to_it(sandbox):
    """A check spends the agent's budget, so a stranger must not run checks in a receiver's name."""
    from attest import build_request
    c, at = sandbox
    run("operator", "create", "Pied")
    run("receiver", "create", "pied-desk")
    run("agent", "create", "pied-bot", "--operator", "Pied", "--actions", "place_order", "--max", "1000")
    me, desk = identity.load("pied-bot"), identity.load("pied-desk")
    env = build_request(me, "pied-desk", "place_order", 600)
    assert c.post("/verify/pied-desk", json=env).status_code == 401                 # no signature
    stranger = identity.new_receiver("not-pied")
    with pytest.raises(Exception, match="only pied-desk"):
        at.check("pied-desk", env, stranger)                                      # wrong key
    assert at.check("pied-desk", env, desk).allowed                               # the receiver itself
    other = build_request(me, "pied-desk", "place_order", 600)
    sig = crypto.sign(desk["priv"], {"verify": env["request"]["id"], "receiver": "pied-desk"})
    r = c.post("/verify/pied-desk", json=other, headers={"X-Attest-Receiver-Sig": sig})
    assert r.status_code == 401                                                   # signature reused on another request


def test_a_delegated_sub_agent_made_with_the_cli_defaults_is_accepted(sandbox, capsys):
    # Before the fix, `attest agent delegate` gave the child a fresh lifetime that outlived its parent,
    # so every sub-agent made with the defaults was refused (attenuation_violation).
    assert run("operator", "create", "Umbrella") == 0
    assert run("receiver", "create", "umbrella-desk", "--min-stake", "5000") == 0
    assert run("agent", "create", "umbrella-buyer", "--operator", "Umbrella", "--actions", "request_quote,place_order",
               "--max", "2000") == 0
    assert run("agent", "delegate", "umbrella-sub", "--parent", "umbrella-buyer", "--actions", "request_quote",
               "--max", "500") == 0
    parent, child = identity.load("umbrella-buyer"), identity.load("umbrella-sub")
    assert child["chain"][-1]["expires_at"] <= parent["chain"][-1]["expires_at"]
    capsys.readouterr()
    assert run("request", "umbrella-sub", "umbrella-desk", "request_quote", "100", "--check") == 0
    assert "ALLOW" in capsys.readouterr().out


def test_the_mcp_tool_signs_its_checks_with_the_receiver_key_file(sandbox, monkeypatch):
    from attest import build_request
    import attest.mcp_server as mcp_server
    c, at = sandbox
    monkeypatch.setattr(mcp_server, "_at", at)
    run("operator", "create", "Mcpco")
    run("receiver", "create", "mcp-desk")
    run("agent", "create", "mcp-bot", "--operator", "Mcpco", "--actions", "request_quote", "--max", "100")
    env = lambda: build_request(identity.load("mcp-bot"), "mcp-desk", "request_quote", 10)
    monkeypatch.delenv("ATTEST_RECEIVER_KEY", raising=False)
    with pytest.raises(Exception, match="only mcp-desk"):
        mcp_server.check_agent("mcp-desk", env())                                  # no key: the sandbox says no
    monkeypatch.setenv("ATTEST_RECEIVER_KEY", str(identity.home() / "mcp-desk.json"))
    out = mcp_server.check_agent("mcp-desk", env())
    assert out["decision"] == "allow" and out["receipt_verified"]
    task = mcp_server.check_for_task(build_request(identity.load("mcp-bot"), "carol", "request_quote", 10), "carol",
                                     {"allowed": True})
    assert task["verdict"] == "pass" and task["verified_signature"]
