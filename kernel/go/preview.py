"""plan_preview — see what the engine WOULD do, before committing.

`derive_plan` is a pure function and the web tier has exposed a preview
of it all along; nothing surfaced it to any agent. This does, plus the
part that makes it a planning tool rather than a status page: an
optional list of HYPOTHETICAL stories, layered onto the real inputs, so
GO can ask "if I create these with these dependencies, what branches and
merges result?" — and fix the dependencies, not the engine, when the
answer surprises it.

THE ENGINE IS NOT REIMPLEMENTED HERE. The import is the control panel's
own `derive_plan` — one algorithm, one owner. In the standalone E0
server, where the control panel's code does not exist, this refuses with
a typed E_NO_ENGINE rather than pretending: a second implementation kept
"for the container" is how two answers to one question get born.
"""

from __future__ import annotations

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id


async def plan_preview(conn, goal_id: str, add: list | None = None) -> dict:
    try:
        from app.services.git_graph.plan import (StoryInput, derive_plan,
                                                 load_inputs)
    except ImportError:
        return Refused(
            "E_NO_ENGINE",
            "plan preview needs the control panel's git engine, which this "
            "standalone kernel server does not carry. On a deployment the "
            "kernel surface is mounted on the control panel and this works.")

    stories = list(await load_inputs(conn, goal_id))

    hypothetical = []
    for i, h in enumerate(add or []):
        rid = (h.get("resource_id") or "").strip()
        repo = None
        if rid:
            # By name or by id, like every other resource tool. The
            # kind filter stays here: resolving says WHICH resource,
            # and this tool additionally needs it to be a repo.
            resolved = await resolve_resource_id(conn, goal_id, rid)
            if isinstance(resolved, Refused):
                return Refused(
                    resolved.code,
                    f"hypothetical story {i}: {resolved.message}",
                    candidates=resolved.candidates)
            repo = await conn.fetchrow(
                """
                SELECT git_repo_id, repo_owner, repo_name, clone_url,
                       default_branch
                  FROM goal_resources
                 WHERE goal_id = $1 AND id = $2 AND kind = 'repo'
                """,
                goal_id, resolved)
            if repo is None:
                return Refused(
                    "E_NO_SUCH_RESOURCE",
                    f"hypothetical story {i} names resource {rid!r}, which "
                    f"is a resource of this goal but not a repo")
        sid = h.get("id") or f"draft-{i + 1}"
        hypothetical.append(sid)
        stories.append(StoryInput(
            story_id=sid,
            owner=h.get("assigned_to") or "",
            repo_id=repo["git_repo_id"] if repo else None,
            repo_owner=repo["repo_owner"] if repo else "",
            repo_name=repo["repo_name"] if repo else "",
            clone_url=repo["clone_url"] if repo else "",
            default_branch=(repo["default_branch"] if repo else "main"),
            depends_on=tuple(h.get("depends_on") or ()),
            state="proposed",
            story_kind="code",
            created_by="preview",
            title=h.get("title") or sid,
            land_together=bool(h.get("land_together")),
        ))

    plan = derive_plan(goal_id, stories)
    out = plan.to_dict()
    out["hypothetical"] = hypothetical
    out["detail"] = (
        "what the engine would derive. Nothing was created — if the shape "
        "surprises you, change the dependencies and preview again.")
    return out
