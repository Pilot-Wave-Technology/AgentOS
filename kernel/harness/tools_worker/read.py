"""Reading things the workspace does not hold: the web, and the store.

Both are worker-side, and in `web_fetch`'s case the placement IS the
guard — `tools_web.fetch` is deny-by-default and re-checks every
redirect hop, and the sandbox shares the worker's network namespace,
so a sandbox-side fetch would have no guard at all.
"""
from __future__ import annotations

from kernel.harness.models.types import ToolResult
from kernel.harness.tools_web import fetch

def web_fetch(tool_args: dict, tool_call_id: str,
              allowed_hosts=None) -> ToolResult:
    """Fetch a URL as text. Worker-side, and that placement is the guard.

    THIS FUNCTION DID NOT EXIST. `executor.py` dispatched `web_fetch` to
    `tools_worker.web_fetch` and this module only imported `fetch` from
    `tools_web` without ever using it, so every call raised
    `AttributeError` and came back as `E_TOOL_RAISED` — on story-coding
    and story-business, the two profiles that offer the tool. The CI test
    could not catch it because it asserts membership of `WORKER_TOOLS`,
    never that the function behind the name exists.

    `fetch` never raises; it returns `(text, error)` with the SSRF guard
    already applied — deny-by-default, every redirect hop re-checked. So
    this is the thin part on purpose: argument validation, and the
    mapping from that pair onto a ToolResult.

    Ec4a — AND THE ALLOWLIST, which is a different question from SSRF.
    `fetch` refuses private, loopback and link-local addresses, which
    stops credential theft from the metadata endpoint. It permits every
    PUBLIC host, and `smtp.sendgrid.net` is a perfectly ordinary public
    host — so "run the job but do not send the email" had no answer.
    Reach now follows the story's grants.

    `allowed_hosts` of `None` means the caller could not say, and the
    fetch proceeds on the SSRF guard alone. That is the console's
    ad-hoc path and the null-executor tests; a run dispatched by the
    worker always carries a set, even when it is empty.
    """
    from kernel.workspace.egress import (is_never_allowed, matches,
                                         refusal, host_of)

    url = (tool_args.get("url") or "").strip()
    if not url:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="web_fetch: url is required", exit_code=1)

    host = host_of(url)
    # THE FLOOR, checked whatever anybody granted. `fetch` refuses the
    # link-local ADDRESS; this refuses the NAME, before DNS gets a
    # chance to turn one into the other.
    if is_never_allowed(host):
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"web_fetch: {host!r} is the cloud metadata endpoint and "
                  f"is never reachable, by any grant.", exit_code=1)
    if allowed_hosts is not None and not matches(host, allowed_hosts):
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="web_fetch: " + refusal(host, allowed_hosts),
                          exit_code=1)

    text, error = fetch(url)
    if error:
        # A refusal, not a crash. A blocked host or a dead server is
        # something the model can act on — try another source, or say it
        # could not be reached — and it should read as a typed tool error
        # rather than as the harness falling over.
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=f"web_fetch: {error}", exit_code=1)
    return ToolResult(tool_call_id=tool_call_id, output=text,
                      error=None, exit_code=0)


def artifact_read(tool_args: dict, tool_call_id: str) -> ToolResult:
    """Read part of a stored output — a range, or the lines matching a pattern.

    The point of the store is that a big output stays reachable. A tool that
    could only return the whole thing would put the 50 MB back in the
    context the bounding just kept it out of, so reads are ranges and greps.
    """
    from kernel.artifacts.store import ArtifactNotFound, get_store

    artifact_id = (tool_args.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="artifact_read: artifact_id is required",
                          exit_code=1)

    store = get_store()
    try:
        total = store.size(artifact_id)
    except ArtifactNotFound:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"artifact_read: no artifact {artifact_id!r}. Ids come "
                  "from the footer of a bounded tool output.",
            exit_code=1)

    pattern = tool_args.get("pattern")
    if pattern:
        # Shared with the assistant's `read_artifact` (H8). Only the
        # envelope differs -- ToolResult here, a plain dict there -- so
        # the bytes and the caps live in kernel.artifacts.reading and the
        # envelope stays at the call site.
        from kernel.artifacts.reading import BadPattern, read_pattern
        try:
            out = read_pattern(artifact_id, pattern, total)
        except BadPattern as exc:
            return ToolResult(tool_call_id=tool_call_id, output="",
                              error=f"artifact_read: invalid pattern: {exc}",
                              exit_code=1)
        return ToolResult(tool_call_id=tool_call_id, output=out,
                          error=None, exit_code=0)

    from kernel.artifacts.reading import read_range
    return ToolResult(
        tool_call_id=tool_call_id,
        output=read_range(artifact_id, tool_args.get("offset") or 0,
                          tool_args.get("length") or 0, total),
        error=None, exit_code=0)
