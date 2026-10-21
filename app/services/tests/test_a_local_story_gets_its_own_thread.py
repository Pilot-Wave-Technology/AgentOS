"""Importing a second story must not unbind the first.

WHAT IT DID. `bind_local_story_thread` resolved ONE persistent (goal, user)
thread, inserted it WITHOUT `threads.story_id`, and then ran

    UPDATE stories SET is_active_focus = false, active_thread_id = NULL
     WHERE active_thread_id = $1 AND id <> $2

so importing a third story silently detached the first two. Downstream, all
quiet: their `/report-message` calls returned `no_active_thread`, their
recent-messages came back empty, and their diffs submitted against a NULL
thread. The conversation the local lane exists to capture simply stopped
being recorded for every story but the newest.

The gateway path had already been fixed, and its docstring named the bug the
local path still had -- "the one-story-per-thread leak that had to be fixed
once already". The fix is not a second resolver with the detach removed; it
is the SAME resolver.
"""

import re

import pytest

from app.services.tests._source import code, source as src


def test_there_is_exactly_one_STORY_thread_writer():
    """Two writers is how the two lanes disagreed in the first place.

    Scoped to STORY threads. The router also creates the persistent
    (goal, user) assistant thread, which is a different object with a
    different lifetime and legitimately its own writer -- an assertion that
    caught it would be asserting the wrong invariant.
    """
    writers = [rel for rel in (
        "app/services/story_threads.py",
        "app/services/local_assistant.py",
        "app/routers/assistant.py",
        ) if re.search(r"INSERT INTO threads\s*\([^)]*story_id", code(rel))]
    assert writers == ["app/services/story_threads.py"], (
        f"story threads are written in more than one place: {writers}")


@pytest.mark.parametrize("rel", [
    "app/services/local_assistant.py",
    "app/routers/assistant.py",
])
def test_both_lanes_use_the_shared_resolver(rel):
    assert "ensure_story_thread" in src(rel), f"{rel} resolves its own thread"


def test_the_resolver_keys_on_the_story():
    """`threads.story_id` has been in the schema all along. Keying on
    (goal, agent, user) is what made one thread serve N stories."""
    body = src("app/services/story_threads.py")
    assert "story_id = $3" in body, "the lookup does not key on the story"
    assert "INSERT INTO threads (id, goal_id, agent_id, story_id" in body, (
        "the insert does not set story_id, so the next lookup cannot find it")


def test_re_importing_returns_the_same_thread():
    """Find-or-create, not create. A story put down and picked up again must
    keep its conversation rather than starting a second empty thread."""
    body = src("app/services/story_threads.py")
    select = body.index("SELECT id FROM threads")
    insert = body.index("INSERT INTO threads")
    assert select < insert, "the resolver inserts before it looks"
    assert "if existing:" in body


def test_the_detach_is_gone():
    """Not made conditional -- gone. With a thread per story there is
    nothing to detach, and a conditional detach is a detach waiting for the
    condition to be got wrong."""
    assert "active_thread_id = NULL" not in code("app/services/local_assistant.py"), (
        "importing a story still unbinds the others")


def test_the_busy_withhold_is_gone():
    """It left a second story `accepted` when the shared thread was busy,
    withholding the `in_progress` transition the git engine's lazy branch cut
    depends on -- so the extension waited ~36s and reported "the engine hasn't
    cut this story's feature branch yet". A branch error for a thread-binding
    refusal. There is no rotation to be busy with now."""
    body = code("app/services/local_assistant.py")
    assert "find_busy_story_on_thread" not in body
    assert "and not busy_story" not in body, (
        "the in_progress transition is still withheld")


def test_concurrency_is_bounded_upstream_and_only_there():
    """The limit belongs in `claim_checkout` via `thread_ceiling`, before the
    lease is claimed -- not in the binder, which runs after the checkout is
    already authorized."""
    assert "thread_ceiling" in src("app/services/local_checkout.py"), (
        "nothing bounds how many stories one person may hold locally")
