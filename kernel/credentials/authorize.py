"""Ec4d — which cloud identity, if any, this execution may be given.

THE BOUNDARY IS THE GRANT, and this module is where that is decided.

It matters more than it looks. The sandbox joins the WORKER's network
namespace (`network_mode=container:<worker>`, see
`kernel/conductor/sandbox_spec.py`), so anything the worker can reach on
the network, model-authored bash can reach too -- including a minting
sidecar. Network placement therefore cannot be the boundary: the sidecar
is reachable by design, and the only thing standing between a run and a
cloud identity is this function saying yes.

So the rule is not "can it ask" but "was it granted". A run reaches a
scope because a person granted that scope's environment and one of its
identities to this story, with a `why` on the grant. Nothing else opens
the door: not a config file, not an env var, not the runtime identity of
the host.

EXACTLY ONE IDENTITY PER EXECUTION, and that is deliberate. Two granted
identities means every command in the run has two possible identities
and no stated rule for which one it uses -- the tooling reads one
credential file, and "which account did this run as" becomes
unanswerable after the fact, which is the first question asked when
something goes wrong. A story that genuinely needs two scopes is two
stories. Refused at the start, where it is cheap.

CLOUD-NEUTRAL BY TABLE, not by if-statement. `clouds.CLOUDS` says which
kinds name a scope, which name an identity, and whether the identity's
own name encodes its scope. GCP and AWS encode it (a service-account
email carries its project, a role ARN carries its account), so the
binding check is derivable and cannot be lied to. Azure does not, and
this module says so rather than pretending to check.
"""
from __future__ import annotations

import logging

from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                       ALL_IDENTITY_KINDS, cloud_of_kind)

logger = logging.getLogger(__name__)


class NotAuthorised(Exception):
    """No identity for this execution, and why. Never a silent None."""

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(f"[{code}] {message}")


async def granted_identity(conn, execution_id: int) -> dict | None:
    """The cloud identity this execution is entitled to, or None.

    Returns `{"cloud", "account", "scope", "region", "resource_id",
    "kind", "environment_id", "relation"}` when exactly one identity is
    granted to the story, its environment is granted too, and the two
    agree about the scope.

    None when no identity is granted -- the ordinary case, and not an
    error: almost no run needs cloud reach.

    Raises `NotAuthorised` when a grant exists but cannot be honoured.
    Falling back to "no credential" there would turn a misconfiguration
    into a run that mysteriously cannot see its own project.
    """
    row = await conn.fetchrow(
        "SELECT goal_id, story_id FROM executions WHERE id = $1",
        execution_id)
    if row is None or not row["story_id"]:
        return None

    held = await conn.fetch(
        """
        SELECT gr.id, gr.kind, gr.name, gr.metadata, sr.relation
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1 AND gr.goal_id = $2
           AND gr.kind = ANY($3::text[])
        """, row["story_id"], row["goal_id"],
        list(ALL_IDENTITY_KINDS) + list(ALL_ENVIRONMENT_KINDS))
    if not held:
        return None

    identities = [r for r in held if r["kind"] in ALL_IDENTITY_KINDS]
    if not identities:
        # An environment without an identity is a scope and nothing more.
        # Holding it must not mint anything, or "where" and "may act
        # there" collapse into one grant.
        return None
    if len(identities) > 1:
        raise NotAuthorised(
            "E_TWO_IDENTITIES",
            f"story {row['story_id']} is granted {len(identities)} cloud "
            f"identities ({', '.join(sorted(r['id'] for r in identities))}). "
            f"A run has one identity: with two, every command has two and "
            f"the record cannot say which it used. Split the work into two "
            f"stories, each granted one.")

    identity = identities[0]
    cloud = cloud_of_kind(identity["kind"])
    if cloud is None:                       # unreachable via the query
        raise NotAuthorised(
            "E_UNKNOWN_CLOUD",
            f"{identity['kind']!r} belongs to no cloud this kernel knows")

    meta = _as_dict(identity["metadata"])
    account = str(meta.get(cloud.account_field) or "").strip()
    if not account:
        raise NotAuthorised(
            "E_NO_ACCOUNT",
            f"{identity['id']!r} is a {cloud.name} {cloud.identity_noun} "
            f"and carries no {cloud.account_field!r}, so there is nothing "
            f"to act as.")

    # Only environments of THIS cloud can scope it. A GCP project does
    # not bound an AWS role.
    environments = [r for r in held if r["kind"] == cloud.environment_kind]
    if not environments:
        raise NotAuthorised(
            "E_NO_ENVIRONMENT",
            f"{identity['id']!r} names a {cloud.name} {cloud.identity_noun} "
            f"and the story does not hold a {cloud.environment_kind}. The "
            f"environment is what says which {cloud.scope_noun} this run "
            f"acts in; grant it alongside the identity.")

    derived = (cloud.scope_from_account(account)
               if cloud.scope_from_account else None)
    if cloud.scope_from_account and derived is None:
        raise NotAuthorised(
            "E_BAD_ACCOUNT",
            f"{identity['id']!r} has {cloud.account_field} {account!r}, "
            f"which is not a {cloud.name} {cloud.identity_noun} "
            f"identifier.")

    for environment in environments:
        scope = str(_as_dict(environment["metadata"])
                    .get(cloud.scope_field) or "").strip()
        if not scope:
            continue
        # THE BINDING, CHECKED WHERE IT CAN BE. For GCP and AWS the
        # identity's own name carries its scope, so this cannot be lied
        # to. For Azure `scope_from_account` is None and the pairing is
        # taken from the registration -- stated in clouds.py rather than
        # silently skipped here.
        agrees = (derived.lower() == scope.lower()
                  if derived is not None else True)
        if agrees:
            return {
                "cloud": cloud.name,
                "account": account,
                "scope": scope,
                "region": str(_as_dict(environment["metadata"])
                              .get("target_region")
                              or _as_dict(environment["metadata"])
                              .get("region") or "").strip(),
                "resource_id": identity["id"],
                "kind": identity["kind"],
                "environment_id": environment["id"],
                "relation": identity["relation"],
                # Whether the pairing above was PROVEN or taken on trust.
                # A caller that wants to refuse unverifiable pairings can,
                # and one that does not is at least not misled.
                "scope_verified": derived is not None,
            }

    named = ", ".join(sorted(
        str(_as_dict(e["metadata"]).get(cloud.scope_field))
        for e in environments))
    raise NotAuthorised(
        "E_SCOPE_MISMATCH",
        f"{identity['id']!r} belongs to {cloud.scope_noun} {derived!r} and "
        f"the environment(s) this story holds name {named}. A run may not "
        f"borrow one {cloud.scope_noun}'s {cloud.identity_noun} to act in "
        f"another.")


def _as_dict(value) -> dict:
    """`metadata` is jsonb, and asyncpg hands it back as TEXT.

    Hit four times in this codebase already, and it fails CLOSED here --
    an unparseable metadata blob means no identity, which is the safe
    direction for a credential decision.
    """
    import json

    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("credential authorize: unreadable metadata")
        return {}
    return out if isinstance(out, dict) else {}
