"""The leg result as it crosses a process boundary. D2's seam.

`loop_result` says what `run_leg` hands back and why there are two
shapes; this says what survives the trip when the leg is in a different
CONTAINER from the thing that asked for it. Its own docstring called
this out before it existed -- "IT IS ABOUT TO BECOME A WIRE FORMAT. D2
moves the leg runner into the worker container, and at that point this
dict crosses a process boundary" -- and D2 is that point.

WHY A FORMAT AND NOT JUST `json.dumps`. The dict was already printed as
JSON by `leg_runner`, and that was fine while the only reader was a test
script written the same afternoon as the writer. It stops being fine the
moment the reader is a CONDUCTOR ON A DIFFERENT DEPLOY than the pod. The
two halves of this seam are built from the same tree but they are not
restarted together: a pod dispatched at 10:00 outlives a conductor
rollout at 10:01, which is the whole reason `leg_lock` had to move into
the pod in the first place (§10). So the two ends WILL disagree about
this dict eventually, and the only question is whether they say so.

THREE PROPERTIES, each of them a failure that has already happened to
something else in this build:

  * A VERSION, so a mismatch is an error and not a silent misread. The
    `parked` key is flattened to three plain fields; if a later phase
    un-flattens it, a conductor on the old build reads `parked` as truthy
    and reports the summary as `None`. It would look like a run that
    parked for no stated reason -- and nothing would log a fault.

  * A MARKER LINE, because stdout is not private. Every module under
    `kernel/` logs, `warnings` writes, and a third-party SDK printing one
    banner line is enough to make `json.loads(stdout)` raise on a leg
    that completed perfectly. The result is found by scanning for its own
    marker rather than by assuming it is the whole stream. This is not
    hypothetical tidiness: `_build_mock_adapter` shares a process with
    provider SDKs whose import-time chatter nobody controls.

  * NO SILENT KEY LOSS. `decode` returns the payload as it arrived. It
    does not project onto a known key list, because the caller that reads
    `stop_reason` by `in` (see `loop_result`) cannot tell a key that was
    never sent from one this module dropped on the way past.

WHAT THIS DELIBERATELY DOES NOT DO. It does not validate the leg's
semantics -- not `steps >= 0`, not "a parked leg has a summary". Those
are `loop_result`'s rules and they are enforced where the dict is BUILT,
by construction. A second copy of them here would be a second thing to
keep in step, and the one that drifted would refuse legs that are fine.

Nor is it a transport. It is a format, and the transport is whatever
runs the runner: today a process in the pod's worker container whose
stdout the caller reads.
"""

from __future__ import annotations

import json

# Bumped when the SHAPE changes in a way an old reader would misread.
# Adding a key nobody reads yet is not a shape change; renaming, nesting
# or removing one is. The rule is the one `loop_result` states: callers
# distinguish the two shapes by `in`, so presence IS meaning.
WIRE_VERSION = 1

# The line is found by this, not by position. See the module docstring.
MARKER = "agentos-leg-result"


class WireError(RuntimeError):
    """The other end sent something this build cannot read.

    Its own type so a caller can tell "the pod disagreed about the
    format" from "the leg failed" -- the first is a deploy fault and
    wants a person, the second is an ordinary outcome the state machine
    already knows how to handle. Collapsing them is how a version skew
    comes to be reported as twelve failed legs.
    """


def encode(result: dict) -> str:
    """One line, self-identifying, safe to print among log output.

    `sort_keys` so two runs of the same leg produce the same bytes --
    which is what lets a test assert on the line rather than on a
    re-parsed dict, and what makes a diff of two captured legs legible.
    """
    return json.dumps({MARKER: WIRE_VERSION, "result": result},
                      sort_keys=True)


def encode_error(message: str) -> str:
    """The leg did not produce a result, and that is still an answer.

    A runner that dies with no line at all leaves the reader unable to
    distinguish "the pod is still working" from "the pod is gone", and
    the reader's only remaining move is a timeout -- which is the shape
    §10 rejected for the lock and should not be reintroduced here for
    the result. So a failure gets a line too.
    """
    return json.dumps({MARKER: WIRE_VERSION, "error": message},
                      sort_keys=True)


def _envelope(text: str) -> dict:
    """The one marked line in `text`, parsed. Raises WireError otherwise.

    LAST MARKED LINE WINS. A retried runner, or a wrapper that echoes
    its child's output, can legitimately produce two; the later one is
    the one that describes the leg that actually finished.
    """
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or MARKER not in line:
            continue
        try:
            got = json.loads(line)
        except ValueError:
            # A log line that merely MENTIONS the marker. Skipped rather
            # than fatal: the marker is a string, and a warning quoting
            # it must not be able to fail a leg that succeeded.
            continue
        if isinstance(got, dict) and MARKER in got:
            found = got
    if found is None:
        raise WireError(
            f"no {MARKER!r} line in the runner's output. The leg may have "
            f"died before it could report; its stderr is the evidence, and "
            f"an empty stream here means the process was killed rather than "
            f"that the leg did nothing.")
    return found


def decode(text: str) -> dict:
    """The result dict the other end sent.

    Raises `WireError` on a version this build cannot read, and on the
    error shape -- because a caller that got an error has no result to
    interpret, and handing it `{}` would be read as a leg that ran and
    did nothing.
    """
    envelope = _envelope(text)
    version = envelope.get(MARKER)
    if version != WIRE_VERSION:
        raise WireError(
            f"the runner speaks leg-result version {version!r} and this "
            f"build reads {WIRE_VERSION}. The pod and the conductor are on "
            f"different deploys; a pod outliving a rollout is expected and "
            f"is exactly why this is checked rather than assumed.")
    if "error" in envelope:
        raise WireError(f"the leg runner reported: {envelope['error']}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise WireError(
            f"a leg-result envelope with no result object: {envelope!r}")
    return result
