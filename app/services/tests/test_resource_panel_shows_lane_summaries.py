"""The resources page shows the summaries, which now live on the lanes.

WHAT NCAL3 LOOKED LIKE, a fresh goal with two trunk lanes of one repo:

    multi_agent_dummy_repo
    repo - https://github.com/.../multi_agent_dummy_r...
    no summary
    [leaf] multi_agent_dummy_repo:cal_backend    locked
    [leaf] multi_agent_dummy_repo:cal_frontend   locked

No summary anywhere, on a goal whose lanes had been scanned. Two
separate faults, and the data was never the problem -- the browser query
selects `summary_preview` for EVERY row through a lateral join, children
included.

THE CHILD ROW HAD NO SUMMARY PILL AT ALL. It renders an icon, the name,
an SA label, a `locked` pill and a `no permission` pill, and has never
rendered anything about a summary. That was survivable while the repo
card carried the summary and the branches beneath it were an index of
names. The moment summaries moved to the lanes -- because work happens
at BRANCH level and a repo card can only show one of them -- this became
the only place they could be seen from, and it showed nothing.

AND THE REPO ROW SAID "no summary", which is the panel's way of saying
"something is missing, go and fix it": its tooltip reads "open the
resource and click Rescan". `scan_resource` skips the repo-level summary
once the goal declares lanes, ON PURPOSE, and `refresh_identity` -- what
Rescan calls -- declines for the same reason. So the pill asked for an
action that nothing would ever perform, for ever, on every laned repo.
A deliberate skip and a failure rendered identically.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

jinja2 = pytest.importorskip("jinja2")

_TEMPLATES = (pathlib.Path(__file__).resolve().parents[2] / "templates")


def _repo(**kw):
    base = {"id": "r1", "kind": "repo", "name": "multi_agent_dummy_repo",
            "uri": "https://github.com/acme/multi_agent_dummy_repo",
            "status": "active", "parent_id": None, "summary_preview": None,
            "summary_stale": True, "locked_by": None}
    base.update(kw)
    return base


def _lane(rid, branch, summary=None, **kw):
    base = {"id": rid, "kind": "branch", "role": "trunk", "uri": "",
            "name": f"multi_agent_dummy_repo:{branch}", "parent_id": "r1",
            "status": "active", "summary_preview": summary,
            "summary_stale": summary is None, "locked_by": None}
    base.update(kw)
    return base


def _render(resources, children, laned=frozenset()):
    """The resource-card loop only — the page's own fragment, rendered."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
                             undefined=jinja2.ChainableUndefined)
    env.filters["strip_md"] = lambda x: (x or "")
    src = (_TEMPLATES / "resources" / "browser.html").read_text(
        encoding="utf-8")
    a = src.index("{% for r in resources %}")
    b = src.index("{% endfor %}", src.index("{% endfor %}", a) + 5)
    frag = src[a:b] + "{% endfor %}"
    html = env.from_string(frag).render(
        pid="NCAL3", resources=resources, children_map=children,
        laned_repos=set(laned), sa_status_map={})
    return re.sub(r"<svg.*?</svg>", "", html, flags=re.S)


# ── the lane's summary is visible ────────────────────────────────────

def test_a_lane_with_a_summary_says_so_and_shows_it():
    """THE FIX. Without it a scanned lane is indistinguishable from an
    unscanned one, and from the page there is no summary anywhere."""
    html = _render([_repo()],
                   {"r1": [_lane("b1", "cal_backend",
                                 "purpose: the calculator backend.")]},
                   laned={"r1"})
    assert "purpose: the calculator backend." in html
    assert html.count(">summary<") == 1


def test_a_lane_without_one_says_that_instead():
    html = _render([_repo()], {"r1": [_lane("b1", "cal_frontend")]},
                   laned={"r1"})
    assert ">no summary<" in html
    assert ">summary<" not in html


def test_a_lane_is_labelled_a_lane():
    """`multi_agent_dummy_repo:cal_backend` reads as a branch like any
    other; which of a goal's branches are LANES is the thing a person
    opening this page is trying to see."""
    html = _render([_repo()], {"r1": [_lane("b1", "cal_backend")]},
                   laned={"r1"})
    assert "(trunk lane)" in html


def test_a_plain_branch_is_not():
    html = _render([_repo()],
                   {"r1": [_lane("b1", "feat/x", role="feature")]},
                   laned=set())
    assert "(trunk lane)" not in html


# ── and the repo stops reporting a deliberate skip as a fault ────────

def test_a_laned_repo_does_not_claim_to_be_missing_a_summary():
    html = _render([_repo()], {"r1": [_lane("b1", "cal_backend")]},
                   laned={"r1"})
    card = html[:html.index("cal_backend")]
    assert "summarised per branch" in card
    assert "no summary" not in card, (
        "the repo-level summary is skipped deliberately; rendering that "
        "as 'no summary' asks for a Rescan that refresh_identity declines")


def test_a_repo_with_no_lanes_still_reports_a_missing_summary():
    """The pill was never wrong in itself. A repo that genuinely has no
    summary and no lanes to carry one still needs to say so."""
    html = _render([_repo()], {}, laned=set())
    assert ">no summary<" in html


def test_a_repo_with_its_own_summary_still_shows_it():
    html = _render([_repo(summary_preview="purpose: the whole app.",
                          summary_stale=False)], {}, laned=set())
    assert "purpose: the whole app." in html


# ── the flag the template reads is actually computed ─────────────────

def test_the_route_marks_which_repos_carry_lanes():
    from app.routers import goal_resources

    src = inspect.getsource(goal_resources.resource_browser)
    assert "laned_repos" in src
    assert '"role") or "") == "trunk"' in src, (
        "a repo with a role='feature' branch child has NOT had its "
        "summary skipped, and must keep saying 'no summary'")
    assert '"laned_repos": laned_repos' in src


def test_the_lock_pill_says_what_holds_it():
    """`locked` with no subject reads as a fault on a fresh goal. It is
    set at story START on every `produces`/`modifies` grant, so on a
    two-lane story both lanes lock at once and that is correct."""
    html = _render([_repo()],
                   {"r1": [_lane("b1", "cal_backend", locked_by="u1",
                                 locked_for="story7")]}, laned={"r1"})
    assert "locked" in html
    assert "story7" in html
