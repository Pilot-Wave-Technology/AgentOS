"""`accept_errand` is reachable, not just implemented.

WHAT SHIPPED. The handler, its guards -- errands only, a person named,
one proposal or ask which -- and twenty-seven tests. GO's prompt gained
a section and a tool-table row. And it was wired ONLY to the
`json:go-action` channel, which is not how GO calls a tool.

GO's tools are gateway-registered and reach internal HTTP endpoints,
gated by `templates.tools_allow`. `accept_errand` was in neither. So GO
read its own prompt, believed it had the tool, looked for it, and told
the person: "I cannot approve an errand proposal or start it -- by
platform design, there is no tool that..." Correct behaviour on a
promise the platform had not kept.

THE SHAPE OF THE MISS, and it is the third of the day. The cancel route
existed and was shadowed by a catch-all. The cancel form existed and
sent an unparseable body. This existed and had no tool. Every one was
tested for what it DID and not for whether anything could reach it.
"""

from __future__ import annotations

import io
import pathlib

from app.services.tests.shipped_tree import IS_DISTRIBUTION, seed_template

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_there_is_an_internal_endpoint_for_it():
    """GO's tools reach internal HTTP endpoints. Without one there is
    nothing for the gateway tool to call."""
    src = _read("app/routers/stories.py")
    assert '@router.post("/internal/goals/{pid}/errands/accept")' in src


def test_the_endpoint_reuses_the_handler_rather_than_copying_it():
    """The guards are the point of the handler. A second copy behind the
    endpoint would drift, and the half that drifted would be the one
    that lets GO start an ordinary story."""
    src = _read("app/routers/stories.py")
    fn = src[src.index("async def internal_accept_errand("):]
    fn = fn[:fn.index("@router.post(", 10)]
    assert "from app.services.go_response import _action_accept_errand" in fn
    assert "_action_accept_errand(pid, body" in fn


def test_the_endpoint_is_bearer_gated_like_its_neighbours():
    src = _read("app/routers/stories.py")
    fn = src[src.index("async def internal_accept_errand("):]
    fn = fn[:fn.index("@router.post(", 10)]
    assert "verify_internal_bearer(request)" in fn


def test_the_template_is_granted_the_tool():
    if IS_DISTRIBUTION:
        # No migration ships; the seeded go-v2 template IS the grant.
        assert "accept_errand" in seed_template("go-v2")["tools_allow"], (
            "a fresh install's GO is not allowed the tool its prompt names")
        return
    mig = _read("migrations/go_accept_errand_tool.sql")
    assert "array_append(tools_allow, 'accept_errand')" in mig
    assert "id = 'go-v2'" in mig


def test_the_migration_is_idempotent():
    """Migrations are applied once, but a hand-run during an incident
    should not double the array."""
    mig = _read("migrations/go_accept_errand_tool.sql")
    assert "NOT ('accept_errand' = ANY(" in mig


def test_the_migration_is_in_the_sequence():
    seq = _read("migrations/SEQUENCE").split()
    assert "go_accept_errand_tool" in seq


def test_the_assistant_is_not_granted_it():
    """Approving an errand to run unattended is a decision about
    somebody's budget and name. The assistant's surface is the
    narrower one on purpose."""
    if IS_DISTRIBUTION:
        assert "accept_errand" not in seed_template("kernel-assistant")["tools_allow"]
        return
    mig = _read("migrations/go_accept_errand_tool.sql")
    assert "kernel-assistant" not in mig


def test_the_phantom_tool_check_exists_now():
    """The test that would have caught this. The existing one asked
    whether every ALLOWED tool was documented; nothing asked whether
    every DOCUMENTED tool was allowed -- so a table row for a
    non-existent tool was invisible."""
    src = _read("app/services/tests/test_gateway_surface.py")
    assert "phantom = sorted(" in src
    assert "the model will reach for them and find nothing" in src


def test_the_exception_list_stays_short():
    """`_NOT_TOOLS` is a hole in that check. Every name added to it is a
    name the check can no longer see."""
    src = _read("app/services/tests/test_gateway_surface.py")
    block = src[src.index("_NOT_TOOLS = {"):]
    block = block[:block.index("}")]
    names = [w for w in block.split('"') if w.isidentifier()]
    assert len(names) <= 6, f"_NOT_TOOLS has grown to {names}"
