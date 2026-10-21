"""Artifacts — the road back for work that is not a commit.

A report, a dataset, a screenshot: things an errand produces
that no branch can carry. `publish_artifact` is reversible
rather than contained — it durably reaches outside the
workspace — and reversible rather than irreversible, because a
publish is a NEW version with the old one retained.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _I, _S, _schema

TOOLS: dict[str, dict] = {
    # ── Artifacts ────────────────────────────────────────────────────────
    "artifact_read": _schema(
        "artifact_read",
        "Read part of a stored output by its artifact id. Large tool "
        "outputs are kept in full and shown to you as head+tail; use this "
        "to read the rest. Cheaper than re-running the command.",
        {"artifact_id": {"type": _S, "description": "Id from a bounded output."},
         "offset": {"type": _I, "description": "Byte offset. Negative reads "
                                               "from the end."},
         "length": {"type": _I, "description": "Bytes to read. Default 8192."},
         "pattern": {"type": _S, "description": "If set, return only matching "
                                                "lines instead of a range."}},
        ["artifact_id"],
        side="worker", effect="contained",
    ),

    "publish_artifact": _schema(
        "publish_artifact",
        "Publish a file you produced as a NEW VERSION of a granted "
        "document/file/dataset resource. The old version is retained. "
        "This is the only way a non-git file survives the container: an "
        "edit you do not publish is lost when the run ends. Requires the "
        "story to hold the resource with produces or modifies.",
        {"resource_id": {"type": _S, "description": "The granted resource."},
         "path": {"type": _S, "description": "Workspace-relative file to "
                                             "publish."},
         "note": {"type": _S, "description": "One line on what changed "
                                             "and why."}},
        ["resource_id", "path"],
        side="worker", effect="reversible",
    ),

    "emit_artifact": _schema(
        "emit_artifact",
        "Keep a file you produced. THIS IS HOW WORK SURVIVES — your "
        "workspace is discarded when the run ends, and a file you do not "
        "emit goes with it. Use it for the deliverable and for anything "
        "worth reading later: the report, the dataset you extracted, the "
        "script that produced them, a log worth keeping. Emitting the same "
        "role and name again supersedes the earlier one, so a rerun reads "
        "as a new version rather than a second answer.",
        {"path": {"type": _S,
                  "description": "Workspace-relative file to keep."},
         "role": {"type": _S,
                  "enum": ["report", "dataset", "method", "log", "figure"],
                  "description": "report = the answer, for a person. "
                                 "dataset = rows another step can use. "
                                 "method = how it was produced. "
                                 "log = what happened. figure = an image."},
         "name": {"type": _S,
                  "description": "What to call it in a list. Defaults to "
                                 "the filename."},
         "summary": {"type": _S,
                     "description": "One or two sentences: what this "
                                    "contains and what it shows. Required "
                                    "— it is how somebody finds this "
                                    "without reading the bytes."},
         "supersedes": {"type": _S,
                        "description": "Artifact id this replaces. Usually "
                                       "omit: the same role and name chains "
                                       "automatically."}},
        ["path", "role", "summary"],
        # Reversible, and for `publish_artifact`'s reason: it durably
        # reaches outside the workspace, and superseding undoes it without
        # losing anything.
        side="worker", effect="reversible",
    ),
}
