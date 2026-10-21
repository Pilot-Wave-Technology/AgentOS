"""What squid actually saw, from its access log.

THE NATIVE FORMAT, not a configured one. squid's default `squid` log
format is nine space-separated fields:

    time elapsed client code/status bytes method URL rfc931 peerstatus

and the host is inside the URL -- `CONNECT host:443` for tunnelled TLS,
a full URL for plain HTTP. Both shapes appear, because a proxy that
handles CONNECT also handles the occasional http:// fetch.

DENIALS ARE KEPT, NOT DROPPED, and they are the more useful half. A
TCP_DENIED on a host nobody allowlisted is the failure a story is about
to report as something else -- "an authentication failure rather than a
blocked host" -- and it is visible here before anybody hits it.
"""

from __future__ import annotations

# What squid writes when it refused. There are several; matching on the
# substring rather than an exact code keeps `TCP_DENIED/403` and
# `TCP_TUNNEL_ABORTED` from needing a table that goes stale.
_DENIED = ("DENIED", "DENIED_REPLY")


def _host_of(target: str) -> str:
    """The host in a log line's URL field, CONNECT or otherwise."""
    target = (target or "").strip()
    if not target:
        return ""
    if "://" in target:
        target = target.split("://", 1)[1]
    target = target.split("/", 1)[0]
    # user@host, then host:port. IPv6 in brackets is left alone: an
    # address is not a name and cannot match a dstdomain rule anyway.
    target = target.rsplit("@", 1)[-1]
    if target.startswith("["):
        return target
    return target.rsplit(":", 1)[0] if ":" in target else target


def parse(text: str) -> dict:
    """`{host: {"allowed": n, "denied": n}}` over one log."""
    seen: dict = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 7:
            continue
        code, target = fields[3], fields[6]
        host = _host_of(target)
        if not host:
            continue
        row = seen.setdefault(host, {"allowed": 0, "denied": 0})
        key = "denied" if any(d in code for d in _DENIED) else "allowed"
        row[key] += 1
    return seen
