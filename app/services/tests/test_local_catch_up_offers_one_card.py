"""The local catch-up sweep: one card, the right stories, and no `ds_*`.

TRUNK MOVES UNDER A BRANCH THAT IS WAITING TO MERGE, and somebody has
to bring it up to date. In the cloud that is the story's own parked run,
offered a plan it approves. Locally there is no run to park, so the
offer is a plan card and the person approving it is the person in the
editor.

THE FAILURES THIS GUARDS AGAINST ARE ALL QUIET ONES: two cards for one
piece of work (the second overwrites the first's pending plan), an
offer naming a trunk position nobody will merge to any more, an errand
offered a catch-up for a merge that will never happen, and the engine
cutting a `ds_*` diff story for a branch the story is already
integrating -- which on bc9679f4 produced two integrations and a stray
assistant thread nobody asked for.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _behind(h, story: str, goal: str, *, owner="acme", name="api",
                  trunk="main", sha="bbbb", seen_ahead=True) -> str:
    """A node whose trunk has moved since it was cut."""
    node = await h.seed_node(story, goal, state="BRANCH_CREATED",
                             owner=owner, name=name, trunk=trunk)
    repo = await h.conn.fetchval(
        "SELECT git_repo_id FROM git_nodes WHERE id = $1", node)
    await h.conn.execute(
        "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, trunk_sha, "
        "   trunk_sha_seen_at) VALUES ($1, $2, $3, "
        "   now() + interval '1 hour') "
        "ON CONFLICT (git_repo_id, trunk_branch) DO UPDATE "
        "   SET trunk_sha = EXCLUDED.trunk_sha, "
        "       trunk_sha_seen_at = EXCLUDED.trunk_sha_seen_at",
        repo, trunk, sha)
    await h.conn.execute(
        "UPDATE git_nodes SET base_sha = 'aaaa' WHERE id = $1", node)
    return node


async def _advance_trunk(h, sha: str, *, owner="acme", name="api",
                         trunk="main") -> None:
    """Trunk moved again, without re-seeding the repo."""
    await h.conn.execute(
        "UPDATE git_repo_trunks SET trunk_sha = $1 "
        "  WHERE git_repo_id = (SELECT id FROM git_repos "
        "                        WHERE owner = $2 AND name = $3) "
        "    AND trunk_branch = $4", sha, owner, name, trunk)


async def _waiting(h, **kw):
    goal, story = await h.seed_story(state="awaiting_merge", **kw)
    await h.conn.execute(
        "UPDATE stories SET execution_mode = 'local', "
        "       current_phase = 'awaiting_merge' WHERE id = $1", story)
    import uuid as _uuid
    tid = str(_uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, status) "
        "VALUES ($1, $2, 'person', 'story', 'active')", tid, goal)
    await h.conn.execute(
        "UPDATE stories SET active_thread_id = $2 WHERE id = $1", story, tid)
    return goal, story


async def _sweep(h) -> int:
    from app.services.local_integration import sweep
    return await sweep(h.conn)


# ── who is offered one ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_behind_local_story_is_offered_a_catch_up():
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        assert await _sweep(h) == 1
        row = await h.conn.fetchrow(
            "SELECT pending_plan_md, pending_plan_path, current_phase "
            "  FROM stories WHERE id = $1", story)
        assert row["pending_plan_md"]
        assert row["current_phase"] == "awaiting_plan_approval"


@pytest.mark.asyncio
async def test_a_story_that_is_not_behind_is_left_alone():
    async with local_http() as h:
        goal, story = await _waiting(h)
        node = await _behind(h, story, goal)
        await h.conn.execute(
            "UPDATE git_nodes SET last_synced_trunk_sha = 'bbbb' WHERE id = $1",
            node)
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_an_errand_is_never_offered_a_catch_up():
    """Its branch never lands, so catching it up is work for a merge the
    platform guarantees will not happen."""
    async with local_http() as h:
        goal, story = await _waiting(h, work_kind="errand")
        await _behind(h, story, goal)
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_merged_lane_is_not_counted_as_behind():
    async with local_http() as h:
        goal, story = await _waiting(h)
        node = await _behind(h, story, goal)
        await h.conn.execute(
            "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", node)
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_story_still_being_written_is_not_offered_one():
    """Approving a plan REPLACES the story's contract. Handing an
    integration plan to a story mid-work re-scopes it to the integration
    checks, for the rest of the run, with no way back."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await h.conn.execute(
            "UPDATE stories SET state = 'in_progress' WHERE id = $1", story)
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_CLOUD_story_is_not_picked_up_by_this_sweep():
    """RULE ZERO. The pod lane has its own sweep, which parks the run on
    a question. Two offers for one branch is the bug this avoids."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await h.conn.execute(
            "UPDATE stories SET execution_mode = 'operator' WHERE id = $1",
            story)
        assert await _sweep(h) == 0


# ── one card, however many lanes ─────────────────────────────────────

@pytest.mark.asyncio
async def test_two_behind_lanes_get_ONE_card_naming_both():
    """Per node it would be two cards for one piece of work, and the
    second would overwrite the first's pending plan."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, name="api", sha="bbbb")
        await _behind(h, story, goal, name="web", sha="cccc")
        assert await _sweep(h) == 1
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "acme/api" in md and "acme/web" in md


@pytest.mark.asyncio
async def test_sweeping_twice_does_not_re_offer():
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        assert await _sweep(h) == 1
        assert await _sweep(h) == 0


@pytest.mark.asyncio
async def test_a_newer_trunk_sha_rewrites_the_pending_offer():
    """An offer naming a position trunk has already left is worse than
    none: the person approves bringing in a sha nobody will merge to."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, sha="bbbb")
        await _sweep(h)
        first = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        await _advance_trunk(h, "dddd")
        assert await _sweep(h) == 1
        second = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert second != first
        assert "dddd" in second


# ── what the plan says ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_plan_carries_the_storys_own_checks_and_the_suite():
    """Approving a plan with checks replaces the contract, so a catch-up
    that carried none would quietly narrow what the story is judged
    against to the suite alone."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await submit_local_plan(
            goal, story, ["do it"], "because",
            [{"run": "cd repos/acme__api && pytest -q", "label": "goal"}])
        await _behind(h, story, goal)
        await _sweep(h)
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "pytest -q" in md, "the story's own check was dropped"
        assert "git grep" in md, "the conflict-marker guard is missing"


@pytest.mark.asyncio
async def test_the_mounts_are_workspace_relative():
    """The checks are written here and run there. The only path both
    ends agree on is the relative one."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "repos/acme__api" in md
        assert "/workspace/" not in md and "C:" not in md


@pytest.mark.asyncio
async def test_the_catch_up_plan_does_not_overwrite_the_work_plan():
    """`plan-01.md` is on the branch and in the pull request: it is the
    record of what was agreed. A machine-written catch-up must not be
    written over it."""
    from app.services.local_integration import integration_plan_path

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        path = await h.conn.fetchval(
            "SELECT pending_plan_path FROM stories WHERE id = $1", story)
        assert path.endswith("-trunk.md")
        assert path == await integration_plan_path(h.conn, story)


# ── and approving one ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approving_it_lands_the_story_in_integrating():
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        assert (await approve_local_plan(goal, story, "person"))["ok"]
        assert await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1",
            story) == "integrating"


@pytest.mark.asyncio
async def test_approving_it_does_not_consume_a_work_cycle():
    """Catching up is not a new intent. Consuming a cycle would renumber
    the plan files underneath a person mid-story."""
    from app.services.local_cycles import current_cycle
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        await approve_local_plan(goal, story, "person")
        assert await current_cycle(h.conn, story) == 1
        assert await h.conn.fetchval(
            "SELECT kind FROM plan_versions WHERE story_id = $1", story
        ) == "integration"


@pytest.mark.asyncio
async def test_the_instruction_says_take_trunk_in_not_start_the_work():
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        await approve_local_plan(goal, story, "person")
        said = " ".join(r["content"] or "" for r in await h.conn.fetch(
            "SELECT content FROM messages WHERE thread_id = "
            "  (SELECT active_thread_id FROM stories WHERE id = $1) "
            "  AND role = 'human'", story))
        assert "trunk" in said.lower()
        assert "Start the work" not in said


@pytest.mark.asyncio
async def test_the_pending_plan_says_it_is_a_catch_up():
    """A catch-up reuses the plan card, so without this the panel shows
    something that looks exactly like a plan the person wrote -- when it
    is platform-written, is about taking a named trunk commit in, and
    carries checks the story never had. Approving REPLACES the story's
    contract, so which one it is decides what the story is judged
    against. The card has always recorded it; `get-state` had no way to
    pass it on."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _sweep(h)
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        assert r.json()["pending_plan"]["kind"] == "integration"


@pytest.mark.asyncio
async def test_an_ordinary_plan_is_still_called_work():
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await submit_local_plan(goal, story, ["do it"], "because",
                                [{"run": "cd repos/acme__api && pytest", "label": "goal"}])
        r = await h.post("/get-state", {"story_id": story},
                         token=h.story_token(story, goal))
        assert r.json()["pending_plan"]["kind"] == "work"


@pytest.mark.asyncio
async def test_an_approved_catch_up_is_not_offered_again():
    """THE LOOP. Approval CLEARS `pending_plan_md`, so the next tick saw
    nothing parked and offered again -- and the lane still read behind,
    because `last_synced_trunk_sha` is written by the ENGINE from
    GitHub's compare and had not caught up yet. On ec6d5d1f that made
    two approved catch-ups for one trunk move and a third parked behind
    them, each one re-asking a person who had already said yes."""
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        assert await _sweep(h) == 1
        assert (await approve_local_plan(goal, story, "person"))["ok"]
        assert await _sweep(h) == 0, (
            "it re-offered a catch-up that has been approved and not yet "
            "delivered")


@pytest.mark.asyncio
async def test_once_the_catch_up_is_DELIVERED_a_later_move_is_offered():
    """The guard is "not yet delivered", not "ever approved". Trunk
    moving again after a catch-up lands is a new fact and deserves a
    new offer."""
    from app.services.local_cycles import spend_cycle
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, sha="bbbb")
        await _sweep(h)
        await approve_local_plan(goal, story, "person")
        await spend_cycle(h.conn, story, "submit")

        await _advance_trunk(h, "eeee")
        assert await _sweep(h) == 1


# ── two lanes of ONE repository ──────────────────────────────────────
#
# The case the derivation could not express. A story cutting one repo
# against two trunks has TWO clones, and the mount used to be computed
# from the repository name alone -- so both lanes named the same
# directory. On 06295e15 the conflict report printed that mount twice,
# one working tree was told to merge two different trunks into itself,
# and the other was never touched. `story_workspace_nodes.subdir` has
# always had the answer (`@<lane>` on the second clone); it just was not
# being read.

async def _second_lane(h, story: str, goal: str, *, trunk: str, sha: str,
                       owner="acme", name="api", branch="f/other") -> str:
    """ANOTHER lane of a repo this story already has a node on.

    Not `_behind`, which creates the repository: a second lane is the
    SAME repository, and that sharing is the whole point of the case.
    """
    import uuid as _uuid
    repo = await h.conn.fetchval(
        "SELECT id FROM git_repos WHERE owner = $1 AND name = $2", owner, name)
    nid = _uuid.uuid4().hex
    await h.conn.execute(
        "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, "
        "   trunk_branch, base_sha) VALUES ($1,$2,$3,$4,'BRANCH_CREATED',$5,'aaaa')",
        nid, repo, goal, branch, trunk)
    await h.conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
        "VALUES ($1,$2,2)", nid, story)
    await h.conn.execute(
        "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, trunk_sha, "
        "   trunk_sha_seen_at) VALUES ($1,$2,$3, now() + interval '1 hour') "
        "ON CONFLICT (git_repo_id, trunk_branch) DO UPDATE "
        "   SET trunk_sha = EXCLUDED.trunk_sha, "
        "       trunk_sha_seen_at = EXCLUDED.trunk_sha_seen_at",
        repo, trunk, sha)
    return nid


async def _record_clone(h, story: str, subdir: str, *,
                        owner="acme", name="api", trunk="main",
                        branch="feature/x") -> None:
    """The clone the workspace actually has on disk, as provisioning records it."""
    repo = await h.conn.fetchval(
        "SELECT id FROM git_repos WHERE owner = $1 AND name = $2", owner, name)
    await h.conn.execute(
        "INSERT INTO story_workspace_nodes (story_id, git_repo_id, branch_name, "
        "   subdir, is_write, repo_owner, repo_name, trunk_branch) "
        "VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7) "
        "ON CONFLICT DO NOTHING",
        story, repo, branch, subdir, owner, name, trunk)


@pytest.mark.asyncio
async def test_two_lanes_of_one_repo_get_TWO_mounts():
    """The bug, stated as a test: one repo, two trunks, two directories.

    A single mount named twice is not a cosmetic duplicate -- it is the
    merge driver being handed one working tree for two different trunk
    shas, and the other clone keeping its conflict unseen.
    """
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, trunk="cal_backend", sha="b1")
        await _second_lane(h, story, goal, trunk="cal_frontend", sha="f1")
        await _record_clone(h, story, "repos/acme__api@cal_backend",
                            trunk="cal_backend", branch="f/back")
        await _record_clone(h, story, "repos/acme__api@cal_frontend",
                            trunk="cal_frontend", branch="f/front")

        assert await _sweep(h) == 1
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "repos/acme__api@cal_backend" in md
        assert "repos/acme__api@cal_frontend" in md


@pytest.mark.asyncio
async def test_each_lane_gets_its_OWN_suite_check():
    """The checks dedupe on the mount. Collapsing two lanes to one
    therefore dropped a whole clone's suite, and the story landed with
    half of it never run."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, trunk="cal_backend", sha="b1")
        await _second_lane(h, story, goal, trunk="cal_frontend", sha="f1")
        await _record_clone(h, story, "repos/acme__api@cal_backend",
                            trunk="cal_backend", branch="f/back")
        await _record_clone(h, story, "repos/acme__api@cal_frontend",
                            trunk="cal_frontend", branch="f/front")
        await _sweep(h)

        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert md.count("repos/acme__api@cal_backend") >= 2, "no suite + marker"
        assert md.count("repos/acme__api@cal_frontend") >= 2


@pytest.mark.asyncio
async def test_the_recorded_subdir_wins_over_the_derived_name():
    """The recorded path is PHYSICAL -- somebody's clone is there. A
    story provisioned under a different layout keeps resolving."""
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal)
        await _record_clone(h, story, "repo")       # the legacy layout
        await _sweep(h)
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "cd 'repo' &&" in md, md
        assert "repos/acme__api" not in md


@pytest.mark.asyncio
async def test_a_mount_is_NOT_lowercased():
    """`repo_clone_subdir` keeps GitHub's casing, so the directory on
    disk is `repos/Pilot-Wave-Technology__...`. The old derivation
    lowercased it -- which finds the clone on Windows and macOS and
    finds nothing on Linux, where a check that cannot `cd` to its mount
    passes by never running."""
    async with local_http() as h:
        goal, story = await _waiting(h, )
        await _behind(h, story, goal, owner="Pilot-Wave", name="API")
        await _record_clone(h, story, "repos/Pilot-Wave__API",
                            owner="Pilot-Wave", name="API")
        await _sweep(h)
        md = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "repos/Pilot-Wave__API" in md


# ── the agent may revise the catch-up, the human still approves ──────
#
# THE EXECUTOR WORKS THE PLAN, SO IT FINDS OUT WHEN THE PLAN IS WRONG --
# the same latitude the pod's executor has. What it must not do is turn
# a catch-up into a feature cycle by revising it: the card used to carry
# no kind, so approving the revision defaulted to `work`, consumed a
# cycle, landed the story in `executing` and replaced "take the trunk
# in" with whatever the new plan said. The contract would have changed
# without anyone deciding to change it.

async def _offered(h):
    """A story with a catch-up parked in front of somebody."""
    goal, story = await _waiting(h)
    await _behind(h, story, goal)
    await _sweep(h)
    return goal, story


@pytest.mark.asyncio
async def test_a_revised_catch_up_is_still_a_catch_up():
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        out = await submit_local_plan(
            goal, story, ["Take cal_backend in, then re-run the suite"],
            "The offered plan named a check that cannot run here.",
            [{"run": "pytest -q", "label": "goal"}])
        # ASSERTED, because without it a REFUSED submit leaves the
        # platform's own offer card in place and the test below reads
        # that instead -- passing while proving nothing.
        assert out.get("ok"), out
        card = await h.conn.fetchval(
            "SELECT card_data FROM messages "
            "  WHERE thread_id = (SELECT active_thread_id FROM stories "
            "                      WHERE id = $1) "
            "    AND card_data::text LIKE '%plan_review%' "
            "  ORDER BY created_at DESC LIMIT 1", story)
        import json
        card = json.loads(card) if isinstance(card, str) else card
        assert card["plan_kind"] == "integration", \
            "a revision must not silently become a work cycle"


@pytest.mark.asyncio
async def test_a_revision_goes_back_to_a_person():
    """ALWAYS APPROVED BY A HUMAN, revision included. The agent does not
    get to approve its own change of contract."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        assert (await submit_local_plan(
            goal, story, ["Take the trunk in"], "Revised.",
            [{"run": "pytest -q", "label": "goal"}])).get("ok")
        row = await h.conn.fetchrow(
            "SELECT current_phase, pending_plan_md FROM stories WHERE id = $1",
            story)
        assert row["current_phase"] == "awaiting_plan_approval"
        assert row["pending_plan_md"]


@pytest.mark.asyncio
async def test_a_revision_does_not_overwrite_the_work_plan_file():
    """`plan-NN.md` is on the branch and in the pull request: it is the
    record of what was agreed for the FEATURE work. A catch-up revision
    belongs beside it, not over it."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        out = await submit_local_plan(
            goal, story, ["Take the trunk in"], "Revised.",
            [{"run": "pytest -q", "label": "goal"}])
        assert out.get("ok"), out
        assert out["plan_path"].endswith("-trunk.md"), out["plan_path"]


@pytest.mark.asyncio
async def test_approving_a_revision_still_consumes_no_cycle():
    from app.services.local_cycles import current_cycle
    from app.services.local_plan_gate import approve_local_plan, submit_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        before = await current_cycle(h.conn, story)
        assert (await submit_local_plan(
            goal, story, ["Take the trunk in"], "Revised.",
            [{"run": "pytest -q", "label": "goal"}])).get("ok")
        await approve_local_plan(goal, story, "person")
        assert await current_cycle(h.conn, story) == before
        assert (await h.conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", story)
        ) == "integrating"


@pytest.mark.asyncio
async def test_an_ordinary_plan_in_an_ordinary_cycle_is_untouched():
    """The kind is READ, not assumed. A story that is not integrating
    registers a work plan exactly as before."""
    from app.services.local_plan_gate import submit_local_plan

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress")
        await h.conn.execute(
            "UPDATE stories SET execution_mode = 'local', "
            "       current_phase = 'planning' WHERE id = $1", story)
        out = await submit_local_plan(goal, story, ["Build the thing"], "Why.",
                                      [{"run": "pytest -q", "label": "goal"}])
        assert out.get("ok"), out
        assert not out["plan_path"].endswith("-trunk.md")


@pytest.mark.asyncio
async def test_a_second_catch_up_does_not_carry_the_first_ONE_S_mounts():
    """DAMAGE MUST NOT SURVIVE ITS OWN REPAIR.

    A catch-up carries the story's own checks forward, because approving
    a plan REPLACES the contract. It used to read the LATEST plan card --
    which after one catch-up IS a catch-up, so the next offer inherited
    the previous offer's generated mount checks. On 06295e15 a plan
    written while the mount was wrong was superseded, and the
    replacement carried the wrong mount forward and failed identically.
    """
    async with local_http() as h:
        goal, story = await _waiting(h)
        await _behind(h, story, goal, sha="bbbb")
        # The story's own check, as a work plan recorded it.
        await h.conn.execute(
            "UPDATE stories SET pending_plan_md = 'x' WHERE id = $1", story)
        from app.services.local_report import report_message
        await report_message(
            story, "agent", "Plan submitted.",
            card_data={"kind": "plan_review", "plan_kind": "work",
                       "plan_path": "p", "steps": ["s"], "plan_hash": "h",
                       "checks": [{"run": "pytest -q", "label": "goal"}]})
        await h.conn.execute(
            "UPDATE stories SET pending_plan_md = NULL, "
            "       current_phase = 'awaiting_merge' WHERE id = $1", story)

        # First offer, written while a stale mount was in play.
        await _sweep(h)
        first = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "pytest -q" in first, "the story's own check was dropped"

        # Trunk moves again; the second offer must not inherit the first's
        # generated mount checks, only the story's own.
        await h.conn.execute(
            "UPDATE stories SET pending_plan_md = NULL WHERE id = $1", story)
        await _advance_trunk(h, "cccc")
        await _sweep(h)
        second = await h.conn.fetchval(
            "SELECT pending_plan_md FROM stories WHERE id = $1", story)
        assert "pytest -q" in second, "the story's own check was dropped"
        # One marker and one suite check per mount, not two of each.
        assert second.count("git grep -n -E") == 1, second


# ── a local plan decision leaves a countable record ─────────────────
#
# In the cloud a plan approval is a `kernel.questions` row of kind
# `authorization` and the scorer counts it. Locally the plan is parked
# on a card and approved in the panel: no question, no execution, and
# approval CLEARS the pending plan -- so a rejection in particular left
# nothing behind at all. Approving a plan is the commonest decision a
# person makes in this lane and it was invisible to the taximeter.

async def _decisions(h, story_id: str) -> list[str]:
    rows = await h.conn.fetch(
        "SELECT payload->>'verdict' AS v FROM ledger_events "
        " WHERE event_type = 'local_plan.decided' "
        "   AND payload->>'story_id' = $1 ORDER BY sequence_number", story_id)
    return [r["v"] for r in rows]


@pytest.mark.asyncio
async def test_approving_a_local_plan_records_one_decision():
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        await approve_local_plan(goal, story, "person")
        assert await _decisions(h, story) == ["approved"]


@pytest.mark.asyncio
async def test_rejecting_a_local_plan_records_one_too():
    """The case that left NOTHING before: rejection clears the pending
    plan, so without this the decision is unrecoverable."""
    from app.services.local_plan_gate import reject_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)
        await reject_local_plan(goal, story, "person", "not like that")
        assert await _decisions(h, story) == ["rejected"]


@pytest.mark.asyncio
async def test_a_failed_decision_event_does_not_undo_the_decision():
    """NEVER FATAL. The approval is already applied and committed when
    the event is written; losing the event costs somebody three points,
    where losing the approval would silently un-approve a plan the
    person approved."""
    import app.ledger.journal as journal
    from app.services.local_plan_gate import approve_local_plan

    async with local_http() as h:
        goal, story = await _offered(h)

        # PATCHED AT THE DEPENDENCY, not at `_record_plan_decision`
        # itself: the guard being tested is the try/except INSIDE that
        # function, and replacing the function removes it. The first
        # version of this test did exactly that and "failed" on its own
        # stub -- proving nothing about the code.
        async def boom(*a, **k):
            raise RuntimeError("ledger is down")

        original = journal.append_event
        journal.append_event = boom
        try:
            out = await approve_local_plan(goal, story, "person")
        finally:
            journal.append_event = original
        assert out.get("ok"), "a ledger failure undid the approval"
        assert await _decisions(h, story) == [], "the event somehow landed"
