"""A non-ASCII filename must not 500 the download.

MEASURED. `GET /goals/ERR10/resources/art_a60f320cfa72/content?download=1`
returned 500 with:

    UnicodeEncodeError: 'latin-1' codec can't encode character
    '\u2014' in position 47

An em dash in the artifact's name. HTTP headers are latin-1, so
`Content-Disposition: attachment; filename="... — ..."` cannot be
encoded, and Starlette raises while BUILDING the response -- several
frames from anything that mentions a filename, so the traceback reads as
a codec problem rather than a naming one.

THE FAULT WAS OLDER THAN THE BUTTON. The same header was already built
for the inline viewer. There was simply no way to ask for a file whose
name had one, so nothing reached it; adding a download button made an
existing bug reachable. And the names in question are written by a model
-- "Calculator Test Summary — Report.md" is what it produces
unprompted -- so this was not an exotic input.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from app.services.content_disposition import content_disposition

ROOT = pathlib.Path(__file__).resolve().parents[3]

NAMES = [
    "Calculator Test Summary — Report.md",   # the one that broke it
    "plain.pdf",
    "日本語.csv",                       # non-latin script
    'quote"inside.txt',
    "back" + chr(92) + "slash.txt",
    "",                                          # no name at all
    # Control characters, built without escapes so the
    # literal cannot be mangled by the tooling that writes
    # this file.
    "tab" + chr(9) + "newline" + chr(10) + ".txt",
]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("disposition", ["attachment", "inline"])
def test_the_header_can_always_be_encoded(disposition, name):
    """The only property that actually matters: Starlette must be able
    to put it on the wire."""
    content_disposition(disposition, name).encode("latin-1")


def test_the_real_name_survives_as_rfc6266():
    """`filename=` alone would mangle every non-English name. Browsers
    prefer `filename*`, so the real one arrives intact."""
    h = content_disposition("attachment", "Report — v2.md")
    assert "filename*=UTF-8''" in h
    assert "%E2%80%94" in h


def test_the_ascii_fallback_keeps_the_shape():
    """`errors="replace"` rather than dropping: "Report — v2.md"
    becomes "Report ? v2.md" and not "Report  v2.md"."""
    h = content_disposition("attachment", "Report — v2.md")
    assert 'filename="Report ? v2.md"' in h


def test_a_quote_cannot_end_the_header_early():
    """A quote in the fallback would close the quoted string -- a broken
    name, and a header-injection shape."""
    h = content_disposition("attachment", 'a"b.txt')
    assert 'filename="a\'b.txt"' in h


def test_an_empty_name_still_produces_a_header():
    assert 'filename="download"' in content_disposition("attachment", "")


# -- both routes use it -------------------------------------------------

def test_the_resource_route_uses_it():
    src = io.open(ROOT / "app/routers/goal_resources.py",
                  encoding="utf-8").read()
    assert "content_disposition(disposition," in src
    assert 'filename="{row' not in src


def test_the_artifact_route_uses_it_too():
    """The same trap, and artifacts are the MORE likely to hit it: their
    names are written by a model."""
    src = io.open(ROOT / "app/routers/artifacts.py", encoding="utf-8").read()
    assert 'content_disposition("attachment", name)' in src
    assert 'filename="{name}"' not in src
