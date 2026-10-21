"""Which resource did you mean — asked once, for every tool that asks.

FIVE SITES LOOKED A RESOURCE UP AND FOUR OF THEM COULD NOT BE ADDRESSED.
`resource_brief`, `grant_resource`, `plan_preview` and the errand
resource check all matched `id = $1` and nothing else. `read_resource`
matched an id, a name or an id prefix, and refused with the goal's
catalogue attached — and its own comment says why:

    The refusal has always said "resource_brief lists what is" while
    returning an empty `candidates`, which costs the caller another
    round trip to learn something this query already knows.

That fix was written once and never carried across, so the same goal
answered the same question five different ways.

WHY A NAME MUST WORK, not only an id. The catalogue is filled by the
control panel, which assigns opaque ids -- `0636e8a5-41b` for a repo
called `multi_agent_dummy_repo`. The NAME is what appears in the goal
brief, on the board, and in what the person actually said; the id is
something GO can only have if some earlier tool happened to hand it
over. `register_resource` says a resource needs "an id the assistant
can name", and the writer of these rows does not honour that -- so the
reader meets it halfway.

AMBIGUITY IS ASKED ABOUT, NOT GUESSED. Two resources can share a name;
`multi_agent_dummy_repo` and `multi_agent_dummy_repo:cal_kernel` differ
by three characters. Where the match is not unique this refuses and
names the ties, which is the judgement kernel/addressing.py makes about
stories and F11 makes about criteria: where the harness cannot know, it
says so.
"""

from __future__ import annotations

from kernel.assistant.context import Refused

# Ordered so an exact id still wins: an id is unambiguous, a name is
# only usually so, and prefix matching stays last because it is the
# loosest. Lifted verbatim from read_resource, which had it right.
_MATCH = """
    SELECT id, name, kind
      FROM goal_resources
     WHERE goal_id = $1
       AND (id = $2 OR lower(name) = lower($2) OR id LIKE $2 || '%')
     ORDER BY (id = $2) DESC, (lower(name) = lower($2)) DESC, created_at
"""

_CATALOGUE = """
    SELECT id, name, kind FROM goal_resources
     WHERE goal_id = $1 AND COALESCE(status, 'active') = 'active'
     ORDER BY kind, name
"""


def _describe(rows) -> list[str]:
    return [f"{r['name']} ({r['kind']}, id {r['id']})" for r in rows]


async def resolve_resource_id(conn, goal_id: str, ref: str,
                              code: str = "E_NO_SUCH_RESOURCE"):
    """The id of the resource `ref` names, or a Refused naming the field.

    Returns a plain string so each caller keeps its own SELECT and the
    columns it needs; only the question "which row did you mean" moves
    here. `code` is the caller's own refusal code -- read_resource says
    E_NO_RESOURCE and the rest say E_NO_SUCH_RESOURCE, and changing
    either would change what a model already reads.
    """
    ref = (ref or "").strip()
    if not ref:
        return Refused(code, f"no resource named for goal {goal_id}",
                       candidates=_describe(
                           await conn.fetch(_CATALOGUE, goal_id)))

    rows = await conn.fetch(_MATCH, goal_id, ref)
    if len(rows) == 1 or (rows and rows[0]["id"] == ref):
        return rows[0]["id"]
    # An exact, unique NAME beats several prefix hits -- the ORDER BY put
    # it first, but only a unique one may be taken without asking.
    exact = [r for r in rows if (r["name"] or "").lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]["id"]

    if rows:
        return Refused(
            code,
            f"{ref!r} matches {len(rows)} resources of {goal_id}; "
            f"name one exactly or give its id",
            candidates=_describe(rows))
    return Refused(
        code, f"{ref!r} is not a resource of {goal_id}",
        candidates=_describe(await conn.fetch(_CATALOGUE, goal_id)))
