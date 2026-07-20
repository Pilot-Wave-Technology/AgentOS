"""Shared helpers for Notion provisioning — URL parsing and SHCA lookup."""
import re


def _parse_notion_page_id(raw: str) -> str:
    """Extract a Notion page UUID from a URL or bare ID string."""
    raw = raw.strip().rstrip("/").split("?")[0].split("#")[0]
    segment = raw.split("/")[-1].lower()
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", segment):
        return segment
    m = re.search(r"([0-9a-f]{32})$", segment)
    if m:
        h = m.group(1)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return ""


async def _shca_for_member(pool, goal_id: str, actor_id) -> dict | None:
    row = await pool.fetchrow(
        """SELECT t.id AS thread_id, a.id AS agent_id
           FROM threads t
           JOIN agents a ON a.id = t.agent_id
           JOIN agent_contexts ac ON ac.agent_id = a.id
           WHERE ac.goal_id = $1
             AND a.type = 'SHCA'
             AND a.status = 'active'
             AND a.owner_actor_id = $2
             AND (t.status IS NULL OR t.status IN ('active', 'open'))
             AND t.task_id IS NULL
           LIMIT 1""",
        goal_id, actor_id,
    )
    return dict(row) if row else None
