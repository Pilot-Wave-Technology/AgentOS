"""The card a person sees has the control the server sent it.

THIS BUG WAS FIXED ONCE AND CAME BACK ONE LAYER UP.

`test_clarification_is_answerable` asserts that `build_card` gives a
clarification an `options` list and `allow_custom: True`, and it does.
Then the browser throws both away:

    if (!card.needs_human) return '';        // parkOptionsHtml

`needs_human` answers an AUTHORIZATION question -- "is a person the only
one who may answer this?" -- and `clarification` is deliberately outside
`HUMAN_ANSWERED_KINDS` because the ASSISTANT is allowed to answer one.
That is what makes `ask_human` cheap. Used as the gate on DRAWING
anything, it turned "the assistant is allowed to help" into "nobody may
reply", for a second time, in a different file.

WATCHED ON NCAL3 RUN 189. The executor asked whether to approve a
completion given Node was missing from the image. The card rendered the
question, the run sat PARKED_HOT, and there was no button and no box.
The only exit was cancelling the run.

WHY THIS TEST RUNS THE JAVASCRIPT. The previous test asserted the
server's payload and passed throughout. A test that stops at the layer
boundary cannot see a defect that lives on the boundary, so this one
takes `build_card`'s real output, feeds it to the real
`parkOptionsHtml` lifted out of the template, and looks at the HTML.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from app.services.kernel_park_cards import build_card

# THE RENDERER MOVED OUT OF THE PAGE. It was inline in
# chat/index.html, which is why the GO's thread showed an approved
# errand's plan card with no buttons -- the markup arrived and nothing
# on that page knew how to draw it. It is now a module both threads
# load, and this test lifts its functions from there.
_TPL = (pathlib.Path(__file__).resolve().parents[3]
        / "static" / "js" / "kernel_park_card.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is needed to run the renderer")


def _fn(name: str) -> str:
    """Lift one top-level function out of the template, verbatim."""
    src = _TPL.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


def _consts() -> str:
    src = _TPL.read_text(encoding="utf-8")
    out = []
    for name in ("PARK_STYLE", "PARK_VERDICTS"):
        i = src.index(f"const {name} = ")
        out.append(src[i:src.index(";\n", i) + 2])
    return "".join(out)


def _render(card: dict) -> str:
    """The real renderer, on a real card, in node."""
    js = (
        "function escapeHtml(s){return String(s).replace(/[&<>\"']/g,"
        "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"
        "\"'\":'&#39;'}[c]));}\n"
        + _consts()
        + _fn("parkNotePlaceholder")
        + _fn("parkOptionsHtml")
        + f"\nprocess.stdout.write(parkOptionsHtml({json.dumps(card)}));\n"
    )
    done = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _row(kind, **kw):
    base = {"kind": kind, "question_text": "does this repo use pnpm?",
            "execution_id": 189, "id": 71, "goal_id": "NCAL3",
            "story_id": "c9fb2632", "payload": {}, "checks": "[]"}
    base.update(kw)
    return base


def _card(kind):
    card = build_card(_row(kind), None, "")
    card.setdefault("question_id", 71)
    return card


# ── the fix ──────────────────────────────────────────────────────────

def test_a_clarification_renders_something_to_answer_with():
    """THE BUG, end to end. The server filled the card in; the browser
    returned an empty string."""
    html = _render(_card("clarification"))
    assert html.strip(), (
        "parkOptionsHtml returned nothing for a card that carries "
        "options and allow_custom -- the run parks with no way out")
    assert "park-note-71" in html, "no box to type the answer into"
    assert "decidePark(71," in html, "no button to send it"


def test_the_box_asks_for_an_answer_not_for_a_complaint():
    html = _render(_card("clarification"))
    assert "Type your answer" in html, (
        "the placeholder read 'What should change?', which is a prompt "
        "for a revision, on a card whose note IS the answer")


def test_the_card_says_which_kind_it_is():
    """`decidePark` needs it to refuse an empty answer, and it must not
    travel through an onclick."""
    html = _render(_card("clarification"))
    assert 'data-park-kind="clarification"' in html


# ── and nothing that worked stopped working ──────────────────────────

@pytest.mark.parametrize("kind", ["authorization", "acceptance",
                                  "permission", "escalation", "direction"])
def test_every_other_kind_still_renders_its_controls(kind):
    html = _render(_card(kind))
    assert "decidePark(71," in html or "park-note-71" in html


def test_a_card_with_nothing_to_offer_still_renders_nothing():
    """The early return is not removed, only re-keyed. A card with no
    options, no custom box and no human needed must stay silent."""
    html = _render({"question_id": 71, "kind": "note", "needs_human": False,
                    "allow_custom": False, "options": []})
    assert html.strip() == ""


def test_a_decided_card_shows_the_outcome_and_no_controls():
    html = _render({"question_id": 71, "kind": "clarification",
                    "needs_human": False, "allow_custom": True,
                    "options": [{"value": "approve", "label": "x"}],
                    "decided": "approve"})
    assert "card-responded" in html
    assert "decidePark" not in html


# ── the sender ───────────────────────────────────────────────────────

def test_an_empty_answer_is_refused_before_it_is_sent():
    """Resolving a question with empty text un-parks the run and tells
    the executor exactly as much as never answering did."""
    src = _TPL.read_text(encoding="utf-8")
    body = src[src.index("async function decidePark("):]
    body = body[:body.index("\n}\n")]
    assert "parkKind === 'clarification'" in body
    assert "!note.trim()" in body


def test_the_answer_text_is_what_the_endpoint_receives():
    """The note is the answer; `decide` passes `text` to the kernel."""
    src = _TPL.read_text(encoding="utf-8")
    assert re.search(r"JSON\.stringify\(checks\s*\?\s*\{\s*verdict,\s*text: note",
                     src), "the note must ride as `text`"

    from app.routers import kernel_park
    import inspect
    ep = inspect.getsource(kernel_park.decide_question)
    assert 'text=str(body.get("text") or "")' in ep
