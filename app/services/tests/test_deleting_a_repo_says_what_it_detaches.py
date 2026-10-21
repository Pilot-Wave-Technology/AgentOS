"""Removing a resource with dependents is visible, in the page and in the API.

THREE FACES OF ONE ASSUMPTION -- that a branch is reachable only through
its repo row -- each failing differently and none of them erroring. The
resolver is covered by `test_a_lane_outlives_its_repo_row`. Here are the
other two, plus the ledger-ordering bug found alongside them.

1. THE PAGE dropped the subtree. Roots were rows with no `parent_id` and a
   child was drawn only via `children_map[root.id]`, so a row whose parent
   had been filtered out rendered nowhere and no query string could reveal
   it. That is what made the GOAL1 incident unrecoverable through the
   product rather than merely wrong.

2. THE DELETE said nothing. `delete_resource` was written for documents --
   soft-delete one row, drop the blob, clean the workspaces -- and repos
   were later routed through the same endpoint. It never learned that a
   repo carries lanes and that stories are granted the LANES.

3. THE LEDGER described a row that was never created, because the
   registered event was appended before the dedupe rebound `resource_id`.
"""
from __future__ import annotations

import contextlib
import io
import pathlib

import pytest

from app.services.resource_tree import build_tree
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ── 1. the page ──────────────────────────────────────────────────────────

def test_an_orphan_is_re_rooted_not_dropped():
    # The GOAL1 shape: the repo row is absent (deleted, so not fetched) and
    # its three lanes remain.
    rows = [
        {"id": "lane-a", "parent_id": "repo-gone"},
        {"id": "lane-b", "parent_id": "repo-gone"},
        {"id": "lane-c", "parent_id": "repo-gone"},
    ]
    roots, children = build_tree(rows)
    assert [r["id"] for r in roots] == ["lane-a", "lane-b", "lane-c"], (
        "rows whose parent is missing render nowhere"
    )
    assert children == {}, "a child was filed under a key nothing reads"
    assert all(r["orphaned_from"] == "repo-gone" for r in roots), (
        "nothing marks the row, so it reads as a normal standalone resource"
    )


def test_a_present_parent_still_nests():
    rows = [
        {"id": "repo-1", "parent_id": None},
        {"id": "lane-a", "parent_id": "repo-1"},
    ]
    roots, children = build_tree(rows)
    assert [r["id"] for r in roots] == ["repo-1"]
    assert [c["id"] for c in children["repo-1"]] == ["lane-a"]
    assert "orphaned_from" not in rows[1], "a nested row was marked orphaned"


def test_order_is_preserved():
    # The query orders by (kind, name) and the page relies on it.
    rows = [{"id": f"r{i}", "parent_id": None} for i in range(5)]
    roots, _ = build_tree(rows)
    assert [r["id"] for r in roots] == [f"r{i}" for i in range(5)]


def test_the_template_badges_a_re_rooted_row():
    html = _src("app/templates/resources/browser.html")
    assert "orphaned_from" in html, (
        "the page re-roots orphans but gives the operator no sign of it"
    )


# ── 2. the delete ────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _fixture(conn, sid: str, *, story_state="in_progress"):
    """A repo with one lane, and a story granted the lane."""
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "dep fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO goal_resources (id,goal_id,kind,name,role,status,"
        " repo_owner,repo_name,created_by) VALUES "
        "($1,$2,'repo','agentOS','feature','active','o','n',$3)",
        f"{sid}-repo", gid, f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO goal_resources (id,goal_id,kind,name,role,status,"
        " branch_name,parent_id,created_by) VALUES "
        "($1,$2,'branch','n:dev','trunk','active','dev',$3,$4)",
        f"{sid}-lane", gid, f"{sid}-repo", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state,story_kind) "
        "VALUES ($1,$2,'dep story',$3,$4,'code')",
        f"{sid}-story", gid, f"{PREFIX}-user", story_state)
    await conn.execute(
        "INSERT INTO story_resources (story_id,resource_id,relation) "
        "VALUES ($1,$2,'modifies')", f"{sid}-story", f"{sid}-lane")
    return gid


@pytest.mark.asyncio
async def test_a_repo_reports_the_lane_and_the_story_it_would_strand():
    from app.services.resource_dependents import blocking_dependents

    async with _conn() as c:
        sid = f"{PREFIX}-dep1"
        gid = await _fixture(c, sid)
        deps = await blocking_dependents(c, gid, f"{sid}-repo", "repo")

    from app.services.resource_dependents import (
        describe_children, describe_stories,
    )

    assert [x["id"] for x in deps["children"]] == [f"{sid}-lane"]
    assert [s["id"] for s in deps["stories"]] == [f"{sid}-story"], (
        "the story is granted the LANE, not the repo -- reporting only the "
        "repo's own grants calls a repo with stranded dependents free"
    )
    assert "dev" in describe_children(deps["children"])
    assert f"{sid}-story" in describe_stories(deps["stories"])


@pytest.mark.asyncio
async def test_a_finished_story_does_not_block():
    from app.services.resource_dependents import blocking_dependents

    async with _conn() as c:
        sid = f"{PREFIX}-dep2"
        gid = await _fixture(c, sid, story_state="reviewed")
        deps = await blocking_dependents(c, gid, f"{sid}-repo", "repo")

    assert deps["stories"] == [], "a finished grant is history, not an objection"
    assert deps["children"], "the lane is still attached and still counts"


@pytest.mark.asyncio
async def test_a_document_is_never_blocked():
    from app.services.resource_dependents import blocking_dependents

    async with _conn() as c:
        sid = f"{PREFIX}-dep3"
        gid = await _fixture(c, sid)
        deps = await blocking_dependents(c, gid, f"{sid}-repo", "document")

    assert deps == {"children": [], "stories": []}, (
        "documents have no dependents; guarding them would make every "
        "upload undeletable"
    )


def test_the_endpoint_refuses_and_offers_cascade():
    src = _src("app/routers/goal_resources.py")
    body = src[src.index("async def delete_resource"):]
    body = body[:body.index("\n@router.")]
    assert "blocking_dependents" in body, "the delete asks nothing before removing"
    assert "cascade" in body, "there is no way to proceed deliberately"
    assert "status = 'archived'" in body, (
        "the cascade deletes the children instead of archiving them -- a lane "
        "the goal may still want is not ours to destroy as a side effect"
    )


# ── 3. the ledger ────────────────────────────────────────────────────────

def test_the_registered_event_names_the_row_that_survives():
    """`resource_id` must be rebound to the reused row BEFORE the event.

    Asserted on source order because the defect IS the order, and the
    symptom -- a ledger id with no `goal_resources` and no `resource_events`
    row behind it -- is only observable by reconciling two tables after the
    fact. GOAL1 carries one such ghost: 01b84264-b1a.
    """
    src = _src("app/routers/goal_resources.py")
    head = src[src.index("async def add_resource"):
               src.index("async def _create_gcp_resource_with_sas")]
    rebind = head.index('resource_id = existing_repo["id"]')
    event = head.index("event_type=GOAL_RESOURCE_REGISTERED")
    assert rebind < event, (
        "the registered event is appended before the dedupe decides which "
        "id survives, so a re-registration logs an id that never exists"
    )
    assert '"reused": reused' in head or '"reused":reused' in head, (
        "the event does not record that it was a reuse rather than a create"
    )


def test_archive_is_guarded_too():
    """Archive and Delete sit next to each other and do the same damage.

    Every lane and provisioning query filters `status = 'active'`, so an
    archived trunk lane is as absent from the model as a deleted one. While
    archive was a blind UPDATE it was a one-click bypass of the guard on the
    button beside it.

    The ENGINE archives merged branches by direct SQL (git_graph/branches.py,
    branch_cleanup.py), not through this endpoint, so guarding it here does
    not interfere with cleanup -- and a merged story is excluded from
    `granted_to_stories` anyway.
    """
    src = _src("app/routers/goal_resources.py")
    body = src[src.index("async def archive_resource"):]
    body = body[:body.index("\n@router.")]
    assert "blocking_dependents" in body, (
        "Archive bypasses the dependency guard that Delete enforces"
    )
    assert 'deps["stories"]' in body
    assert "cascade" not in body, (
        "archive offers a bypass for story grants; there must not be one"
    )


# ── 4. the body read that turned every delete into a 500 ────────────────

class _FakeRequest:
    """Enough of a Request for `wants_cascade`."""

    def __init__(self, *, query=None, form=None, form_raises=None):
        self.query_params = query or {}
        self._form = form
        self._raises = form_raises

    async def form(self):
        if self._raises:
            raise self._raises
        if self._form is None:
            raise AssertionError("form() should not have been reached")
        return self._form


@pytest.mark.asyncio
async def test_an_unparseable_body_is_not_a_cascade_and_not_a_500():
    """The Delete form has NO input fields, so the shared interceptor posts
    an empty FormData -- a multipart body that is just the closing boundary,
    which python-multipart rejects. A bare `await request.form()` here made
    every delete a 500, including the ones the guard was about to refuse
    with a useful message. Found on dev, goal MREP1, 2026-10-06.
    """
    from multipart.exceptions import MultipartParseError

    from app.routers.goal_resources import wants_cascade

    req = _FakeRequest(form_raises=MultipartParseError(
        "Did not find CR at end of boundary (40)"))
    assert await wants_cascade(req) is False


@pytest.mark.asyncio
async def test_cascade_can_be_asked_for_in_the_query_string():
    from app.routers.goal_resources import wants_cascade

    # Reached without touching the body at all -- `form()` would assert.
    assert await wants_cascade(_FakeRequest(query={"cascade": "1"})) is True


@pytest.mark.asyncio
async def test_cascade_in_the_body_still_works():
    from app.routers.goal_resources import wants_cascade

    assert await wants_cascade(_FakeRequest(form={"cascade": "on"})) is True
    assert await wants_cascade(_FakeRequest(form={})) is False


# ── 5. a retired lane can be added back ─────────────────────────────────

def test_the_duplicate_check_ignores_retired_lanes():
    """Deleting a trunk lane was irreversible through the UI.

    The duplicate check had no status filter, so a deleted or archived lane
    went on refusing its own branch for ever -- "'X' is already a trunk lane
    of R in this goal" -- while `unarchive_resource` accepts only
    status='archived' and so could not bring a DELETED one back. Between
    them there was no way to re-add a lane you had removed, which is the
    dead end the GOAL1 operator was in. Found on dev the first time anyone
    deleted a lane and tried again (MREP1, 2026-10-06).
    """
    src = _src("app/routers/goal_resources.py")
    body = src[src.index("async def add_resource"):
               src.index("async def _create_gcp_resource_with_sas")]

    dup = body[body.index("dup = await pool.fetchval"):]
    dup = dup[:dup.index("if dup:")]
    assert "coalesce(status, 'active') = 'active'" in dup, (
        "a retired lane still blocks its own branch from being re-added"
    )

    assert "revive_lane" in body, (
        "nothing brings the retired row back, so re-adding would mint a "
        "second row for the same branch -- and the INSERT carries "
        "ON CONFLICT (goal_id, kind, name) DO NOTHING, so it would in fact "
        "produce no row at all, silently"
    )
    revive = body[body.index("if revive_lane:"):]
    revive = revive[:revive.index("else:")]
    assert "status = 'active'" in revive, "the revived row is not reactivated"
    assert "parent_id = $4" in revive, (
        "the revived lane keeps its old parent, which may be the very repo "
        "row that was deleted"
    )


def test_reviving_reuses_the_id_rather_than_minting_one():
    """`story_resources` grants and `parent_id` chains point at the id, so
    reuse keeps those intact and keeps the resource's history in one place."""
    src = _src("app/routers/goal_resources.py")
    body = src[src.index("async def add_resource"):
               src.index("async def _create_gcp_resource_with_sas")]
    revive = body[body.index("if revive_lane:"):]
    revive = revive[:revive.index("else:")]
    assert "working_branch_id = revive_lane" in revive, (
        "the revived row is updated but the rest of the handler goes on "
        "using a freshly minted id"
    )
