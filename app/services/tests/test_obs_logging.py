"""Structured logging: the ids reach the line without the call site knowing.

The property under test is the one that makes this cheap: an ordinary
`logger.info("...")`, unchanged, comes out carrying the run, the goal,
the story and the leg. If that stops being true the design is not worth
having, because the alternative is editing 482 call sites.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from kernel.obs import bound, clear, configure, reset_for_tests
from kernel.obs import fields as F
from kernel.obs.render import JsonRenderer, TextRenderer
from kernel.obs.setup import ContextFilter


@pytest.fixture()
def capture(monkeypatch):
    """A root logger wired exactly as `configure` wires it, into a buffer."""
    reset_for_tests()
    clear()
    monkeypatch.setenv("AGENTOS_COMPONENT", "worker")
    monkeypatch.setenv("BUILD_SHA", "abc1234")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonRenderer())
    handler.addFilter(ContextFilter(F.process_fields()))
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.handlers = saved
        clear()
        reset_for_tests()


def _lines(buf):
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


def test_an_unchanged_call_site_carries_the_ids(capture):
    log = logging.getLogger("kernel.worker.loop")
    with bound(run_id="0192f3a1", goal_id="KER28", story_id="991a9f53",
               work_kind="errand", execution_id=13616, leg=4):
        log.info("claimed a leg")

    (line,) = _lines(capture)
    assert line["message"] == "claimed a leg"
    for key, value in (("run_id", "0192f3a1"), ("goal_id", "KER28"),
                       ("story_id", "991a9f53"), ("work_kind", "errand"),
                       ("execution_id", 13616), ("leg", 4)):
        assert line[key] == value, f"{key} did not reach the line"
    assert line["component"] == "worker"
    assert line["build_sha"] == "abc1234"
    assert line["severity"] == "INFO"


def test_ids_do_not_leak_out_of_their_scope(capture):
    log = logging.getLogger("t")
    with bound(execution_id=1):
        log.info("inside")
    log.info("outside")

    inside, outside = _lines(capture)
    assert inside["execution_id"] == 1
    assert "execution_id" not in outside, (
        "context escaped its block -- a later line would be attributed to "
        "an execution that had already finished")


def test_nested_scopes_merge_rather_than_replace(capture):
    log = logging.getLogger("t")
    with bound(goal_id="KER28", execution_id=13616):
        with bound(leg=4):
            log.info("in a leg")

    (line,) = _lines(capture)
    assert line["goal_id"] == "KER28"
    assert line["execution_id"] == 13616
    assert line["leg"] == 4, "the inner scope replaced instead of merging"


def test_concurrent_tasks_do_not_see_each_others_ids(capture):
    """The asyncio property the LegSupervisor depends on."""
    log = logging.getLogger("t")

    async def leg(execution_id):
        with bound(execution_id=execution_id):
            await asyncio.sleep(0)
            log.info("step")

    async def main():
        await asyncio.gather(leg(1), leg(2), leg(3))

    asyncio.run(main())
    seen = sorted(line["execution_id"] for line in _lines(capture))
    assert seen == [1, 2, 3], (
        "legs saw each other's ids; contextvars are not being copied per "
        "task and every log line is attributed to whichever leg ran last")


def test_none_is_dropped_rather_than_logged_as_null(capture):
    log = logging.getLogger("t")
    with bound(goal_id="KER28", story_id=None):
        log.info("x")
    (line,) = _lines(capture)
    assert "story_id" not in line


def test_an_event_name_is_queryable(capture):
    log = logging.getLogger("t")
    log.info("finished", extra={"event": "leg_end"})
    (line,) = _lines(capture)
    assert line["event"] == "leg_end", (
        "a person filtering jsonPayload.event has nothing to filter on")


def test_an_exception_is_a_field_not_newlines_in_the_message(capture):
    log = logging.getLogger("t")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("it broke")
    (line,) = _lines(capture)
    assert "\n" not in line["message"]
    assert "ValueError: boom" in line["exception"]


def test_the_trace_key_appears_only_with_a_project(monkeypatch, capture):
    log = logging.getLogger("t")
    with bound(run_id="0192f3a1c2d34e5f8091a2b3c4d5e6f7"):
        log.info("no project set")
    assert F.TRACE_KEY not in _lines(capture)[0]

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "${GCP_PROJECT_ID}")
    with bound(run_id="0192f3a1c2d34e5f8091a2b3c4d5e6f7"):
        log.info("project set")
    line = _lines(capture)[1]
    assert line[F.TRACE_KEY].startswith("projects/${GCP_PROJECT_ID}/traces/")
    assert "-" not in line[F.TRACE_KEY].rsplit("/", 1)[1]


def test_the_renderer_is_swappable_without_touching_the_fields():
    """The modularity claim, asserted rather than asserted in prose."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello",
                               None, None)
    ContextFilter({"component": "worker"}).filter(record)
    record.agentos.update({"goal_id": "KER28", "event": "tick"})

    as_json = json.loads(JsonRenderer().format(record))
    as_text = TextRenderer().format(record)

    assert as_json["goal_id"] == "KER28"
    assert "goal_id=KER28" in as_text
    assert "hello" in as_text
    assert "event=tick" in as_text


def test_configure_is_idempotent(monkeypatch):
    """Three kernel entrypoints call basicConfig; a second configure must
    not leave two handlers printing every line twice."""
    reset_for_tests()
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure(component="worker")
        after_one = len(root.handlers)
        configure(component="worker")
        assert len(root.handlers) == after_one == 1
    finally:
        root.handlers = saved
        reset_for_tests()


def test_every_context_field_is_documented():
    """fields.py is the documentation; an undocumented field is a field
    nobody will know to query."""
    for name, why in F.CONTEXT_FIELDS.items():
        assert why and len(why) > 20, f"{name} has no reason recorded"
