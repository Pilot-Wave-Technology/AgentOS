"""`stories.work_kind` -> the (kind, domain) pair a profile is keyed by.

WHY IT IS ITS OWN MODULE. There is no `stories.domain` column: the domain
is DERIVED, and it was derived in exactly one place -- inside the kernel's
execution-creation path, which a local run never reaches. Local execution
needs the same answer, and a second copy of a derivation is how a local run
and a pod run of one story come to resolve DIFFERENT profiles. Both runs
work; they simply disagree about what the agent may do, which is not a
difference anybody goes looking for.

In `kernel/profiles/` because it is part of how a profile is keyed, and
because `app/` imports `kernel/` freely while the reverse is a single
deliberate exception.
"""

# The work itself, as opposed to `review`. Spelled once.
WORK_PHASE = ""


def derive_kind_domain(work_kind: str | None,
                       domain: str = "") -> tuple[str, str]:
    """NULL `work_kind` means story.

    The column was added after the rows it describes, so `!= 'story'` would
    call every older row an errand.

    `errand/ops` is the only errand profile, and a story seeded as
    `errand/coding` is refused at dispatch -- after the row exists, so the
    failure arrives a long way from its cause.
    """
    kind = (work_kind or "story").strip() or "story"
    if not domain:
        domain = "ops" if kind == "errand" else "coding"
    return kind, domain
