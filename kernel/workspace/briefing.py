"""What the workspace IS, written where the workspace lives.

MOVED OUT OF `kernel/worker/runner.py`, and the move is the fix. The
conductor wrote WORKSPACE.md into the tree it had just cloned, which is
right on Docker -- it cloned into its own filesystem. On Kubernetes
`prepare_workspace` returns as soon as it knows the pod will compose
its own workspace, and the write sat AFTER that return. So no execution
on a pod has ever had one: the executor got a tree with two checkouts
of one repository and nothing saying which it may edit, and guessing
wrong is silent until a commit lands on the wrong branch.

So the builder lives here, called from BOTH sides -- the conductor for
a local workspace, `kernel/workspace/podinit.py` for a pod's, right
after `CompositeDriver.acquire` returns. Same text from the same
mounts, which is the only way the two substrates cannot drift.

THE LEASE IS CONDUCTOR-ONLY, and the pod passes None. A cloud
credential is issued after the workspace exists and is written into it
by the conductor; a pod composing its own tree has not been granted one
yet at this point. `_cloud_section(None)` renders nothing, which is
also what a run with no grant should see.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

def cloud_section(lease: dict | None) -> str:
    """What this run can reach, in the file the model actually reads.

    Ec4d — WITHOUT THIS THE REACH IS INVISIBLE. A cloud grant delivers a
    lease, not a tree, so it appears nowhere in the mount list; on KER25
    errand 91cdcd6e the run held a project and an identity and its
    workspace said nothing about either. The executor wrote a report
    that leaned entirely on the repository, which was the only thing its
    workspace admitted existed.

    Names the project and the identity and NEVER the token -- the point
    of the lease being a file is that the secret is not in anything that
    gets written, printed or committed.
    """
    if not lease:
        return ""
    region = f" ({lease['region']})" if lease.get("region") else ""
    return (
        "\n## Cloud reach\n\n"
        f"This run acts as **{lease['account']}** in "
        f"**{lease['scope']}**{region}.\n\n"
        f"- Granted by `{lease['granted_via']}`; expires "
        f"{lease['expires_at']}.\n"
        "- The credential is a FILE under `$AGENTOS_CREDENTIALS`. The "
        "cloud SDKs are already pointed at it -- run the command, "
        "authentication is done.\n"
        "- Never print, copy or commit it, and never put it in an "
        "artifact.\n"
        "- This is the ONLY cloud identity this run has. A permission "
        "error is the answer, not an obstacle: report it and stop.\n")


def write_workspace_md(root: str, mounts, lease: dict | None = None) -> None:
    """The workspace, described for whoever has to work in it.

    Written from the mounts themselves so it cannot drift from what was
    actually cloned. The rules are computed, not boilerplate: the first
    version said "a ...@branch directory is a snapshot for reading" --
    true when only READ lanes were suffixed, and flatly wrong the day a
    story wrote two lanes of one repo and BOTH of its working checkouts
    were suffixed. A description that contradicts the tree is worse than
    none: the executor believed it.
    """
    nl = chr(10)
    writable = [m for m in mounts if m.writable and m.binding == "git"]
    readonly = [m for m in mounts if not m.writable]
    multi = len(writable) > 1

    lines = ["# Workspace layout", ""]
    if multi:
        lines += [
            f"This story writes **{len(writable)} branches**. Each has its "
            f"own checkout below; put each part of the work in the "
            f"checkout for the branch it belongs on, and commit in each.",
            "",
        ]
    lines += [
        "| path | branch | access | what it is |",
        "|------|--------|--------|------------|",
    ]
    for m in mounts:
        access = "READ-WRITE" if m.writable else "READ-ONLY"
        if m.binding == "none":
            # HONEST ABOUT THE DEAD END. This used to say "reach it
            # through its own tools", which was false: the executor has
            # no tool for a resource that is not on disk. Three
            # documents mounted this way once, and the executor -- told
            # they were reachable and finding no way to reach them --
            # planned the whole story from the story text and invented
            # the rules the documents existed to state.
            what = (f"granted {m.kind or 'resource'}, but NOT on disk and "
                    f"NOT readable from here. If the work depends on it, "
                    f"say so with ask_human rather than guessing")
        elif m.binding == "copyin":
            what = ("INPUT DOCUMENT -- read it before you plan against "
                    "it; it is here because the work depends on what it "
                    "says. Read-only: edits are never saved")
        elif m.writable:
            what = ("working checkout -- edit and commit here"
                    if not multi else
                    f"working checkout for the {m.ref or '?'} branch -- "
                    f"the work belonging on this branch goes here")
        else:
            what = ("another branch's landed state, for reference -- "
                    "integrate AGAINST it, never edit it")
        lines.append(f"| {m.at or '.'} | {m.ref or ''} | {access} | "
                     f"{what} |")

    lines += ["", "Rules:"]
    if writable:
        lines.append(
            "- Edit only inside the READ-WRITE checkout"
            + ("s" if multi else "")
            + " above" + (" -- each on its own branch." if multi else ".")
            + " The branch is already checked out: do NOT create, switch"
              " or merge branches.")
    if multi:
        lines.append(
            "- `story_commit` commits EVERY writable checkout that has "
            "changes, each to its own branch. Work that belongs on one "
            "branch must not be written into another's checkout.")
    else:
        lines.append("- `story_commit` commits the writable checkout.")
    if readonly:
        lines.append(
            "- READ-ONLY mounts are inputs. Edits there are never "
            "committed, never pushed, and are lost.")
    lines.append(
        "- Checks run offline in a clean container built from your "
        "committed manifests, so anything the tests need must be "
        "committed and applied with `env_setup`.")
    lines.append("")

    body = nl.join(lines)
    # The cloud section last, because it is about what this run may
    # REACH rather than what it may edit -- and because a run without a
    # grant should see no mention of credentials at all.
    body += cloud_section(lease).replace(chr(10), nl)

    with open(os.path.join(root, "WORKSPACE.md"), "w",
              encoding="utf-8") as fh:
        fh.write(body)


