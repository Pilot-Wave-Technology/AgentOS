"""What the verifier remembers about one execution BETWEEN ticks.

5.16 item 2. `volume.stale(stamped, wanted)` takes `stamped` as a
parameter on purpose -- E8 built the predicate and declined to invent a
storage location for the caller's copy -- and this is that location:
`kernel.verification_state`, one row per execution.

THE VOLUME IS STILL THE AUTHORITY. `volume.STAMP` is written by the
prepare Job onto the disk that actually has the bytes, and
`prepare_script` re-reads it before installing anything. This row is the
SCHEDULER's copy, held because the scheduler has no volume: it runs four
seconds at a time in the verifier Deployment, and attaching an RWOP disk
to read one file would cost more than the Job it is trying to avoid. A
stale row therefore costs one pod and never a wrong answer.

ONE MODULE OWNS THE TABLE, including the counter that has nothing to do
with environments (`disruptions`, 5.8 row 3). Two modules writing one row
is how two modules come to disagree, which is the argument `volume.py`
makes about a path spelled twice and `deps_layout` makes about a layout.

EVERY WRITE IS AN UPSERT AND EVERY STATEMENT IS SPELLED OUT. The row is a
cache -- created by whoever first has something to say about an execution
-- and a builder that assembled these statements from a dict of columns
was harder to read than the statements are.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# How long a `building_key` claim is believed before another process may
# take it. Comfortably past `prepare_volume.DEFAULT_TIMEOUT_S` (900s),
# because the Job's own `activeDeadlineSeconds` is the real bound and a
# verifier that dies mid-prepare must not wedge the execution for ever --
# a claim that cannot live on a connection needs a timeout instead of a
# lifetime.
# DERIVED, NEVER A FREE NUMBER, and the coupling is the whole point.
#
# This releases a build claim on the reasoning that "its Job has long
# since hit its deadline". That was true while the prepare budget was
# 900s and this was 1800. It stops being true the moment a prepare may
# legitimately run longer -- and then this reaper releases the claim of
# a Job that is STILL WRITING to the disk, `claim_build` lets a second
# Job take it, and two prepares attach one ReadWriteOncePod volume.
# `pod.pvc_manifest` chose RWOP so the API server refuses that overlap;
# the second Job would sit Pending until its own deadline while the
# execution waited on an environment nobody was building.
#
# A PREPARE JOB OUTLIVES THE PROCESS THAT SUBMITTED IT. It is a
# Kubernetes Job, so a verifier that dies mid-prepare leaves the Job
# running -- which is why this cannot be tuned down to "how long a
# verifier takes to restart". It has to exceed the longest build any
# story may legitimately ask for.
#
# THE MARGIN covers the Job's own teardown and the cluster's scheduling
# latency, neither of which is instant at the moment the deadline lands.
from kernel.verifier.setups import CEILING_S as _MAX_PREPARE_S

STALE_MARGIN_S = 900.0
STALE_BUILD_S = _MAX_PREPARE_S + STALE_MARGIN_S


async def current(conn, execution_id: int) -> dict:
    """This execution's row, or the empty one. Never None.

    An absent row reads as "nothing known", which every caller wants to
    behave exactly as a row with a NULL `env_key` does -- `volume.stale`
    calls that stale, and rebuilding an environment that was already
    correct costs a Job while trusting an unknown one costs a false pass.
    """
    row = await conn.fetchrow(
        "SELECT env_key, env_commit, building_key, build_error, "
        "disruptions, mounts_env::text AS mounts_env, "
        "built_at_generation, "
        "EXTRACT(EPOCH FROM (now() - building_since)) AS building_for, "
        "EXTRACT(EPOCH FROM (now() - checked_at)) AS checked_ago "
        "FROM verification_state WHERE execution_id = $1", execution_id)
    if row is None:
        return {"env_key": None, "env_commit": None, "building_key": None,
                "build_error": None, "disruptions": 0, "building_for": None,
                "built_at_generation": None,
                "checked_ago": None, "mounts_env": None}
    return dict(row)


def building(state: dict) -> str | None:
    """The hash a prepare Job is building right now, or None.

    A claim older than `STALE_BUILD_S` is NOT building: the process that
    took it is gone, its Job has long since hit its deadline, and holding
    the execution on its behalf would turn one dead verifier into one
    permanently unverifiable story.
    """
    key = state.get("building_key")
    if not key:
        return None
    for_s = state.get("building_for")
    if for_s is not None and float(for_s) > STALE_BUILD_S:
        return None
    return key


async def claim_build(conn, execution_id: int, key: str) -> bool:
    """Take the right to build `key` for this execution. False if taken.

    A COMPARE-AND-SET AND NOT A LOCK. Two verifier replicas and the
    conductor all run `adjudicate_pass`, so this needs the same exclusion
    `verdict_lock` gives a verdict -- but a prepare Job outlives the
    connection that submitted it, and an advisory lock dies with its
    connection.

    `now()` is the SERVER's clock in both branches. Three processes
    comparing their own notions of when a claim was taken is the kind of
    skew that shows up once, under load, as two Jobs on one RWOP disk.
    """
    got = await conn.fetchval(
        "INSERT INTO verification_state "
        "(execution_id, building_key, building_since) "
        "VALUES ($1, $2, now()) ON CONFLICT (execution_id) DO UPDATE SET "
        "building_key = EXCLUDED.building_key, building_since = now(), "
        "updated_at = now() "
        "WHERE verification_state.building_key IS NULL "
        "OR verification_state.building_since < "
        "now() - make_interval(secs => $3) "
        "RETURNING execution_id", execution_id, key, float(STALE_BUILD_S))
    return got is not None


async def now_building(conn, execution_id: int, key: str) -> None:
    """Relabel a claim this process already holds with the real hash.

    `claim_build` has to be taken BEFORE the tree is read -- the tick four
    seconds from now must see that a rebuild is under way -- and the
    manifest hash is not known until after it. So the claim starts
    labelled with the commit and is relabelled here, which is what lets
    `rebuild._waiting` name the hash being built rather than the tree it
    came from. `building_since` does not move: the timeout measures the
    prepare, not the naming of it.
    """
    await conn.execute(
        "UPDATE verification_state SET building_key = $2, updated_at = now() "
        "WHERE execution_id = $1 AND building_key IS NOT NULL",
        execution_id, key)


async def built(conn, execution_id: int, key: str, commit: str,
                mounts_env: dict | None = None,
                generation: int | None = None) -> None:
    """The environment on the volume IS `key`, as of `commit`.

    Written for a cache HIT as well as for a Job: both mean the same
    thing to the next tick, and recording only the Job would send the
    sweep back to the pod for a head it has already accounted for.
    """
    # `generation` STAMPS THE COLD CLOCK, and only when a build actually
    # ran. This is called for a cache HIT too -- the comment above says
    # why -- and a hit installed nothing, so recording it would reset the
    # counter without rebuilding anything and the cold rebuild would
    # never come due. `COALESCE` keeps whatever was there on a hit.
    await conn.execute(
        "INSERT INTO verification_state "
        "(execution_id, env_key, env_commit, mounts_env, checked_at, "
        " built_at_generation) "
        "VALUES ($1, $2, $3, $4::jsonb, now(), $5) "
        "ON CONFLICT (execution_id) DO UPDATE SET "
        "env_key = EXCLUDED.env_key, env_commit = EXCLUDED.env_commit, "
        "mounts_env = EXCLUDED.mounts_env, "
        "built_at_generation = COALESCE("
        "    EXCLUDED.built_at_generation, "
        "    verification_state.built_at_generation), "
        "building_key = NULL, build_error = NULL, checked_at = now(), "
        "updated_at = now()", execution_id, key, commit,
        json.dumps(mounts_env or {}), generation)


async def build_failed(conn, execution_id: int, commit: str,
                       error: str) -> None:
    """The prepare failed. `env_key` is left alone, deliberately.

    The volume still holds whatever it held, and pretending otherwise
    would be this module inventing a fact about a disk it cannot see.
    `env_commit` DOES move: 5.8 row 5 makes a broken manifest the
    executor's business, so the next tick must reach the error path
    rather than spend another Job on the same commit.
    """
    await conn.execute(
        "INSERT INTO verification_state "
        "(execution_id, env_commit, build_error, checked_at) "
        "VALUES ($1, $2, $3, now()) ON CONFLICT (execution_id) DO UPDATE SET "
        "env_commit = EXCLUDED.env_commit, "
        "build_error = EXCLUDED.build_error, building_key = NULL, "
        "checked_at = now(), updated_at = now()",
        execution_id, commit, error[:1000])


async def released(conn, execution_id: int) -> None:
    """Give the build claim back without recording an outcome.

    For the prepare that RAISED rather than reported: a cluster call that
    threw says nothing about the manifest, so neither `env_key` nor
    `build_error` may move. Without this the execution would hold its own
    claim until `STALE_BUILD_S` -- half an hour of a story not being
    verified because one API call failed.
    """
    await conn.execute(
        "UPDATE verification_state SET building_key = NULL, "
        "updated_at = now() WHERE execution_id = $1", execution_id)


async def checked(conn, execution_id: int) -> None:
    """Stamp the cooldown for the overlap path.

    Called when the head was asked for and had not moved. Only the
    RUNNING sweep reads it: a claim waiting on a verdict is checked on
    the tick it arrives, because a cooldown there would be a window in
    which a false pass is possible (5.9).
    """
    await conn.execute(
        "INSERT INTO verification_state (execution_id, checked_at) "
        "VALUES ($1, now()) ON CONFLICT (execution_id) DO UPDATE SET "
        "checked_at = now(), updated_at = now()", execution_id)


async def disrupted(conn, execution_id: int) -> int:
    """One more consecutive ending by the substrate. Returns the count."""
    return await conn.fetchval(
        "INSERT INTO verification_state (execution_id, disruptions) "
        "VALUES ($1, 1) ON CONFLICT (execution_id) DO UPDATE SET "
        "disruptions = verification_state.disruptions + 1, "
        "updated_at = now() RETURNING disruptions", execution_id) or 1


async def decided(conn, execution_id: int) -> None:
    """A claim was judged, so the disruption run is over.

    Only ever a reset, and only on a row that exists: an execution that
    has never been disrupted needs no row, and creating one to write a
    zero would make this table grow with every verdict.
    """
    await conn.execute(
        "UPDATE verification_state SET disruptions = 0, updated_at = now() "
        "WHERE execution_id = $1 AND disruptions <> 0", execution_id)
