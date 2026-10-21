"""The diff, and an honest statement of what it did not look at.

THREE ANSWERS, and only one of them is a failure:

  REACHED AND NOT ALLOWED   an install needs a host the list does not
                            carry. The next story to need it fails in a
                            way that reads as something else, so this
                            exits non-zero.
  ALLOWED AND NEVER REACHED reach granted for no reason anybody can now
                            point at. Reported, never failed: an entry
                            added for a language nothing has used this
                            month is not a defect.
  REACHED AND ALLOWED       the list doing its job.

WHY THE BLIND SPOTS ARE PRINTED EVERY TIME. "The allowlist is measured"
would otherwise be read as "every way out of a pod is measured", and it
is not. A reader who believes the stronger claim stops looking at the
five places this cannot see.
"""

from __future__ import annotations

from app.services.egress_audit import allowlist

# WHAT THIS MEASUREMENT DOES NOT COVER. Each is a real way out of a pod
# that leaves no line in the log being read.
BLIND_SPOTS = (
    "DNS. The egress rules carry no `to:` clause at all, so name "
    "resolution is not constrained and never appears here.",
    "Cloud SQL on 3307. A separate path with its own policy; the proxy "
    "never sees it.",
    "Everything in NO_PROXY. By construction those connections do not "
    "go through squid, so they are not in its log.",
    "Tools that ignore *_PROXY. Default-deny drops them, so they are "
    "neither logged nor reported -- they HANG, which is the failure "
    "mode this whole area exists around.",
    "The second proxy replica. One replica's log is half the traffic; "
    "read both or the unused half is wrong.",
)


def compare(reached: dict, allowed: set) -> dict:
    """`{unknown, unused, used}` -- the three answers, as data."""
    unknown, used, covered = {}, {}, set()
    for host, counts in sorted(reached.items()):
        entry = allowlist.covers(allowed, host)
        if entry:
            covered.add(entry)
            used[host] = {**counts, "entry": entry}
        else:
            unknown[host] = dict(counts)
    return {"unknown": unknown, "used": used,
            "unused": sorted(allowed - covered)}


def render(result: dict, *, source: str = "") -> str:
    """The report a person reads, blind spots included."""
    lines = ["# Egress allowlist, measured"]
    if source:
        lines.append(f"against {source}")
    lines.append("")

    unknown = result["unknown"]
    lines.append(f"## Reached and NOT allowed -- {len(unknown)}")
    if not unknown:
        lines.append("(none)")
    for host, counts in unknown.items():
        # A DENIAL IS THE LOUDER CASE. It means squid already refused,
        # so something is failing right now for a reason that does not
        # name the host.
        state = "DENIED" if counts.get("denied") else "allowed by another rule"
        lines.append(f"- {host}  ({state}; "
                     f"{counts.get('allowed', 0)} ok / "
                     f"{counts.get('denied', 0)} denied)")

    unused = result["unused"]
    lines.append("")
    lines.append(f"## Allowed and never reached -- {len(unused)}")
    lines.append("Reported, not failed: an entry for a language nothing "
                 "used this month is not a defect.")
    for entry in unused:
        lines.append(f"- {entry}")

    lines.append("")
    lines.append(f"## Reached and allowed -- {len(result['used'])}")
    for host, counts in result["used"].items():
        lines.append(f"- {host} via {counts['entry']}")

    lines.append("")
    lines.append("## What this did not look at")
    for spot in BLIND_SPOTS:
        lines.append(f"- {spot}")
    return "\n".join(lines)


def failed(result: dict) -> bool:
    """Only an unknown host reached is a failure."""
    return bool(result["unknown"])
