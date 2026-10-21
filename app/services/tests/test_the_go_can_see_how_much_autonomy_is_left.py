"""Autonomy is bounded, and the GO could not see any of the bounds.

WHAT G1 ACTUALLY TURNED OUT TO BE. The plan proposed two routes to
letting the GO run an errand unattended: extend standing approval (G1)
or add an approve verb (G2). Reading the code, **the mechanism was
already complete and already enforced** -- `standing_approval` pins a
grant to the exact plan hash a person approved, `schedules.py:168`
enforces `max_runs`, and `expires_at` and `revoked_at` both work. There
was nothing to extend.

What was missing was that none of it was READABLE. The GO could watch an
errand run and not know it was on its last run, that the grant expires
tonight, or that somebody had revoked it an hour ago. Asked "how many
more of these are there?" it had no way to answer, and an agent with no
way to answer a factual question answers it anyway.

SO IT GOES IN THE TURN, not behind a tool. `errand_block` already runs
once per turn on an errand thread, so this costs one query and no new
tool -- and the answer is useless a turn late: somebody asking "is this
still autonomous" wants it now, not after a round trip.

AND THE PIN IS THE PART WORTH SAYING OUT LOUD. A steer that changes what
done means produces a NEW plan, which the grant does not cover, so the
errand parks for a person who by definition went away. That is correct
behaviour and an expensive surprise, so the GO is told before it steers
rather than after.

G2 -- the approve verb -- IS NOT BUILT AND IS NOT NEEDED FOR THIS. See
the plan's Phase G.
"""
from __future__ import annotations

import datetime as dt
import inspect

from app.services import go_thread_scope as gts


def _g(**kw):
    base = {"approved_by": "alice", "approved_plan_hash": "abc",
            "revoked_at": None, "expires_at": None, "max_runs": None,
            "run_count": 0, "cron": "0 9 * * *"}
    base.update(kw)
    return base


# ── the four states a grant can be in ─────────────────────────────────

def test_no_grant_says_every_run_waits_for_a_person():
    said = gts._grant_lines(None)
    assert "none" in said and "waits for a person" in said


def test_an_unapproved_schedule_is_the_same_as_none():
    """A schedule exists with `approved_by IS NULL` whenever somebody
    proposed an errand and nobody has said yes. That is a legitimate
    state, not a broken grant."""
    assert "none" in gts._grant_lines(_g(approved_by=None))


def test_a_revoked_grant_says_whose_it_is_again():
    """`revoked_at` does double duty: it stops the errand running
    unattended AND hands it back to the assistant. Saying only the first
    leaves the GO steering something it is now refused on."""
    said = gts._grant_lines(_g(revoked_at=dt.datetime(2026, 9, 1)))
    assert "REVOKED" in said
    assert "assistant's again" in said


def test_a_live_grant_names_who_approved_it():
    assert "approved by alice" in gts._grant_lines(_g())


# ── the bounds ────────────────────────────────────────────────────────

def test_it_says_how_many_runs_are_LEFT_not_how_many_there_were():
    """"3 of 5" is arithmetic somebody has to do while deciding whether
    to spend one."""
    said = gts._grant_lines(_g(max_runs=5, run_count=3))
    assert "2 run(s) left of 5" in said


def test_an_exhausted_grant_does_not_report_a_negative():
    said = gts._grant_lines(_g(max_runs=2, run_count=5))
    assert "0 run(s) left" in said


def test_an_unbounded_grant_claims_no_bound():
    said = gts._grant_lines(_g())
    assert "run(s) left" not in said
    assert "expires" not in said


def test_an_expiry_is_named_with_its_timezone():
    said = gts._grant_lines(_g(expires_at=dt.datetime(2026, 10, 1, 9, 0)))
    assert "expires 2026-10-01 09:00 UTC" in said


# ── the pin, which is the expensive surprise ──────────────────────────

def test_a_pinned_grant_warns_that_a_steer_can_cost_it():
    """A steer changing what done means makes a NEW plan, the grant does
    not cover it, and the errand parks for somebody who went away."""
    said = gts._grant_lines(_g())
    assert "a new plan is not covered" in said.lower()
    assert "parks for a person" in said


def test_it_does_not_tell_the_go_to_stop_steering():
    """The warning has to be a cost, not a prohibition. An errand doing
    the wrong thing should be steered."""
    said = gts._grant_lines(_g())
    assert "Steer anyway when it is wrong" in said


def test_an_unpinned_grant_is_explained_rather_than_alarming():
    """A grant is minted with a NULL hash and pinned on the FIRST
    approval, so this is the ordinary state before the first plan lands
    -- and it looks exactly like a missing pin."""
    said = gts._grant_lines(_g(approved_plan_hash=None))
    assert "first run still parks for plan approval" in said
    assert "not covered" not in said


# ── how it is read ────────────────────────────────────────────────────

def test_the_grant_is_read_in_the_turn_and_not_behind_a_tool():
    src = inspect.getsource(gts)
    assert "_GRANT" in src
    assert "errand_schedules" in src


def test_it_uses_the_same_scoping_as_the_drive_gate():
    """A grant with `story_id IS NULL` covers the whole goal; one naming
    a story covers that story. The narrower wins."""
    assert "(s.story_id IS NULL OR s.story_id = $2)" in gts._GRANT
    assert "ORDER BY (s.story_id IS NOT NULL) DESC" in gts._GRANT, (
        "a goal-wide grant could be reported for a story that has its own")


def test_it_deliberately_omits_the_plan_pin_from_the_predicate():
    """Same reason `go_may_drive` does: the hash is NULL until the first
    plan is approved, and a grant that exists is still a grant."""
    assert "approved_plan_hash = " not in gts._GRANT


def test_a_failed_read_does_not_cost_the_turn():
    """This is context, not the answer. A GO that replies without
    knowing the bounds is worse than one that replies; one that does not
    reply is worse than both."""
    src = inspect.getsource(gts.errand_block)
    block = src.split("_GRANT", 1)[1]
    assert "except Exception" in block
    assert "grant = None" in block


def test_a_goal_thread_still_gets_nothing():
    """Every turn the GO takes today is a goal-thread turn. The grant
    block must not reach one, or this is a regression dressed as a
    feature."""
    src = inspect.getsource(gts.errand_block)
    assert src.index('if not thread_id:') < src.index("_GRANT")
    assert src.index('!= "errand"') < src.index("_GRANT")
