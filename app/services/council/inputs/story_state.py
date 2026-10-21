"""What every story and errand was asked to do, and what it did.

READ FROM THE DATABASE, NEVER FROM A WORKSPACE. The stage this replaces
resolved a workspace through `shca_instance_id` and read `SCOPE.md` and
cycle plans out of it. A pod-executed story has no such workspace, so
those documents came back empty -- silently -- and the reviewer judged a
description against nothing. Workspaces are pod-local and reaped; these
columns outlive every substrate change.

COUNTS COME FROM SQL, NOT FROM THE MODEL COUNTING ROWS. `LEDGER.md` is
arithmetic done in Postgres. It is the only thing in the review that can
say "47 stories, 3 blocked" and be right, and it stays right whatever
happens to the document below it.

NOT A SUMMARY -- A RECONCILIATION. Asked, delivered and verdict sit side
by side and the stage is told to flag where they disagree, not to
adjudicate. A summary is exactly where an anomaly gets smoothed away, and
"a story did something wrong" is the thing that has to survive to
`goal_health`.

LIVE WORK FIRST. With a million-token window everything fits, so the
ordering is for attention rather than capacity: what is unfinished, or
finished badly, is what a reviewer should read while it is still fresh.
"""

from __future__ import annotations

from app.database import get_pool
from app.models.story import is_engine_git_story

# `result_summary` on real goals runs a few hundred characters (CAL05:
# median 528, max 646), so it goes in WHOLE. Clipping bought nothing and
# cost the tail of the executor's own account of its work.
_MAX_SUMMARY = 20000


async def ledger(goal_id: str) -> str:
    """`LEDGER.md` — the counts, computed in SQL."""
    pool = get_pool()
    by_state = await pool.fetch(
        "SELECT COALESCE(work_kind,'story') AS shape, state, count(*) AS n "
        "FROM stories WHERE goal_id = $1 "
        "GROUP BY 1, 2 ORDER BY 1, 2", goal_id)
    by_verdict = await pool.fetch(
        "SELECT COALESCE(verdict,'(none)') AS verdict, count(*) AS n "
        "FROM story_reviews WHERE goal_id = $1 GROUP BY 1 ORDER BY 2 DESC",
        goal_id)
    total = sum(r["n"] for r in by_state)

    out = ["# Ledger", "",
           f"**{total}** work item(s) in this goal. These numbers are "
           f"computed in SQL and are authoritative — if your reading of "
           f"the detail below disagrees with them, the numbers are right.",
           "", "## By shape and state", ""]
    out += [f"- {r['shape']} / {r['state']} — {r['n']}" for r in by_state] \
        or ["_(no stories)_"]
    out += ["", "## Review verdicts", ""]
    out += [f"- {r['verdict']} — {r['n']}" for r in by_verdict] \
        or ["_(nothing reviewed yet)_"]
    out.append("")
    return "\n".join(out)


async def _rows(goal_id: str):
    pool = get_pool()
    return await pool.fetch(
        """
        SELECT s.id, s.title, s.description, s.acceptance_criteria,
               s.state, COALESCE(s.work_kind, 'story') AS shape,
               s.result_summary, s.created_at, s.updated_at,
               -- The three columns `is_engine_git_story` reads. Named
               -- together because the version of that bug which mattered
               -- was a caller checking only `created_by`.
               s.created_by, s.story_kind, s.merge_group_id,
               r.verdict, r.reviewed_at,
               EXISTS (SELECT 1 FROM council_findings f
                        WHERE f.goal_id = s.goal_id AND f.story_id = s.id
                          AND f.status = 'open') AS has_open_finding
          FROM stories s
          LEFT JOIN story_reviews r
                 ON r.story_id = s.id AND r.reviewed_state = 'done'
         WHERE s.goal_id = $1
         ORDER BY
           -- Engine plumbing last: it is not goal work, and putting it
           -- first would spend the reviewer's attention on merges.
           (s.created_by = 'engine' OR s.story_kind = 'merge'
            OR s.merge_group_id IS NOT NULL),
           -- unfinished first, then finished-badly, then the rest
           (s.state NOT IN ('reviewed', 'cancelled')) DESC,
           (COALESCE(r.verdict, '') NOT IN ('approve', 'skipped_errand')) DESC,
           s.updated_at DESC NULLS LAST
        """, goal_id)


def _one(row) -> list[str]:
    shape = row["shape"]
    engine = is_engine_git_story(row)
    head = f"### {row['id']} — {row['title']}  [{row['state']}]"
    if engine:
        head += "  (engine git operation)"
    elif shape == "errand":
        head += "  (errand)"
    out = [head, ""]

    if engine:
        # FOUND BY THE FIRST LIVE RUN. The reviewer raised
        # `merge-unrecorded-and-unreviewed` against a merge story for
        # having no result summary, no artifacts and no verdict -- doing
        # exactly what the prompt asked. It was the input that was
        # wrong: these are mechanical git operations the ENGINE wrote,
        # excluded from review everywhere else in the system precisely
        # because reviewing them is meaningless, and they record none of
        # that paperwork by design. Presenting them like goal work
        # manufactures a finding every single cycle.
        out += ["**This is engine plumbing, not goal work.** A merge, "
                "diff or fix story the git engine authored. It is never "
                "reviewed, never records a result summary, and never "
                "emits artifacts — that is by design, not an omission. "
                "**Do not raise a finding about its missing paperwork.** "
                "Judge it only on whether it completed; one stuck in a "
                "non-terminal state for a long time IS worth reporting.",
                ""]

    if row["has_open_finding"]:
        out += ["> An open council finding names this item. It is here "
                "because of that, whatever its state.", ""]

    out += ["**Asked:**",
            (row["description"] or "").strip() or "_(no description)_"]
    ac = (row["acceptance_criteria"] or "").strip()
    if ac:
        out += ["", "**Acceptance criteria:**", ac]

    out += ["", "**Delivered:**"]
    delivered = (row["result_summary"] or "").strip()
    if delivered:
        out += ["```", delivered[:_MAX_SUMMARY], "```"]
    elif engine:
        out.append("_(engine plumbing records no result summary — expected)_")
    elif row["state"] in ("done", "reviewed"):
        out.append("_(this item finished but recorded nothing about what it "
                   "did — worth flagging)_")
    else:
        out.append("_(not finished)_")

    verdict = row["verdict"]
    if engine:
        out += ["", "**Verdict:** engine git operations are never reviewed "
                "— the absence of a verdict here is correct."]
    elif shape == "errand":
        out += ["", "**Verdict:** errands are not code-reviewed "
                f"(`{verdict or 'none recorded'}`) — judge it on what it "
                "emitted, not on a review."]
    elif verdict:
        out += ["", f"**Verdict:** `{verdict}`"]
        if verdict == "not_produced":
            out.append("_`not_produced` means NOBODY LOOKED at this work. "
                       "It is unreviewed, not approved._")
    elif row["state"] in ("done", "reviewed"):
        out += ["", "**Verdict:** _(none recorded — unreviewed)_"]
    out.append("")
    return out


async def story_state(goal_id: str) -> str:
    """`STORY_STATE.md` — asked / delivered / verdict, per item."""
    rows = await _rows(goal_id)
    if not rows:
        return "# Story state\n\n_(this goal has no stories or errands yet)_\n"

    return "\n".join([
        "# Story state", "",
        "Every story and errand in this goal. **Unfinished and "
        "finished-badly work is listed first.**", "",
        "`Delivered` is `stories.result_summary`, which is assembled by the "
        "harness rather than written as prose: it is a list of artifacts "
        "followed by the executor's completion claim, serialised with its "
        "JSON quoting intact. Read past the punctuation — the test counts "
        "and file names inside it are the substance, and comparing them "
        "against `Asked` is your job.", "",
        "A story's work lands on trunk. An errand's never does: what "
        "survives is what it emitted.", "",
        "---", "",
    ] + [line for r in rows for line in _one(r)])
