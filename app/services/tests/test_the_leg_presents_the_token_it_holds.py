"""A leg talks to its own workspace with the token it was given.

MEASURED ON EXECUTION 223. The story was DONE -- seven tests written and
passing, the report artifact emitted at 5591 bytes, the claim recorded
with four counts against a baseline -- and it could not be delivered.
`publish_execution` runs in the pod since the leg moved there, dials
podfs over loopback, and `token_for` tried to DERIVE a token:

    TokenMissing: AGENT_TOKEN_SIGNING_KEY is unset, so no workspace
    token can be derived. It is mounted from `conductor-secrets`

which the pod deliberately does not have -- `POD_ENV_ALLOWLIST` is a
closed list and carries no credentials. So publish raised on every
attempt, the run bounced on `publish failed`, and a finished story
burned its legs.

EVERY PIECE OF THE ANSWER WAS ALREADY WRITTEN. `derive`: "Called on the
CONDUCTOR side... The worker never calls this: it has the answer and not
the key." `configured_token`: "The token THIS process was given. The
worker side." `read_only_of`: "Both ends compute it: the conductor from
`derive`, the worker from the value it was given." Nothing joined them,
because when they were written the only podfs client WAS the conductor.
The cutover made the worker one and no one came back.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

TOKEN = "tok-for-one-execution"
MINE = 4242


@pytest.fixture
def in_pod(monkeypatch):
    """A process that IS an execution pod, holding its own token."""
    from kernel.workspace import podfs_auth, podfs_client

    monkeypatch.setenv(podfs_auth.ENV_VAR, TOKEN)
    monkeypatch.setenv(podfs_client.EXECUTION_POD_ENV, str(MINE))
    monkeypatch.delenv(podfs_auth.SIGNING_KEY_VAR, raising=False)
    return podfs_client


def test_the_leg_presents_what_it_holds_instead_of_deriving(in_pod):
    """The whole bug: it has the answer and was asking for the key."""
    assert in_pod.token_for(MINE, False) == TOKEN


def test_the_narrow_scope_is_computed_from_the_held_token(in_pod):
    """`read_only_of` exists so the worker can do this without the
    signing key -- "the worker from the value it was given"."""
    from kernel.workspace.podfs_auth import read_only_of

    assert in_pod.token_for(MINE, True) == read_only_of(TOKEN)


def test_a_call_about_another_execution_gets_no_token_from_here(in_pod):
    """THE PROPERTY A FIRST CUT OF THIS BROKE, caught by
    `test_a_token_for_another_execution_is_not_recognised_at_all`.

    Returning the held token whatever was asked for would make a call
    ABOUT another execution look authentic to a server that shares this
    one's secret -- a 401 became a 200. The held token belongs to ONE
    execution, and `AGENTOS_EXECUTION_ID` says which."""
    from kernel.workspace.podfs_auth import TokenMissing

    with pytest.raises(TokenMissing):
        in_pod.token_for(MINE + 1, False)


def test_a_conductor_still_derives(monkeypatch):
    """It talks to many executions and holds no token of its own. The
    old path is unchanged, which is what keeps every non-pod caller
    working."""
    from kernel.workspace import podfs_auth, podfs_client

    monkeypatch.delenv(podfs_auth.ENV_VAR, raising=False)
    monkeypatch.delenv(podfs_client.EXECUTION_POD_ENV, raising=False)
    monkeypatch.setenv(podfs_auth.SIGNING_KEY_VAR, "a-signing-key")

    assert podfs_client.token_for(MINE, False) == podfs_auth.derive(MINE)


def test_the_pod_is_not_given_a_signing_key_to_fix_this():
    """THE TEMPTING FIX, and the wrong one. A signing key mints a token
    for EVERY execution; the pod needs one for itself. Shipping the key
    would also put it in reach of a sandbox that shares the pod's
    network namespace."""
    body = (ROOT / "kernel/conductor/sandbox_spec.py").read_text(
        encoding="utf-8")

    assert "AGENT_TOKEN_SIGNING_KEY" not in body, (
        "the signing key is on the pod's env allowlist, which hands "
        "every execution's token to one pod")


# ── and the evidence it writes is not silently dropped ─────────────────

def test_the_leg_may_read_the_table_it_writes():
    """`_record` writes `INSERT ... ON CONFLICT (...) DO NOTHING`, whose
    inference reads the index, so INSERT alone is refused outright.

    It failed QUIETLY: the try/except around it says "A CLAIM THAT
    CANNOT BE RECORDED IS STILL A CLAIM", so the denial was logged once
    and the run carried on -- with `criterion_results` empty, the
    ticker's check half blank, and a person offered a claim with no
    observations beside it. That handler is right, and it is exactly why
    this had to be found by reading a log rather than by a red test."""
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, shipped_grants

    if IS_DISTRIBUTION:
        import re

        leg = [ln for ln in shipped_grants().splitlines()
               if re.match(r"GRANT .* ON kernel\.criterion_results TO "
                           r"agentos_leg;", ln)]
        assert any("SELECT" in ln for ln in leg), (
            "agentos_leg may not read kernel.criterion_results")
        for verb in ("UPDATE", "DELETE"):
            assert not any(verb in ln for ln in leg), (
                "the evidence stops being append-only")
        return
    sql = (ROOT / "migrations"
           / "leg_selects_its_own_criterion_results.sql").read_text(
        encoding="utf-8")

    assert "GRANT SELECT ON kernel.criterion_results TO agentos_leg;" in sql
    for verb in ("UPDATE ON kernel.criterion_results",
                 "DELETE ON kernel.criterion_results"):
        assert f"GRANT {verb}" not in sql, (
            "the evidence stops being append-only")


def test_the_migration_is_a_new_name_and_not_an_edited_one():
    """An earlier draft of this grant went out under a different name
    and was withdrawn. Any database that applied the draft has that name
    in its ledger and would SKIP a re-used one silently -- which is what
    `apply.py` did when this was first rewritten in place."""
    seq = (ROOT / "migrations/SEQUENCE").read_text(encoding="utf-8").split()

    assert "leg_selects_its_own_criterion_results" in seq
    assert "leg_reads_back_its_own_checks" not in seq, (
        "the withdrawn name is back in SEQUENCE, and a database that saw "
        "the draft will skip the real one")
