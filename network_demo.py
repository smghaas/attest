"""Run the Attest ledger on four nodes (one per team) and tell the story.

    python network_demo.py            # prints the run and writes docs/network_demo.json

Every number below comes from the real code in network/: real Ed25519 signatures, real hashcash
puzzles at 12 bits, real Merkle branches. Nothing is scripted except the order of events.
"""
import json
import time
from pathlib import Path

from gate.crypto import keygen
from network import ledger as L
from network.node import LightClient, Node, connect

TEAMS = [("Acme", 25000), ("Beta", 25000), ("Gamma", 20000), ("Delta", 20000)]
keys = {t: keygen() for t, _ in TEAMS}
vals = [keys[t][1] for t, _ in TEAMS]
stakes = {keys[t][1]: s for t, s in TEAMS}
log: list = []
nodes = [Node(t, keys[t][0], keys[t][1], stakes, vals, log) for t, _ in TEAMS]
connect(nodes)
N = {n.name: n for n in nodes}
light = LightClient(vals)
steps: list = []
cursor = [0]


def snap(title, text, result=None):
    events = log[cursor[0]:]
    cursor[0] = len(log)
    steps.append({"title": title, "text": text, "events": events, "result": result or [],
                  "nodes": [{"name": n.name, "online": n.online, "height": n.chain.height(n.chain.tip),
                             "final": n.chain.height(n.chain.finalized), "stake": stakes[n.pub]
                             if n.pub not in n.state.suspended else n.state.stakes[n.pub],
                             "suspended": n.pub in n.state.suspended} for n in nodes]})
    print(f"\n== {title}\n{text}")
    for e in events:
        print(f"   [{e['node']}] {e['kind']}: {e['detail']}")
    for r in (result or []):
        print(f"   -> {r}")


def agent(team, name, actions=("quote", "place_order"), cap=10000):
    priv, pub = keygen()
    return {"name": name, "priv": priv, "pub": pub,
            "passport": L.make_passport(keys[team][0], keys[team][1], priv, pub, name, actions, cap)}


def register(*agents):
    t0 = time.time()
    for a in agents:
        assert N["Acme"].submit(a["passport"])[0]
    N["Beta"].propose()
    return time.time() - t0


def trade(provider, buyer, req, status="accepted", amount=1500):
    rec = L.make_outcome(provider["priv"], provider["pub"], buyer["priv"], buyer["pub"], req, amount, status)
    ok, why = N["Gamma"].submit(rec)
    assert ok, why
    return rec


def decide(nodes_, a, **kw):
    d, why = light.decide(nodes_, a["pub"], **kw)
    return f"{a['name']}: {d.upper()} ({why})"


# 1. the network
snap("Four teams start a node each",
     "Each team runs a node and holds stake: Acme 25k, Beta 25k, Gamma 20k, Delta 20k. "
     "To add a block a node must solve a 12-bit puzzle and sign as a staked validator.")

# 2. identities
seller = agent("Acme", "acme-quotes")
buyers = [agent("Beta", "beta-buyer"), agent("Gamma", "gamma-buyer"), agent("Delta", "delta-buyer")]
secs = register(seller, *buyers)
snap("Agents register a passport",
     f"Each operator signs a passport, and each agent signs it too. Registering costs a hashcash puzzle "
     f"({L.REG_BITS} bits). The block is mined, gossiped, and voted on by all four stakers. "
     f"Took {secs:.2f}s.")

# 3. history
for i, b in enumerate(buyers):
    trade(seller, b, f"order-{i}")
trade(seller, buyers[0], "order-3", "accepted", 800)
N["Delta"].propose()
h = N["Acme"].history(seller["pub"])
snap("The ledger records what the agent did",
     "Four trades, each signed by BOTH sides. Nobody judged them. This is the agent's history, "
     "readable by anyone.",
     [f"acme-quotes: {h['accepted']} accepted, {h['disputed']} disputed, volume ${h['volume']:,.0f}, "
      f"in blocks {sorted({e['height'] for e in h['entries']})}"])

# 4. a seller decides from the chain
snap("A stranger seller checks the agent, with no call to us",
     "The seller keeps only block headers and checks each record against a Merkle branch.",
     [decide([N["Acme"], N["Beta"]], seller), decide([N["Acme"], N["Beta"]], buyers[2])])

# 5. tampering
blk = N["Acme"].chain.best_chain()[-1]
forged = json.loads(json.dumps(blk))
forged["records"][0]["amount"] = 999999
parent = N["Beta"].chain.blocks[forged["header"]["prev"]]
state, why = L.validate_block(forged, N["Beta"].chain.states[forged["header"]["prev"]], parent["header"])
assert state is None
snap("Attack: edit a past record",
     "An attacker changes an amount inside a block. Each record's id is a hash of its content, and "
     "the block header commits to all the ids with a Merkle root, so one changed byte is caught.",
     [f"every peer refuses it: {why}"])

# 6. double pledge
op = keys["Delta"]
N["Delta"].submit(L.make_pledge(op[0], op[1], "SellerA", 15000, ts=1))
ok, why = N["Delta"].submit(L.make_pledge(op[0], op[1], "SellerB", 15000, ts=2))
N["Delta"].propose()
snap("Attack: back two sellers with the same stake",
     "Delta stakes 20,000 and promises 15,000 to SellerA, then 15,000 to SellerB. "
     "This is the double spend.", [f"second pledge refused: {why}"])

# 7. wash trading
w1, w2 = agent("Delta", "shady-1"), agent("Delta", "shady-2")
register(w1, w2)
for i in range(8):
    trade(w1, w2, f"wash-{i}")
N["Acme"].propose()
snap("Attack: two agents trade with each other to fake a history",
     "Eight 'accepted' trades between shady-1 and shady-2. At most 2 credits count per counterparty.",
     [decide([N["Acme"], N["Beta"]], w1)])

# 8. a lying node
c1, c2 = agent("Beta", "cheater"), agent("Gamma", "victim")
register(c1, c2)
trade(c1, c2, "x1")
trade(c1, c2, "x2", "disputed")
trade(c1, c2, "x3", "disputed")
N["Delta"].propose()
liar, real = N["Acme"], N["Acme"].entries
liar.entries = lambda a, kinds=("passport", "outcome"): [e for e in real(a, kinds)
                                                       if e["record"].get("status") != "disputed"]
one = decide([liar], c1, min_credit=1)
two = decide([liar, N["Beta"]], c1, min_credit=1)
liar.entries = real
snap("Attack: a node hides the bad record",
     "Acme's node hides the disputes against the cheater. Asking one node is not safe. "
     "Asking two is.", [f"asked 1 node: {one}", f"asked 2 nodes: {two}"])

# 9. a node dies
N["Gamma"].online = False
register(agent("Beta", "late-agent"))
d_dead = decide([N["Acme"], N["Beta"]], seller)
N["Gamma"].come_online()
snap("A node goes down and comes back",
     "Gamma is switched off while a new block is mined. The seller still verifies. "
     "Gamma restarts and catches up by itself.", [d_dead])

# 10. validator cheats
cheat = N["Delta"]
tip = N["Acme"].chain.tip
first = L.mine_block(cheat.priv, cheat.pub, tip, N["Acme"].chain.height(tip) + 1, [], ts=1)
second = L.mine_block(cheat.priv, cheat.pub, tip, N["Acme"].chain.height(tip) + 1, [], ts=2)
for n in nodes:
    n.receive_block(first, None)
cheat.vote(second, force=True)
N["Acme"].propose()
snap("A validator votes for two different blocks",
     "Delta signs two conflicting blocks at one height. Any node can publish the two signatures as proof. "
     "The chain halves Delta's stake and suspends it. Proof of stake needs a punishment, and this is it.")

# 11. final view
h = N["Beta"].history(seller["pub"])
snap("The ledger after all that",
     "Everything the agent did is still there, in order, and nobody can edit it.",
     [f"acme-quotes: {h['trades']} trades, {h['accepted']} accepted, ${h['volume']:,.0f} volume",
      f"chain height {N['Acme'].chain.height(N['Acme'].chain.tip)}, "
      f"final up to {N['Acme'].chain.height(N['Acme'].chain.finalized)}"])

out = Path(__file__).parent / "docs" / "network_demo.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"puzzle_bits": L.POW_BITS, "reg_bits": L.REG_BITS, "steps": steps}, indent=1))
print(f"\nwrote {out}")
