"""Anyone checks an agent for a task and chooses what must be true."""
import pytest
from fastapi.testclient import TestClient

from attest import Attest, build_request, do_work, identity
from gate.server import app


@pytest.fixture
def at(monkeypatch, tmp_path):
    monkeypatch.setenv("ATTEST_SANDBOX", "1")
    monkeypatch.setenv("ATTEST_DIR", str(tmp_path / "me"))
    monkeypatch.setenv("ATTEST_RATE_WRITE", "1000")
    monkeypatch.setenv("ATTEST_RATE_CHECK", "1000")
    a = Attest("http://testserver")
    a.http = TestClient(app)
    return a


def person_agent(name="ana-assistant", actions=("book_meeting",), limit=500):
    me = identity.new_operator(f"Ana-{name}", backer_kind="person")      # a person, no money down
    return identity.new_agent(name, me, "my assistant", list(actions), limit)


def items(out):
    return {i["key"]: i["status"] for i in out["items"]}


def test_a_person_backed_agent_with_no_money_passes_with_proof_of_work(at):
    agent = person_agent()
    env = build_request(agent, "bob", "book_meeting", 0)
    need = dict(backer=True, allowed=True, min_stake=1000, work_bits=12, either=True)
    without = at.check_task(env, "bob", **need)
    assert without["verdict"] == "fail" and items(without)["backing"] == "fail"
    out = at.check_task(do_work(build_request(agent, "bob", "book_meeting", 0), 12), "bob", **need)
    assert out["verdict"] == "pass" and out["verified_signature"]
    assert "(person)" in next(i["why"] for i in out["items"] if i["key"] == "backer")


def test_money_down_is_enough_when_either_is_accepted(at):
    op = identity.new_operator("Initech-x")
    at.faucet(op)
    agent = identity.new_agent("initech-bot", op, "t", ["send_invoice"], 5000)
    out = at.check_task(build_request(agent, "acme-ap", "send_invoice", 1200), "acme-ap",
                        allowed=True, min_stake=5000, work_bits=20, either=True)
    assert out["verdict"] == "pass"


def test_the_checker_decides_what_matters(at):
    agent = person_agent("cleaner-bot", ("quote_cleaning",), 300)
    env = lambda act, amt: build_request(agent, "landlord", act, amt)
    assert at.check_task(env("quote_cleaning", 100), "landlord")["verdict"] == "pass"       # only: who is behind it
    assert items(at.check_task(env("pay_rent", 100), "landlord", allowed=True))["allowed"] == "fail"
    assert items(at.check_task(env("quote_cleaning", 900), "landlord", allowed=True))["allowed"] == "fail"
    assert at.check_task(env("quote_cleaning", 250), "landlord", allowed=True, human_above=200)["verdict"] == "wait"


def test_a_request_made_for_someone_else_or_changed_fails(at):
    agent = person_agent("x-bot", ("chat",), 10)
    env = build_request(agent, "alice", "chat", 0)
    assert items(at.check_task(env, "mallory"))["signed"] == "fail"           # shown to the wrong person
    env["request"]["amount"] = 5                                               # changed after signing
    assert items(at.check_task(env, "alice"))["signed"] == "fail"


def test_a_stranger_with_no_backer_fails_the_backer_check(at):
    from gate import crypto
    priv, pub = crypto.keygen()
    lone = {"name": "lone", "priv": priv, "pub": pub, "credential": None, "chain": []}
    out = at.check_task(build_request(lone, "carol", "chat", 0), "carol", backer=True)
    assert items(out)["signed"] == "pass" and items(out)["backer"] == "fail"
    assert at.check_task(do_work(build_request(lone, "carol", "chat", 0), 10), "carol",
                         backer=False, work_bits=10)["verdict"] == "pass"     # work alone, if that is all carol asks


def test_someone_who_vouches_only_for_themself_has_no_backer_but_can_put_money_down(at):
    me = identity.new_operator("Bob Stone", backer_kind="self")
    at.faucet(me)
    bob = identity.new_agent("bob", me, "me", ["lease_apartment"], 2000)
    out = at.check_task(build_request(bob, "alice", "lease_apartment", 1500), "alice",
                        backer=True, allowed=True, min_stake=1000)
    assert items(out) == {"signed": "pass", "backer": "fail", "allowed": "pass", "stake": "pass"}
    assert "only for themself" in next(i["why"] for i in out["items"] if i["key"] == "backer")
    out = at.check_task(build_request(bob, "alice", "lease_apartment", 1500), "alice",
                        backer=False, allowed=True, min_stake=1000)
    assert out["verdict"] == "pass"                       # alice may accept money down instead of a backer
