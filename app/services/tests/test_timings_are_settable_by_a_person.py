"""C5 -- the compressed profile is reachable from the panel, and expires.

The point of `kernel/timings.py` is that proving a two-hour tool behaves
does not require two hours. That is only true if a person can turn it on
for one story without editing constants, and only SAFE if what they turn
on turns itself off.

These tests hold the second half: no route to a permanent setting, no
route to a fleet-wide one, and a typo in the overrides field reported to
the person who typed it rather than raised at dispatch an hour later.
"""

from __future__ import annotations

import pytest

from app.services import timings_admin
from kernel import timings


# -- the page cannot describe a profile the fleet stopped using --------

def test_the_knob_table_is_read_from_the_kernel_not_restated():
    table = timings_admin.knob_table()
    assert {k["name"] for k in table} == set(timings.knob_names())
    by_name = {k["name"]: k for k in table}
    production = timings.values(timings.PRODUCTION)
    compressed = timings.values(timings.COMPRESSED)
    for name in timings.knob_names():
        assert by_name[name]["production"] == production[name]
        assert by_name[name]["compressed"] == compressed[name]


# -- overrides: a typo is a complaint, never an exception --------------

def test_a_known_knob_is_accepted():
    got, complaints = timings_admin.clean_overrides('{"job_max_s": 300}')
    assert got == {"job_max_s": 300}
    assert complaints == []


def test_an_unknown_knob_is_dropped_and_reported():
    got, complaints = timings_admin.clean_overrides('{"leg_budget": 1}')
    assert got == {}
    assert any("leg_budget" in c for c in complaints)


def test_malformed_json_is_a_complaint_and_not_a_raise():
    got, complaints = timings_admin.clean_overrides("{job_max_s: 300")
    assert got == {} and complaints


def test_a_non_object_is_a_complaint():
    got, complaints = timings_admin.clean_overrides("[1, 2, 3]")
    assert got == {} and complaints


def test_an_empty_field_is_not_a_complaint():
    assert timings_admin.clean_overrides("   ") == ({}, [])


@pytest.mark.parametrize("bad", ['{"job_max_s": 0}', '{"job_max_s": -5}',
                                 '{"job_max_s": true}', '{"job_max_s": "9"}'])
def test_a_value_that_is_not_a_positive_number_is_dropped(bad):
    got, complaints = timings_admin.clean_overrides(bad)
    assert got == {} and complaints


# -- the expiry is the safety, so there is no way around it ------------

def test_the_form_offers_no_permanent_setting():
    assert 0 not in timings_admin.TTL_CHOICES
    assert max(timings_admin.TTL_CHOICES) <= timings_admin.TTL_MAX_HOURS
    assert timings_admin.TTL_MAX_HOURS <= 72, (
        "a profile that outlives the session it was set for makes real "
        "runs fragile in ways nobody connects to a setting flipped on "
        "Tuesday")


def test_the_write_always_sets_an_expiry():
    import inspect
    src = inspect.getsource(timings_admin.set_profile)
    assert "expires_at" in src and "make_interval" in src
    assert "NULL" not in src.upper().replace("NOT NULL", "")


def test_the_route_clamps_rather_than_refuses_a_bad_ttl():
    """The failure worth preventing is a long-lived profile, not a typo."""
    import inspect

    from app.routers import admin_timings

    src = inspect.getsource(admin_timings.set_for_story)
    assert "min(max(" in src
    assert "TTL_MAX_HOURS" in src


# -- scope and authority -----------------------------------------------

def test_every_route_is_admin_only():
    """Two knobs destroy other people's runs; the kill switch pattern."""
    import inspect

    from app.routers import admin_timings

    for name in ("page", "set_for_story", "clear_for_story"):
        src = inspect.getsource(getattr(admin_timings, name))
        assert "require_admin" in src, f"{name} is not admin-gated"


def test_the_setting_is_per_story_and_the_story_must_exist():
    import inspect

    from app.routers import admin_timings

    src = inspect.getsource(admin_timings.set_for_story)
    assert "SELECT id FROM stories WHERE id = $1" in src, (
        "a typo in a story id would otherwise sit in the table looking set")


def test_an_unknown_profile_is_refused():
    import inspect

    from app.routers import admin_timings

    src = inspect.getsource(admin_timings.set_for_story)
    assert "profile not in timings.PROFILES" in src


# -- schema qualification, which execution 246 paid for ----------------

def test_every_kernel_table_is_schema_qualified():
    """The panel's pooled connections do not carry `search_path`."""
    src = open("app/services/timings_admin.py", encoding="utf-8").read()
    assert "kernel.story_timings" in src
    assert " story_timings" not in src.replace("kernel.story_timings", "")


# -- clearing does not reach back into a run already going -------------

def test_clearing_says_dispatched_runs_keep_their_stamp():
    import inspect

    from app.routers import admin_timings

    src = inspect.getsource(admin_timings.clear_for_story)
    assert "Runs already " in src
    assert "dispatched keep the profile they started under" in src
