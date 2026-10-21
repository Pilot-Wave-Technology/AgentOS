"""Every sandbox file tool stays inside the workspace.

WHAT WENT WRONG WITHOUT THIS. `_resolve` was
`path if isabs(path) else join(workspace, path)` -- no containment at
all. An absolute path was taken verbatim and `..` climbed straight out.
Across 22,500 file-tool calls on dev and next, models pointed a tool
outside the workspace 30 times: reading `/opt/agentos-bridge/shim.py`,
grepping `/opt`, listing the bridge's own source. Every one of those
landed somewhere small, so nothing broke and nobody noticed.

Then execution 101 passed `path: ".."`, which resolves to `/`, and
`grep` walked the container. It read synthetic files under `/proc` and
`/sys` by LINE -- and some of those have no newline, so one "line" grew
until the sandbox hit its 9GiB limit and was OOM-killed. The bridge
shim lives in that container, so every later tool call came back
`errno 99`, and the model concluded the bridge service needed
restarting and asked a person to do it.

TWO THINGS WERE WRONG AND THIS IS THE FIRST. The reads also need
bounding -- a 9GiB file inside the workspace is just as fatal -- but
containment is what makes the blast radius the workspace instead of
the machine, and it is what stops a blocked executor reading the
platform's own source looking for a way on. That last one is not
hypothetical: exec 407 on next went through `/opt/agentos-bridge`
during the run that is on record for hunting credentials.

ABSOLUTE PATHS INSIDE THE WORKSPACE STILL WORK, deliberately. Models
write `/workspace/.agentos-out/job-xyz.log` and mean it; refusing that
would break a thing that is both common and correct. The test is where
the path LANDS, not how it was spelled.

REALPATH, so a symlink out is caught too -- `.venv` and friends are
full of links, and a tool that resolved them after the check would be
checking the wrong string.
"""

import os

import errors

#: What the model is told. It names where the path LANDED and what the
#: root is, because "outside the workspace" without either is a riddle
#: and the next attempt is then another guess.
#:
#: `E_OUTSIDE_SCOPE` and not a new code: the vocabulary is closed, an
#: unrecognised code is rewritten to `E_TOOL_CRASHED`, and this one
#: already means the right thing -- "this is POLICY, not a mistake you
#: can correct by rephrasing ... Work inside the workspace".
_OUT = ("{p!r} resolves to {r!r}, which is outside the workspace at "
        "{w!r}.")


def contain(workspace: str, path: str):
    """`(resolved, None)` when inside, `(None, error)` when not.

    The error is a ready-to-return bridge failure, so callers stay a
    two-line check and cannot accidentally continue on a bad path.
    """
    base = os.path.realpath(workspace)
    raw = path if os.path.isabs(path) else os.path.join(base, path)
    target = os.path.realpath(raw)
    if target != base and not target.startswith(base + os.sep):
        return None, errors.fail(
            "E_OUTSIDE_SCOPE",
            _OUT.format(p=path, r=target, w=base), exit_code=1)
    return target, None
