"""One card that fails to draw must not take the others with it.

THE SYMPTOM. "Plan cards are disappearing after the page refreshes."
The rows were in the database, the JSON was in the HTML, and the thread
rendered no cards at all.

THE ORDER WAS THE BUG. `hydrateParkCards` removed the <script> holding
a card's JSON and THEN built its HTML from the parsed object. Any throw
in the builder -- one unexpected field on one of sixteen card kinds --
landed between the two, leaving an empty container whose data had
already been deleted. And the throw escaped the `forEach`, so every
card below it was never hydrated either and stayed an invisible
<script>.

WHY IT SURVIVED BEING LOOKED AT. An exception inside a
DOMContentLoaded handler leaves nothing on the page. The failure and a
card that was never posted look identical from the outside, and they
need opposite fixes -- which is the same confusion the fan-out
watcher-count logging was added to settle.

THE FIX IS THREE INDEPENDENT CHANGES and all three are needed: the JSON
outlives the build, a failure is contained to its own card, and a
failed card still renders something a person can read and report.
"""

from __future__ import annotations

import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
JS = io.open(ROOT / "static/js/kernel_park_card.js", encoding="utf-8").read()


def _hydrate() -> str:
    body = JS[JS.index("function hydrateParkCards("):]
    out = []
    for line in body.splitlines(True):
        out.append(line)
        if line.rstrip() == "}":
            break
    return "".join(out)


def test_the_json_outlives_the_build():
    """`scriptEl.remove()` before the HTML exists is what turns a throw
    into a container that is empty AND unrecoverable -- a reload cannot
    help, because the data is gone from the DOM."""
    fn = _hydrate()
    fn = _hydrate()
    assert fn.index("el.innerHTML = html") > fn.index("scriptEl.remove()")
    drop = fn.index("scriptEl.remove()", fn.index("let html"))
    assert drop > fn.index("buildCardInnerHtml(card)"), (
        "the card JSON is still removed before the builder runs")


def test_one_cards_failure_is_contained():
    """Without a try/catch the throw leaves the `forEach` and every
    card after it is skipped -- the whole thread goes blank from one
    bad field."""
    fn = _hydrate()
    assert "try {" in fn and "} catch (err) {" in fn
    assert fn.index("try {") < fn.index("buildCardInnerHtml(card)")


def test_a_failed_card_still_says_something():
    """Blank is the worst outcome: it is indistinguishable from a card
    that was never posted. The title and body are plain strings and
    cannot throw, so they are always renderable."""
    fn = _hydrate()
    tail = fn[fn.index("} catch (err) {"):]
    assert "could not be drawn" in tail
    assert "escapeHtml(card.title" in tail


def test_the_fallback_cannot_reintroduce_the_xss_it_escapes():
    """The failing path is the one most likely to be reached by odd
    content, so it is the last place to relax escaping. No markdown
    renderer here -- `renderMd` is a candidate for what threw."""
    fn = _hydrate()
    tail = fn[fn.index("} catch (err) {"):]
    assert "renderMd" not in tail
    assert "escapeHtml(card.body" in tail


def test_the_failure_reaches_the_console():
    """Silent is how this lasted. The next one should name the card
    type in one line."""
    fn = _hydrate()
    tail = fn[fn.index("} catch (err) {"):]
    assert "console.error" in tail and "card.card_type" in tail


def test_the_unescape_is_not_a_mis_escaped_regex():
    """THE BUG THAT MADE EVERY CARD VANISH, for as long as this file has
    existed.

    The unescape was `.replace(/<\//g, '</')`. That literal's THIRD `/`
    closes the regex, leaving `<\` followed by `/g` -- a DIVISION by an
    undefined `g`. Every call threw `ReferenceError: g is not defined`,
    inside the try, caught by the silent `return`.

    So no card on the goal thread ever hydrated. It read as a
    persistence bug four separate times, because cards still ARRIVE live
    via `buildCardElement`, which never calls this. Measured in the
    browser: 3 `.card-container`, 3 surviving `script.card-json`, before
    AND after calling `hydrateParkCards()` by hand.

    `chat/index.html` spells the same unescape `/<\\//g` -- one
    backslash more, and correct -- which is why the assistant's thread
    never had it."""
    assert "replace(/<" not in JS, (
        "the unescape is a regex again; it was mis-escaped once and the "
        "failure is silent")
    # Built from chr(92) so this assertion cannot itself be mis-escaped
    # -- which is the whole failure being guarded against.
    bs = chr(92)
    # `'<\\/'` in JS source is the three characters < \ / -- so two
    # backslashes in the file.
    assert ("split('<" + bs + bs + "/').join('</')") in JS


def test_the_file_still_parses():
    """A ReferenceError at call time is silent. A SyntaxError is not --
    but it would take the whole module with it, and every card with it.

    NODE IS NOT IN THE TEST IMAGE, and this test broke the build finding
    that out. Skipped rather than deleted: run locally it is the only
    thing here that would catch a genuinely unparseable file, and a
    check that silently does nothing in CI is worth less than one that
    says so."""
    import shutil
    import subprocess
    import pathlib
    if shutil.which("node") is None:
        pytest.skip("node is not available in this image")
    root = pathlib.Path(__file__).resolve().parents[3]
    r = subprocess.run(
        ["node", "-e",
         "new Function(require('fs').readFileSync("
         "'static/js/kernel_park_card.js','utf8'))"],
        cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]
