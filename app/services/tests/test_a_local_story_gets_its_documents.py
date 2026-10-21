"""The documents a local story was granted reach its workspace.

THE LAYOUT ALREADY PROMISED THEM. `local_bundle._layout_note` draws
the checkout root for every local session and names a directory in it:

    |- docs/               resources served to you  (READ-ONLY)

and nothing in the local lane ever created it. The pod lane does --
`mounts.story_resources` maps each granted document to `docs/<name>`
and the workspace builder fetches the bytes from
`/internal/resource-object` -- so the same story could read its brief
in a pod and not in the editor, while being told in both that it
could. Observed on CALY5/f4f2e9c4: imported locally with resources
attached, no `docs/` on disk.

That is worse than the documents being absent. A session pointed at a
directory that does not exist does not conclude the system is wrong;
it concludes it has misread the layout, and goes looking.

THE GRANT IS THE LIST. `story_resources` is the grant table, and it is
the same join `/internal/resource-object` authorises each read
against -- so what the bundle offers and what the download allows
cannot drift apart. The goal's catalogue is NOT the list: a goal may
hold twenty documents and a story be entitled to two.
"""
from __future__ import annotations

import inspect

import pytest

from app.services.local_resources import docs_for_story, safe_name
from app.services.tests.local_http import local_http


async def _granted(h, *, kind: str = "document", name: str = "Brief.pdf",
                   uri: str = "gs://bucket/brief.pdf"):
    goal, story = await h.seed_story(state="accepted")
    # `goal_resources.id` has no default — the application mints it.
    rid = await h.conn.fetchval(
        "INSERT INTO goal_resources (id, goal_id, kind, name, gcs_uri, created_by) "
        "VALUES (gen_random_uuid(),$1,$2,$3,$4,'u1') RETURNING id",
        goal, kind, name, uri)
    await h.conn.execute(
        "INSERT INTO story_resources (story_id, resource_id, relation) "
        "VALUES ($1,$2,'needs')", story, rid)
    return goal, story, str(rid)


# ── the list ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_granted_document_is_listed():
    async with local_http() as h:
        _, story, rid = await _granted(h)
        docs = await docs_for_story(h.conn, story)
        assert [d["resource_id"] for d in docs] == [rid]
        assert docs[0]["filename"] == "Brief.pdf"


@pytest.mark.asyncio
async def test_a_document_the_story_was_not_granted_is_not_listed():
    """The goal's catalogue is not the story's entitlement. Handing the
    editor every document in the goal would both leak and drown."""
    async with local_http() as h:
        goal, story, _ = await _granted(h)
        await h.conn.execute(
            "INSERT INTO goal_resources (id, goal_id, kind, name, gcs_uri, created_by) "
            "VALUES (gen_random_uuid(),$1,'document','Secret.pdf',"
            "'gs://b/s.pdf','u1')", goal)
        docs = await docs_for_story(h.conn, story)
        assert [d["name"] for d in docs] == ["Brief.pdf"]


@pytest.mark.asyncio
async def test_a_grant_with_no_stored_object_is_skipped():
    """A catalogue entry with nothing behind it would promise the
    extension a download that can only 404."""
    async with local_http() as h:
        _, story, _ = await _granted(h, uri="")
        assert await docs_for_story(h.conn, story) == []


@pytest.mark.asyncio
async def test_a_repository_is_not_a_document():
    """Repos are cloned, with their own tokens, and arrive as `repos[]`.
    Listing one here would have the extension try to download a git
    remote as a file."""
    async with local_http() as h:
        _, story, _ = await _granted(h, kind="repo",
                                     uri="https://github.com/o/n.git")
        assert await docs_for_story(h.conn, story) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["doc", "document", "file", "dataset"])
async def test_every_document_kind_the_pod_takes_is_taken_here(kind):
    """`doc` is the kernel's older word and `document` is what upload
    writes. The two lanes disagreeing about what counts as a document
    has cost a story before — so the list is imported, not retyped."""
    async with local_http() as h:
        _, story, _ = await _granted(h, kind=kind)
        assert len(await docs_for_story(h.conn, story)) == 1, kind


@pytest.mark.asyncio
async def test_two_names_that_sanitise_alike_do_not_overwrite():
    """The catalogue enforces unique NAMES per (goal, kind), so the
    collision cannot come from two identical names — it comes from the
    sanitiser, which maps every awkward character to the same dash.
    `a/b.pdf` and `a:b.pdf` are two different documents and one
    filename, and silently writing one over the other loses a document
    the session was promised."""
    async with local_http() as h:
        goal, story, _ = await _granted(h, name="a/b.pdf")
        rid2 = await h.conn.fetchval(
            "INSERT INTO goal_resources (id, goal_id, kind, name, gcs_uri, "
            "created_by) VALUES (gen_random_uuid(),$1,'document','a:b.pdf',"
            "'gs://b/2.pdf','u1') RETURNING id", goal)
        await h.conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1,$2,'needs')", story, rid2)

        names = [d["filename"] for d in await docs_for_story(h.conn, story)]
        assert len(names) == 2
        assert len(set(names)) == 2, names


@pytest.mark.asyncio
async def test_a_listing_failure_does_not_break_the_checkout():
    """This rides on the hydration bundle. A story whose documents
    cannot be listed must still be workable — the repo, the persona and
    the plan are what nothing happens without."""
    class _Broken:
        async def fetch(self, *a, **kw):
            raise RuntimeError("no")
    assert await docs_for_story(_Broken(), "s1") == []


# ── the filename ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Brief.pdf", "Brief.pdf"),
    ("Q3 Report (final).xlsx", "Q3 Report -final-.xlsx"),
    ("../../id_rsa", "id_rsa"),
    ("..", "resource-abcd1234"),
    ("", "resource-abcd1234"),
])
def test_the_filename_cannot_escape_the_docs_directory(raw, expected):
    """A person typed the catalogue name, so it can hold anything, and
    `docs/../../id_rsa` is the obvious thing it must never hold."""
    got = safe_name(raw, "abcd1234-0000")
    assert got == expected
    assert "/" not in got and "\\" not in got
    assert ".." not in got


def test_an_awkward_name_still_arrives():
    """Stripped, not rejected: a document with an odd name should still
    reach the session, under a name it can read."""
    assert safe_name("répôrt ✓.pdf", "x") not in ("", None)


# ── the wiring ───────────────────────────────────────────────────────

def test_the_bundle_carries_the_list():
    from app.services import local_hydrate
    src = inspect.getsource(local_hydrate.build_hydration_bundle)
    assert '"docs"' in src and "docs_for_story" in src


def test_the_bytes_are_not_inlined_in_the_bundle():
    """A granted PDF can be 25MB and the bundle is parsed as JSON in
    memory by the extension."""
    from app.services import local_resources
    src = inspect.getsource(local_resources.docs_for_story)
    for byte_ish in ("download_resource_uri", "b64", "content"):
        assert byte_ish not in src, byte_ish


def test_the_download_is_grant_scoped_like_the_pods():
    """Taking a bare `gs://` would make this an open proxy onto the
    resource bucket for anybody holding any story token."""
    from app.routers import local_execution
    src = inspect.getsource(local_execution.local_resource_object)
    assert "FROM story_resources sr" in src
    assert "_enforce_local_scope" in src
    assert "verify_internal_bearer" in src


def test_the_two_lanes_share_the_document_kinds():
    from app.services.local_resources import DOC_KINDS as local_kinds
    from kernel.workspace.mounts import DOC_KINDS as pod_kinds
    assert tuple(local_kinds) == tuple(pod_kinds), (
        "the local lane and the pod disagree about what a document is")
