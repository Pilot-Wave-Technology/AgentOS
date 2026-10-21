"""An errand's repos are writable, whichever door granted them.

THE RULE HAD TWO DOORS AND WE CLOSED ONE. The upgrade was written into
`kernel/go/errand_resources.py`, whose comment claimed `validate` "has
one caller ... the GO, the assistant and a scheduled errand all go
through it". That is true of the PROPOSAL door only. A hand-started
errand takes the other one: the GO creates the story and then calls
`grant_resource`, which reaches `mounts.grant` and never passes through
`validate` at all.

Measured on dev 2026-10-07: errand `f87d306f`, two branches of one
repository, both stored `needs`, both mounted read-only -- while
proposal #9 twenty minutes earlier came out `modifies` with the note
below attached, because it went through the door that had the rule.

A rule with two doors is one rule written twice, which is how a fix
comes to be half-applied. It lives here now, imported by both, and
this module imports nothing so it cannot create a cycle in either
direction.

WHY AN ERRAND NEEDS TO WRITE SOMEWHERE. Its deliverable is an artifact,
and `emit_artifact` takes a PATH -- a file that already exists. With
every mount read-only, `writable_scope_for` returns [] and the `write`
tool refuses everywhere, so the model finds the one thing that still
works: a shell heredoc. ERR17's errands 409 and 410 spent 8 and 3
permission parks and 115 and 123 model calls writing one report through
`python3 -c`, against 37 and 47 for the same shape of errand with a
writable repo.

REPOS AND BRANCHES ONLY. Cloud reach stays read-only -- a run ACTS AS an
identity, it never edits one -- and a document is `copyin`, which has
nothing to write to.

THE COST, ACCEPTED DELIBERATELY: an errand can no longer be granted a
repo read-only. Nothing it writes is pushed (`workspace/deliver.py`
holds every writable mount back for an errand), so what it may edit is
a scratch tree that is discarded when the run ends.
"""
from __future__ import annotations

# The kinds that have a tree to write in. `doc`/`dataset` are `copyin`
# and `gcp_*`/`aws_*`/`azure_*` are cloud reach; neither is a checkout.
WRITABLE_KINDS = ("repo", "branch")

# NOT SILENT. The GO asks for `needs` and the story holds `modifies`;
# without this on the grant, an audit sees two answers and nothing
# joining them.
NOTE = ("granted writable because an errand needs a checkout to work "
        "in; nothing it writes is pushed")


def upgrade(kind: str, relation: str, why: str = "") -> tuple[str, str, bool]:
    """`(relation, why, changed)` for one errand grant.

    Only `needs` moves, and only on a checkout. `produces` and
    `modifies` are already writable and are left exactly as asked, so
    this can be called on every grant without inspecting the caller.
    """
    if relation != "needs" or kind not in WRITABLE_KINDS:
        return relation, why, False
    return "modifies", (f"{why} ({NOTE})" if why else NOTE), True
