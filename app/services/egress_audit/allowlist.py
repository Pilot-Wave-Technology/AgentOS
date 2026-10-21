"""The allowlist, as data, read from the manifest that deploys it.

ONE SOURCE. The list lives in `deploy/k8s/conductor.yaml` as squid ACL
lines, and a second copy anywhere -- in a test, in a report, in a
docstring -- is a copy that goes stale silently and then reports a
correct configuration as a defect.
"""

from __future__ import annotations

import re

MANIFEST = "deploy/k8s/conductor.yaml"

# `acl <name> dstdomain <domain> [<domain> ...]`, which is the only form
# the manifest uses. Anything else is not parsed rather than guessed at:
# a regex that quietly matched a `dstdom_regex` line would report hosts
# as allowed that squid matches differently.
_ACL = re.compile(r"^\s*acl\s+(\w+)\s+dstdomain\s+(.+?)\s*$")


def parse(text: str) -> dict:
    """`{acl name: {domain, ...}}` from squid configuration text."""
    out: dict = {}
    for line in text.splitlines():
        match = _ACL.match(line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        if rest.startswith("#"):
            continue
        out.setdefault(name, set()).update(
            token for token in rest.split() if not token.startswith("#"))
    return out


def load(path: str = MANIFEST) -> dict:
    with open(path, encoding="utf-8") as handle:
        return parse(handle.read())


def covers(allowed: set, host: str) -> str:
    """Which entry admits `host`, or "" if none does.

    squid's `dstdomain` matches a leading dot as "this domain and any
    subdomain", and a bare name as that name exactly. Both are
    implemented here rather than approximated, because approximating
    the permissive direction reports a real hole as covered.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    for entry in sorted(allowed, key=len, reverse=True):
        rule = entry.strip().lower()
        if rule.startswith("."):
            if host == rule[1:] or host.endswith(rule):
                return entry
        elif host == rule:
            return entry
    return ""
