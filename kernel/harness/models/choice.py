"""Which model a run uses, and who decided. One resolution, one place.

WHAT WAS TRUE BEFORE, and it was broken rather than merely rigid.
`AGENTOS_ASSIST_MODEL` was one deployment-wide variable read as TWO
INCOMPATIBLE THINGS:

    service_tick / pod_main   get_adapter(it)      wants 'gemini'
                                                   RAISES on anything else
    quick.ASSIST_MODEL        used as a model id   defaults to
                                                   'gemini-3.8-flash'

So setting it to the shape either one documents breaks the other, and
nobody could pick a model at all without picking the wrong thing for
half the system.

THE ORDER, and it is the same one `base_image.for_goal` established:

    the story's choice  ->  the goal's  ->  the deployment default

NULL means inherit at every level, so every existing row keeps exactly
today's behaviour. A person who has never opened the dropdown sees no
change.

AND THE EXECUTOR'S IS FROZEN AT DISPATCH. `executions.executor_model` is
written once, from this resolution, and read from there for the life of
the run -- "once dispatched for a story it cannot be changed". Editing
the goal afterwards changes the next run. A leg that changed model
mid-story would make its own step journal unreadable: half the reasoning
from one model, half from another, and the cost attribution wrong for
both.

THE VERIFIER'S IS NOT FROZEN, and the difference is deliberate. Which
model JUDGES is a property of the goal's standards, and a goal that
raises its standards should have the next verification use the new one
-- including a re-verification of work already done. There is nothing to
make unreadable: an assessment is one call, not a journal.

NO PROVIDER ANYWHERE IN THIS FILE. `models/__init__.py` argues it:
"A user picks a MODEL. Which seam serves it is our problem, not theirs."
`registry.provider_of` derives it, `adapter_for_model` uses it, and two
fields that could disagree would disagree eventually -- as a vendor 404
several seconds into a leg.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# The variable, with ONE meaning now: a MODEL ID. It used to mean a
# provider to two of its four readers, which is the bug this module
# exists to end. Named in the open rather than hidden in an
# `environ.get` default -- the B1 rule, whose reason is that a default
# nobody can see is a policy nobody reviewed.
DEFAULT_ENV = "AGENTOS_ASSIST_MODEL"

# What a deployment that has chosen nothing gets. `quick.py`'s own
# default, kept so that removing the variable changes nothing.
FALLBACK = "gemini-3.8-flash"


class UnknownModel(ValueError):
    """A model nobody can serve. Refused where it is SET, not where it
    is used.

    A story dispatched with a model the registry does not know runs,
    reaches a vendor, and comes back as a 404 several seconds and one
    confusing log line later -- with an admission slot and a pod spent.
    The menu is `registry.SUPPORTED`; the refusal names it.
    """


def default_model() -> str:
    """The deployment's choice, or the fallback."""
    if DEFAULT_ENV not in os.environ:
        return FALLBACK
    return os.environ[DEFAULT_ENV].strip() or FALLBACK


def validate(model: str | None) -> str | None:
    """`model` if the registry can serve it; raises if it cannot.

    None passes through -- it means INHERIT, which is not a choice and
    has nothing to validate. Callers storing a person's selection should
    run this first, so the refusal happens in the form rather than in a
    leg.
    """
    if model is None:
        return None
    wanted = model.strip()
    if not wanted:
        return None
    from kernel.harness.models.registry import SUPPORTED    # noqa: PLC0415

    if wanted not in SUPPORTED:
        raise UnknownModel(
            f"{wanted!r} is not a model this deployment can serve. "
            f"Choose one of: {', '.join(sorted(SUPPORTED))}")
    return wanted


def _first(*choices) -> str:
    """The first non-empty choice, else the deployment default."""
    for choice in choices:
        if choice and str(choice).strip():
            return str(choice).strip()
    return default_model()


async def frozen_for(conn, execution_id: int) -> str | None:
    """The model THIS RUN was dispatched with, or None for "nobody chose".

    WHAT THE LEG READS, and the ONLY model question it may ask. It
    touches `executions` and nothing else -- deliberately, twice over.

    THE GRANT. `agentos_leg` holds SELECT on `kernel.executions` and
    does NOT hold it on `goals`. A resolution that fell through to the
    goal would be `permission denied for table goals` inside the
    execution pod, on the one path that has no fallback. The narrow read
    is what keeps this inside the audited grant set.

    THE PRECEDENCE. None means "no person expressed a preference", which
    is a DIFFERENT fact from "the deployment default", and the caller
    needs them apart: a profile that names its own model must still win
    over a default nobody chose, and must still lose to a story somebody
    did choose. Collapsing the two here -- which the version of this
    function that returned `str` did -- makes that undecidable at the
    call site.
    """
    got = await conn.fetchval(
        "SELECT NULLIF(btrim(coalesce(executor_model, '')), '') "
        "FROM executions WHERE id = $1", execution_id)
    return str(got).strip() if got else None


async def chosen_for_story(conn, story_id, goal_id) -> str | None:
    """A person's choice for this story, or None. NO deployment default.

    Split out of `for_story` so dispatch can tell "chosen" from
    "inherited" and freeze only the former -- see `freeze`.

    `story_id` may be None -- an errand has a goal and no story, and
    inherits the goal's choice, which is the behaviour a person would
    expect from a setting described as "this goal's stories".
    """
    story_choice = None
    if story_id:
        story_choice = await conn.fetchval(
            "SELECT NULLIF(btrim(coalesce(executor_model, '')), '') "
            "FROM stories WHERE id = $1", story_id)
    if story_choice:
        return str(story_choice).strip()
    if goal_id:
        goal_choice = await conn.fetchval(
            "SELECT NULLIF(btrim(coalesce(executor_model, '')), '') "
            "FROM goals WHERE id = $1", goal_id)
        if goal_choice:
            return str(goal_choice).strip()
    return None


async def for_story(conn, story_id, goal_id) -> str:
    """The model a story's legs should use, default included.

    Kept for callers that want a concrete answer to display. Dispatch
    uses `chosen_for_story` instead, because it must not freeze a
    default as though somebody had picked it.
    """
    return await chosen_for_story(conn, story_id, goal_id) or default_model()


async def for_verifier(conn, goal_id) -> str:
    """The model that JUDGES this goal's work.

    Goal-level only, and 5.x's reason is in `assess.py`: the assessor is
    the second key, and a model that can pass work makes verification
    unreproducible. Which model judges is a standard the goal sets, not
    a per-story convenience -- a story that could choose its own judge
    could choose a lenient one.
    """
    if not goal_id:
        return default_model()
    return _first(await conn.fetchval(
        "SELECT NULLIF(btrim(coalesce(verifier_model, '')), '') "
        "FROM goals WHERE id = $1", goal_id))


async def judge_for(conn, execution_id) -> str:
    """The model that judges THIS run's work.

    A RUN-SHAPED WRAPPER over `for_verifier`, which is goal-shaped. Both
    the assessor (`second_key`) and the investigator (`verdict` ->
    `investigate`) need it and both start from an execution id, so the
    `SELECT goal_id` lived in one of them and the other simply did not
    ask -- which is how the investigator came to run on hardcoded Gemini
    while the assessor honoured the goal's choice. Two models judging
    one run, and only one of them the one anybody picked.
    """
    goal_id = await conn.fetchval(
        "SELECT goal_id FROM executions WHERE id = $1", execution_id)
    return await for_verifier(conn, goal_id)


async def freeze(conn, execution_id: int, model: str | None) -> None:
    """Record the CHOICE this run was dispatched with, once, at dispatch.

    `WHERE executor_model IS NULL` so a second dispatch pass cannot
    rewrite a running story's model. Not an error when it matches
    nothing -- it means somebody got here first, which is the outcome
    this guard exists to produce.

    A NULL `model` WRITES NOTHING, and that is the point. The column
    means "a person picked this", not "this is what ran": freezing a
    resolved default would make every run look like a deliberate choice
    and would override a profile's own model with a default nobody
    asked for. Nothing chosen, nothing frozen, and the leg then falls
    through to the profile and the deployment default as it always did.
    """
    if not model:
        return
    await conn.execute(
        "UPDATE executions SET executor_model = $2 "
        "WHERE id = $1 AND executor_model IS NULL", execution_id, model)


# ── the menu, which is NARROWER than what the adapters can serve ───────
#
# `registry.SUPPORTED` is a CAPABILITY list: every model the kernel's
# adapters know how to talk to. What a person should be OFFERED is a
# smaller, curated set, and the two are different questions.
#
# GEMINI: THE 3.8 SERIES ONLY (directive 2026-09-18). The registry
# carries 3-flash-preview through 3.7-flash, and offering nine Gemini
# models -- four of them previews -- on a goal that matters is a menu
# nobody can choose from well. A PREFIX and not a list, so a
# `gemini-3.8-pro` picks itself up the day the registry gains one
# rather than needing this line edited.
#
# ANTHROPIC: unrestricted. Every model in the registry already satisfies
# the standing rule (post-opus-4.6 only).
#
# OFFERED IS A SUBSET OF ACCEPTED, deliberately. `validate` still takes
# anything the adapters can serve, so a goal configured before this
# curation keeps working -- a narrowing menu must not turn an existing
# setting into an error nobody can clear. What it cannot do is be WIDER
# than `validate`, which would be a form offering options it then
# refuses; a test pins that direction.
GEMINI_OFFERED_PREFIX = "gemini-3.8"


def offerable(model_id: str, provider: str) -> bool:
    """Should a person be shown this model?"""
    if provider == "gemini":
        return model_id.startswith(GEMINI_OFFERED_PREFIX)
    return True


def menu() -> list:
    """The dropdown, grouped by vendor. Presentation only.

    Grouping is not storage: nothing records a provider, because
    `provider_of` derives it and two fields that can disagree eventually
    do -- as a vendor 404 several seconds into a leg.
    """
    from kernel.harness.models.registry import available  # noqa: PLC0415

    groups: dict = {}
    for spec in available():
        if offerable(spec.id, spec.provider):
            groups.setdefault(spec.provider, []).append(spec.id)
    return [{"provider": p, "models": sorted(ms)}
            for p, ms in sorted(groups.items())]
