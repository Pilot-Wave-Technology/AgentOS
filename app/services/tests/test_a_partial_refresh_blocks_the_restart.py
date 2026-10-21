"""PARTIAL is the outcome that makes a restart unsafe, so it gets a test.

THE ASYMMETRY THIS ENCODES. An agent that fails completely is harmless:
nothing was written, so it stays exactly as stale as it was, and a
restart does it no harm. An agent that half-succeeds is the dangerous
one -- prompts written, tools refused -- because after the restart it
runs new prompts describing tools it does not have, and every call to a
renamed or missing tool is refused. That is the mismatch the entire
design exists to prevent, and it can only happen INSIDE one agent.

So the two must never be conflated, and the run must refuse to recommend
a restart when any agent carries the second shape.
"""

from __future__ import annotations

import pytest

from app.services.agent_refresh.apply import Outcome, apply_to_agent
from app.services.agent_refresh.eligibility import Refreshable
from app.services.agent_refresh.render import Desired
from app.services.agent_refresh.run import _applied_report


class FakeGateway:
    """Records what was pushed; fails whatever it is told to."""

    def __init__(self, fail_on: tuple[str, ...] = ()):
        self.fail_on = fail_on
        self.files: list[tuple[str, str]] = []
        self.tools: list[tuple[str, list, list]] = []
        self.skills: list[tuple[str, str]] = []

    async def set_agent_file(self, agent_id, name, content, encoding="utf8"):
        if "file" in self.fail_on:
            raise RuntimeError("gateway said no")
        self.files.append((agent_id, name))

    async def set_agent_tools(self, agent_id, allow, deny):
        if "tools" in self.fail_on:
            raise RuntimeError("gateway said no")
        self.tools.append((agent_id, allow, deny))

    async def set_agent_skill(self, agent_id, skill_name, content):
        if "skills" in self.fail_on:
            raise RuntimeError("gateway said no")
        self.skills.append((agent_id, skill_name))


def _agent() -> Refreshable:
    return Refreshable(gateway_agent_id="asst-1", agent_type="SHCA",
                       template_id="base-1", goal_id="G1",
                       thread_id="t1", workspace=None)


def _desired(**kw) -> Desired:
    base = dict(files={"AGENTS.md": "a", "SOUL.md": "s"},
                tools_allow=["read"], tools_deny=[], skills={})
    base.update(kw)
    return Desired(**base)


@pytest.mark.asyncio
async def test_prompts_then_tools_then_skills():
    """Order matters: only prompts are inert, so the live changes go
    last, as close to the restart as possible."""
    gw = FakeGateway()
    await apply_to_agent(gw, _agent(), _desired(skills={"s1": "body"}))
    assert [n for _, n in gw.files] == ["AGENTS.md", "SOUL.md"]
    assert gw.tools and gw.skills


@pytest.mark.asyncio
async def test_files_in_tools_out_is_partial():
    """THE UNSAFE SHAPE."""
    gw = FakeGateway(fail_on=("tools",))
    out = await apply_to_agent(gw, _agent(), _desired())
    assert out.files, "the prompts did land"
    assert out.partial, "so this agent is half-changed"
    assert not out.clean


@pytest.mark.asyncio
async def test_everything_failing_is_not_partial():
    """An untouched agent is merely stale, and a restart is safe."""
    gw = FakeGateway(fail_on=("file", "tools", "skills"))
    out = await apply_to_agent(gw, _agent(), _desired(skills={"s": "b"}))
    assert out.failures
    assert not out.touched
    assert not out.partial, (
        "nothing landed, so there is no mismatch to protect against -- "
        "conflating this with PARTIAL would block restarts for no reason")


@pytest.mark.asyncio
async def test_an_empty_allowlist_is_never_sent():
    """To the gateway an empty allow list means ALL TOOLS PERMITTED, not
    none. Sending one would silently widen an agent's surface."""
    gw = FakeGateway()
    out = await apply_to_agent(gw, _agent(), _desired(tools_allow=[]))
    assert gw.tools == [], "an empty allow list must not be pushed"
    assert not out.tools_set


@pytest.mark.asyncio
async def test_a_deny_list_without_an_allow_list_is_refused():
    """It would read as 'everything, minus these' -- the opposite of a
    narrowing."""
    gw = FakeGateway()
    out = await apply_to_agent(
        gw, _agent(), _desired(tools_allow=[], tools_deny=["bash"]))
    assert gw.tools == []
    assert any("deny" in f for f in out.failures)


@pytest.mark.asyncio
async def test_the_applier_refuses_memory_even_if_handed_it():
    """Defence in depth. `render` filters first, so this can only happen
    through a renderer bug -- which is exactly when the last check
    matters. The gateway will NOT refuse it: its own allowlist permits
    memory files."""
    gw = FakeGateway()
    out = await apply_to_agent(
        gw, _agent(), _desired(files={"MEMORY.md": "wiped", "AGENTS.md": "a"}))
    written = [n for _, n in gw.files]
    assert "MEMORY.md" not in written, (
        "a refresh must never write memory, whatever it was handed")
    assert written == ["AGENTS.md"]
    assert out.refused


def test_the_report_refuses_to_recommend_a_restart_when_partial():
    partial = Outcome("asst-1", files=["AGENTS.md"], failures=["tools: nope"])
    text = _applied_report([partial])
    assert "DO NOT RESTART YET" in text
    assert "NEXT: restart the gateway" not in text
    assert "asst-1" in text, "the operator has to know which one"


def test_the_report_recommends_a_restart_when_clean():
    ok = Outcome("asst-1", files=["AGENTS.md"], tools_set=True)
    text = _applied_report([ok])
    assert "NEXT: restart the gateway" in text
    assert "DO NOT RESTART" not in text


def test_a_clean_failure_still_allows_a_restart():
    """Stale is not dangerous. Blocking here would train people to
    ignore the warning that matters."""
    dead = Outcome("asst-1", failures=["file AGENTS.md: unreachable"])
    text = _applied_report([dead])
    assert "NEXT: restart the gateway" in text
    assert "DO NOT RESTART" not in text


def test_the_restart_advice_names_the_vm_recreation_risk():
    """REWRITTEN FOR THE PATH ACTUALLY USED.

    This asserted on snapshot-and-graceful-stop advice, which is right
    for a `systemctl restart` nobody here does. The real path is the
    openclaw gateway deploy, and that pipeline opens with terraform,
    whose own comment says it RECREATES THE VM if `startup.sh.tpl`
    changed. A recreated VM takes /home/openclaw/.openclaw with it --
    every agent's memory and every transcript, unrecoverable.

    That is the one hazard the operator cannot see from the button, so
    the report has to name it.
    """
    text = _applied_report([Outcome("asst-1", files=["AGENTS.md"])])
    assert "RECREATES THE VM" in text
    assert "startup.sh.tpl" in text
    assert "MEMORY.md" in text
