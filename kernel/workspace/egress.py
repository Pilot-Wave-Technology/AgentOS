"""Ec4a — which hosts a run may reach, derived from what it was granted.

THE FINDING THIS ANSWERS. `errand-ops/profile.toml` said *"No web tools: an
errand gets no egress unless someone adds it here deliberately"* and it was
false. The sandbox runs with `network_mode: container:{self_id}` — it
SHARES the worker's network namespace — so it has full egress plus reach to
the worker's localhost, and it has `bash`, so nothing about the tool
surface changes that.

`tools_web.fetch` refuses private, loopback and link-local addresses, which
stops credential theft from `169.254.169.254`. It permits every PUBLIC
host, which is a different question and the one that matters here:
`smtp.sendgrid.net` is a perfectly ordinary public host.

**This is why "run the job but don't send the email" had no answer.** Three
mechanisms should have caught it and all three miss. Egress is open. The
effect classifier is structurally blind to an effect one function call deep
inside `python main.py`. And `effect_policy` was never set. A six-stage
interceptor chain logs `proceed` at every stage and the email goes out.

THE HONEST PRIMITIVE IS DEFAULT-DENY WITH AN ALLOWLIST. Then "don't send
the email" becomes *"nobody granted `smtp.sendgrid.net`"* — enforced by
policy rather than by the model's discretion, and it generalises to every
errand in the catalogue instead of being one special case.

WHAT THIS MODULE IS AND IS NOT. It decides WHICH HOSTS, from the story's
own grants. It does not enforce at the network — that is the sandbox's
namespace, and changing it is a separate and much riskier change. Enforcing
here covers `web_fetch`; `bash` still reaches whatever the namespace does,
and that gap is named in `docs/refactor/progress/stage-ec.md` rather than
implied to be closed.

A POLICY THAT DENIES EVERYTHING BY DEFAULT MUST SAY SO WELL. The refusal
names the host, says grants are where reach comes from, and lists what IS
reachable — otherwise an executor reads it as a bug and retries.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts every run may reach whatever it was granted, because the run
# cannot function without them and none is a place to send data TO.
#
# DELIBERATELY TINY. Every entry here is a hole in default-deny, and the
# test for belonging is "no errand can work without it AND an exfiltration
# through it would be absurd". A package index fails the second test in
# principle — you can publish to PyPI — which is why `npm publish` and
# `twine upload` are irreversible and need a person regardless.
BASE_HOSTS = (
    "pypi.org", "files.pythonhosted.org",       # pip
    "registry.npmjs.org",                        # npm
    "deb.debian.org", "security.debian.org",     # apt
    "proxy.golang.org", "crates.io",             # go, rust
    "static.crates.io",
)


def _as_mapping(value):
    """A jsonb column, whichever way the driver handed it over.

    ASYNCPG RETURNS JSONB AS TEXT unless a codec is registered, and this
    project has been bitten before: a cost backfill selected 2,299 rows,
    rejected every one on an `isinstance(..., dict)` check, and reported
    "0 written" without an error. Accepting both shapes is one line; a
    silent empty result is a bug that reads as "nothing was granted".
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            got = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}
    return {}


def host_of(url_or_host: str) -> str:
    """The hostname, from a URL or a bare host. Lowercased, no port."""
    text = str(url_or_host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # A bare host, possibly with a port or a path.
        text = "//" + text
    host = (urlparse(text).hostname or "").strip().lower()
    return host.rstrip(".")


def _hosts_from_row(row) -> set[str]:
    """Every host one granted resource implies.

    A repo grant implies wherever it is cloned from — `github.com` for
    almost everything, and an enterprise host for the rest, which is why
    it is read off the row rather than assumed.
    """
    out = set()
    # `uri` ONLY, not gcs_uri. See the note in mounts.py: goal_resources
    # writes the same value into both columns, so gcs_uri here was a
    # second reading of the first, and it put a GCS-shaped column name
    # into a kernel that is meant to be cloud-free.
    for field in ("uri", "clone_url"):
        try:
            value = row[field]
        except (KeyError, TypeError):
            continue
        host = host_of(value)
        if host:
            out.add(host)
    # A cloud resource says which API hosts it needs, because the verb
    # set alone cannot: the same `logs.read` is a different endpoint on
    # every provider. Ec4c's `capability_grants` is where these come
    # from; a plain dict on the row works until then.
    try:
        metadata = _as_mapping(row["metadata"])
    except (KeyError, TypeError):
        metadata = {}
    for entry in metadata.get("hosts") or ():
        host = host_of(entry)
        if host:
            out.add(host)
    return out


async def allowed_hosts(conn, execution_id: int) -> frozenset:
    """Hosts this run may reach: its grants, plus the base set.

    Scoped to the STORY's grants rather than the goal's. A goal may own a
    production database that this story was never given, and reach that
    follows from ownership rather than from a grant is not a grant.
    """
    rows = await conn.fetch(
        """
        SELECT r.uri, r.clone_url, r.metadata
          FROM story_resources sr
          JOIN goal_resources r ON r.id = sr.resource_id
          JOIN kernel.executions e ON e.story_id = sr.story_id
         WHERE e.id = $1
        """, execution_id)
    out = set(BASE_HOSTS)
    for row in rows:
        out |= _hosts_from_row(row)
    return frozenset(out)


def matches(host: str, allowed) -> bool:
    """Is this host reachable under the allowlist?

    A grant on `github.com` covers `api.github.com` and
    `codeload.github.com` — one repository is not usable without them, and
    forcing three registrations to clone one repo is the friction that
    gets an allowlist replaced with `*`.

    It does NOT cover `github.com.evil.test`, which is the whole reason
    this is a suffix test on a DOT BOUNDARY rather than `endswith`.
    """
    host = host_of(host)
    if not host:
        return False
    for entry in allowed:
        entry = host_of(entry)
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def refusal(host: str, allowed) -> str:
    """What to tell an executor whose reach was refused.

    NAMES THE HOST AND THE ROUTE. "blocked" reads as a bug and gets
    retried; "nobody granted this, and grants are how reach happens" is
    something the executor can act on — by asking, or by using what it
    has.
    """
    reachable = ", ".join(sorted(h for h in allowed
                                 if h not in BASE_HOSTS)) or "nothing else"
    return (
        f"E_HOST_NOT_GRANTED: {host_of(host) or host!r} is not reachable "
        f"from this run. Reach follows GRANTS, not the network: a host is "
        f"available when the story holds a resource that names it. "
        f"Granted here: {reachable} (plus package registries). "
        f"If this errand genuinely needs it, ask_human for the resource to "
        f"be registered and granted — do not look for another route to it.")


# Hosts nothing should ever reach, whatever anybody granted. `matches`
# is an allowlist and this is the floor under it: a grant that named one
# of these by accident must not open it.
_NEVER = re.compile(
    r"^(metadata\.google\.internal|metadata|169\.254\.169\.254)$")


def is_never_allowed(host: str) -> bool:
    """The cloud metadata endpoint, by name as well as by address.

    `tools_web` already refuses link-local ADDRESSES, which covers
    `169.254.169.254`. It does not refuse `metadata.google.internal`
    before resolving it — and on a GCP VM that name resolves to exactly
    that address, so the check that matters is done twice, once on each
    side of DNS.
    """
    return bool(_NEVER.match(host_of(host)))
