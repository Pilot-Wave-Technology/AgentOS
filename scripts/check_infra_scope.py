#!/usr/bin/env python3
"""Refuse an infra build that would touch anything but its own environment.

WHY THIS RUNS FIRST AND FAILS THE BUILD.

`cloudbuild-infra.yaml` creates databases, service accounts, IAM bindings
and Cloud Run services. Every one of those commands takes a NAME, and
every name comes from a substitution. A substitution is one edit away
from saying `prod`.

The commands are individually harmless-looking. `gcloud sql databases
create` is a create. `gcloud run services update --image` is an update.
Pointed at the wrong name they are, respectively, a new database nobody
asked for and PRODUCTION SERVING A DIFFERENT BUILD. Nothing in gcloud
will ask whether you meant it.

So the names are checked against the environment before the first one is
used, and the build stops if any of them belongs to somewhere else. This
is cheap, it runs in seconds, and it is the difference between "an infra
pipeline" and "an infra pipeline that cannot reach production".

TWO RULES, and the second is the one that matters:

  every target must be scoped to this environment -- it contains the
  env name, as a word.

  no target may be scoped to a DIFFERENT known environment. This is not
  implied by the first: `control-panel-prod-staging` contains "next" and is
  obviously wrong, and a check that only looked for the right word would
  pass it.

Usage:
    python3 scripts/check_infra_scope.py --env next TARGET [TARGET ...]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# Environments this pipeline is ALLOWED to build. Deliberately not
# "anything the caller passes": the whole point is that a typo in the
# trigger cannot aim this at production.
#
# IT CANNOT BE A FIXED LIST EITHER. These three are OUR environment
# names. An installation that is not ours has its own -- and with the
# list hardcoded, its very first infra build died here, on step one,
# telling the operator their own environment was not buildable. That is
# the guard refusing the only thing it was ever pointed at.
#
# So the allowed set is declared, in AGENTOS_BUILDABLE_ENVS, rather than
# assumed. The teeth are unchanged: a typo in a trigger substitution
# still cannot aim the build anywhere, because the typo would have to
# appear in BOTH the substitution and the declared set -- and `prod`
# only becomes buildable if somebody writes `prod` there on purpose.
_DEFAULT_BUILDABLE = ("next", "dev", "feature")


def _env_tuple(var: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(var, "").replace(",", " ").split()
    return tuple(raw) if raw else fallback


BUILDABLE = _env_tuple("AGENTOS_BUILDABLE_ENVS", _DEFAULT_BUILDABLE)

# Every environment that exists, so a target scoped to one of the others
# can be recognised and refused. The buildable set is folded in: an
# environment you are allowed to build must be one the second rule can
# recognise, or a target scoped to it reads as unscoped.
KNOWN_ENVS = tuple(dict.fromkeys(
    ("prod", "dev", "feature", "next", "acp") + BUILDABLE))

# Names that are shared across environments on purpose. They are read,
# never created or modified, and they carry no env scope -- so the
# scoping rule below would reject them for the wrong reason.
SHARED = {
    "firebase-service-account",
    "github-app-key",
    "github-webhook-secret",
    # The Cloud SQL INSTANCE is shared; the databases on it are not. The
    # instance is only ever an argument to `--instance`, and creating a
    # database on it does not modify it.
    "control-panel-db",
}


def _word(name: str, env: str) -> bool:
    """Is `env` present as a word, not as a substring of another word?

    ANY non-alphanumeric is a boundary, not just `-` and `_`. The first
    version listed the two separators used in resource names and was
    immediately wrong about a service account: the email
    `control-panel-staging@${GCP_PROJECT_ID}...` ends the word with `@`,
    so the guard refused its own build's target as "not scoped to next".
    A dot does the same in a bucket or a hostname.

    `nextdoor` still does not match, because `d` is alphanumeric — which
    is the distinction that actually matters here and the one the
    separator list was a clumsy proxy for.
    """
    return re.search(rf"(?:^|[^a-z0-9]){re.escape(env)}(?:$|[^a-z0-9])",
                     name.lower()) is not None


def offences(env: str, targets: list[str]) -> list[str]:
    out = []
    if env not in BUILDABLE:
        out.append(
            f"env={env!r} is not one this pipeline may build "
            f"({', '.join(BUILDABLE)}). If {env!r} is one of your own "
            f"environments, declare it: set AGENTOS_BUILDABLE_ENVS to a "
            f"space- or comma-separated list that includes it. If it is "
            f"not, this build is aimed at somebody else's infrastructure "
            f"-- fix the substitution, not the variable.")
        # Everything below would be measured against a bad env, so stop.
        return out

    for target in targets:
        if not target or target in SHARED:
            continue

        strays = [e for e in KNOWN_ENVS if e != env and _word(target, e)]
        if strays:
            out.append(
                f"{target!r} is scoped to {', '.join(strays)} — this build "
                f"is for {env}. A create here is somebody else's resource; "
                f"an update here is somebody else's outage.")
            continue

        if not _word(target, env):
            out.append(
                f"{target!r} is not scoped to {env}. Every resource this "
                f"pipeline touches must name its environment, so that a "
                f"wrong substitution reads as wrong rather than as "
                f"plausible.")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("targets", nargs="+")
    args = ap.parse_args(argv[1:])

    bad = offences(args.env, args.targets)
    checked = [t for t in args.targets if t]
    print(f"check_infra_scope: env={args.env} "
          f"targets={len(checked)}")

    if not bad:
        for target in checked:
            print(f"  ok  {target}")
        print("check_infra_scope: OK — every target belongs to this "
              "environment")
        return 0

    print("", file=sys.stderr)
    print("check_infra_scope: REFUSED — this build would touch resources "
          "outside its environment:", file=sys.stderr)
    for line in bad:
        print(f"  {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Nothing has been created or changed. Fix the substitutions in "
          "cloudbuild-infra.yaml.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
