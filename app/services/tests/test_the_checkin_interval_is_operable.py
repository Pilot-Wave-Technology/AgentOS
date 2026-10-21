"""The check-in cadence is a number an operator can change and see.

WHY IT HAD TO BECOME ONE. It shipped as `WINDOW_S = 900` and the first
thing anybody wanted was to shorten it to watch the feature work.
Fifteen minutes per observation is not a test cycle, and a redeploy to
change a number is not one either -- so the feature was, in practice,
unobservable.

THE SECOND HALF OF THE SAME PROBLEM is that the old cadence was a
wall-clock bucket, which bounds cost and says nothing about the gap
between two wakes. Shortening a bucket would have given a shorter but
still arbitrary gap. Both had to move together or the knob would have
been a knob on a number that did not mean what it said.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# -- reachable ---------------------------------------------------------

def test_there_is_a_route_to_set_it():
    src = _read("app/routers/admin_controls.py")
    assert '@router.post("/settings/controls/errand-checkin")' in src


def test_only_a_platform_admin_may_move_it():
    """Every other operator switch on this page is admin-gated, and a
    cadence that governs spend is not the one to make the exception."""
    src = _read("app/routers/admin_controls.py")
    assert "require_admin" in src[src.index(
        "async def errand_checkin_set("):][:300]


def test_the_change_reaches_the_ledger():
    """`updated_by` is a convenience, not an audit trail. Every other
    control on this page appends an event and this one is no different."""
    src = _read("app/routers/admin_controls.py")
    fn = src[src.index("async def errand_checkin_set("):]
    assert "_emit(" in fn[:fn.index("return {\"ok\"")]


def test_the_page_shows_the_current_value():
    """A settings box that does not render what is in force is a box
    that lies the first time a write is clamped or fails."""
    page = _read("app/templates/admin/controls.html")
    assert "checkin-seconds" in page
    assert "{{ checkin_seconds }}" in page
    src = _read("app/routers/admin_controls.py")
    assert "_checkin_state()" in src


def test_a_viewer_sees_it_and_cannot_change_it():
    page = _read("app/templates/admin/controls.html")
    card = page[page.index("Errand check-in"):]
    card = card[:card.index("Active agents")]
    assert "admin-only to change" in card


# -- honest about what it governs -------------------------------------

def test_the_page_says_event_wakes_are_not_on_this_clock():
    """THE MISREADING THIS PREVENTS. An operator who sets 3600 and then
    watches a parked plan sit for an hour would conclude the feature is
    broken. A park wakes GO within a minute at any setting; this number
    is the quiet-case cadence only."""
    page = _read("app/templates/admin/controls.html")
    card = page[page.index("Errand check-in"):]
    card = card[:card.index("Active agents")]
    assert "Event wakes are not on this clock" in card


def test_it_says_what_a_check_in_costs():
    page = _read("app/templates/admin/controls.html")
    card = page[page.index("Errand check-in"):]
    card = card[:card.index("Active agents")]
    assert "one GO turn per running errand" in card


# -- clamped ----------------------------------------------------------

def test_the_route_clamps_rather_than_refusing():
    """A number outside the range is a typo. The useful answer is the
    nearest legal value with the page showing what landed, not a form
    that refuses and loses what was typed."""
    src = _read("app/routers/admin_controls.py")
    fn = src[src.index("async def errand_checkin_set("):]
    assert "max(errand_checkin.MIN_S, min(errand_checkin.MAX_S, wanted))" in fn


def test_a_non_number_is_refused_and_not_coerced():
    """`int("")` raising inside the handler would be a 500 on a typo."""
    src = _read("app/routers/admin_controls.py")
    fn = src[src.index("async def errand_checkin_set("):]
    assert "except (TypeError, ValueError):" in fn
    assert "status_code=400" in fn[:fn.index("reason = await")]


def test_the_reader_clamps_too_and_does_not_trust_the_row():
    """The route is not the only way a row changes -- a migration, a
    console, a future importer. The consumer clamps because it is the
    one place every read passes through."""
    src = _read("app/services/errand_checkin.py")
    assert "max(MIN_S, min(MAX_S, raw))" in src


def test_an_unreadable_setting_does_not_stop_the_sweep():
    """Every caller is on a loop that must keep running. A blanked row
    should slow the feature to its default, not stop it."""
    svc = _read("app/services/platform_settings.py")
    fn = svc[svc.index("async def get_int("):]
    fn = fn[:fn.index("async def set_int(")]
    assert "except Exception" in fn and "return default" in fn
