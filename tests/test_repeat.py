"""A receiver that checks the same request again gets the original answer, marked as a repeat (HW9 part 3).

Before this, the second check of a request was refused as a replay, so a receiver that crashed after asking the
gate, or retried a tool call, or got a message delivered twice, saw `refuse` for an order it had never served.
In the HW9 cold-start crash runs the restarted shop agents avoided that only by searching the gate's public log of
every decision for their old receipts (usability/runs/crash-before-*).
"""
import copy

import pytest

from bench.world import RECEIVER, demo_world, request
from gate import crypto


@pytest.fixture
def w():
    world = demo_world()
    yield world
    world.close()


def test_a_second_check_of_the_same_request_returns_the_original_answer(w):
    env = request(w.agents["proc"], "place_order", 6000, "PO-1")
    first = w.gate.verify(RECEIVER, env)
    again = w.gate.verify(RECEIVER, copy.deepcopy(env))
    assert (first["decision"], first["repeat"]) == ("allow", False)
    assert (again["decision"], again["reason"], again["repeat"]) == ("allow", "ok", True)
    assert again["receipt"] == first["receipt"]                       # the same signed receipt, not a new one
    assert len(w.gate.receipts(100)) == 1                             # nothing new in the public log


def test_a_repeat_is_not_counted_twice(w):
    proc = w.agents["proc"]                                           # mandate: orders up to 10,000
    env = request(proc, "place_order", 6000, "PO-1")
    w.gate.verify(RECEIVER, env)
    w.gate.verify(RECEIVER, env)
    w.gate.verify(RECEIVER, env)
    # 6,000 spent once, so 4,000 more still fits; had the repeats counted, this would be cumulative_over_limit
    assert w.gate.verify(RECEIVER, request(proc, "place_order", 4000, "PO-2"))["decision"] == "allow"


def test_a_refusal_is_repeated_too(w):
    env = request(w.agents["proc"], "place_order", 12000, "too big")
    assert w.gate.verify(RECEIVER, env)["reason"] == "over_limit"
    again = w.gate.verify(RECEIVER, env)
    assert (again["decision"], again["reason"], again["repeat"]) == ("refuse", "over_limit", True)


def test_a_different_request_reusing_the_nonce_is_still_a_replay(w):
    proc = w.agents["proc"]
    env = request(proc, "place_order", 1000, "PO-1")
    w.gate.verify(RECEIVER, env)
    body = {k: v for k, v in env["request"].items() if k not in ("id", "sig")}
    body["amount"] = 2000.0                                           # new content, same nonce, properly signed
    other = {**env, "request": crypto.signed(proc["priv"], body)}
    r = w.gate.verify(RECEIVER, other)
    assert (r["decision"], r["reason"], r["repeat"]) == ("refuse", "replay", False)


def test_an_altered_copy_is_not_answered_from_the_earlier_check(w):
    env = request(w.agents["proc"], "place_order", 1000, "PO-1")
    w.gate.verify(RECEIVER, env)
    forged = copy.deepcopy(env)
    forged["request"]["sig"] = "00" * 64                              # same id, signature no longer valid
    r = w.gate.verify(RECEIVER, forged)
    assert r["repeat"] is False and r["decision"] == "refuse"
    swapped = copy.deepcopy(env)
    swapped["chain"] = w.agents["sub_ok"]["chain"]                    # same request, different mandate chain
    assert w.gate.verify(RECEIVER, swapped)["repeat"] is False


def test_only_the_receiver_it_was_checked_for_gets_the_repeat(w):
    env = request(w.agents["proc"], "request_quote", 100, "quote")
    w.gate.verify(RECEIVER, env)
    w.gate.register_receiver("other-shop", min_stake=0)
    r = w.gate.verify("other-shop", env)
    assert r["repeat"] is False and r["reason"] == "wrong_receiver"


def test_the_puzzle_flow_is_unchanged(w):
    env = request(w.agents["newcomer"], "request_quote", 50, "hello")
    first = w.gate.verify(RECEIVER, env)
    assert (first["reason"], first["repeat"]) == ("pow_required", False)
    ch = first["challenge"]
    from gate.core import solve_pow as solve
    nonce, _ = solve(ch["seed"], ch["bits"])
    solved = w.gate.verify(RECEIVER, {**env, "pow": {"challenge_id": ch["challenge_id"], "nonce": nonce}})
    assert (solved["reason"], solved["repeat"]) == ("pow_ok", False)


def test_someone_checking_a_copy_first_no_longer_spoils_it_for_the_receiver(w):
    # A third party that saw the envelope presents it first. Before, that used up the nonce and the real receiver
    # got `replay`. Now the receiver gets the same answer the gate gave (and the spend counted once).
    env = request(w.agents["proc"], "place_order", 3000, "PO-1")
    w.gate.verify(RECEIVER, copy.deepcopy(env))                        # the eavesdropper
    r = w.gate.verify(RECEIVER, env)                                   # the shop
    assert (r["decision"], r["repeat"]) == ("allow", True)
