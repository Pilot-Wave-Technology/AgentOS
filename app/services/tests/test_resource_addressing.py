"""A resource can be named, and a miss says what there was.

NCAL1 had both of these registered and visible on the Resources page:

    0636e8a5-41b  repo    multi_agent_dummy_repo
    ecd0c318-86e  branch  multi_agent_dummy_repo:cal_kernel

GO asked resource_brief for 'multi_agent_dummy_repo' -- the name, which
is what the board, the brief and the person all use -- and was told
"'multi_agent_dummy_repo' is not a resource of NCAL1 (candidates: [])".
Both halves were wrong: it IS a resource, and the candidates were one
query away.
"""
import pytest

from kernel.assistant.context import Refused
from kernel.go.resource_ref import resolve_resource_id

ROWS = [
    {"id": "0636e8a5-41b", "name": "multi_agent_dummy_repo", "kind": "repo"},
    {"id": "ecd0c318-86e", "name": "multi_agent_dummy_repo:cal_kernel",
     "kind": "branch"},
]


class _Conn:
    """Answers the two queries resolve_resource_id asks, over ROWS."""

    def __init__(self, rows=ROWS):
        self.rows = rows

    async def fetch(self, sql, goal_id, ref=None):
        if ref is None:                       # the catalogue query
            return list(self.rows)
        low = ref.lower()
        hit = [r for r in self.rows
               if r["id"] == ref or r["name"].lower() == low
               or r["id"].startswith(ref)]
        hit.sort(key=lambda r: (r["id"] != ref, r["name"].lower() != low))
        return hit


@pytest.mark.asyncio
async def test_the_name_resolves():
    assert await resolve_resource_id(_Conn(), "NCAL1",
                                     "multi_agent_dummy_repo") == "0636e8a5-41b"


@pytest.mark.asyncio
async def test_the_branch_name_resolves_to_the_branch():
    # Two names differing by a suffix must not collapse into one.
    assert await resolve_resource_id(
        _Conn(), "NCAL1", "multi_agent_dummy_repo:cal_kernel") == "ecd0c318-86e"


@pytest.mark.asyncio
async def test_an_exact_id_still_wins():
    assert await resolve_resource_id(_Conn(), "NCAL1",
                                     "ecd0c318-86e") == "ecd0c318-86e"


@pytest.mark.asyncio
async def test_case_does_not_matter_for_a_name():
    assert await resolve_resource_id(_Conn(), "NCAL1",
                                     "Multi_Agent_Dummy_Repo") == "0636e8a5-41b"


@pytest.mark.asyncio
async def test_a_miss_names_the_catalogue():
    out = await resolve_resource_id(_Conn(), "NCAL1", "nope")
    assert isinstance(out, Refused)
    assert out.code == "E_NO_SUCH_RESOURCE"
    # The exact failure that cost a round trip: candidates was empty.
    assert out.candidates
    assert any("multi_agent_dummy_repo" in c for c in out.candidates)
    assert any("0636e8a5-41b" in c for c in out.candidates)


@pytest.mark.asyncio
async def test_an_ambiguous_prefix_is_asked_about_not_guessed():
    # Two ids sharing a prefix: picking one is a coin flip whose loss is
    # silent. kernel/addressing.py makes the same call about stories.
    rows = [{"id": "abc111", "name": "one", "kind": "repo"},
            {"id": "abc222", "name": "two", "kind": "repo"}]
    out = await resolve_resource_id(_Conn(rows), "NCAL1", "abc")
    assert isinstance(out, Refused)
    assert len(out.candidates) == 2


@pytest.mark.asyncio
async def test_an_empty_reference_refuses_with_the_catalogue():
    out = await resolve_resource_id(_Conn(), "NCAL1", "  ")
    assert isinstance(out, Refused)
    assert len(out.candidates) == 2


@pytest.mark.asyncio
async def test_read_resource_keeps_its_own_refusal_code():
    # A model already reads E_NO_RESOURCE from read_resource; moving the
    # lookup must not rename it.
    out = await resolve_resource_id(_Conn(), "NCAL1", "nope",
                                    code="E_NO_RESOURCE")
    assert out.code == "E_NO_RESOURCE"
