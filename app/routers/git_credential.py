"""Just-in-time git credential broker for story workspaces.

WHY THIS EXISTS (research/MULTI_REPO_STORIES_PLAN.md M5): today a GitHub
installation token is written into each clone's `.git/config` credential helper and
rewritten every ~50 minutes by a background loop. That means a live, push-capable
token sits at rest on the VM for the life of every story, and multi-repo makes it
worse — N clones means N helper installs and N refreshes.

Instead, git asks US for credentials at the moment it needs them. The workspace holds
no token at all; one is minted per git operation, handed to git over stdout, and never
persisted anywhere.

REQUEST SHAPE. This is called by a git credential helper installed in each clone (see
`shca.workspace.setup` in the gateway). Git's credential protocol feeds a helper
key=value lines on stdin, but parsing stdin inside an inline shell helper is fragile,
so the gateway instead bakes the identifying parameters into the URL — it can do that
because **the helper is installed per clone**, so it always knows which repo it is
for. We therefore need no stdin parsing here.

RESPONSE SHAPE. `text/plain` in git-credential format:

    username=x-access-token
    password=ghs_...

AUTH. `verify_internal_bearer`, which accepts the `OPENCLAW_GATEWAY_TOKEN` already
injected into every story workspace (the same token the existing assistant hooks use
against `/internal/assistant/*`). It does NOT depend on the per-agent JWT stack, which
is still inert. Note the shared `OPENCLAW_AUTH_TOKEN` is deliberately absent from the
workspace env (blocked by the gateway's host-env security policy), so the workspace
side must use the gateway token.

SCOPING is the security boundary: possession of the bearer is not enough. The
requested repo must be one this story actually holds, and a repo the story holds
READ-ONLY gets a read-only token. Two sources answer that, in order: the recorded
workspace layout, then the story's resource grants — a kernel assistant's workspace
records no layout because it clones nothing, so the grant is the only proof there
is, and it is the one the layout was derived from anyway. See
`_resolve_story_repo_scope`.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.database import get_pool
from app.github_tokens import generate_installation_token
from app.internal_auth import verify_internal_bearer
from app.services.story_repo_scope import granted_repo_scope
from app.services.story_workspace_layout import resolve_workspace_clones

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

# GitHub installation-token permission sets. A token's permissions apply uniformly to
# every repo it is scoped to, which is exactly why a read-only request is minted as a
# SEPARATE, single-repo token rather than being folded into the write token.
#
# NO `workflows: write`, AND THAT OMISSION IS THE FIX. Measured on
# execution 204: the token came back, git authenticated with it, and the
# push was refused
#
#     remote: Write access to repository not granted ... error: 403
#
# while `app/services/local_diff.py::mint_push_token` -- which asks for
# `contents:write, metadata:read` and nothing else -- had been creating
# branches and pushing against the same App, on the same repo, all week.
# The only difference between the path that worked and the path that did
# not was this key.
#
# `workflows` is needed ONLY to create or update files under
# `.github/workflows/`. Asking for it on every credential is a permission
# we almost never need, on a request that fails as a whole when the
# installation does not hold it -- least privilege and the measurement
# agree. A story that genuinely needs to edit a workflow should escalate
# for it explicitly, per story, rather than every push carrying the ask.
#
# `app/services/git_graph/github_ops.py::mint_repo_token` still takes
# `generate_installation_token`'s default, which DOES include workflows.
# Left alone deliberately: the engine merges server-side through the API
# and `git_graph/branches.py:197` notes `contents:write covers
# create-ref`, so it does not need it either -- but changing two things
# at once when only one is measured is how the next three rounds get
# spent. Revisit once the granted-permission logging lands.
_WRITE_PERMISSIONS = {"contents": "write", "metadata": "read"}
_READ_PERMISSIONS = {"contents": "read", "metadata": "read"}


async def _resolve_story_repo_scope(
    pool, story_id: str, requested_repo: str,
) -> tuple[list[str], dict, str]:
    """Decide which repos a credential request may cover, and at what permission.

    Returns (repo_names, permissions, reason) or raises HTTPException.

    Two cases:
      * A specific repo was requested (the normal path — the helper is per clone).
        It must be one of the story's provisioned repos. A read-only repo yields a
        read-only, single-repo token so a context clone can never push.
      * No repo requested (fallback). Scope to the story's WRITE repos with write
        permission — this is what a legacy single-repo helper would have had.
    """
    rows = await resolve_workspace_clones(pool, story_id)
    if not rows:
        # NO RECORDED LAYOUT. That used to mean exactly one thing — a legacy
        # workspace — and was refused. It now means two, and only one of them is
        # unprovable.
        #
        # `story_workspace_nodes` records what was cloned into the ASSISTANT's
        # gateway workspace. A kernel assistant clones nothing there on purpose
        # (`assistant_kind.WORKSPACE_DOCS`): it delegates, and the checkout that
        # exists is the EXECUTOR's, in its own sandbox. So provisioning writes no
        # rows and this branch is reached on the normal path, for a story whose
        # scope is perfectly provable — from the grant that authorised the mount
        # in the first place. Refusing it 404'd every clone the kernel assistant's
        # executor attempted.
        #
        # The grant is the boundary this endpoint exists to enforce; the
        # workspace-node table was only ever derived from it. So fall back to the
        # source rather than to a refusal.
        rows = await granted_repo_scope(pool, story_id)
    if not rows:
        # Neither a recorded layout NOR a repo grant. THIS is the unprovable case
        # the refusal was written for, and it stays refused: an empty scope means
        # we cannot show the caller should reach ANY repo, and minting a wide
        # token on an unprovable request is the exact failure this endpoint exists
        # to avoid.
        #
        # Nor does a legacy story need this endpoint. Credentials reach a legacy
        # workspace three other ways, none of which come through here: the gateway
        # embeds a git credential-helper in RESOURCES.md, and local execution mints its
        # own scoped tokens (`mint_repo_clone_token` / `mint_push_token`).
        raise HTTPException(
            status_code=404,
            detail=(
                f"story {story_id} has neither a recorded workspace layout nor a "
                f"repo grant, so no repo scope can be proven. Legacy workspaces do "
                f"not use the credential broker — they carry an embedded credential "
                f"helper, and local checkouts mint their own scoped tokens."
            ),
        )

    if requested_repo:
        wanted = requested_repo.strip().lower()
        # A REPO NAME CAN MATCH SEVERAL CLONES. A story writing two trunk
        # lanes of one repository has two rows under one name, so "first
        # match wins" hands back whichever the sort happened to put first --
        # and if a read-only row sorted first, a WRITE clone of the same
        # repository would be given read-only credentials and its push would
        # fail with a permission error naming the wrong cause. Write wins
        # whenever any matching clone is writable; the credential is scoped
        # to the repository either way, not to one lane of it.
        matched = [
            row for row in rows
            # Accept either the bare name or owner/name, since git's remote URL
            # gives the helper the full path and the gateway may pass either.
            if wanted in (
                (row["repo_name"] or "").lower(),
                f"{(row['repo_owner'] or '').lower()}/{(row['repo_name'] or '').lower()}",
            )
        ]
        if matched:
            name = matched[0]["repo_name"]
            if any(row["is_write"] for row in matched):
                return [name], _WRITE_PERMISSIONS, "write repo"
            return [name], _READ_PERMISSIONS, "read-only context repo"
        raise HTTPException(
            status_code=403,
            detail=(
                f"repo {requested_repo!r} is not provisioned for story {story_id}"
            ),
        )

    # ONE NAME PER REPOSITORY -- two lanes of one repo are two clones sharing
    # a name, and a token must not be scoped to the same repository twice.
    write_names = list(dict.fromkeys(
        r["repo_name"] for r in rows if r["is_write"]
    ))
    if not write_names:
        raise HTTPException(
            status_code=403,
            detail=f"story {story_id} has no write repos",
        )
    return write_names, _WRITE_PERMISSIONS, "all write repos (no repo specified)"


@router.api_route("/git-credential", methods=["GET", "POST"])
async def git_credential(request: Request) -> PlainTextResponse:
    """Mint a short-lived git credential for one story's repo.

    GET and POST both work: git credential helpers are usually written as a one-line
    `curl`, and allowing GET keeps that helper as simple as possible. Nothing here
    mutates state, so GET is semantically correct.
    """
    verify_internal_bearer(request)

    story_id = (request.query_params.get("story_id") or "").strip()
    requested_repo = (request.query_params.get("repo") or "").strip()
    if not story_id:
        raise HTTPException(status_code=400, detail="story_id is required")

    pool = get_pool()
    repo_names, permissions, reason = await _resolve_story_repo_scope(
        pool, story_id, requested_repo,
    )

    try:
        result = await generate_installation_token(repo_names, permissions)
    except Exception as exc:
        _log.exception(
            "git_credential: token mint failed for story=%s repos=%s", story_id, repo_names,
        )
        raise HTTPException(
            status_code=502, detail=f"token mint failed: {exc}",
        ) from exc

    token = result.get("token") or ""
    if not token:
        _log.error(
            "git_credential: empty token returned for story=%s repos=%s", story_id, repo_names,
        )
        raise HTTPException(status_code=502, detail="empty token returned")

    _log.info(
        "git_credential: issued %s credential for story=%s repos=%s (%s)",
        "read-only" if permissions is _READ_PERMISSIONS else "write",
        story_id, ",".join(repo_names), reason,
    )
    # Git reads key=value lines and ignores keys it does not know. The token is never
    # written to disk here — it exists only in this response body and git's memory.
    return PlainTextResponse(
        f"username=x-access-token\npassword={token}\n",
        headers={"Cache-Control": "no-store"},
    )
