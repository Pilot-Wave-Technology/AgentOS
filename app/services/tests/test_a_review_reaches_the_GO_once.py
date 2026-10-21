"""A story review is handed to the GO exactly once.

NOTIFY IS A BROADCAST AND THAT IS THE WHOLE BUG. The review is
announced with `pg_notify('story_review_ready')` and delivered by
whoever is LISTENing -- so every control-plane instance ran the
delivery and the GO was told once per instance. Story 291c4901's review
landed in the orchestrator channel twice, 2.3 seconds apart, from a
single `story_reviews` row.

It is not a rare race either. Cloud Run keeps an old revision's
container, and its LISTEN, running for a while after a deploy, so every
deploy opens a window where two instances are listening; a
horizontally-scaled deployment is in that state permanently.

THE CLAIM IS A CONDITIONAL UPDATE. Exactly one caller sees
`delivered_at` move off NULL and that caller delivers. A lock would
answer "is somebody delivering right now", which is the wrong question
-- the second instance arrives afterwards, not during.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


def _capture_sends(monkeypatch) -> list:
    """Both outbound surfaces, stubbed; returns the GO wakes.

    The orchestrator card is real I/O and blocks in a unit test, and the
    GO wake talks to the gateway. Neither is what these tests are about:
    the question is how MANY times delivery is attempted.
    """
    sent: list = []

    async def spy_go(goal_id, message, **kw):
        sent.append(message)

    async def spy_card(*a, **kw):
        return True

    monkeypatch.setattr("app.services.go_events._send_system_message_to_go",
                        spy_go)
    monkeypatch.setattr(
        "app.services.orchestrator_delivery.persist_review_to_orchestrator",
        spy_card)
    return sent


async def _reviewed(h, verdict: str = "revise", text: str = "the review"):
    """A story with a recorded, undelivered review."""
    goal, story = await h.seed_story(state="done")
    # A GO TO TELL. Without these columns `deliver` returns before it
    # sends anything, so a test that counts sends passes with zero --
    # which is how the first version of these passed while proving
    # nothing at all.
    await h.conn.execute(
        "UPDATE goals SET go_agent_id = 'go-x', go_thread_id = 't-x' "
        " WHERE id = $1", goal)
    await h.conn.execute(
        "INSERT INTO story_reviews (id, story_id, goal_id, reviewed_state, "
        "   review_text, reviewer_agent_id, run_id, verdict) "
        "VALUES (gen_random_uuid(), $1, $2, 'done', $3, 'local-session', "
        "        'local', $4)", story, goal, text, verdict)
    return goal, story


@pytest.mark.asyncio
async def test_two_listeners_deliver_the_review_once(monkeypatch):
    """The bug, as it happened: one announcement, handled twice.

    SPIES ON THE GO SEND, not on `deliver` -- the claim lives inside
    `deliver` now, so replacing it would bypass the very guard under
    test. It also counts what the GO actually receives, which is the
    thing that was wrong.
    """
    from app.services import story_review_delivery as mod

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        await mod.deliver_recorded(goal, story)
        await mod.deliver_recorded(goal, story)
        assert len(sent) == 1, f"the GO was told {len(sent)} times about one review"


@pytest.mark.asyncio
async def test_the_claim_is_recorded_on_the_row(monkeypatch):
    """`delivered_at` is the answer to 'has this ever been delivered',
    which is what makes a later sweep safe to run."""
    from app.services.story_review_delivery import deliver_recorded

    _capture_sends(monkeypatch)

    async with local_http() as h:
        goal, story = await _reviewed(h)
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) is None
        await deliver_recorded(goal, story)
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) is not None


@pytest.mark.asyncio
async def test_a_second_call_does_not_claim_what_it_did_not_deliver(monkeypatch):
    """The loser of the race must not stamp the row: a claim that is
    taken without delivering is how a review is marked sent and never
    arrives."""
    from app.services.story_review_delivery import deliver_recorded

    _capture_sends(monkeypatch)

    async with local_http() as h:
        goal, story = await _reviewed(h)
        await deliver_recorded(goal, story)
        first = await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story)
        await deliver_recorded(goal, story)
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) == first, "the second caller moved the delivery stamp"


@pytest.mark.asyncio
async def test_a_story_with_no_review_is_not_an_error(monkeypatch):
    """The announcement can outrun the write, and a missing row is a
    thing to log rather than to raise at a NOTIFY handler."""
    from app.services.story_review_delivery import deliver_recorded

    _capture_sends(monkeypatch)

    async with local_http() as h:
        goal, story = await h.seed_story(state="done")
        assert await deliver_recorded(goal, story) is False


# ── the OTHER door ───────────────────────────────────────────────────
#
# The claim went into `deliver_recorded` first, which is the NOTIFY
# path -- and the council calls `deliver` directly, so it never passed
# through the guard at all. "Delivered once" then depended on which
# door the review came through, which is the precise thing one shared
# function is supposed to make unnecessary. 7c61ade5's review reached
# the GO three times with a single claim stamped on the row.

@pytest.mark.asyncio
async def test_the_direct_caller_is_guarded_too(monkeypatch):
    """`deliver` is the council's door. It must claim as well."""
    from app.services import story_review_delivery as mod

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        await mod.deliver(goal, story, "the review")
        await mod.deliver(goal, story, "the review")
        assert len(sent) == 1, f"the GO was told {len(sent)} times"


@pytest.mark.asyncio
async def test_an_interim_review_is_not_silenced_by_the_final_one(monkeypatch):
    """PER (STORY, REVIEWED_STATE). They are different deliveries of
    different text, and one flag for the story would send whichever
    arrived first and swallow the other."""
    from app.services import story_review_delivery as mod

    _capture_sends(monkeypatch)

    async with local_http() as h:
        goal, story = await _reviewed(h)
        await h.conn.execute(
            "INSERT INTO story_reviews (id, story_id, goal_id, reviewed_state, "
            "   review_text, reviewer_agent_id, run_id, verdict) "
            "VALUES (gen_random_uuid(), $1, $2, 'in_progress', 'interim', "
            "        'local-session', 'local', 'partial')", story, goal)
        await mod.deliver(goal, story, "final")
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews "
            " WHERE story_id = $1 AND reviewed_state = 'in_progress'",
            story) is None, "delivering the final review claimed the interim one"


# ── the backstop: a NOTIFY that was never received ───────────────────

@pytest.mark.asyncio
async def test_a_missed_announce_is_delivered_late(monkeypatch):
    """The failure this was built for, and it is silent.

    LISTEN/NOTIFY has no redelivery and no acknowledgement. If the
    listening connection is down for the instant the announce fires,
    the review is simply never delivered — `delivered_at` stays NULL,
    nothing errors, and nothing anywhere records that the GO was not
    told. Observed on feature with story 3a1095ea: reviewed `approve`
    at 15:45:32, story moved to `reviewed`, GO never told.

    `review_finish` already claimed in a comment that "the panel's own
    sweep finds a review nobody delivered". There was no sweep.
    """
    from app.services.story_review_delivery import deliver_undelivered

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        # Older than the grace window — i.e. the announce is not merely
        # in flight, it was lost.
        await h.conn.execute(
            "UPDATE story_reviews SET reviewed_at = now() - interval '1 h' "
            " WHERE story_id = $1", story)

        n = await deliver_undelivered(h.conn)
        assert n == 1, "the lost review was never picked up"
        assert len(sent) == 1, "the GO was not told"
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) is not None


@pytest.mark.asyncio
async def test_the_sweep_does_not_race_the_announce(monkeypatch):
    """A review written a second ago is still in flight on the NOTIFY
    path. Delivering it here would be the duplicate this file exists to
    prevent — the claim would stop a second SEND, but the sweep should
    not be relying on that every five minutes."""
    from app.services.story_review_delivery import deliver_undelivered

    _capture_sends(monkeypatch)
    async with local_http() as h:
        await _reviewed(h)                       # reviewed_at = now()
        assert await deliver_undelivered(h.conn) == 0


@pytest.mark.asyncio
async def test_the_sweep_never_sends_an_already_delivered_review(monkeypatch):
    """It runs beside the NOTIFY path, so it must be idempotent against
    it — the claim in `deliver` is what makes that true."""
    from app.services.story_review_delivery import (deliver_recorded,
                                                    deliver_undelivered)

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        await deliver_recorded(goal, story)
        await h.conn.execute(
            "UPDATE story_reviews SET reviewed_at = now() - interval '1 h' "
            " WHERE story_id = $1", story)

        assert await deliver_undelivered(h.conn) == 0
        assert len(sent) == 1, (
            f"the GO was told {len(sent)} times; the sweep re-sent a "
            f"review the NOTIFY path had already delivered")


@pytest.mark.asyncio
async def test_one_bad_review_does_not_strand_the_rest(monkeypatch):
    """The next row may be the one that has been waiting an hour."""
    from app.services import story_review_delivery as mod

    _capture_sends(monkeypatch)
    async with local_http() as h:
        await _reviewed(h)
        await h.conn.execute(
            "UPDATE story_reviews SET reviewed_at = now() - interval '1 h'")

        seen = []

        async def boom(goal_id, story_id):
            seen.append(story_id)
            raise RuntimeError("nope")

        monkeypatch.setattr(mod, "deliver_recorded", boom)
        assert await mod.deliver_undelivered(h.conn) == 0
        assert seen, "the sweep never reached a review"


@pytest.mark.asyncio
async def test_the_backstop_is_registered_where_it_will_run():
    """A loop nobody starts is a comment. `slack_membership_reconcile`
    is registry-only on this tier, so the registry is the place."""
    from app.services.scheduler import registry

    assert "story_review_backstop" in [l.name for l in registry.loops()]


@pytest.mark.asyncio
async def test_the_review_sweep_runs_in_the_wake_backstop():
    """WHY IT IS HERE AND NOT ONLY IN THE SCHEDULER.

    `_backstop` is where every other missed wake self-heals, on a 30s
    poll. `story_review_ready` was the one channel with nothing behind
    it, which is why it was the only one that could be lost for good:
    on feature, the node and story-done wakes for story 3a1095ea were
    replayed by this sweep seconds before the review was announced and
    dropped. Everything looked healthy.
    """
    import inspect

    from app.services import go_wakes
    src = inspect.getsource(go_wakes._backstop)
    assert "deliver_undelivered" in src, (
        "the review sweep is not in the wake backstop, so a missed "
        "announce is only recovered by the 5-minute scheduler loop — "
        "and not at all if the scheduler is not running")


@pytest.mark.asyncio
async def test_a_claim_that_delivered_nothing_is_given_back(monkeypatch):
    """CLAIMED AND NOT DELIVERED IS THE WORST OF BOTH.

    The claim is taken before anything is sent. A caller that then
    bails leaves the row stamped `delivered_at` with nobody told — and
    the sweep looks for NULL, so it would never retry. The review would
    be lost permanently while the row claimed it had been delivered.
    """
    from app.services.story_review_delivery import deliver_recorded

    _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        # A goal whose GO is not provisioned — the bail-out path.
        await h.conn.execute(
            "UPDATE goals SET go_agent_id = NULL WHERE id = $1", goal)

        assert await deliver_recorded(goal, story) is False
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) is None, (
            "the row is stamped delivered but nothing was sent, and the "
            "sweep will never look at it again")


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_deleted_goals_reviews_alone(monkeypatch):
    """Goal delete is soft and does not cascade, so every review of
    every story in every dead goal is still sitting here undelivered.

    The first run of this sweep on feature delivered 37 of them. Waking
    a deleted goal's GO is the zombie this codebase already carries
    eight guards against; this is the ninth.
    """
    from app.services.story_review_delivery import deliver_undelivered

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        await h.conn.execute(
            "UPDATE story_reviews SET reviewed_at = now() - interval '1 h' "
            " WHERE story_id = $1", story)
        await h.conn.execute(
            "UPDATE goals SET status = 'deleted' WHERE id = $1", goal)

        assert await deliver_undelivered(h.conn) == 0
        assert not sent, "a deleted goal's GO was woken"


@pytest.mark.asyncio
async def test_the_sweep_does_not_reach_back_months(monkeypatch):
    """A backstop for an announce missed minutes ago, not a migration
    of everything ever missed. Reviews reaching to June went out in one
    burst the first time this ran, and the GO acts on every one."""
    from app.services.story_review_delivery import deliver_undelivered

    sent = _capture_sends(monkeypatch)
    async with local_http() as h:
        goal, story = await _reviewed(h)
        await h.conn.execute(
            "UPDATE story_reviews SET reviewed_at = now() - interval '40 d' "
            " WHERE story_id = $1", story)

        assert await deliver_undelivered(h.conn) == 0
        assert not sent, "a four-month-old review was handed to the GO"
        assert await h.conn.fetchval(
            "SELECT delivered_at FROM story_reviews WHERE story_id = $1",
            story) is None, (
            "the old review was marked delivered to silence it; it should "
            "stay an honest, visible record of what was missed")
