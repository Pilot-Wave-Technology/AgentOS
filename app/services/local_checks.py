"""The checks a local cycle owes, and whether they passed at this commit.

THE PLATFORM RUNS THE CHECKS, NOT THE AGENT -- the same rule as the
cloud, taken for the same reason. An agent that both does the work and
reports whether the work is good is marking its own homework, and the
failure mode is not dishonesty but optimism: a suite it believes it ran.
The extension runs them and posts what happened; this records it and
answers the one question the submit gate asks.

"DID THE CHECKS PASS" IS A QUESTION ABOUT A COMMIT. A pass recorded
before the last three commits says nothing about what is being pushed,
so every result carries the head sha it was measured at and the gate
asks about the sha being submitted. This is the whole reason the answer
is stored rather than carried in the submit call: the extension could
simply assert it passed, and then the assertion is the evidence.

WHAT THE CYCLE OWES comes from the plan card -- the normalised checks
that were actually approved, not a re-parse of the plan's markdown. The
markdown is a rendering, and binding the gate to a rendering means a
change to the renderer silently changes what is required.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def record(pool, story_id: str, head_sha: str, results) -> int:
    """Store one report. Returns how many results were written.

    UPSERT, because re-running a check at the same commit REPLACES its
    result. A row per attempt would make "did it pass" a question about
    which row you read, and the honest answer is always the latest.
    """
    from app.services.local_cycles import current_cycle  # noqa: PLC0415

    cycle = await current_cycle(pool, story_id)
    written = 0
    for item in results or []:
        run = str((item or {}).get("run") or "").strip()
        if not run:
            continue
        await pool.execute(
            """
            INSERT INTO local_check_results
                   (story_id, cycle, mount, run, label, passed, head_sha, detail)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (story_id, cycle, mount, run, head_sha)
            DO UPDATE SET passed = EXCLUDED.passed,
                          label  = EXCLUDED.label,
                          detail = EXCLUDED.detail,
                          reported_at = now()
            """,
            story_id, cycle, str(item.get("mount") or ""), run,
            str(item.get("label") or "unlabelled"), bool(item.get("passed")),
            head_sha, str(item.get("detail") or "")[-4000:])
        written += 1
    _log.info("local checks story=%s cycle=%s sha=%s results=%d",
              story_id, cycle, head_sha[:12], written)
    return written


async def required(pool, story_id: str) -> list[dict]:
    """The checks this cycle's approved plan says define done."""
    import json  # noqa: PLC0415

    from app.services.local_report import latest_card  # noqa: PLC0415

    card = await latest_card(pool, story_id, "plan_review")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    out, seen = [], set()
    for c in (card or {}).get("checks") or []:
        run = (c or {}).get("run")
        if run and run not in seen:
            out.append({"run": run, "label": c.get("label") or "unlabelled"})
            seen.add(run)
    return out


async def outstanding(pool, story_id: str, head_sha: str) -> list[str]:
    """Which required checks have not passed at `head_sha`. Empty = clear.

    NOT RUN AND FAILED ARE THE SAME ANSWER HERE, deliberately: both mean
    "nothing says this commit is good". They read differently to a
    person, which is why the refusal names the checks rather than
    counting them.
    """
    want = await required(pool, story_id)
    if not want:
        return []
    from app.services.local_cycles import current_cycle  # noqa: PLC0415

    cycle = await current_cycle(pool, story_id)
    rows = await pool.fetch(
        "SELECT run FROM local_check_results "
        " WHERE story_id = $1 AND cycle = $2 AND head_sha = $3 AND passed",
        story_id, cycle, head_sha)
    passed = {r["run"] for r in rows}
    return [c["run"] for c in want if c["run"] not in passed]


async def refusal(pool, story_id: str, head_sha: str) -> dict | None:
    """The submit refusal, or None when the commit is clear to push.

    IT NAMES THEM. "Checks have not passed" leaves a person to guess
    which, and the usual answer -- a suite they did not know the
    integration plan had added -- is exactly the one they cannot guess.
    """
    missing = await outstanding(pool, story_id, head_sha)
    if not missing:
        return None
    return {
        "ok": False,
        "reason": "checks_not_passed",
        "error": ("these checks have not passed at the commit you are "
                  "submitting (" + (head_sha[:12] or "no sha") + "): "
                  + "; ".join(missing) + ". "
                  + await _where_they_did_pass(pool, story_id, head_sha)
                  + "Run them with run_check at this commit, then call "
                  "propose_completion again."),
        "checks": missing,
    }


async def _where_they_did_pass(pool, story_id: str, head_sha: str) -> str:
    """The commit the checks last passed at, when it is not this one.

    THE HALF OF THE REFUSAL c72e8263 WAS MISSING. "Not passed at d712ead"
    read as "your checks fail", when they had passed one commit earlier
    and the commit being submitted was one nobody ran them on. Naming the
    other sha says what actually happened: something committed after the
    checks, and that commit needs them again.
    """
    from app.services.local_cycles import current_cycle  # noqa: PLC0415

    last = await pool.fetchval(
        "SELECT head_sha FROM local_check_results "
        " WHERE story_id = $1 AND cycle = $2 AND passed AND head_sha <> $3 "
        " ORDER BY reported_at DESC LIMIT 1",
        story_id, await current_cycle(pool, story_id), head_sha)
    if not last:
        return ""
    return (f"They last passed at {str(last)[:12]}; every commit since then "
            "needs them again, including one made only to save files the "
            "checks rewrote. ")
