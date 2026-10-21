"""Which executor profile a story runs under — asked the same way everywhere.

WHY THIS EXISTS. The kernel resolves `(kind, domain)` at the moment it
creates an execution (`kernel/assistant/write.py`), and a local run never
creates one. So a local run had no profile at all: the extension shipped a
hardcoded tool surface and a CLAUDE.md of phase-DAG doctrine, neither with
any relationship to what a pod would have done with the same story.

THE DERIVATION IS THE RISK, NOT THE LOOKUP. `kind` comes from
`stories.work_kind`, but **there is no `stories.domain` column** -- domain is
derived, and it was derived in exactly one place. Two derivations would let
a local run and a pod run of one story resolve DIFFERENT profiles, which is
the sort of difference nobody looks for: both runs work, and they disagree
about what the agent may do.

So the rule lives here and both callers use it.
"""

import logging

logger = logging.getLogger(__name__)

# THE DERIVATION LIVES IN THE KERNEL, beside the registry it keys into,
# and is imported rather than repeated -- see `kernel/profiles/story_keys`
# for why a second copy is the whole risk. Re-exported here so a caller
# that wants "the profile for this story" needs one import.
from kernel.profiles.story_keys import WORK_PHASE, derive_kind_domain

__all__ = ["WORK_PHASE", "derive_kind_domain", "resolve_for_story",
           "resolve_local_for_story"]


async def resolve_for_story(pool, story_id: str, phase: str = WORK_PHASE):
    """The `Profile` this story would run under. Raises `UnknownProfileError`.

    Deliberately NOT swallowing the refusal: a pair nobody built is a story
    that cannot run, and saying so here -- where a person asked -- is the
    whole reason `registry.resolve` fails closed rather than handing back a
    default with `write` and `bash` on it.
    """
    from kernel.profiles.registry import resolve

    work_kind = await pool.fetchval(
        "SELECT work_kind FROM stories WHERE id = $1", story_id)
    kind, domain = derive_kind_domain(work_kind)
    return resolve(kind, domain, phase)


async def resolve_local_for_story(pool, story_id: str, phase: str = WORK_PHASE):
    """The LOCAL `Profile` this story would run under. Raises `UnknownProfileError`.

    THE KEY IS SHARED, THE PROFILE IS NOT. `derive_kind_domain` is the same
    derivation the pod lane uses -- a second one would let the two lanes
    disagree about which profile a story even is, which is the failure nobody
    looks for because both runs work. What differs is only the tree it keys
    into: `profiles-local/`, which describes a run in somebody's editor rather
    than in a sandbox. See `kernel.profiles.local_registry` for why that is a
    separate tree and not a variant.
    """
    from kernel.profiles.local_registry import resolve_local

    work_kind = await pool.fetchval(
        "SELECT work_kind FROM stories WHERE id = $1", story_id)
    kind, domain = derive_kind_domain(work_kind)
    return resolve_local(kind, domain, phase)
