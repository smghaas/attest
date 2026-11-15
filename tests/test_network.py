"""Tests for the Attest ledger: proof of work, proof of stake, finality, the double-spend rules."""
import copy

import pytest

from gate.crypto import keygen
from network import ledger as L
from network.node import LightClient, Node, connect


@pytest.fixture(autouse=True)
def easy_puzzles(monkeypatch):
    monkeypatch.setattr(L, "POW_BITS", 8)
    monkeypatch.setattr(L, "REG_BITS", 6)


def make_net(n=4, stake=10000):
    keys = [keygen() for _ in range(n)]
    stakes = {pub: stake for _, pub in keys}
    vals = [pub for _, pub in keys]
    log = []
    nodes = [Node(f"node{i}", priv, pub, stakes, vals, log) for i, (priv, pub) in enumerate(keys)]
    connect(nodes)
    return nodes, keys, vals, log


def agent(op, name="a", actions=("quote",), max_amount=10000):
    priv, pub = keygen()
    return priv, pub, L.make_passport(op[0], op[1], priv, pub, name, actions, max_amount)


def settle(nodes, rec):
    ok, why = nodes[0].submit(rec)
    assert ok, why
    nodes[1].propose()


def trade(nodes, provider, buyer, request, status="accepted", amount=1000):
    rec = L.make_outcome(provider[0], provider[1], buyer[0], buyer[1], request, amount, status)
    settle(nodes, rec)
    return rec


def test_registration_needs_a_puzzle():
    nodes, keys, *_ = make_net()
    priv, pub = keygen()
    doc = L.make_passport(keys[0][0], keys[0][1], priv, pub, "a", ["quote"], 100)
    doc["nonce"] += 1
    assert not L.check_passport(doc)


def test_block_needs_work_and_a_staked_proposer():
    nodes, keys, vals, _ = make_net()
    outsider = keygen()
    block = L.mine_block(outsider[0], outsider[1], nodes[0].chain.tip, 1, [])
    assert nodes[0].chain.add(block) == (False, "proposer is not a validator")
    block = L.mine_block(keys[0][0], keys[0][1], nodes[0].chain.tip, 1, [], bits=2)
    assert nodes[0].chain.add(block)[1] == "puzzle easier than the network minimum"


def test_tampered_block_is_rejected():
    nodes, keys, *_ = make_net()
    a = agent(keys[0])
    nodes[0].submit(a[2])
    block = nodes[1].propose()
    parent = nodes[2].chain.blocks[block["header"]["prev"]]
    parent_state = nodes[2].chain.states[block["header"]["prev"]]
    assert L.validate_block(block, parent_state, parent["header"])[0] is not None   # the real block is fine
    edited = copy.deepcopy(block)
    edited["records"][0]["max_amount"] = 10 ** 9
    state, why = L.validate_block(edited, parent_state, parent["header"])
    assert state is None and "does not match its id" in why
    other = agent(keys[1])[2]                            # swap in a different, valid record
    swapped = copy.deepcopy(block)
    swapped["records"] = [other]
    state, why = L.validate_block(swapped, parent_state, parent["header"])
    assert state is None and "Merkle root" in why


def test_a_queued_pledge_counts_against_the_next_one():
    nodes, keys, *_ = make_net()
    op = keys[0]
    assert nodes[0].submit(L.make_pledge(op[0], op[1], "A", 7000, ts=1))[0]
    ok, why = nodes[0].submit(L.make_pledge(op[0], op[1], "B", 7000, ts=2))   # first is still queued
    assert not ok and "overpledge" in why


def test_records_reach_every_node_and_blocks_become_final():
    nodes, keys, *_ = make_net()
    a = agent(keys[0])
    settle(nodes, a[2])
    for n in nodes:
        assert a[1] in n.state.passports
        assert n.chain.height(n.chain.tip) == 1
        assert n.chain.finalized == n.chain.tip       # all four validators voted


def test_the_same_stake_cannot_back_two_sellers():
    nodes, keys, *_ = make_net()
    op = keys[0]
    settle(nodes, L.make_pledge(op[0], op[1], "seller-A", 7000, ts=1))
    ok, why = nodes[0].submit(L.make_pledge(op[0], op[1], "seller-B", 7000, ts=2))
    assert not ok and "overpledge" in why


def test_overpledge_in_a_block_makes_the_block_invalid():
    nodes, keys, *_ = make_net()
    op = keys[0]
    p1, p2 = L.make_pledge(op[0], op[1], "A", 7000, ts=1), L.make_pledge(op[0], op[1], "B", 7000, ts=2)
    block = L.mine_block(keys[1][0], keys[1][1], nodes[0].chain.tip, 1, [p1, p2])
    ok, why = nodes[0].chain.add(block)
    assert not ok and "overpledge" in why


def test_conflicting_outcome_for_one_request_is_refused():
    nodes, keys, *_ = make_net()
    s, b = agent(keys[0], "seller"), agent(keys[1], "buyer")
    settle(nodes, s[2])
    settle(nodes, b[2])
    seller, buyer = (s[0], s[1]), (b[0], b[1])
    trade(nodes, seller, buyer, "req-1", "accepted")
    again = L.make_outcome(seller[0], seller[1], buyer[0], buyer[1], "req-1", 1000, "disputed", ts=5)
    ok, why = nodes[0].submit(again)
    assert not ok and "first record wins" in why


def test_an_outcome_needs_both_signatures():
    nodes, keys, *_ = make_net()
    s, b = agent(keys[0], "seller"), agent(keys[1], "buyer")
    settle(nodes, s[2])
    settle(nodes, b[2])
    rec = L.make_outcome(s[0], s[1], b[0], b[1], "req-2", 500, "accepted")
    rec["buyer_sig"] = rec["sig"]                       # the provider signs for the buyer
    ok, _ = nodes[0].submit(rec)
    assert not ok


def test_heavier_fork_wins_but_not_below_a_final_block():
    nodes, keys, *_ = make_net()
    a = agent(keys[0])
    settle(nodes, a[2])                                  # block 1, final on every node
    final = nodes[0].chain.finalized
    # a rival block 1 with more work, built on genesis, must not displace the final chain
    rival = L.mine_block(keys[2][0], keys[2][1], nodes[0].chain.genesis, 1, [], bits=14)
    ok, _ = nodes[0].chain.add(rival)
    assert ok and nodes[0].chain.tip == final


def test_a_validator_that_signs_two_blocks_is_slashed():
    nodes, keys, *_ = make_net()
    cheat = nodes[3]
    first = L.mine_block(cheat.priv, cheat.pub, nodes[0].chain.tip, 1, [], ts=1)
    second = L.mine_block(cheat.priv, cheat.pub, nodes[0].chain.tip, 1, [], ts=2)
    for n in nodes:
        n.receive_block(first, None)
    cheat.vote(second, force=True)                       # the second, conflicting vote
    nodes[0].propose()
    for n in nodes:
        assert cheat.pub in n.state.suspended
        assert n.state.stakes[cheat.pub] == 5000


def test_offline_node_catches_up():
    nodes, keys, *_ = make_net()
    nodes[3].online = False
    a = agent(keys[0])
    settle(nodes, a[2])
    assert nodes[3].chain.height(nodes[3].chain.tip) == 0
    nodes[3].come_online()
    assert a[1] in nodes[3].state.passports


def test_light_client_decides_from_history_and_checks_every_branch():
    nodes, keys, vals, _ = make_net()
    s, b = agent(keys[0], "seller"), agent(keys[1], "buyer")
    peers = [agent(keys[i % 4], f"peer{i}") for i in range(3)]
    for x in (s, b, *peers):
        settle(nodes, x[2])
    seller = (s[0], s[1])
    for i, p in enumerate(peers):
        trade(nodes, seller, (p[0], p[1]), f"r{i}")
    light = LightClient(vals)
    decision, why = light.decide(nodes[:2], s[1])
    assert decision == "allow", why
    assert light.decide(nodes[:2], b[1])[0] == "unknown"    # a passport but no history yet
    assert light.decide(nodes[:2], "nobody")[0] == "refuse"


def test_wash_trading_between_two_agents_is_capped():
    nodes, keys, vals, _ = make_net()
    x, y = agent(keys[0], "x"), agent(keys[1], "y")
    settle(nodes, x[2])
    settle(nodes, y[2])
    for i in range(8):
        trade(nodes, (x[0], x[1]), (y[0], y[1]), f"wash{i}")
    light = LightClient(vals)
    assert light.decide(nodes[:2], x[1], min_credit=3, pair_cap=2)[0] == "unknown"


def test_a_lying_node_cannot_hide_a_dispute_from_a_client_that_asks_two():
    nodes, keys, vals, _ = make_net()
    s, b = agent(keys[0], "seller"), agent(keys[1], "buyer")
    settle(nodes, s[2])
    settle(nodes, b[2])
    seller, buyer = (s[0], s[1]), (b[0], b[1])
    trade(nodes, seller, buyer, "ok1")
    trade(nodes, seller, buyer, "bad1", "disputed")
    trade(nodes, seller, buyer, "bad2", "disputed")
    liar = nodes[0]
    real_entries = liar.entries
    liar.entries = lambda agent_, kinds=("passport", "outcome"): [
        e for e in real_entries(agent_, kinds) if e["record"].get("status") != "disputed"]
    light = LightClient(vals)
    assert light.decide([liar], s[1], min_credit=1)[0] == "allow"        # fooled by one node
    assert light.decide([liar, nodes[1]], s[1], min_credit=1)[0] == "refuse"


def test_history_lists_what_the_agent_has_done():
    nodes, keys, vals, _ = make_net()
    s, b = agent(keys[0], "seller"), agent(keys[1], "buyer")
    settle(nodes, s[2])
    settle(nodes, b[2])
    trade(nodes, (s[0], s[1]), (b[0], b[1]), "h1", "accepted", 2500)
    trade(nodes, (s[0], s[1]), (b[0], b[1]), "h2", "disputed", 900)
    h = nodes[2].history(s[1])
    assert (h["trades"], h["accepted"], h["disputed"], h["volume"]) == (2, 1, 1, 2500)
