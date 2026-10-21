"""Anything on the resources page that has bytes can be taken away.

WHAT WAS MISSING. The resources browser listed uploaded documents and
catalogued artifacts and offered no way to get either. A Download link
existed on the DETAIL page, but only inside the "extraction failed, try
again" panel -- so it appeared exactly when the file was least useful
and never otherwise.

TWO STORES, ONE URL. An uploaded document keeps its bytes in GCS under
`gcs_uri`. A catalogued artifact has no `gcs_uri` at all: it lives in
the content-addressed artifact store, under a resource id of `art_` plus
the first twelve characters of its artifact id (see the join in
`app/routers/artifacts.py`). That is a truncation, so the full id has to
be looked up rather than reconstructed.

Both are served from the one resource endpoint, so a page listing
resources never has to know where any particular one keeps its bytes --
and a third store would be one branch in the router rather than a new
branch in every page.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _route() -> str:
    src = _read("app/routers/goal_resources.py")
    body = src[src.index("async def resource_content("):]
    return body[:body.index("# ── Lock / Unlock")]


# -- the endpoint -------------------------------------------------------

def test_it_serves_an_uploaded_document_from_gcs():
    assert "download_gcs_uri_bytes(row[\"gcs_uri\"])" in _route()


def test_it_serves_a_catalogued_artifact_from_the_artifact_store():
    src = _read("app/routers/goal_resources.py")
    fn = src[src.index("async def _artifact_bytes("):]
    assert "from kernel.artifacts.aio import aget" in fn
    assert "left(m.artifact_id, 12) = $1" in fn, (
        "the resource id is a TRUNCATION of the artifact id, so it has "
        "to be looked up rather than reconstructed")


def test_the_artifact_lookup_is_scoped_to_the_goal():
    """The artifact store is content-addressed and GOAL-BLIND -- it
    cannot enforce who may read what. The join to `executions` is the
    only thing stopping one goal reading another's bytes by guessing a
    resource id."""
    src = _read("app/routers/goal_resources.py")
    fn = src[src.index("async def _artifact_bytes("):]
    # LEFT JOIN since 2026-10-01: an artifact belongs to an execution (a pod
    # run) or directly to a story (a run in an editor, which creates no
    # execution). An inner join filtered every local artifact out silently.
    # The scoping itself is unchanged -- see the COALESCE below.
    assert "JOIN kernel.executions e ON e.id = m.execution_id" in fn
    assert "COALESCE(e.goal_id, m.goal_id) = $2" in fn, (
        "the lookup is not scoped to the goal for BOTH kinds of owner")


def test_a_catalogued_row_whose_bytes_are_gone_says_so():
    """A manifest row without content is a different fault from a
    missing row, and saying which saves somebody checking permissions
    when the store is what is wrong."""
    src = _read("app/routers/goal_resources.py")
    fn = src[src.index("async def _artifact_bytes("):]
    assert "status_code=410" in fn


# -- download vs preview ------------------------------------------------

def test_download_is_explicit_and_not_left_to_the_browser():
    """The `download` attribute on an anchor is ignored in cases a
    person cannot predict, and the failure looks like a broken button.
    The server says `attachment` when asked."""
    route = _route()
    assert 'query_params.get("download")' in route
    assert '"attachment" if want_download else "inline"' in route


def test_the_viewer_still_gets_inline():
    """This route also backs the in-page viewer, where a PDF must
    render rather than land in the downloads folder."""
    assert 'else "inline"' in _route()


# -- the page -----------------------------------------------------------

def _page() -> str:
    return _read("app/templates/resources/browser.html")


def test_every_downloadable_row_has_a_button():
    page = _page()
    assert page.count("content?download=1") == 2, (
        "the page lists resources at two levels -- top-level rows and "
        "children under a repo -- and both need the button")


def test_the_template_is_not_told_where_the_bytes_live():
    """`downloadable` is computed in the route. A template testing for
    `gcs_uri` would silently omit every artifact, which is the half of
    this that is easiest to miss."""
    page = _page()
    assert "r.downloadable" in page and "child.downloadable" in page
    assert "r.gcs_uri" not in page
    src = _read("app/routers/goal_resources.py")
    assert 'r["downloadable"] = bool(r.get("gcs_uri"))' in src
    assert 'startswith("art_")' in src


def test_a_repo_or_branch_gets_no_button():
    """There are no bytes to hand over, and a button that 404s is worse
    than no button."""
    src = _read("app/routers/goal_resources.py")
    block = src[src.index('r["downloadable"]'):]
    block = block[:block.index("# Build tree")]
    assert "gcs_uri" in block and "art_" in block
    assert "repo" not in block and "branch" not in block


def test_the_child_download_is_not_nested_inside_the_row_link():
    """A nested <a> is invalid HTML: the browser closes the outer one
    early and everything after it moves somewhere unpredictable."""
    page = _page()
    block = page[page.index("<!-- Children (branches, files under repo) -->"):]
    block = block[:block.index("{% endfor %}")]
    assert block.count("<a ") == block.count("</a>"), (
        "the child block's anchors are unbalanced")
    # The ROW link must close before the download anchor opens. The
    # last `</a>` in the block belongs to the download itself, so
    # compare against the FIRST one.
    assert block.index("</a>") < block.index("child.downloadable"), (
        "the download sits inside the row link")
