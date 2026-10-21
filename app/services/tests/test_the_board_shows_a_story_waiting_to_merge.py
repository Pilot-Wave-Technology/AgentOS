"""A story waiting for its merge is ON the board, under `in progress`.

IT WAS NOWHERE, AND THAT IS WORSE THAN A COSMETIC GAP. The board builds
one column per entry in `STORY_STATES_DISPLAY` and silently DROPS any
story whose state is not among them. `awaiting_merge` got its own
migration, its own constraint value and its own handling in the board
TEMPLATE -- and was never added to that list, so a story whose work is
pushed and whose pull request is open vanished from the board.

The reasonable conclusion on seeing a story gone is that it finished.
It is the opposite: `awaiting_merge` is the ONE state that needs a
person to go and do something, on GitHub.

UNDER `in progress`, NOT A COLUMN OF ITS OWN (Abhishek, 2026-10-14). A
story waiting on its pull request is still in progress, and a seventh
column would split one phase of work across two places. The CARD is
what carries the distinction -- without it the story reads as stalled
work rather than as a pull request somebody needs to merge.

NOT A LOCAL-LANE BUG. A cloud coding story holds the same state for the
same reason, so this was true of every story that has ever waited to
land.
"""
from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2]
BOARD = SRC / "templates" / "stories" / "board.html"


def test_it_is_not_a_column_of_its_own():
    from app.routers.stories import STORY_STATES_DISPLAY

    assert "awaiting_merge" not in STORY_STATES_DISPLAY


def test_it_is_folded_into_in_progress():
    """The remap, pinned at source: the board router has no behavioural
    seam to drive from a test, and the whole failure was a story that
    reached neither a column nor an error."""
    src = (SRC / "routers" / "stories.py").read_text(encoding="utf-8")
    i = src.index("columns = {s: [] for s in STORY_STATES_DISPLAY}")
    block = src[i:i + 900]
    assert 'state == "awaiting_merge"' in block, (
        "a story waiting on its merge reaches no column and is dropped")
    assert block.index('state == "awaiting_merge"') < block.index("if state in columns"), (
        "the remap happens after the column test, so it has no effect")


def test_the_card_says_what_it_is_waiting_for():
    """Folded in without a marker, the story reads as stalled work."""
    html = BOARD.read_text(encoding="utf-8")
    assert "story.state == 'awaiting_merge'" in html
    assert "merge it on GitHub" in html.lower() or "merge it on github" in html.lower()


def test_every_state_the_database_permits_is_reachable_on_the_board():
    """THE GUARD THAT WOULD HAVE CAUGHT THIS. A state can be added to the
    constraint without being added to the board, and the only symptom is
    stories quietly going missing.

    `rejected` and `cancelled` are deliberate omissions -- nobody wants
    columns for them -- so they are named rather than inferred.
    """
    from app.routers.stories import STORY_STATES_DISPLAY

    from app.services.tests.shipped_tree import IS_DISTRIBUTION, create_table

    if IS_DISTRIBUTION:
        # The constraint as the squashed schema declares it.
        m = re.search(r"CONSTRAINT stories_state_check CHECK .*",
                      create_table("stories"))
        sql = m.group(0) if m else ""
    else:
        sql = (SRC.parent / "migrations" / "story_awaiting_merge_state.sql").read_text(
            encoding="utf-8")
    permitted = set(re.findall(r"'([a-z_]+)'::text", sql))
    assert permitted, "the state constraint could not be read"
    router = (SRC / "routers" / "stories.py").read_text(encoding="utf-8")
    shown = set(STORY_STATES_DISPLAY)
    missing = []
    for state in sorted(permitted - shown - {"rejected", "cancelled"}):
        # Either a column, or explicitly remapped onto one.
        if f'state == "{state}"' not in router:
            missing.append(state)
    assert missing == [], (
        f"{missing} are valid story states that reach no board column — "
        "a story in one of them is dropped without a trace")


def test_the_column_count_matches_the_grid():
    """Add or remove a state and forget `grid-cols-N` and the last column
    wraps underneath, which reads as a rendering bug."""
    from app.routers.stories import STORY_STATES_DISPLAY

    html = BOARD.read_text(encoding="utf-8")
    m = re.search(r'id="story-board-columns"[^>]*grid-cols-(\d+)', html)
    assert m, "the board's column grid could not be found"
    assert int(m.group(1)) == len(STORY_STATES_DISPLAY)
