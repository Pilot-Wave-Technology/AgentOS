"""A Slack button only works for somebody who is in the goal.

THE FAILURE THIS PREVENTS IS CROSS-TENANT CONTROL. A button's action_id
carries the id it acts on -- `ctrl_pause_<story>`, `diff_approve_<approval>`,
`card_approve_<task>`. The handler resolved the clicking Slack user to a
control-panel uid and then acted, which proves the clicker is *a* user and
nothing about whether they may touch THAT story. One branch, `recover_*`,
did check. The other four did not, so a mapped Slack user could approve a
plan, approve-and-PUSH a diff, or replan a story in a goal they have no
role on, from an action_id pasted out of another channel.

The table at the bottom is the real test: it walks every action-id family
`actions.py` knows about, so a sixth family added without a check fails
here rather than in production. The member cases exist so the suite cannot
be satisfied by refusing everything, which is the obvious wrong fix.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import asyncpg
import pytest

PREFIX = "slackauthz"

_DSNS = [
    os.environ.get("DATABASE_URL", ""),
    "postgresql://agentos:agentos@localhost:55432/agentos_test",
]


async def _connect_or_skip():
    attempts = []
    for dsn in [d for d in _DSNS if d]:
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                        # noqa: BLE001
            attempts.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
            continue
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name "
            "IN ('stories','goals','goal_members','users','tasks','approvals')")
        if n == 6:
            return conn
        await conn.close()
        attempts.append(f"{dsn.rsplit('@', 1)[-1]}: only {n}/6 tables")
    pytest.skip("no database with the expected schema. Tried: " + "; ".join(attempts))


@contextlib.asynccontextmanager
async def _world():
    """One goal, a member, an outsider, and a story/task/approval to act on."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal = f"{PREFIX[:5].upper()}1"
        member, outsider = f"{PREFIX}-member", f"{PREFIX}-outsider"
        story = f"{PREFIX}-story"
        task = f"{PREFIX}-task"
        approval = str(uuid.uuid4())

        for uid in (member, outsider):
            await conn.execute(
                # system_role is spelled out: the column DEFAULT is 'user',
                # which the CHECK does not allow, so an insert that omits it
                # fails. See the note in the Slack build plan.
                "INSERT INTO users (uid, email, display_name, system_role) "
                "VALUES ($1,$2,$3,'member') ON CONFLICT (uid) DO NOTHING",
                uid, f"{uid}@example.test", uid)
        await conn.execute(
            "INSERT INTO goals (id, name, created_by, status) "
            "VALUES ($1,'authz probe',$2,'active') ON CONFLICT (id) DO NOTHING",
            goal, member)
        # ONLY the member is joined. The outsider is a real, signed-in user
        # with no role here -- which is exactly the case that was allowed.
        await conn.execute(
            "INSERT INTO goal_members (goal_id, uid, role, added_by) "
            "VALUES ($1,$2,'member',$2) ON CONFLICT DO NOTHING", goal, member)
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, created_by, state, assigned_to) "
            "VALUES ($1,$2,'authz probe',$3,'in_progress',$3)", story, goal, member)
        await conn.execute(
            "INSERT INTO tasks (id, goal_id, title, created_by, state) "
            "VALUES ($1,$2,'t',$3,'awaiting_approval')", task, goal, member)
        await conn.execute(
            "INSERT INTO approvals (id, story_id, kind) VALUES ($1,$2,'diff')",
            approval, story)
        # `get_goal_role` is the canonical membership check and reads the
        # GLOBAL pool. Reimplementing it here would let the real one drift
        # away from what these tests pin, so point the global at our
        # rolled-back connection instead.
        from unittest.mock import patch
        with patch("app.auth.get_pool", lambda: _Pool(conn)):
            yield conn, dict(goal=goal, member=member, outsider=outsider,
                             story=story, task=task, approval=approval)
    finally:
        await tx.rollback()
        await conn.close()


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """asyncpg connection dressed as the pool the code expects."""

    def __init__(self, conn):
        self._c = conn

    async def fetchval(self, *a, **k):
        return await self._c.fetchval(*a, **k)

    async def fetchrow(self, *a, **k):
        return await self._c.fetchrow(*a, **k)

    async def fetch(self, *a, **k):
        return await self._c.fetch(*a, **k)

    async def execute(self, *a, **k):
        return await self._c.execute(*a, **k)


# ── the resolvers find the right goal ────────────────────────────────────────

def test_each_id_space_resolves_to_its_goal():
    """If a resolver returns None the check refuses, so a resolver that is
    quietly broken looks exactly like a working guard. Pin them."""
    from app.channels.slack import authz

    async def body():
        async with _world() as (conn, w):
            pool = _Pool(conn)
            assert await authz.goal_for_story(pool, w["story"]) == w["goal"]
            assert await authz.goal_for_task(pool, w["task"]) == w["goal"]
            assert await authz.goal_for_approval(pool, w["approval"]) == w["goal"]
    _run(body())


def test_an_unknown_id_resolves_to_nothing_and_is_refused():
    from app.channels.slack import authz

    async def body():
        async with _world() as (conn, w):
            pool = _Pool(conn)
            assert await authz.goal_for_story(pool, "no-such-story") is None
            assert await authz.authorize(
                pool, user_uid=w["member"], kind="story",
                subject_id="no-such-story") is False
    _run(body())


# ── the rule itself, over every id-space ─────────────────────────────────────

@pytest.mark.parametrize("kind,key", [
    ("story", "story"),          # ctrl_pause_ / ctrl_replan_ / ctrl_approve_plan_
    ("task", "task"),            # card_approve_ / card_changes_ / card_reply_ / …
    ("approval", "approval"),    # diff_approve_ / diff_reject_ / diff_details_
])
def test_an_outsider_is_refused_and_a_member_is_allowed(kind, key):
    from app.channels.slack import authz

    async def body():
        async with _world() as (conn, w):
            pool = _Pool(conn)
            assert await authz.authorize(
                pool, user_uid=w["outsider"], kind=kind,
                subject_id=w[key]) is False, f"{kind}: outsider was allowed"
            # The other half. Without it, `return False` passes this file.
            assert await authz.authorize(
                pool, user_uid=w["member"], kind=kind,
                subject_id=w[key]) is True, f"{kind}: member was refused"
    _run(body())


def test_an_unknown_kind_raises_rather_than_silently_allowing():
    """A typo'd kind must not fall through to a permissive default."""
    from app.channels.slack import authz

    async def body():
        async with _world() as (conn, w):
            with pytest.raises(ValueError):
                await authz.authorize(_Pool(conn), user_uid=w["member"],
                                      kind="storey", subject_id=w["story"])
    _run(body())


# ── the shared card service refuses on its own ───────────────────────────────

def test_the_card_service_refuses_an_outsider_without_touching_the_task():
    """The check lives in `handle_card_response`, not only at the call sites,
    because the call sites keep multiplying -- button, modal submit, websocket.
    The modal path had no guard of its own at all."""
    from unittest.mock import patch

    async def body():
        async with _world() as (conn, w):
            pool = _Pool(conn)
            before = await conn.fetchval(
                "SELECT state FROM tasks WHERE id = $1", w["task"])
            with patch("app.services.card_responses.get_pool", lambda: pool):
                from app.services.card_responses import handle_card_response
                res = await handle_card_response(
                    task_id=w["task"], option_id="approve", value="yes",
                    user_uid=w["outsider"])
            assert res.ok is False
            assert "authoriz" in (res.error or "").lower()
            after = await conn.fetchval(
                "SELECT state FROM tasks WHERE id = $1", w["task"])
            assert after == before, "a refused response still changed the task"
    _run(body())


# ── every family is guarded, including ones added later ──────────────────────

def test_every_action_id_family_is_behind_a_check():
    """THE ONE THAT CATCHES THE NEXT GAP.

    The point is not that today's four families are guarded -- the tests
    above prove that. It is that `_handle_block_actions` dispatches on a
    fixed set of regexes, and the way this bug happened was somebody adding
    a family and not adding the check. So: every regex the dispatcher
    matches on must have an `authorize(` or an explicit role check between
    the match and the action.
    """
    import inspect
    import re

    from app.channels.slack import actions

    src = inspect.getsource(actions._handle_block_actions)
    # Both spellings: the module-local `_CTRL_ACTION_RE.match(...)` and the
    # imported `PARK_ACTION_RE` / `ERRAND_ACTION_RE`, which are iterated
    # over in a loop. The first version of this guard required a leading
    # underscore and silently stopped seeing the GO families the moment
    # they were added — a guard that cannot see a new family is the
    # failure it exists to prevent.
    pattern = re.compile(r"(?<![A-Za-z0-9])([A-Z_]*ACTION_RE)(?![A-Za-z0-9_])")
    families = sorted({m.group(1) for m in pattern.finditer(src)})
    assert len(families) >= 6, f"expected six families, found {families}"

    # FROM EACH FAMILY'S LAST MENTION TO THE NEXT DISPATCH, which is
    # what a branch actually is. Three earlier shapes were wrong and
    # each failed quietly in its own way, so they are worth naming:
    #   * splitting on "<FAM>.match" missed PARK/ERRAND, which are
    #     iterated in a loop as `regex.match`, and raised IndexError
    #   * `str.index` found `_ACTION_RE` INSIDE `_CTRL_ACTION_RE`, so
    #     that segment collapsed to five characters
    #   * a fixed 2000-char window ran past the end of its own branch
    #     into the next one's `authorize(` — so removing a real check
    #     left the guard green, which is the one outcome a guard must
    #     never have
    last: dict[str, int] = {}
    for m in pattern.finditer(src):
        last[m.group(1)] = m.end()

    # Families mentioned within a few characters of each other are ONE
    # dispatch, not two: PARK and ERRAND are handled by a single loop and
    # appear together in its tuple, 28 characters apart. Treating them
    # separately cut PARK's segment to nothing and failed a branch that
    # is in fact guarded.
    order = sorted(last.items(), key=lambda kv: kv[1])
    groups: list[tuple[list[str], int]] = []
    for fam, pos in order:
        if groups and pos - groups[-1][1] < 200:
            groups[-1][0].append(fam)
            groups[-1] = (groups[-1][0], pos)
        else:
            groups.append(([fam], pos))

    starts = [pos for _, pos in groups]
    for n, (fams, pos) in enumerate(groups):
        fam = " / ".join(fams)
        segment = src[pos:starts[n + 1] if n + 1 < len(starts) else len(src)]
        # `go_actions.` counts: those handlers do the goal-role check
        # beside the service call they guard, which is stronger than
        # checking here — a second caller of the service cannot skip it.
        guarded = ("authorize(" in segment or "get_goal_role" in segment
                   or "go_actions." in segment)
        assert guarded, (
            f"{fam} dispatches without a goal-membership check. Every button "
            "family must authorize before it acts — see app/channels/slack/authz.py")
