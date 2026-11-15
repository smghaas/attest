# NANDA Town integration

Started as a copy of the Attest x NANDA Town integration submitted as
https://github.com/projnanda/nandatown/pull/352 (branch `integration/attest-gate-seller`,
commit b878589, built on Town commit 6f9a7c0). **This copy is now ahead of the PR** (full explanation:
`docs/NANDATOWN.md`). Since b878589:

- the seller's refusals tell the buyer how to get served (gate address and key, receiver name, what to attach)
- the buyer can start knowing nothing about Attest and learn the gate from the seller (`ATTEST_DISCOVER=1`),
  and can use an agent file made with Attest's own CLI (`ATTEST_IDENTITY`)
- two new tests: a stranger finds the gate through Town; an operator registered on a sandbox gate is served
- `catalog/`: the entry Town's on-ramp generates for Attest's API, with answers to its open questions

To update the PR, copy `examples/attest/` and `tests/test_attest_integration.py` over the files in the PR
branch (they sit at the same paths in the Town repo). `catalog/` is for Town's service catalog review and
is not part of the PR.

The files sit at the paths they have inside the Town repo:

    examples/attest/        world.py, attest_seller.py, attest_buyer.py, README.md
    tests/test_attest_integration.py

To run them, copy both into a nandatown checkout, `pip install -e` this repo there, and set
`ATTEST_HOME` to this repo. 22 tests passed against Town 6f9a7c0 and Attest f000f3b.
