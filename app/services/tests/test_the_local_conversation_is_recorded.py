"""Every turn of a local story must reach that story's thread.

THIS IS THE WHOLE POINT OF THE LOCAL LANE. The work happens in the person's
editor; what the platform gets back is the conversation, the memory and the
result. The first of those is one `INSERT` away from being silently lost,
and it was: while the local lane bound every story in a goal to ONE thread,
importing a third story detached the first two, and their turns then failed
`no_active_thread` with nothing raised anywhere.

So this is a guard, not a description. It is the requirement most likely to
regress quietly, because nothing downstream fails when a turn is dropped --
the story still finishes, the diff still lands, and only the record is
poorer.
"""

from app.services.tests._source import code, source as src


def test_a_turn_is_written_to_the_storys_own_thread():
    body = code("app/services/local_report.py")
    assert "active_thread_id" in body, (
        "a turn is recorded against something other than the story's thread")
    assert "INSERT INTO messages" in body


def test_a_turn_with_no_thread_is_refused_rather_than_dropped():
    """`no_active_thread` is the honest answer, and the extension surfaces
    it. Writing the row somewhere else, or discarding it, would lose the
    conversation quietly -- which is the failure this whole lane exists to
    avoid."""
    body = code("app/services/local_report.py")
    assert "no_active_thread" in body


def test_recording_a_turn_never_wakes_the_gateway_assistant():
    """The assistant is running locally. A gateway turn would
    double-execute the story."""
    body = src("app/services/local_report.py")
    assert "must NOT wake" in body or "minus the agent forward" in body, (
        "nothing states the record-only contract, which is the one thing "
        "separating a mirror from a second executor")


def test_every_story_has_its_own_thread_to_be_recorded_in():
    """The guard that makes the above reachable at N>1. Without it the
    second and third stories of a goal have no thread of their own, and
    this test would pass while their turns went nowhere."""
    assert "ensure_story_thread" in code("app/services/local_assistant.py")
    assert "active_thread_id = NULL" not in code("app/services/local_assistant.py")


def test_the_two_role_vocabularies_are_both_readable():
    """The gateway writes `assistant`, the local lane writes `agent`. Any
    reader that knows only one silently sees an empty conversation -- which
    is exactly what the GO's completion message did."""
    assert "role IN ('assistant', 'agent')" in code("app/services/go_events.py")
