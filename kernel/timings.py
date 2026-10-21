"""How long things are allowed to take, and how to make that minutes.

THE PROBLEM. Proving that a two-hour tool does not stall a leg, does not
burn tokens, and reports honestly when it hits its budget means running
something for two hours -- or lying to the clocks. This makes the second
option a first-class, audited, expiring, per-story setting instead of a
developer editing constants and forgetting.

PER STORY, SET BY THE PLATFORM. Not platform-wide, and not declared by
the story. Two of these knobs are destructive when they reach work that
did not ask for them:

  * `reclaim_stale_hours` at 0.02 gives a FAILED execution 72 seconds
    before its pod, its volume and its workspace are taken. `reclaim`
    says exactly one FAILED execution ever holds a pod -- a leg-ceiling
    non-convergence -- "and that is precisely the run whose workspace
    answers the question". Fleet-wide, that deletes the evidence from
    somebody else's failure.
  * `startup_budget_s` at 60 is below the ~100 s a node takes to scale
    up, so an unrelated story's pod is destroyed while it is still
    scheduling, and dispatch rebuilds it, and so on.

And the story cannot choose its own, for the reason `verifier/budget`
gives about contracts: the model whose work is being measured must not
set the measurement.

STAMPED AT DISPATCH, NOT READ LIVE. The execution carries the profile it
started under. That is what makes an expiry safe -- a TTL lapsing
mid-run cannot leave a job with a compressed budget and production
reclaim patience at the same time, which is a state neither profile
describes. It is also what lets a card say which profile THIS run used.

AND IT IS WHY THE SANDBOX WORKS AT ALL. Three of these are enforced
inside the sandbox container, which has no database, no DSN and a closed
env list. Resolved at dispatch, they are literals in the pod spec like
every other sandbox setting; resolved live, they would be unreachable.
"""

from __future__ import annotations

PRODUCTION = "production"
COMPRESSED = "compressed"

# Every knob, with the value that is true today. A name absent here
# cannot be set: `timings_overrides` validates against these keys, the
# way `ALLOWED_ENV_KEYS` bounds what a setup may export.
#
# WHAT IS DELIBERATELY NOT HERE:
#
#   setup_budget_s   it is `job_default_s + 60` by construction, and a
#                    test pins the equality. Two knobs for one number is
#                    how "the checks fail for a reason that is not the
#                    work" comes back.
#   leg_budget       E0 measured this and wrote it down: "LEG SIZE IS NOT
#                    A TEST-DURATION KNOB ... a leg below the orientation
#                    floor does not make a run shorter, it makes it never
#                    finish." Exercise a leg boundary with a task that
#                    needs two legs.
#   wedged_probe_s   `probe_wedged` has no caller. A knob for it would
#                    configure nothing.
#   startup_budget_s and reclaim_stale_hours
#                    THEY WERE HERE AND DID NOTHING, which is worse than
#                    absent: a page advertised two numbers, a person set
#                    them, and `promote.STARTUP_BUDGET_S` and
#                    `reclaim.STALE_HOURS` went on reading their own
#                    constants. Neither was in SANDBOX_ENV or WORKER_ENV,
#                    so nothing delivered them anywhere.
#
#                    Removed rather than wired, and the removal is the
#                    better half of the accident. Both are facts about a
#                    POD -- how long it may take to become ready, how
#                    long its remains are kept -- and the story-timings
#                    migration argues at length that these two are
#                    "destructive when they reach work that did not ask
#                    for them": 0.02 hours takes a failed run's
#                    workspace 72 seconds after it fails, and that run is
#                    "precisely the run whose workspace answers the
#                    question". A per-story setting reaching a per-pod
#                    clock is a shape to leave unbuilt.
#
# EVERY KNOB LEFT HERE HAS A READER, and a test asserts it. That is the
# rule this list broke and now keeps.
_KNOBS: dict[str, tuple] = {
    #                     production   compressed   where it is read
    "job_default_s":        (1800,        20,       "sandbox"),
    "job_max_s":            (7200,        180,      "sandbox"),
    "job_output_max":       (32 * 1024,   2048,     "sandbox"),
    "wait_slice_s":         (20,          5,        "sandbox"),
    "poll_interval_max_s":  (15,          2,        "worker"),
    "stall_ceiling":        (8,           3,        "harness"),
}

# `job_max_s` COMPRESSED IS 180, NOT 60, and the reason is a pinned
# relation rather than taste: `JOB_MAX_S > BASH_MAX_S` is asserted, and
# `BASH_MAX_S` is 120. Below it, `limits.bash_timeout` refuses a long
# foreground call with "use bash_background" while `bash_background`
# would have the shorter ceiling of the two -- a refusal that sends the
# caller somewhere worse.
#
# `wait_slice_s` PRODUCTION IS 20, NOT 120, and this is the same kind of
# pinned relation -- caught before it shipped rather than after.
# `bridge_timeout("bash_wait")` returns `limits.WAIT_BRIDGE_S`, which is
# 45, and that constant's comment says it "has to out-wait that slice or
# every wait comes back as a dead bridge". A 120 s slice under a 45 s
# HTTP read does not make the model wait longer: every slice times out,
# the worker reads a dead bridge, and its reachability check destroys a
# pod that was only waiting. 20 is what the sandbox has always used and
# is known good; `tools_jobs.wait_slice_s` clamps anything above the
# bridge regardless, so the two cannot drift apart again.

# The env names the pod carries. Only the sandbox-read knobs need these;
# the rest are read by processes that have a database.
SANDBOX_ENV = {
    "job_default_s": "AGENTOS_JOB_DEFAULT_S",
    "job_max_s": "AGENTOS_JOB_MAX_S",
    "job_output_max": "AGENTOS_JOB_OUTPUT_MAX",
    # THE MODEL'S `bash_wait` READS IT HERE, and it did not before. The
    # knob reached `jobs_client` -- the HARNESS job path -- while
    # `tools_jobs.WAIT_SLICE_S` was a frozen module constant, so the
    # model's wait was 20 s whatever the setting said. Both paths now
    # resolve the same variable at call time.
    "wait_slice_s": "AGENTOS_WAIT_SLICE_S",
}

# The two the WORKER reads. It has a database and could look them up,
# but `run_job`'s `wait_s` and poll interval are Python DEFAULT
# ARGUMENTS -- bound at function-definition time, so rebinding the
# module constant after a read changes nothing. Carrying them as env and
# resolving at call time is the same shape the sandbox already uses, and
# it avoids threading a connection into a low-level bridge client.
WORKER_ENV = {
    # `wait_slice_s` IS IN BOTH MAPS ON PURPOSE. The sandbox bounds its
    # own slice and `jobs_client` bounds the harness job path; they are
    # two readers of one number, and giving them two names would be two
    # numbers that can disagree.
    "wait_slice_s": "AGENTOS_WAIT_SLICE_S",
    "poll_interval_max_s": "AGENTOS_POLL_INTERVAL_MAX_S",
    # THE STALL CLOCK, which is the knob a compressed run most needs.
    # A polling regression is exactly what these tools exist to prevent,
    # and at a ceiling of 3 it ends a test leg in seconds instead of
    # hiding until an hour-long suite in production.
    "stall_ceiling": "AGENTOS_STALL_CEILING",
}

PROFILES = (PRODUCTION, COMPRESSED)


def knob_names() -> tuple[str, ...]:
    return tuple(_KNOBS)


def values(profile: str | None, overrides: dict | None = None) -> dict:
    """Every knob's value under this profile.

    PRODUCTION IS THE DEFAULT AND THE FALLBACK. An unknown profile, a
    missing stamp and an expired row all land here -- the direction the
    verification switch chose, because a settings fault must not change
    fleet behaviour at the moment it can least explain itself.
    """
    idx = 1 if profile == COMPRESSED else 0
    out = {name: spec[idx] for name, spec in _KNOBS.items()}
    for name, raw in (overrides or {}).items():
        if name not in _KNOBS:
            # Named but unknown: ignored rather than raising. A typo in
            # an override must not take down the dispatch that reads it.
            continue
        try:
            out[name] = type(_KNOBS[name][0])(raw)
        except (TypeError, ValueError):
            continue
    return out


def sandbox_env(profile: str | None, overrides: dict | None = None) -> dict:
    """The subset the pod spec carries as container env, as strings."""
    resolved = values(profile, overrides)
    return {env: str(resolved[name]) for name, env in SANDBOX_ENV.items()}


def worker_env(profile: str | None, overrides: dict | None = None) -> dict:
    """The subset the worker container carries, as strings."""
    resolved = values(profile, overrides)
    return {env: str(resolved[name]) for name, env in WORKER_ENV.items()}


def is_compressed(profile: str | None) -> bool:
    return profile == COMPRESSED


def label(profile: str | None) -> str:
    """What a card, a brief or a progress answer says about this run.

    LOUD WHEN IT IS NOT PRODUCTION. The lesson inverted: a feature's
    absence once looked identical to it having nothing to do, so
    compressed timings must never look like production.
    """
    return "" if not is_compressed(profile) else (
        "timings: COMPRESSED — this run's budgets, poll intervals and "
        "reclaim clock are deliberately shortened for testing. Durations "
        "here do not mean what they would in production.")
