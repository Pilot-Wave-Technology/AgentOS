"""A stalled run must not be described as running.

RUN 308, MEASURED. `stalled_at` was set at 15:35:30 with a
`stall_card_note` of `provider_unavailable`. Over the next twelve
minutes the check-in digest told GO, twelve times:

    - it is still running and you have not checked in for 1 minute.

GO had already discovered the truth for itself at 15:35:43 through
`story_progress` -- "stuck at step 0 with provider_unavailable" -- and
was then reassured eleven times that nothing was wrong. The run was
cancelled by hand in the end.

A WAKE THAT MISINFORMS IS WORSE THAN NO WAKE. The digest exists so GO
can act without looking further; once it is wrong, the ones that are
right stop being worth trusting. This is the only place in the module
where a fault produces confident text rather than silence.
"""

from __future__ import annotations

import asyncio
import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = io.open(ROOT / "app/services/go_errand_watch.py",
              encoding="utf-8").read()


class _Pool:
    """No questions, no jobs -- so the digest is only about the state."""

    async def fetch(self, *_a, **_k):
        return []


def _digest(**row):
    from app.services.go_errand_watch import digest

    base = {"id": 1, "goal_id": "G", "story_id": "s1", "state": "RUNNING",
            "title": "t", "stall_reason": None,
            "stalled_s": None}
    base.update(row)
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        digest(_Pool(), base, 900, 120))


def test_a_stall_is_reported():
    out = _digest(stall_reason="provider_unavailable", stalled_s=720)
    assert "STALLED" in out
    assert "provider_unavailable" in out


def test_a_stalled_run_is_not_called_still_running():
    """THE EXACT SENTENCE THAT WAS WRONG. Both lines can be produced
    from one row, and printed together GO reads past the one that
    matters."""
    out = _digest(stall_reason="provider_unavailable", stalled_s=720)
    assert "nothing has been reported" not in out


def test_a_healthy_run_still_gets_its_check_in():
    """The stall guard must not suppress the ordinary case -- that is
    the wake the whole module exists for."""
    out = _digest()
    assert "nothing has been reported" in out
    assert "STALLED" not in out


def test_the_stall_says_what_to_do_about_it():
    """"It is stalled" with no next step costs a turn and buys a
    question. The digest's contract is that what GO needs to decide
    travels with the wake."""
    out = _digest(stall_reason="provider_unavailable", stalled_s=300)
    assert "ask the executor" in out
    assert "stop or restart" in out


def test_the_query_reads_the_stall_columns():
    """The digest can only know what `_LIVE` selects, and the first cut
    of it selected neither column -- which is why the fault was
    invisible from inside the sweep."""
    assert "e.stall_reason" in SRC


def test_the_stall_is_read_from_stall_reason_not_the_card_mirror():
    """`stall_card_note` RECORDS WHAT WAS ALREADY POSTED, so the card
    poster can spot a CHANGE via `stall_reason IS DISTINCT FROM
    stall_card_note`. It is not cleared when the stall lifts.

    Run 310 finished installing and came up RUNNING with `stall_reason`
    NULL and the note still reading "installing: its environment is
    still installing (workspace-setup)". Reading the mirror would have
    told GO a healthy run had stalled, for a reason already resolved --
    and "installing" was never a stall to begin with. Across the table
    the two columns disagree on real rows."""
    assert "e.stall_reason" in SRC
    code = " ".join(l for l in SRC.splitlines()
                    if not l.lstrip().startswith(("--", "#")))
    assert "stall_card_note" not in code, (
        "the digest reads the card mirror again")
    assert 'row.get("stall_reason")' in SRC
