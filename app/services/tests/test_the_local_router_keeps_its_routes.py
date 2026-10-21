"""The local lane's HTTP surface is the extension's only way in.

`GET /internal/local/stories` was deleted by accident in dc5d5d94 -- the edit that
replaced the `/memory-writeback` handler above it took the next decorator with it.
Nothing failed: `list_local_stories` was still imported, still correct, still
covered by its own tests, and every other route still worked. The extension simply
opened on `/internal/local/stories -> 404 {}` and could not list a single story.

A route is deleted silently because no caller lives in this repo. So the set is
pinned here by READING THE SOURCE rather than importing the app (these tests run
without a database, and importing the router pulls in the pool).
"""
from __future__ import annotations

import ast
import pathlib

ROUTERS = pathlib.Path(__file__).resolve().parents[3] / "app" / "routers"

# Every route the VS Code extension and the local runner call, as of 2026-10-01.
# Adding one here is cheap; losing one is a dead extension.
EXPECTED = {
    ("get", "/stories"),
    # The bytes of one granted document, so the editor can build the
    # `docs/` directory every local session's CLAUDE.md already draws.
    # The pod fetches the same thing from `/internal/resource-object`,
    # which the extension cannot authenticate against.
    ("get", "/resource-object"),
    # Stopping a story from the editor. Deliberately NOT the existing
    # `/internal/goals/{pid}/stories/{sid}/cancel`, which takes an
    # internal bearer and checks nothing else — a story token on a
    # laptop must not be able to cancel a different story.
    ("post", "/cancel"),
    ("post", "/auth-exchange"), ("post", "/auth-refresh"), ("post", "/auth-revoke"),
    ("post", "/story-token"), ("post", "/checkout"), ("post", "/heartbeat"),
    ("post", "/complete"), ("post", "/hydrate"), ("post", "/github-token"),
    ("post", "/profile"), ("post", "/memory-hydrate"), ("post", "/memory-writeback"),
    ("post", "/plan-submit"), ("post", "/plan-approve"),
    ("post", "/plan-reject"), ("post", "/story-done"), ("post", "/done-check"),
    ("post", "/story-recover"), ("post", "/diff-decide"), ("post", "/diff-submit"),
    # The agent's completion claim — the local equivalent of the kernel's
    # `propose_completion`, and the entry point the submit cycle lacked.
    ("post", "/propose-completion"),
    # The extension reports what the checks did, and at which commit.
    # The platform runs them, so this is the only way the control plane
    # learns they passed -- and `diff-submit` will not accept a commit
    # it has heard nothing about.
    ("post", "/check-report"),
    ("post", "/claim-release"),
    ("post", "/recent-messages"), ("post", "/get-state"), ("post", "/report-message"),
    ("post", "/push-token"),
    ("post", "/artifact-emit"), ("post", "/artifact-list"),
    ("post", "/more-work"), ("post", "/continue-plan"),
    ("post", "/story-review"),
}


def _declared() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name in ("local_execution.py", "local_execution_run.py"):
        tree = ast.parse((ROUTERS / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                # @router.get("/path") / @router.post("/path")
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "router"
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    found.add((dec.func.attr, dec.args[0].value))
    return found


def test_the_local_router_keeps_its_routes():
    missing = EXPECTED - _declared()
    assert not missing, (
        "the local router lost "
        + ", ".join(f"{m.upper()} /internal/local{p}" for m, p in sorted(missing))
        + " -- the extension calls these and gets a 404 with no other symptom"
    )


def test_the_story_list_is_a_GET_and_is_present():
    # Called out separately because it is the extension's ENTRY POINT: every other
    # route is unreachable until this one answers, so its loss is total, not partial.
    assert ("get", "/stories") in _declared(), (
        "GET /internal/local/stories is gone; the extension cannot list any story"
    )


def test_every_declared_route_is_accounted_for():
    # The other direction: a NEW route should be added to EXPECTED deliberately, so
    # this list stays the readable inventory of the extension's surface.
    extra = _declared() - EXPECTED
    assert not extra, (
        "new local routes are not listed in EXPECTED: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(extra))
    )
