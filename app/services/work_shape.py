"""I3a — which SHAPE of work a story is, decided in one place.

A story produces a branch that merges. An errand produces artifacts from a
workspace that is thrown away. Both agents choose between them by the same
rule, and `stories.work_kind` is where the choice is recorded so the
planner's judgement survives to the thing that runs the work.

WHY ITS OWN MODULE. The rule belongs to `internal_create_story`, and that
router imports `app.auth`, which imports `firebase_admin` — absent on a
dev machine. So a rule living there is a rule nobody can test without the
whole web stack, which is the same shape as `models/__init__` making every
worker-side tool unimportable until Ec2 made its adapter imports lazy.

It is four lines. It is here because four lines that decide whether work
gets a Verifier deserve a test that runs anywhere.
"""

from __future__ import annotations

# The closed set `stories.work_kind` accepts. NULL is the third state and
# means `story` — what every row predating the column says.
SHAPES = ("story", "errand")


def work_kind_of(raw) -> str | None:
    """The work shape from `create_story`'s `kind`, or None for a story.

    ANYTHING BUT `errand` IS A STORY, including nonsense. Defaulting an
    unrecognised value to the narrower shape would let a typo silently
    downgrade a feature to something with no branch and no Verifier, and
    then throw the workspace away with the work still in it. The expensive
    direction is not the one to guess in.

    NULL rather than the string `story`, because NULL is what every
    existing row says and the two have to mean the same thing.
    """
    return "errand" if str(raw or "").strip().lower() == "errand" else None
