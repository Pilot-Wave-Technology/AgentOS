"""Every locally served script carries a version token.

WHAT THIS COST, on 2026-09-24. A fix to the card renderer in
`static/js/kernel_park_card.js` was written, tested, committed, pushed
and deployed green -- and the browser testing it went on running the
cached old file, because that script tag had no `?v=`. The bug
reproduced perfectly while the fix sat on the server. From the outside
that is indistinguishable from a fix that does not work, and the next
move is to change something that was already right.

Only `app.js`, `tour.js` and `app.css` were versioned, each by its own
hand-written constant. Nineteen other tags had nothing -- which is what
three bespoke constants produce over time. There is now one helper,
`request.state.asset_v(path)`, and this test is what stops the
twenty-second tag from being added without it.

Third-party CDN scripts are not covered: they are versioned in the URL
already and are not ours to bust.
"""

from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "app/templates"

# A local SCRIPT tag. Anything with a scheme is a CDN, versioned in its
# own URL and not ours to bust. Images are excluded deliberately: a
# stale favicon is a cosmetic nuisance, while a stale script is a fix
# that silently did not ship, and only the second is worth a rule.
LOCAL_SRC = re.compile(r'src="(/static/js/[^"]+)"')


def _tags():
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = io.open(path, encoding="utf-8").read()
        for m in LOCAL_SRC.finditer(text):
            yield path.relative_to(ROOT).as_posix(), m.group(1)


def test_every_local_static_script_has_a_version_token():
    bare = [(p, s) for p, s in _tags() if "?v=" not in s]
    assert not bare, (
        "these script tags are served from cache forever, so a fix to "
        "the file never reaches a returning browser: "
        + "; ".join(f"{p} -> {s}" for p, s in bare))


def test_the_stylesheet_is_versioned_too():
    """Same failure, slower to notice, because a stale stylesheet looks
    like a design regression rather than a broken feature."""
    base = io.open(TEMPLATES / "base.html", encoding="utf-8").read()
    css = re.findall(r'href="(/static/css/[^"]+)"', base)
    assert css, "no local stylesheet found — has the path moved?"
    assert all("?v=" in h for h in css), css


def test_there_is_one_helper_and_not_a_constant_per_file():
    """THE ROOT CAUSE. A constant per file means a new file gets none,
    silently, and nobody finds out until a fix appears not to work."""
    main = io.open(ROOT / "app/main.py", encoding="utf-8").read()
    assert "def _asset_version(" in main
    assert "request.state.asset_v = _asset_version" in main


def test_the_helper_is_memoised_and_not_a_stat_per_render():
    """A `getmtime` per tag per render would put the filesystem on the
    path of every page. A Cloud Run revision has immutable static
    files, so one read per file per process is correct."""
    main = io.open(ROOT / "app/main.py", encoding="utf-8").read()
    fn = main[main.index("def _asset_version("):]
    fn = fn[:fn.index("@app.middleware")]
    assert "_ASSET_VERSIONS" in fn
    assert "if static_relative_path not in _ASSET_VERSIONS" in fn
