"""read_resource — the GO reads what it governs, one page at a time.

THE DOCTRINE HOLDS, THE BLINDFOLD GOES. GO plans the project, not the
code, and its senses (`resource_brief`, `code_state`, the DONE digests)
stay the cheap default. But a planner that cannot open ANY file — a
design doc somebody uploaded, a README, the one interface its next
story hinges on — plans on hearsay. This tool reads one file from one
governed resource, paged by line, so a targeted question costs a page
and inhaling a tree is still visibly the wrong tool for the job.

WHAT IT READS, BY KIND:
  repo / branch    the file at `path` on the resource's branch (or a
                   named ref), fetched live from the forge
  document / file  the latest PUBLISHED version (resource_publishes —
  / dataset        the write-back road), falling back to the uploaded
                   original where a store fetcher is available

LAYERING. The kernel never imports the control panel. Fetchers are
INJECTED at startup by whoever hosts these tools: the control panel
registers its GitHub-App fetcher and its GCS fetcher; the standalone E0
surface registers none and says so honestly. Same seam as everything
else in this package: the capability is the host's, the vocabulary is
the kernel's.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id

logger = logging.getLogger(__name__)

MAX_LINES = 400
MAX_BYTES = 60_000

# Injected by the host at startup. See module docstring.
_repo_fetcher = None    # async (repo_name, path, ref) -> str | None
_doc_fetcher = None     # async (resource_id, filename) -> bytes | None
_extractor = None       # async (filename, blob) -> str | None

# Extensions the host's extractor turns into pageable text. Everything
# else that decodes as text passes straight through (.txt, .md, .csv,
# .json, code); a binary with no extractor refuses honestly.
EXTRACT_EXTS = (".pdf", ".xlsx", ".xls", ".docx")


def set_repo_fetcher(fn) -> None:
    global _repo_fetcher
    _repo_fetcher = fn


def set_doc_fetcher(fn) -> None:
    global _doc_fetcher
    _doc_fetcher = fn


def set_extractor(fn) -> None:
    global _extractor
    _extractor = fn


def _page(text: str, start_line: int, max_lines: int) -> dict:
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    want = min(int(max_lines or 200), MAX_LINES)
    chunk = lines[start - 1:start - 1 + want]
    body = chr(10).join(chunk)
    clipped_bytes = False
    if len(body.encode("utf-8", "replace")) > MAX_BYTES:
        body = body.encode("utf-8", "replace")[:MAX_BYTES].decode(
            "utf-8", "replace")
        clipped_bytes = True
    return {"start_line": start, "lines_returned": len(chunk),
            "total_lines": total,
            "truncated": (start - 1 + want) < total or clipped_bytes,
            "content": body}


async def read_resource(conn, goal_id: str, resource_id: str,
                        path: str = "", start_line: int = 1,
                        max_lines: int = 200, ref: str = ""):
    """One page of one file from one of this goal's resources."""
    # AN EMPTY ID IS NOT A PREFIX OF EVERYTHING. The lookup below
    # prefix-matches so a person can name a resource by its first few
    # characters -- but `'' || '%'` matches every row, and `LIMIT 1`
    # then returns the goal's oldest resource with no hint that it was
    # not the one asked for. A caller that forgot the argument got a
    # confident read of the wrong document.
    if not str(resource_id or "").strip():
        return Refused("E_NO_RESOURCE_ID",
                       "name the resource to read; resource_brief and "
                       "goal_state both list the ids")

    # BY NAME AS WELL AS BY ID. `VALIDATION_RULES.md` is what a resource
    # is called in RESOURCES.md, in resource_brief and in the story that
    # asked about it -- the opaque `4fdb48c6-acb` appears nowhere a
    # planner naturally reads. GO asked by name, got E_NO_RESOURCE, and
    # concluded the document was not registered; it was, and it was the
    # only thing standing between it and the file.
    #
    # Ordered so an exact id still wins: an id is unambiguous, a name is
    # only usually so, and prefix matching stays last because it is the
    # loosest.
    # This matching and this refusal used to live here and only here,
    # which is why four sibling tools could not be addressed by name.
    # Moved to kernel/go/resource_ref.py unchanged; the behaviour of
    # THIS tool, including its E_NO_RESOURCE code, is the same.
    resolved = await resolve_resource_id(
        conn, goal_id, str(resource_id), code="E_NO_RESOURCE")
    if isinstance(resolved, Refused):
        return resolved
    row = await conn.fetchrow(
        """
        SELECT id, kind, name, repo_name, branch_name, default_branch,
               uri, clone_url
          FROM goal_resources WHERE goal_id = $1 AND id = $2
        """, goal_id, resolved)

    if row["kind"] in ("repo", "branch"):
        if not (path or "").strip():
            return Refused("E_NO_PATH",
                           "a repo read names one file; use code_state "
                           "and resource_brief to decide which")
        if _repo_fetcher is None:
            return Refused("E_NO_FETCHER",
                           "this surface has no forge fetcher; repo "
                           "reads are served where the control panel "
                           "hosts these tools")
        at_ref = (ref or row["branch_name"] or row["default_branch"]
                  or "main")
        try:
            text = await _repo_fetcher(row["repo_name"], path.strip(),
                                       at_ref)
        except Exception as e:                              # noqa: BLE001
            logger.warning("read_resource %s %s: %s", resource_id, path, e)
            text = None
        if text is None:
            return Refused("E_NO_FILE",
                           f"{path!r} was not readable at "
                           f"{row['repo_name']}@{at_ref}")
        return {"resource": row["id"], "kind": row["kind"],
                "repo": row["repo_name"], "ref": at_ref, "path": path,
                **_page(text, start_line, max_lines)}

    # Documents, files, datasets: the latest published version is the
    # governed truth (it is what the write-back road produced); the
    # uploaded original is the fallback where the host can fetch it.
    pub = await conn.fetchrow(
        """
        SELECT filename, content FROM resource_publishes
         WHERE resource_id = $1 AND content IS NOT NULL
         ORDER BY created_at DESC LIMIT 1
        """, row["id"])
    blob, filename, origin = None, None, ""
    if pub and pub["content"]:
        blob, filename, origin = pub["content"], pub["filename"], "published"
    elif (row["uri"] or "").startswith("artifact://"):
        # AN ARTIFACT RESOURCE LIVES IN THE CONTENT STORE, not in the
        # object store the doc fetcher reads. D2 made every emitted
        # report a grantable document; this reader knew only the GCS
        # road, so `read_resource` on one refused with E_NO_CONTENT --
        # "no published version ... and the uploaded original is not
        # fetchable from here" -- which is true of GCS and false of the
        # artifact, whose bytes were present the whole time.
        #
        # `read_artifact` remains the direct route and is what both
        # prompts point at. This is for the case a planner reaches a
        # granted document by the name it was granted under.
        from kernel.artifacts.uri import read_artifact_uri

        try:
            blob = read_artifact_uri(row["uri"])
            filename = path or row["name"]
            origin = "artifact"
        except Exception as e:                              # noqa: BLE001
            logger.warning("artifact fetch %s: %s", resource_id, e)
    elif _doc_fetcher is not None:
        try:
            blob = await _doc_fetcher(row["id"], path or row["name"])
            filename, origin = path or row["name"], "uploaded"
        except Exception as e:                              # noqa: BLE001
            logger.warning("doc fetch %s: %s", resource_id, e)
    if not blob:
        return Refused("E_NO_CONTENT",
                       f"no published version of {row['name']!r} and the "
                       f"uploaded original is not fetchable from here")

    # Binary document formats page as EXTRACTED TEXT -- the same
    # pdfplumber/openpyxl/python-docx path the resource scanner has
    # always used, injected by the host like the fetchers. A PDF is a
    # document to a planner, not a byte stream.
    lowered = (filename or "").lower()
    if lowered.endswith(EXTRACT_EXTS):
        if _extractor is None:
            return Refused("E_NO_EXTRACTOR",
                           f"{filename!r} needs text extraction and this "
                           f"surface has no extractor; it is served where "
                           f"the control panel hosts these tools")
        try:
            text = await _extractor(filename, blob)
        except Exception as e:                              # noqa: BLE001
            logger.warning("extract %s: %s", filename, e)
            text = None
        if not text:
            return Refused("E_EXTRACT_FAILED",
                           f"{filename!r} could not be converted to text")
        return {"resource": row["id"], "kind": row["kind"],
                "filename": filename, "origin": origin,
                "extracted": True,
                **_page(text, start_line, max_lines)}

    if b"\x00" in blob[:4096]:
        return Refused("E_BINARY",
                       f"{filename!r} is binary ({len(blob)} bytes) with "
                       f"no extractor for its format; there is nothing "
                       f"useful to page through")
    return {"resource": row["id"], "kind": row["kind"],
            "filename": filename, "origin": origin,
            **_page(blob.decode("utf-8", "replace"), start_line,
                    max_lines)}
