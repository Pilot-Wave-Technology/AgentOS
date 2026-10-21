"""The conformance suite — shipped WITH the client, and run by consumers.

WHY THESE TESTS LIVE INSIDE THE PACKAGE. multi-agent-RL consumes this
client by `pip install`, not by cloning agentos. Tests kept beside
the package rather than inside it are not in the wheel, so a consumer
gets the library and no way to check it against their own database --
which is precisely the check that matters, because the thing they share
with us is a HASH-CHAINED table. A schema disagreement does not present
as "the schema differs"; it presents as a chain that will not verify.

It is not hypothetical. The `schema.sql` this package shipped refused
`goal_orchestrator`, `executor` and `verifier` -- 29% of the events on a
live deployment -- and a test written to catch exactly that drift was
green the whole time. A consumer who could have run the suite against
their own database would have seen it on day one.

    pip install "agentos-ledger-client[test]"
    pytest --pyargs agentos_ledger_client

WHAT IS NOT HERE, deliberately. `packages/agentos-ledger-client/tests/`
still holds two modules that assert about agentos's OWN tree --
that `schema.sql` matches `migrations/SEQUENCE`, and that the package
is wired correctly into this repository's images and CI. Those are
repo-integration tests, not conformance: they need a checkout, they
would skip for every consumer, and shipping them would be noise in
somebody else's test run.

The line between the two directories is a question: "does this test the
CLIENT, or does it test THIS REPOSITORY?" Only the first ships.
"""
