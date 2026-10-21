"""The one guard in the refresh that no window and no restart protects.

WHY THIS FILE EXISTS SEPARATELY. Every other mistake a refresh can make
is recoverable by running it again with the source corrected. Writing an
agent's `MEMORY.md` is not: the agent has been accumulating it since it
was created, nothing else holds a copy, and the archive-to-GCS path runs
on DELETE, not on refresh.

AND NOTHING DOWNSTREAM WILL CATCH IT. The gateway's own
`ALLOWED_FILE_NAMES` includes `MEMORY.md`, `memory.md` and
`USER_MEMORY.md`, because its memory tooling writes them legitimately. So
`agents.files.set("MEMORY.md", ...)` returns success. The list in
`agent_refresh.managed` is the entire defence, which is why it is tested
by name rather than by behaviour alone.

The failure this is really guarding against is not today's code. It is
somebody, a year from now, adding a filename to `MANAGED_FILES` to make
one more thing refreshable, and not knowing that one of these is load
bearing.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.agent_refresh import managed
from app.services.agent_refresh.managed import (
    MANAGED_FILES, NEVER_WRITE, is_writable, refuse_reason)

# Spelled out here rather than imported, deliberately. Importing the
# constant would make this test agree with the module by construction --
# it would pass just as happily if somebody emptied the set.
UNRECOVERABLE = ("MEMORY.md", "memory.md", "USER_MEMORY.md")

# Not unrecoverable, but not ours: per-user content, or state the gateway
# wrote into the file itself.
NOT_OURS = ("IDENTITY.md", "USER.md", "HEARTBEAT.md", "BOOTSTRAP.md")


@pytest.mark.parametrize("name", UNRECOVERABLE)
def test_memory_is_never_writable(name):
    """If this fails, a refresh can destroy an agent's memory."""
    assert not is_writable(name), (
        f"{name} became writable. The gateway will accept it -- its own "
        f"allowlist permits memory files -- so nothing downstream will "
        f"refuse this. An agent's memory has no other copy.")
    assert name in NEVER_WRITE


@pytest.mark.parametrize("name", NOT_OURS)
def test_files_owned_by_something_else_are_not_writable(name):
    assert not is_writable(name), name


@pytest.mark.parametrize("name", MANAGED_FILES)
def test_the_managed_files_are_writable(name):
    """The set is narrow, but it must not be empty in practice."""
    assert is_writable(name)


def test_the_managed_set_is_only_prompts():
    """A refresh regenerates prompts. Anything else in this tuple is a
    widening of blast radius that should be argued for in the module
    docstring first."""
    assert set(MANAGED_FILES) == {"AGENTS.md", "SOUL.md"}, (
        "MANAGED_FILES changed. That is a decision about what a "
        "fleet-wide write may touch -- read managed.py's docstring, and "
        "if the change is right, change this test deliberately.")


def test_unknown_files_are_refused_not_allowed():
    """DENY BY DEFAULT. A name nobody has considered is one nobody has
    thought about the consequences of writing."""
    for name in ("NOTES.md", "config.json", "", "   ",
                 "../../etc/passwd", "skills/x/SKILL.md"):
        assert not is_writable(name), name


def test_the_refusal_says_which_kind_of_no_it_is():
    """A person reading the plan output needs to tell "I protected this"
    from "I have never heard of this"."""
    assert "memory" in refuse_reason("MEMORY.md").lower()
    assert "managed set" in refuse_reason("NOTES.md")


def test_the_never_list_is_checked_at_the_push_not_only_the_render():
    """Defence in depth: a renderer bug must not be able to route around
    the list by handing the pusher a filename it never filtered."""
    from app.services.agent_refresh import apply as apply_mod
    src = inspect.getsource(apply_mod)
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "is_writable" in code, (
        "the applier must re-check every filename it is about to write; "
        "trusting the renderer's filtering makes the render the only "
        "guard, and the render is the part most likely to change")


def test_the_module_explains_why_the_gateway_will_not_help():
    """The comment is load-bearing. Somebody widening this list will
    reasonably assume the gateway validates filenames -- it does, and its
    list permits memory."""
    doc = inspect.getdoc(managed) or ""
    assert "ALLOWED_FILE_NAMES" in doc
