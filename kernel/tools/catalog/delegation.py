"""Delegation and egress — the two that reach furthest.

`subagent` is classified by its flavour, because what matters
at the spawn is how much the child could do at all: an
`investigate` child has no shell and no write, a `build` child
keeps `bash` and so reaches as far as `bash` does.

Egress is never in the base surface. A profile that wants it
says so.
"""
from __future__ import annotations

from kernel.tools.catalog.spec import _S, _schema

TOOLS: dict[str, dict] = {
    # ── Delegation ───────────────────────────────────────────────────────
    "subagent": _schema(
        "subagent",
        "Delegate separable work to a child agent. The child runs under a "
        "narrower envelope than yours and returns a short written answer, "
        "never its transcript. Use for surveying an unfamiliar area, or a "
        "self-contained change in files you are not editing.",
        {"flavour": {"type": _S, "enum": ["investigate", "build"],
                     "description": "investigate = read-only; build = its "
                                    "own worktree, merged by you."},
         "task": {"type": _S, "description": "Self-contained instruction."}},
        ["flavour", "task"],
        side="worker", effect="by_argument",
    ),

    # ── Egress — never in the base surface ───────────────────────────────
    "web_fetch": _schema(
        "web_fetch", "Fetch a URL and return its content as text.",
        {"url": {"type": _S}},
        ["url"],
        side="worker", effect="reversible",
    ),
    "web_search": _schema(
        "web_search", "Search the web and return results.",
        {"query": {"type": _S}},
        ["query"],
        side="loop",
    ),
}
