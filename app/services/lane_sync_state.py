"""What trunk a lane has actually been brought up to date with.

THREE PREDICATES ASKED THIS AND ALL THREE GOT IT WRONG THE SAME WAY --
the cloud's catch-up sweep, the local one, and the local submit gate.
Each carried its own copy of:

    COALESCE(gn.last_synced_trunk_sha, gn.base_sha) IS DISTINCT FROM
    t.trunk_sha

The fallback to `base_sha` asserts "a branch is in sync with the trunk
it was cut from". That is true BY CONSTRUCTION of a freshly cut branch:
`create_ref` makes it point at `base_sha`, so it contains that commit.

IT IS FALSE OF A REOPENED BRANCH, and that is the whole bug.
`reopen_for_next_cycle` clears `base_sha` -- deliberately, because a
stale one makes the engine compare new work against the previous cut
and report it as already landed -- and the create pass then stamps the
LIVE TRUNK TIP in its place. But the branch is not re-cut: it already
exists, carrying the previous round's commits, and with a merge-commit
strategy it is behind trunk by exactly its own merge commit.

So after a reopen the two halves of the system disagreed, and each
refused on its own reading:

  * the grind pass asks GITHUB, sees a lane behind trunk, and declines
    to open a pull request for it -- right in general;
  * the sweeps ask the CACHE, see `base_sha == trunk_sha`, conclude the
    lane is level, and offer no catch-up.

No pull request, and no offer of the thing that would produce one.
Seen on ec6d5d1f (2026-10-14), and it was systematic: every reopened
lane landed there, in both lanes, because both reopened through the
same function.

THE PATH THAT PRODUCED IT IS NOW CLOSED. More work after a merge is
refused outright -- "more work should always be done before merging"
-- so nothing routinely bumps a node past cycle 1 any more, and this
predicate is inert in the ordinary case: cycle 1 takes the `base_sha`
fallback exactly as before.

IT IS KEPT BECAUSE IT IS SIMPLY TRUER. "No sync recorded" means no
sync happened; reading it as "synced to its base" was only ever sound
for a branch still sitting where it was cut. If anything ever reopens
a lane again -- the machinery is still there, reachable from
`park_resolve` -- the trap does not come back with it.

THE FIX IS TO STOP PRETENDING NULL MEANS SYNCED. `last_synced_trunk_sha`
is the field that records a sync, and it is NULL after a reopen because
none has happened. The fallback is only sound while the branch still
sits where it was cut -- which is cycle 1 and only cycle 1.
"""

from __future__ import annotations

# The trunk this lane is known to contain, or NULL when nothing is known.
#
# `gn` is the `git_nodes` alias the caller uses. Written as a fragment
# rather than a function because all three callers are set-based SQL
# sweeps, and pulling them into Python would turn one query into a
# query plus a loop.
SYNCED_TRUNK_SQL = """COALESCE(
        gn.last_synced_trunk_sha,
        -- ONLY ON THE FIRST CYCLE. A freshly cut branch contains its
        -- base by construction. A REOPENED one does not: `base_sha` is
        -- re-stamped with the live trunk tip while the branch keeps the
        -- previous round's history, so it is behind by its own merge
        -- commit and claiming otherwise is how it gets stranded with no
        -- pull request and no catch-up.
        CASE WHEN COALESCE(gn.cycle, 1) = 1 THEN gn.base_sha END
    )"""

# "This lane is not known to contain the current trunk." NULL propagates
# correctly through `IS DISTINCT FROM`: nothing known means not synced,
# which is the honest reading and the safe direction -- the cost is one
# redundant catch-up offer, against a story stranded for ever.
BEHIND_TRUNK_SQL = f"{SYNCED_TRUNK_SQL} IS DISTINCT FROM t.trunk_sha"
