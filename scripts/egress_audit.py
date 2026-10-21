#!/usr/bin/env python3
"""Measure the egress allowlist against what installs actually reached.

A STANDING CHECK, NOT A ONE-OFF. A single measurement drifts the moment
the sandbox image or a `setups.REGISTRY` row moves, and the phase that
introduced this moves both. Run it after every change to either.

HOW TO PRODUCE THE LOG. Run the registry's installs on the current
sandbox image through the proxy, then read squid's access log off EVERY
replica -- one replica's log is half the traffic:

    kubectl -n agentos get pods -l app=egress-proxy -o name |
      while read -r p; do
        kubectl -n agentos exec "$p" -- cat /var/log/squid/access.log
      done > /tmp/squid-access.log

    python scripts/egress_audit.py /tmp/squid-access.log

Exit 1 means a host was reached that the allowlist does not carry --
the next story to need it fails in a way that names something else.
Entries nothing reached are reported and never fail.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.egress_audit import accesslog, allowlist, report  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("access_log",
                        help="squid access.log, concatenated across replicas")
    parser.add_argument("--manifest", default=allowlist.MANIFEST,
                        help="where the allowlist is declared")
    parser.add_argument("--acl", default="full_hosts",
                        help="which ACL to measure (full_hosts or store_hosts)")
    args = parser.parse_args(argv)

    allowed = allowlist.load(args.manifest).get(args.acl)
    if not allowed:
        print(f"no ACL named {args.acl!r} in {args.manifest}", file=sys.stderr)
        return 2

    with open(args.access_log, encoding="utf-8", errors="replace") as handle:
        reached = accesslog.parse(handle.read())

    result = report.compare(reached, allowed)
    print(report.render(result,
                        source=f"{args.manifest} [{args.acl}]"))
    return 1 if report.failed(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
