"""An errand's checkout is writable whichever door granted it.

THE FIX WAS HALF-APPLIED AND NOTHING SAID SO. The upgrade lived in
`kernel/go/errand_resources.validate`, under a comment asserting that
`validate` "has one caller ... the GO, the assistant and a scheduled
errand all go through it". That is the PROPOSAL door. A hand-started
errand takes the other one -- the GO creates the story, then calls
`grant_resource`, which reaches `mounts.grant` and never passes through
`validate`.

MEASURED ON DEV, 2026-10-07. Errand `f87d306f`, two trunk lanes of one
repository, both stored `needs` and both mounted read-only:

    resource_id  | relation | kind   | ref                 | granted_by
    7fa3d9ec-d0a | needs    | branch | calculator_frontend | go:vsct1
    b19c223f-7ee | needs    | branch | calculator_backend  | go:vsct1

Those rows carry `at`/`ref`/`binding`/`granted_by`, which the proposal
replay never writes -- the fingerprint of `mounts.grant`. Twenty
minutes earlier, proposal #9 came out `modifies` with the upgrade note
attached, because it went through the door that had the rule.

WHY IT MATTERS: `writable` is derived at read time from
`relation IN ('produces','modifies')`, so a `needs` grant means
`writable_scope_for` returns [] and the `write` tool refuses
everywhere. The model then writes its artifact through a shell
heredoc, which is what the destructive gate parks on.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.workspace import errand_write


# ── 1. the rule itself ───────────────────────────────────────────────

def test_a_read_grant_on_a_checkout_becomes_a_write():
    relation, why, changed = errand_write.upgrade("repo", "needs", "")
    assert (relation, changed) == ("modifies", True)
    assert why == errand_write.NOTE, "the upgrade must say it happened"


def test_a_branch_counts_as_a_checkout():
    """f87d306f's two grants were `branch`, not `repo` -- a lane of a
    repository is still a tree with a working copy."""
    relation, _, changed = errand_write.upgrade("branch", "needs", "")
    assert (relation, changed) == ("modifies", True)


def test_the_stated_reason_is_kept_not_replaced():
    _, why, _ = errand_write.upgrade("repo", "needs", "Read the layout")
    assert why.startswith("Read the layout"), (
        "the GO's own reason reaches the executor's brief; losing it "
        "costs the one line saying what each place is FOR")
    assert errand_write.NOTE in why


@pytest.mark.parametrize("kind", ["gcp_project", "aws_account", "doc",
                                  "dataset", "document"])
def test_cloud_reach_and_documents_stay_read_only(kind):
    """A run ACTS AS an identity, it never edits one, and a document is
    `copyin` -- there is nothing to write to. The upgrade must not
    route around the refusal that states this."""
    relation, why, changed = errand_write.upgrade(kind, "needs", "why")
    assert (relation, why, changed) == ("needs", "why", False)


@pytest.mark.parametrize("relation", ["produces", "modifies"])
def test_a_write_relation_is_left_exactly_as_asked(relation):
    """`produces` and `modifies` are different facts on the CP model.
    Normalising one to the other here would quietly rewrite what the
    caller said, and both are already writable."""
    got, why, changed = errand_write.upgrade("repo", relation, "why")
    assert (got, why, changed) == (relation, "why", False)


# ── 2. both doors reach it ───────────────────────────────────────────

def test_the_proposal_door_calls_the_shared_rule():
    from kernel.go import errand_resources
    src = inspect.getsource(errand_resources._one)
    assert "errand_write.upgrade(" in src, (
        "the proposal door has its own copy of the rule again")


def test_the_grant_door_calls_the_shared_rule():
    from kernel.workspace import mounts
    src = inspect.getsource(mounts.grant)
    assert "errand_write.upgrade(" in src, (
        "`grant_resource` reaches mounts.grant and nothing else -- this "
        "is the door errand f87d306f came through")
    assert "_is_errand" in src, (
        "the upgrade must be gated on the story being an errand; a "
        "coding story's read grant is a read grant")


def test_the_grant_door_hands_back_what_it_stored():
    """`execution_mounts.writable` is written from the Mount OBJECT, not
    from the stored relation. A caller that kept its own copy would
    record a read-only mount against a row saying `modifies`, and the
    live run still could not write."""
    from kernel.workspace import live_grant, mounts
    assert "-> tuple[Mount, str]" in inspect.getsource(mounts.grant)
    src = inspect.getsource(live_grant.grant_to_story)
    assert "mount, relation = await grant(" in src, (
        "grant_to_story discards the upgraded mount")


def test_the_go_is_told_the_relation_that_was_stored():
    """The GO reads this reply to know what its story holds. Echoing
    the request back would say `needs` about a tree it may write."""
    from kernel.go import tools
    src = inspect.getsource(tools.grant_resource)
    assert 'out.get("relation")' in src


# ── 3. and it actually writes the row ────────────────────────────────

@pytest.mark.asyncio
async def test_a_hand_started_errand_grant_is_stored_writable():
    """END TO END through `mounts.grant`, the real entry point, with
    the exact shape f87d306f arrived in: kind='branch', relation
    'needs', on a story whose work_kind is 'errand'."""
    from kernel.workspace.mounts import Mount, grant

    stored: dict = {}
    deleted: list = []

    class _Conn:
        async def fetchval(self, sql, *a):
            return "errand" in sql and True   # _is_errand
        async def execute(self, sql, *a):
            if sql.strip().startswith("DELETE"):
                deleted.append(a)
                return
            stored["relation"] = a[2]
            stored["notes"] = a[9]

    mount = Mount(at="repos/acme__app@lane", binding="git", kind="branch",
                  ref="lane", resource_id="r1", writable=False)
    got, relation = await grant(_Conn(), "G1", "s1", mount,
                                granted_by="go:g1", relation="needs",
                                why="Read the backend endpoints")

    assert relation == "modifies", "the row would mount read-only"
    assert stored["relation"] == "modifies"
    assert got.writable is True, (
        "the Mount the caller records against the live execution must "
        "be writable too, or the running leg still cannot write")
    assert "Read the backend endpoints" in stored["notes"]
    assert errand_write.NOTE in stored["notes"]
    assert deleted, (
        "a resource already stored `needs` must lose that row -- "
        "ON CONFLICT keys on the relation, so it would otherwise be "
        "joined by a second row and one resource would have two answers")


@pytest.mark.asyncio
async def test_a_coding_story_read_grant_is_untouched():
    """The gate, from the other side. Only an errand is upgraded."""
    from kernel.workspace.mounts import Mount, grant

    stored: dict = {}

    class _Conn:
        async def fetchval(self, sql, *a):
            return False                       # not an errand
        async def execute(self, sql, *a):
            stored["relation"] = a[2]

    mount = Mount(at="repos/acme__app", binding="git", kind="repo",
                  resource_id="r1", writable=False)
    got, relation = await grant(_Conn(), "G1", "s1", mount,
                                relation="needs", why="context")

    assert relation == "needs"
    assert stored["relation"] == "needs"
    assert got.writable is False
