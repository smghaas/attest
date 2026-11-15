# NANDA Town integration

Copy of the Attest x NANDA Town integration submitted as
https://github.com/projnanda/nandatown/pull/352 (branch `integration/attest-gate-seller`,
commit b878589, built on Town commit 6f9a7c0).

The files sit at the paths they have inside the Town repo:

    examples/attest/        world.py, attest_seller.py, attest_buyer.py, README.md
    tests/test_attest_integration.py

To run them, copy both into a nandatown checkout and set `ATTEST_HOME` to this repo.
The PR is the source of truth; this copy is here so the Attest repo holds everything.
