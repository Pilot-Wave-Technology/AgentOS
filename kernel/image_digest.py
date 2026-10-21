"""Turning the image a contract NAMES into the image it PINS.

WHY THIS EXISTS. Every run on 2026-09-17 -- executions 205, 207, 208,
209 and 210 -- logged the same parity warning:

    the contract names an image by TAG, not a digest -- the Verifier
    may not have run the image that was approved

`contracts.base_digest` has existed since F1 and every READER prefers it
(`kernel/verifier/*`, `kernel/conductor/contract_config.py`). No WRITER
has ever set it. `kernel/base_image.py` answers the question "which
image?" with a NAME -- the deployment's `AGENTOS_SANDBOX_BASE_IMAGE`, a
goal's `sandbox_base_image`, or E0's literal -- and a name is the thing
the warning is about. So the discipline existed at the bottom
(`infra/e0/fixtures/Dockerfile` pins every layer by digest) and at the
top (the column) and nothing joined them. This is the join.

WHY NOT ASK A REGISTRY. That was the first idea and E0 refuses it on
purpose. The Docker Engine route that would answer -- `GET
/images/{name}/json` -> `RepoDigests` -- is not reachable: the socket
proxy runs `CONTAINERS=1 POST=1 NETWORKS=1` and `infra/e0/preflight.sh`
says in as many words "Do NOT add IMAGES=1 to the socket proxy." On
Kubernetes there is no registry client at all; the driver talks to the
API server, and `kernel/drivers/registry.py` is a registry of DRIVERS,
not of images. Widening either surface to learn a string the deployment
already knows would be a poor trade.

AND THE LOCAL FIXTURE HAS NO DIGEST TO FIND ANYWAY. `agentos-kernel/
fixture:dev` was built by hand and pushed nowhere, so its `RepoDigests`
is empty; the only sha256 Docker would offer is the config `Id`, which
is not a pullable reference. `base_digest` IS dispatched as the image
(`contract_config.overrides_from`), so recording an `Id` would turn a
parity warning into an ImagePullBackOff -- NCAL3 again, by our own hand.

SO THE DEPLOYMENT DECLARES IT, which is the same argument
`kernel/base_image.py` already makes about the name: "A FACT ONLY THE
LOCAL ENVIRONMENT DECLARES IS NOT A FACT." The thing that built and
pushed the image is the only thing that knows its digest, and it can say
so in `AGENTOS_SANDBOX_BASE_DIGEST` next to the name it already sets.

AND IT IS CHECKED AGAINST THE NAME RATHER THAN TRUSTED. A declared
digest for some other repository is worse than no digest: it would
silence the warning AND dispatch the wrong image. When the repositories
disagree the answer is "not pinned", with the reason.

`os.environ[...] if ... in os.environ` rather than `.get(name, default)`:
B1's rule for `kernel/`, so a default is always visible where it is
chosen.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

ENV_VAR = "AGENTOS_SANDBOX_BASE_DIGEST"

# Where an unresolved pin records WHY, on the contract itself. A NULL
# column says "not pinned"; this says what stopped it, so the parity
# concern can name the repair instead of leaving a person to guess.
UNPINNED_KEY = "base_digest_unresolved"

_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


def repository(ref: str) -> str:
    """The repository part of an image reference, tag and digest removed.

    The tag is the last colon AFTER the last slash: a registry host may
    carry a port (`host:5000/agentos/fixture:dev`) and that colon is not
    a tag. Getting this wrong would make every mismatch check a coin
    toss.
    """
    name = (ref or "").strip().split("@", 1)[0]
    head, sep, tail = name.rpartition("/")
    return f"{head}{sep}{tail.split(':', 1)[0]}"


# "THE DEPLOY LOOKED AND FOUND NOTHING", spelled as a word rather than
# as emptiness. `scripts/infra/render.py` refuses a substitution that is
# unset OR EMPTY -- `if not env.get(name)` -- because rendering one blank
# would apply a manifest naming nothing. So a deploy that cannot resolve
# the digest cannot pass the empty string; it has to pass a value, and
# `EXECUTION_SQL_INSTANCE` already established `off` as that value.
#
# Treated here as ABSENT rather than as malformed, so the reason a person
# reads is "no digest is published for this image" -- which names the
# repair -- instead of "'off' is not a repository@sha256:... reference",
# which reads as somebody having configured it wrongly.
UNRESOLVED = "off"


def declared() -> str | None:
    """The digest reference this deployment publishes, if any."""
    if ENV_VAR not in os.environ:
        return None
    value = os.environ[ENV_VAR].strip()
    if value == UNRESOLVED:
        return None
    return value or None


def resolve(image: str) -> tuple[str | None, str | None]:
    """(digest reference to record, reason it could not be pinned).

    Exactly one of the two is ever set. The reason is a sentence, not a
    code: it is shown to a person beside a verdict.
    """
    named = (image or "").strip()
    if not named:
        return None, "the contract names no image at all"
    if _PINNED.search(named):
        # Already a digest -- a goal may name one in `sandbox_base_image`,
        # and then there is nothing to resolve.
        return named, None

    pin_ref = declared()
    if pin_ref is None:
        return None, (f"no digest is published for {named}; the build that "
                      f"pushes it should set {ENV_VAR} to its "
                      f"repository@sha256:... reference")
    if not _PINNED.search(pin_ref):
        return None, (f"{ENV_VAR} is {pin_ref!r}, which is not a "
                      f"repository@sha256:... reference")
    if repository(pin_ref) != repository(named):
        # Silencing the warning with a digest for a DIFFERENT image would
        # also dispatch that image. Refusing is the cheap outcome.
        return None, (f"{ENV_VAR} pins {repository(pin_ref)} but this "
                      f"contract names {repository(named)}")
    return pin_ref, None


def pin(image: str, spec: dict | None = None) -> tuple[str | None, dict]:
    """(base_digest, workspace_spec) for a contract about to be written.

    The pair is returned together because they must agree: a contract
    with a digest must not also carry a note saying it has none.
    """
    digest, reason = resolve(image)
    out = dict(spec or {})
    if reason:
        out[UNPINNED_KEY] = reason
        logger.warning("contract image %s is not pinned: %s", image, reason)
    else:
        out.pop(UNPINNED_KEY, None)
    return digest, out
