"""resource_brief — what a resource IS, for the price of a row read.

Division of labour: `code_state` says what changed, `resource_brief`
says what it is, `read_file` answers one targeted question. The brief is
maintained by the system when events happen (G4's scanner v2), so
reading it here costs bytes and no GitHub call — the gate asserts that.

Staleness is stamped, never hidden: `as_of` is the identity card's sha,
`landings_since` counts digest rows appended after it was written, and
`identity_stale` says a landing touched the card's own subject
(manifests, README). GO decides whether that matters; the tool's job is
to make sure it CAN.

`refresh: true` enqueues a rebuild — NOTIFY to the control panel's
router, which owns the scanner — and says so. It never blocks: a brief
that takes thirty seconds to answer is a brief nobody consults.
"""

from __future__ import annotations

import json

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id


async def resource_brief(conn, goal_id: str, resource_id: str,
                         refresh: bool = False) -> dict:
    # BY NAME TOO, and the catalogue named on a miss. GO asked for
    # 'multi_agent_dummy_repo' -- the name on the board and in the
    # brief -- and got "is not a resource of NCAL1 (candidates: [])"
    # while the row sat there under an id the control panel generated.
    resolved = await resolve_resource_id(conn, goal_id, resource_id)
    if isinstance(resolved, Refused):
        return resolved
    resource_id = resolved
    row = await conn.fetchrow(
        """
        SELECT id, kind, name, COALESCE(clone_url, uri) AS source,
               summary_stale, head_sha, repo_name
          FROM goal_resources WHERE goal_id = $1 AND id = $2
        """,
        goal_id, resource_id)
    if row is None:
        return Refused("E_NO_SUCH_RESOURCE",
                       f"{resource_id!r} is not a resource of {goal_id}")

    if refresh:
        await conn.execute(
            "UPDATE goal_resources SET summary_stale = true WHERE id = $1",
            resource_id)
        await conn.execute(
            "SELECT pg_notify('resource_refresh', $1)",
            json.dumps({"goal": goal_id, "resource": resource_id}))

    identity = await conn.fetchrow(
        """
        SELECT content, previous_scan_sha, scanned_at
          FROM resource_summaries
         WHERE resource_id = $1 AND summary_type = 'identity'
         ORDER BY scanned_at DESC LIMIT 1
        """,
        resource_id)
    digest = await conn.fetch(
        """
        SELECT content, scanned_at FROM resource_summaries
         WHERE resource_id = $1 AND summary_type = 'digest'
         ORDER BY scanned_at DESC LIMIT 15
        """,
        resource_id)
    # Only needed to explain an ABSENT card, so it is read only then --
    # a brief that has one should not pay for a question about lanes.
    lanes = []
    if not identity and (row["kind"] or "") == "repo":
        lanes = [r["branch_name"] for r in await conn.fetch(
            "SELECT branch_name FROM goal_resources "
            "WHERE goal_id = $1 AND kind = 'branch' AND role = 'trunk' "
            "  AND repo_name = $2 AND coalesce(branch_name, '') <> '' "
            "  AND coalesce(status, 'active') = 'active'",
            goal_id, row["repo_name"])]

    landings_since = 0
    if identity:
        landings_since = await conn.fetchval(
            """
            SELECT count(*) FROM resource_summaries
             WHERE resource_id = $1 AND summary_type = 'digest'
               AND scanned_at > $2
            """,
            resource_id, identity["scanned_at"])

    return {
        "resource": resource_id, "kind": row["kind"], "name": row["name"],
        "source": row["source"],
        "identity": identity["content"] if identity else None,
        # The honesty stamp. A brief with no identity card says so
        # rather than improvising one from the digest.
        "as_of": identity["previous_scan_sha"] if identity else None,
        "landings_since": landings_since,
        "identity_stale": bool(row["summary_stale"]),
        "activity": [d["content"] for d in digest],
        "refresh": ("queued — the card is rebuilt in the background; "
                    "read again later" if refresh else None),
        "detail": _no_card_detail(row, lanes) if not identity else None,
    }


def _no_card_detail(row, lanes) -> str:
    """What to say when there is no identity card.

    IT USED TO SAY "pass refresh: true", ALWAYS. For a repo row whose
    goal has declared trunk branches that advice is wrong twice over:
    the refresh is declined by design, and the card the caller wants
    exists already -- on the lanes. Repeating the generic remedy sends
    the GO round a loop with no terminating signal, which is how an
    agent burns a leg on a card that was never going to appear.
    """
    if (row["kind"] or "") == "repo" and lanes:
        named = ", ".join(sorted(lanes))
        return (f"a repo row carries no identity card: this goal works on "
                f"{named}, and each of those is its own resource with its "
                f"own card. Ask about the branch you mean. The activity "
                f"digest above is still real.")
    return ("no identity card yet — pass refresh: true to build one; "
            "the activity digest above is still real")
