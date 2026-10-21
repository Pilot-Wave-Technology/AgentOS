"""I5f — which card the GOAL thread renders, and nothing else.

THE GOAL THREAD HAS NEVER RENDERED A CARD. Every message in it is
markdown, and that was right until GO gained something to ASK for: an
errand that runs unattended needs a person to say yes, and nobody can
say yes to prose. `errand_schedules` INSERT is a human privilege, so a
button is the only way a grant is ever created.

WHY HERE AND NOT IN THE ROUTER. `app/routers/goal_thread.py` imports
`firebase_admin`, which is not installed in a plain checkout — so a
parser living there cannot be tested without the whole auth stack. The
same reason `work_shape.py` was extracted in Phase 3, and the same shape
of lesson as every other guard in this build that could not run where
the work happens.

ONE CARD TYPE, deliberately. A renderer that guessed would change every
message in the thread; this one returns None for everything else, so
what renders today renders identically tomorrow.
"""

from __future__ import annotations

import json

CARD_TYPE = "errand_proposal"


def proposal_of(raw):
    """The errand proposal in this message's card_data, or None.

    asyncpg RETURNS jsonb AS TEXT unless a codec is registered. That has
    caught this project three times and always fails CLOSED — the card
    reads as absent rather than broken, so the page looks fine and the
    feature is simply missing, which is exactly how it survives a review.
    Both shapes are accepted here.

    Malformed JSON returns None rather than raising: this runs once per
    message on every render of the thread, and one bad row must not take
    the page down with it.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or raw.get("card_type") != CARD_TYPE:
        return None
    return raw
