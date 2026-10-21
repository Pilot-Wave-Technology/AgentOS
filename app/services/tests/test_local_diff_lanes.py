"""A multi-lane diff submission is the WHOLE story, or it is refused.

Each of these used to be accepted and merely flagged "unverified": a lane sent
with no commit, a lane naming a repo the story has no branch in, a lane the
client left out, and two lanes of one repo sent without saying which is which.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tests.local_http import local_http


async def _two_lane_story(h, same_repo=False):
    goal, story = await h.seed_story(phase="executing")
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, status) VALUES ($1, $2, 1, 'approved')",
        str(uuid.uuid4()), story)
    if same_repo:
        rid = uuid.uuid4().hex
        await h.conn.execute("INSERT INTO git_repos (id, owner, name) VALUES ($1, 'acme', 'mono')", rid)
        for trunk in ("main", "release"):
            nid = uuid.uuid4().hex
            await h.conn.execute(
                "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, trunk_branch) "
                "VALUES ($1, $2, $3, $4, 'BRANCH_CREATED', $5)", nid, rid, goal, f"feat/{story}-{trunk}", trunk)
            await h.conn.execute("INSERT INTO git_node_stories (git_node_id, story_id, seq) VALUES ($1, $2, 1)", nid, story)
    else:
        await h.seed_node(story, goal, name="api")
        await h.seed_node(story, goal, name="web")
    return goal, story


def _entry(name, sha="abc123", trunk="main"):
    return {"name": name, "trunk": trunk, "diff": "d", "stat": "s", "truncated": False, "commit_sha": sha}


async def _submit(story, repos):
    import app.services.local_diff as ld
    with patch.object(ld, "verify_commit_pushed", AsyncMock(return_value=("verified", ""))), \
         patch.object(ld, "post_diff_review", AsyncMock(return_value={
             "ok": True,
             # A UUID, as the real `post_diff_review` returns. "a1" passed
             # only while nothing read the id; the settle reads it.
             "approval_id": "00000000-0000-4000-8000-0000000000a1"})):
        return await ld.submit_local_diff(story, "", "", False, "", repos=repos)


@pytest.mark.asyncio
async def test_every_lane_with_its_commit_is_accepted():
    async with local_http() as h:
        _, story = await _two_lane_story(h)
        assert (await _submit(story, [_entry("acme/api"), _entry("acme/web")]))["ok"] is True


@pytest.mark.asyncio
async def test_a_lane_with_no_commit_is_refused():
    async with local_http() as h:
        _, story = await _two_lane_story(h)
        r = await _submit(story, [_entry("acme/api"), _entry("acme/web", sha="")])
        assert (r["ok"], r["reason"]) == (False, "lane_without_commit")


@pytest.mark.asyncio
async def test_a_repo_the_story_has_no_lane_in_is_refused():
    async with local_http() as h:
        _, story = await _two_lane_story(h)
        r = await _submit(story, [_entry("acme/api"), _entry("acme/web"), _entry("acme/elsewhere")])
        assert (r["ok"], r["reason"]) == (False, "unknown_lane")


@pytest.mark.asyncio
async def test_leaving_a_lane_out_is_refused_and_names_it():
    async with local_http() as h:
        _, story = await _two_lane_story(h)
        r = await _submit(story, [_entry("acme/api")])
        assert (r["ok"], r["reason"]) == (False, "lane_missing")
        assert "acme/web" in r["error"]


@pytest.mark.asyncio
async def test_two_lanes_of_one_repo_must_say_which_is_which():
    async with local_http() as h:
        _, story = await _two_lane_story(h, same_repo=True)
        r = await _submit(story, [_entry("acme/mono", trunk=""), _entry("acme/mono", trunk="")])
        assert (r["ok"], r["reason"]) == (False, "unknown_lane")
        ok = await _submit(story, [_entry("acme/mono", trunk="main"), _entry("acme/mono", trunk="release")])
        assert ok["ok"] is True


@pytest.mark.asyncio
async def test_a_lane_already_merged_is_not_required():
    async with local_http() as h:
        goal, story = await _two_lane_story(h)
        await h.conn.execute(
            "UPDATE git_nodes SET state = 'MERGED' WHERE id IN (SELECT git_node_id FROM git_node_stories "
            "WHERE story_id = $1) AND git_repo_id IN (SELECT id FROM git_repos WHERE name = 'web')", story)
        assert (await _submit(story, [_entry("acme/api")]))["ok"] is True
