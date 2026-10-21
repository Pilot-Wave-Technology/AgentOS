"""Reading and writing the per-story timings profile, for the panel.

WHY THE PANEL AND NOT A CONSTANT. Proving that a two-hour tool does not
stall a leg, does not burn tokens and reports honestly at its budget
means either running something for two hours or lying to the clocks.
`kernel/timings.py` makes the second option first-class; this makes it
reachable by a person, which is the difference between an audited
setting and a developer editing constants and forgetting.

EVERY WRITE CARRIES AN EXPIRY, and the form cannot offer "never". A
compressed profile that outlives the session it was set for makes real
runs fragile in ways nobody connects to a setting flipped on Tuesday --
so the TTL is a required field with a short default, the table's column
is `NOT NULL`, and `for_story` filters on it.

VALIDATION IS AGAINST `knob_names()`, the same shape `ALLOWED_ENV_KEYS`
uses to bound what a setup may export. An unknown knob is dropped and
reported to the person who typed it, never raised at dispatch: a typo in
a settings form must not be a failed run an hour later.

AND A FEW KNOBS HAVE A CEILING THAT IS NOT TASTE. `wait_slice_s` above
`limits.WAIT_BRIDGE_S` does not make the model wait longer -- it makes
every slice time out as a dead bridge, and the worker's reachability
check destroys a pod that was only waiting. The sandbox clamps it
anyway, so this is the second of two guards; it exists because a form
that silently accepts a number the runtime will ignore is a form that
teaches people the setting does not work.

The panel connection is not schema-qualified -- `kernel.` is written out
in every statement here. Execution 246 answered 500 three times on
`relation "executions" does not exist` for exactly this.
"""

from __future__ import annotations

import json
import logging

# `kernel.bridge.limits` BY PACKAGE PATH, not the bare `import limits`
# the sandbox uses. That flat form is right inside the sandbox, where
# the bridge directory IS the path; from here it would put a very
# generic name on the panel's own import path.
from kernel import timings
from kernel.bridge import limits as _limits

logger = logging.getLogger(__name__)

# Knobs whose upper bound is a RELATION to something else, not a
# preference. `{knob: (ceiling, why)}` -- the reason travels with the
# number so the complaint can say it.
CEILINGS = {
    # `bridge_timeout("bash_wait")` is WAIT_BRIDGE_S and it "has to
    # out-wait that slice or every wait comes back as a dead bridge".
    # Five seconds of margin for the answer to travel, matching the
    # sandbox's own clamp.
    "wait_slice_s": (
        float(_limits.WAIT_BRIDGE_S) - 5.0,
        "a slice longer than the bridge read makes every wait time out "
        "as a dead bridge"),
    # `JOB_MAX_S > BASH_MAX_S` is asserted: below the foreground cap,
    # `bash` refuses a long call with "use bash_background" while
    # bash_background has the shorter ceiling of the two.
    "job_max_s": (float(_limits.JOB_MAX_S),
                  "the job ceiling is the longest a criterion may declare"),
}

# What the form offers, and the reason the list is short. An hour is a
# test session; a day is the outside of one. Anything longer is a
# request to make the setting permanent, which it must not be.
TTL_CHOICES = (1, 4, 12, 24)
TTL_DEFAULT = 4
TTL_MAX_HOURS = 72


def knob_table() -> list[dict]:
    """Every knob and both its values, for the page to render.

    Read from `kernel.timings` rather than restated here: a second copy
    of these numbers in a template is how a page comes to describe a
    profile the fleet stopped using.
    """
    production = timings.values(timings.PRODUCTION)
    compressed = timings.values(timings.COMPRESSED)
    return [{"name": name,
             "production": production[name],
             "compressed": compressed[name]}
            for name in timings.knob_names()]


def clean_overrides(raw: str) -> tuple[dict, list[str]]:
    """`(overrides, complaints)` from whatever the form field held.

    Never raises. A malformed field is a complaint the person reads
    immediately, not a dispatch that fails an hour later.
    """
    text = (raw or "").strip()
    if not text:
        return {}, []
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return {}, [f"overrides ignored -- not valid JSON ({exc})"]
    if not isinstance(parsed, dict):
        return {}, ["overrides ignored -- expected an object of knob: value"]

    known = set(timings.knob_names())
    out, complaints = {}, []
    for name, value in parsed.items():
        if name not in known:
            complaints.append(f"{name!r} is not a knob; ignored")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            complaints.append(f"{name!r} must be a number; ignored")
            continue
        if value <= 0:
            complaints.append(f"{name!r} must be positive; ignored")
            continue
        ceiling = CEILINGS.get(name)
        if ceiling is not None and value > ceiling[0]:
            complaints.append(
                f"{name!r} capped at {ceiling[0]:g} -- {ceiling[1]}")
            value = ceiling[0]
        out[name] = value
    return out, complaints


async def live_rows(conn) -> list[dict]:
    """Every story with a profile set, expired ones last.

    Expired rows are shown rather than hidden: "why is this story slow
    again" is answered by seeing that the setting lapsed at 14:02.
    """
    rows = await conn.fetch(
        """
        SELECT t.story_id, t.profile, t.expires_at, t.overrides, t.set_by,
               t.reason, t.updated_at, (t.expires_at > now()) AS live,
               s.title
          FROM kernel.story_timings t
          LEFT JOIN stories s ON s.id = t.story_id
         ORDER BY (t.expires_at > now()) DESC, t.updated_at DESC
         LIMIT 100
        """)
    out = []
    for row in rows:
        raw = row["overrides"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        out.append({**dict(row), "overrides": raw if isinstance(raw, dict)
                    else {}})
    return out


async def set_profile(conn, story_id: str, profile: str, hours: float,
                      overrides: dict, set_by: str, reason: str) -> None:
    """Upsert one story's profile. The caller has already validated."""
    await conn.execute(
        """
        INSERT INTO kernel.story_timings
            (story_id, profile, expires_at, overrides, set_by, reason,
             updated_at)
        VALUES ($1, $2, now() + make_interval(secs => $3), $4::jsonb, $5,
                $6, now())
        ON CONFLICT (story_id) DO UPDATE SET
            profile    = EXCLUDED.profile,
            expires_at = EXCLUDED.expires_at,
            overrides  = EXCLUDED.overrides,
            set_by     = EXCLUDED.set_by,
            reason     = EXCLUDED.reason,
            updated_at = now()
        """,
        story_id, profile, float(hours) * 3600.0, json.dumps(overrides),
        set_by, reason)


async def clear_profile(conn, story_id: str) -> None:
    """Drop the row. Runs already dispatched keep the stamp they carry."""
    await conn.execute(
        "DELETE FROM kernel.story_timings WHERE story_id = $1", story_id)
