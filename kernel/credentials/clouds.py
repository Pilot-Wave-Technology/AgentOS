"""Ec4d — what a cloud identity looks like, per cloud.

ONE SHAPE, THREE CLOUDS. Every cloud here has the same two-part grant: a
SCOPE (where the work happens -- a project, an account, a subscription)
and an IDENTITY within it (a service account, a role, a managed
identity). A run is granted both, and reaches exactly that scope.

  cloud   scope                      identity              scope encoded
                                                           in identity?
  gcp     project        target_project_id   service acct  sa_email       YES
  aws     account        account_id          role          role_arn       YES
  azure   subscription   subscription_id     managed ident client_id      NO

THAT LAST COLUMN IS THE INTERESTING ONE, and it is why this is a table
rather than an if-statement.

For GCP and AWS the identity's own name CONTAINS the scope -- a service
account is `x@<project>.iam.gserviceaccount.com`, a role ARN is
`arn:aws:iam::<account>:role/x`. So "does this identity belong to the
scope it was granted with" is DERIVABLE, and the check is free and
cannot be lied to.

An Azure client id encodes nothing. Two managed identities in two
different subscriptions look identical. So for Azure the same question
cannot be answered from the values alone: it has to be asserted at
registration and, if it matters, verified against the API. Pretending
otherwise -- checking a binding that cannot be checked -- would be worse
than saying so, because it would read as enforcement.

WHAT IS DELIBERATELY NOT HERE: how to mint, what the lease file looks
like, which environment variables point at it. Those are the provider's,
because they differ per cloud in ways no table flattens -- GCP hands back
one token, AWS hands back three values, Azure hands back a federated
token that is exchanged again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Cloud:
    """How one cloud's grants are shaped."""

    name: str
    # The resource kind naming WHERE work happens.
    environment_kind: str
    # The resource kinds naming WHO the run acts as.
    identity_kinds: tuple
    # The metadata field on the identity resource holding its name.
    account_field: str
    # The metadata field on the environment resource holding the scope.
    scope_field: str
    # Extracts the scope from an identity's own name, or None when the
    # cloud does not encode it. None is a statement, not an omission:
    # see the module docstring.
    scope_from_account: object = None
    # What a person calls these, for refusal messages that read like the
    # cloud's own documentation rather than like this table.
    scope_noun: str = "scope"
    identity_noun: str = "identity"


# `<name>@<project>.iam.gserviceaccount.com`
_GCP_SA = re.compile(
    r"^[a-z0-9][a-z0-9-]{4,29}@(?P<scope>[a-z][a-z0-9-]{4,28}[a-z0-9])"
    r"\.iam\.gserviceaccount\.com$")

# `arn:aws:iam::<account-id>:role/<path/><name>` -- account id is 12 digits.
_AWS_ROLE = re.compile(
    r"^arn:aws[a-z-]*:iam::(?P<scope>\d{12}):role/[\w+=,.@/-]{1,512}$")


def _gcp_scope(account: str) -> str | None:
    m = _GCP_SA.match((account or "").strip().lower())
    return m.group("scope") if m else None


def _aws_scope(account: str) -> str | None:
    # ARNs are case-sensitive in the resource part; only the prefix is
    # lowercased for matching, and the account id is digits either way.
    m = _AWS_ROLE.match((account or "").strip())
    return m.group("scope") if m else None


CLOUDS = {
    "gcp": Cloud(
        name="gcp",
        environment_kind="gcp_environment",
        identity_kinds=("gcp_test_sa", "gcp_deploy_sa"),
        account_field="sa_email",
        scope_field="target_project_id",
        scope_from_account=_gcp_scope,
        scope_noun="project",
        identity_noun="service account",
    ),
    "aws": Cloud(
        name="aws",
        environment_kind="aws_account",
        identity_kinds=("aws_test_role", "aws_deploy_role"),
        account_field="role_arn",
        scope_field="account_id",
        scope_from_account=_aws_scope,
        scope_noun="account",
        identity_noun="role",
    ),
    "azure": Cloud(
        name="azure",
        environment_kind="azure_subscription",
        identity_kinds=("azure_test_identity", "azure_deploy_identity"),
        account_field="client_id",
        scope_field="subscription_id",
        # A client id encodes no subscription. Stated, not forgotten.
        scope_from_account=None,
        scope_noun="subscription",
        identity_noun="managed identity",
    ),
}

# Every kind this module knows, so a caller can ask the database for all
# of them in one query rather than three.
ALL_ENVIRONMENT_KINDS = tuple(c.environment_kind for c in CLOUDS.values())
ALL_IDENTITY_KINDS = tuple(
    k for c in CLOUDS.values() for k in c.identity_kinds)


def cloud_of_kind(kind: str) -> Cloud | None:
    """Which cloud a resource kind belongs to, or None."""
    for cloud in CLOUDS.values():
        if kind == cloud.environment_kind or kind in cloud.identity_kinds:
            return cloud
    return None
