"""Where the control panel's internal endpoints are.

THE DEFECT THIS CLOSES, which cost five dispatch attempts on `next`.
`GIT_CREDENTIAL_URL` had exactly one reader (`_credential_helper`) and
exactly one writer:

    infra/e0/docker-compose.yml:54

-- the LOCAL Docker file. On Kubernetes nothing set it, so the helper
returned "" and the clone ran unauthenticated. GitHub answered by asking
for a username, git had prompting disabled, and the pod died in
`workspace-compose` with

    fatal: could not read Username for 'https://github.com'

which reads as a terminal problem and is an unset-variable problem. "It
worked when we were testing locally" was exact: the only declaration of
the fact lived in the local environment's own file.

SO IT IS DERIVED, NOT DECLARED AGAIN. Adding the variable to
`conductor.yaml` would fix this deployment and leave the next one to
rediscover it -- two declarations of one fact, which is how it got here.
The control panel's address is already a deployment fact every container
that needs a broker has (`CONTROL_PANEL_URL`), and the paths under
`/internal` are OUR OWN, fixed by our own routing table. Same shape as
`resolveKernelUrl` in the gateway, built for the same bug a week ago.

AN EXPLICIT VARIABLE STILL WINS, because E0 points at
`host.docker.internal:8000` while its `CONTROL_PANEL_URL` may name
something else entirely, and that file is a working configuration -- not
one to migrate under a deployment that is mid-debug.

EMPTY IS A LEGITIMATE ANSWER, not an error. A workspace of public repos
and local corpus origins composes perfectly well with no broker at all,
which is what E0's corpus tests and the `none` binding do. The callers
that need one say so themselves, in their own words, with the mount in
hand -- a refusal here could not name the repo it was refusing for.
"""

from __future__ import annotations

import os

# Our own routing table, and the reason this module can derive rather
# than be told: these paths are not a remote service's API, they are the
# control panel's internal surface, defined in this repository.
GIT_CREDENTIAL = "git-credential"
RESOURCE_OBJECT = "resource-object"
# The write side of the same seam: the pod holds no cloud
# credential, so it asks the control panel to store what it
# emitted, exactly as it asks the panel to read what it was
# granted. See app/routers/artifact_object.py.
ARTIFACT_OBJECT = "artifact"
# And the read-back of the same seam. An `artifact` check that
# trusts the manifest row is a check that passes on bytes nobody
# can open. See kernel/verifier/artifact_reachable.py.
ARTIFACT_EXISTS = "artifact-exists"
# And the verdict a reviewer reaches. The pod may not write
# `story_reviews` -- its execution identity is scoped to the
# kernel schema -- so it reports and the panel records.
STORY_REVIEW = "story-review"
# And what a long job is doing, for a person who asks mid-run. The
# assistant cannot reach into the pod's sandbox -- the buffer lives in a
# process behind a network policy -- so the pod reports and the panel
# records. Same seam, same reason, and the one that repeats: this is
# throttled where the other four fire once.
EXECUTION_JOBS = "execution-jobs"


def _explicit(var: str) -> str:
    """`in os.environ`, never `.get(var, "")`.

    B1 bans a default argument across kernel/ and F7 enforces it: a
    silent default turns "this deployment is misconfigured" into "the
    feature quietly does nothing", which is precisely the failure above.
    """
    return (os.environ[var] if var in os.environ else "").strip()


def control_panel() -> str:
    """The control panel's base URL, without a trailing slash."""
    return _explicit("CONTROL_PANEL_URL").rstrip("/")


def endpoint(path: str, override: str = "") -> str:
    """A control-panel internal endpoint, or "" if there is no panel.

    `override` names an environment variable that, when set, is used
    verbatim -- the E0 escape hatch described above.
    """
    direct = _explicit(override) if override else ""
    if direct:
        return direct
    base = control_panel()
    return f"{base}/internal/{path}" if base else ""
