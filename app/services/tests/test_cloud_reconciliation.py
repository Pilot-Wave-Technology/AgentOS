"""Ec4d — the credential road has to be reachable through the product.

WHAT THIS FILE EXISTS FOR. The whole credential system was built and
tested — authorise, mint, lease, pointer, sweep, three clouds, the
sidecar, the leak guards — and none of it could be used, because:

  * `GRANTABLE_KINDS` listed repo/branch/document/file/dataset, so a
    `gcp_environment` never appeared in the catalogue GO is handed after
    `create_story`. GO could not grant what it could not see.
  * NO executor prompt mentioned credentials at all. A run handed a
    lease would not know it had one, where it was, or that printing it
    is the one thing it must not do.

A capability nothing can reach is a capability that does not exist. That
is the same failure as `read_artifact` being served and denied, and as
`land_together` being taught and undeclared — a fact true in one place
and absent from the place that would use it.
"""
from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]

EXECUTOR_PROMPTS = ("profiles/errand-ops/prompt.md",
                    "profiles/story-coding/prompt.md",
                    "profiles/story-business/prompt.md")


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _flat(rel: str) -> str:
    return re.sub(r"\s+", " ", _read(rel)).lower()


# ── GO can see what it is expected to grant ──────────────────────────────

def test_every_cloud_kind_is_grantable():
    """The catalogue GO is handed after create_story must contain the
    kinds its prompt tells it to grant, or the instruction is advice
    about something invisible."""
    from app.services.story_catalogue import GRANTABLE_KINDS
    from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                           ALL_IDENTITY_KINDS)

    for kind in tuple(ALL_ENVIRONMENT_KINDS) + tuple(ALL_IDENTITY_KINDS):
        assert kind in GRANTABLE_KINDS, (
            f"{kind} is grantable in principle and invisible to GO")
    # And the ordinary kinds did not get lost on the way.
    for kind in ("repo", "branch", "document", "file", "dataset"):
        assert kind in GRANTABLE_KINDS


def test_the_kinds_come_from_the_registry_not_a_second_list():
    """One row in `clouds.py` should be enough to add a cloud. A literal
    list here is the second place to forget."""
    src = _read("app/services/story_catalogue.py")
    assert "ALL_IDENTITY_KINDS" in src and "ALL_ENVIRONMENT_KINDS" in src


def test_only_one_module_builds_the_grantable_catalogue():
    """Both creation paths go through `grant_guidance`. A second query
    would be the shape that let the gateway path ship without the
    catalogue at all."""
    src = _read("app/services/go_response.py")
    assert "grant_guidance" in src
    assert "'repo', 'branch'" not in src, (
        "go_response builds its own kind list again")


# ── GO knows what a cloud grant means ────────────────────────────────────

def test_gos_prompt_teaches_the_two_part_grant():
    text = _flat("kernel/go/AGENTS.md")
    assert "cloud reach" in text
    # Both halves, and that one alone is not enough.
    assert "environment on its own mints nothing" in text
    assert "identity" in text and "acts as" in text


def test_gos_prompt_states_the_rules_that_are_actually_enforced():
    """Each of these is a refusal in code. A prompt that omitted them
    would have GO discover them by being refused, a leg at a time."""
    text = _flat("kernel/go/AGENTS.md")
    assert "always `needs`" in text, "the write-grant refusal is untaught"
    assert "one identity per story" in text, "E_TWO_IDENTITIES is untaught"
    assert "must belong to the environment" in text, (
        "E_SCOPE_MISMATCH is untaught")
    assert "never register these" in text, (
        "GO is not told a person adds these, so it will try")


def test_gos_prompt_says_deploy_belongs_to_the_pipeline():
    """The mistake this design exists to make visible: giving a code-
    writing story a deploying identity."""
    text = _flat("kernel/go/AGENTS.md")
    assert "grant the least that works" in text
    assert "build pipeline" in text


# ── executors know what they are holding ─────────────────────────────────

def test_every_executor_prompt_explains_the_credential():
    """A run handed a lease and told nothing has a secret it does not
    know the rules for."""
    for rel in EXECUTOR_PROMPTS:
        text = _flat(rel)
        assert "agentos_credentials" in text, f"{rel} never names the pointer"
        assert "most runs have none" in text, (
            f"{rel} does not say this is the exception, so it reads as "
            f"something every run must think about")


def test_every_executor_prompt_forbids_making_it_durable():
    """The two surfaces that outlive the lease are an artifact and a
    commit, and both are one command away."""
    for rel in EXECUTOR_PROMPTS:
        text = _flat(rel)
        assert "never print it" in text, rel
        assert "commit" in text and "artifact" in text, rel


def test_every_executor_prompt_forbids_routing_around_a_refusal():
    """THE BEHAVIOUR THAT MATTERS MOST. A permission error is the answer,
    not an obstacle: a run that goes looking for the metadata endpoint or
    another account after being refused is doing exactly what the grant
    exists to prevent."""
    for rel in EXECUTOR_PROMPTS:
        text = _flat(rel)
        assert "metadata endpoint" in text, rel
        assert "do not look for another route" in text, rel
        assert "say so and stop" in text, rel


def test_executors_are_told_to_use_the_tooling_not_the_file():
    """The SDKs are already pointed at the lease. An executor parsing it
    by hand is one that will eventually echo it."""
    for rel in EXECUTOR_PROMPTS:
        text = _flat(rel)
        assert "use the tooling, not the file" in text, rel


# ── the refusal a credential actually gets ───────────────────────────────

def test_a_write_grant_on_a_credential_says_the_right_thing():
    """The generic refusal talks about git and publish_artifact, which
    would send GO looking for a write-back path that should not exist."""
    src = _read("kernel/go/tools.py")
    assert "E_CREDENTIAL_IS_NOT_WRITABLE" in src
    i = src.index("E_CREDENTIAL_IS_NOT_WRITABLE")
    message = src[i:i + 700]
    assert "acts as" in message.lower()
    assert "iam" in message.lower(), (
        "the refusal does not say where widening actually happens")
    # It must be reached BEFORE the git-backed refusal, or a credential
    # gets the message about publish_artifact.
    assert src.index("E_CREDENTIAL_IS_NOT_WRITABLE") < \
        src.index("E_NOT_GIT_BACKED")


def test_the_assistant_can_explain_a_permission_error():
    """It is the one an operator asks, and the honest answer is usually
    'it was not granted that' rather than 'something is broken'."""
    text = _flat("kernel/assistant/AGENTS.md")
    assert "cloud reach" in text
    assert "not granted that reach" in text
    assert "cannot see the credential" in text
