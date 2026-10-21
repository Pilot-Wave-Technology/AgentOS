"""Phase 1 · a surface that exists and cannot be reached is not a surface.

Three regressions, one shape. Ec2g built `list_artifacts`, Ec3b built the
`permission` question kind, and both shipped with one declaration missing —
so the tool was denied at the gateway and the card had no buttons. This is
the N-declarations bug, committed in the stage whose stated theme is the
N-declarations bug.

WHY THESE TESTS AND NOT ONLY THE GATE. `test_h8b_prompt.py` already carried
two assertions that catch the `list_artifacts` half, and both were red for
weeks. Nobody saw them because it read `/app/kernel/assistant/SOUL.md` — a
path that exists in one container target — which is the same defect
`test_effects_cover_profiles.py` had before Ec0b. The gate is repo-relative
now, and these run in the ordinary suite so a laptop sees them too. Two
doors on one rule, deliberately.
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION, seed_template

ROOT = pathlib.Path(__file__).resolve().parents[3]
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _prompts() -> str:
    return "\n".join(
        io.open(ROOT / "kernel" / "assistant" / name, encoding="utf-8").read()
        for name in ("SOUL.md", "AGENTS.md"))


# ── I1b · the gate can run at all ────────────────────────────────────────

def test_the_prompt_gate_is_not_pinned_to_a_container_path():
    """THE REASON THE OTHER TWO SHIPPED BROKEN. Correct logic behind a
    path only one target satisfies is logic that never runs, and Ec0b
    already learned this once on a different file."""
    src = io.open(ROOT / "infra/e0/ci/test_h8b_prompt.py",
                  encoding="utf-8").read()
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert '"/app"' not in code and "'/app'" not in code, (
        "the gate is pinned to a container path again")
    assert "os.path.abspath(__file__)" in code, (
        "the gate no longer resolves the repo from its own location")


# ── I1a · list_artifacts is reachable ────────────────────────────────────

def test_every_assistant_tool_is_named_in_the_prompt():
    """A tool the model is never told about is a tool it never calls,
    whatever the router serves."""
    from kernel.assistant import TOOLS

    text = _prompts()
    missing = [t for t in TOOLS if f"`{t}`" not in text]
    assert not missing, f"served and undocumented: {missing}"


def test_list_artifacts_is_described_as_the_errand_answer():
    """Naming it is not enough. `read_artifact` needs an id, and for an
    errand there is nowhere else an id comes from — the run happened
    unwatched and its workspace is gone."""
    text = _prompts()
    # THE ROW *FOR* THE TOOL, not the first row that mentions it. Anchored
    # on the leading table cell: `read_artifact` and `read_resource` both
    # point at `list_artifacts` in their own descriptions — correctly, it
    # is where ids come from — so a substring match returned whichever
    # row happened to sit highest, and the answer moved every time the
    # table gained a row.
    row = next((l for l in text.splitlines()
                if l.lstrip().startswith("| `list_artifacts`")), "")
    assert row, "list_artifacts has no row of its own in the tool table"
    assert "errand" in row.lower(), (
        "the row does not say what it is for")
    for role in ("report", "dataset", "method", "log", "figure"):
        assert role in row, f"the row never mentions role {role!r}"


def test_the_migration_grants_it_and_is_in_the_sequence():
    """The other half. `story_progress` and `show_card` once shipped
    served, declared, routed — and denied by `tools_allow`."""
    if IS_DISTRIBUTION:
        # The grant ships as the seeded template row the gateway reads.
        allowed = seed_template("kernel-assistant")["tools_allow"]
        assert "list_artifacts" in allowed, (
            "kernel-assistant is seeded without list_artifacts")
        return
    sql = ROOT / "migrations/kernel_assistant_list_artifacts.sql"
    assert sql.is_file(), "no migration grants list_artifacts"
    body = io.open(sql, encoding="utf-8").read()
    assert "kernel-assistant" in body and "list_artifacts" in body
    seq = io.open(ROOT / "migrations/SEQUENCE", encoding="utf-8").read()
    assert "kernel_assistant_list_artifacts" in seq, "not in SEQUENCE"


@pytest.mark.skipif(not os.environ.get("DATABASE_URL")
                    and not os.path.exists(ROOT / "local-development"),
                    reason="no local stack")
def test_the_template_really_allows_it():
    """Against the database, because that is what the gateway reads."""
    asyncpg = pytest.importorskip("asyncpg")

    async def go():
        import json

        from kernel.assistant import TOOLS
        for dsn in (os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
            if not dsn:
                continue
            try:
                conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
            except Exception:                               # noqa: BLE001
                continue
            allowed = await conn.fetchval(
                "SELECT tools_allow FROM templates WHERE id='kernel-assistant'")
            await conn.close()
            if allowed is None:
                pytest.skip("no kernel-assistant template in this database")
            if isinstance(allowed, str):
                allowed = json.loads(allowed)
            denied = sorted(set(TOOLS) - set(allowed or []))
            assert not denied, (
                f"served by the kernel and forbidden by the template: "
                f"{denied}")
            return
        pytest.skip("no database reachable")
    asyncio.run(go())


# ── I1c · a permission park is answerable ────────────────────────────────

def _row(kind="permission", text="Approve this irreversible call?\n\n"
                                 "    terraform apply -auto-approve\n\n"
                                 "It changes real infrastructure."):
    return {"id": 1, "execution_id": 9, "goal_id": "g", "story_id": "s",
            "kind": kind, "question_text": text}


def test_a_permission_park_renders_buttons():
    """THE REGRESSION. `needs_human` was False, so `options` was empty and
    the client renders nothing at all — the run parks for ever with a
    person looking at it and nothing to click. Ec3a made every cloud CLI
    irreversible, so this is an errand's normal path."""
    from app.services.kernel_park_cards import HUMAN_ONLY_KINDS, build_card

    assert "permission" in HUMAN_ONLY_KINDS
    card = build_card(_row(), None, "")
    assert card["needs_human"] is True
    assert card["options"], "a card a person must answer has no buttons"
    values = {o["value"] for o in card["options"]}
    assert values == {"approve", "revise", "declined"}


def test_the_card_shows_the_command_and_asks_about_it():
    """"Approve plan" is the wrong question — there is no plan on this
    card, there is a command. A person cannot tell a typo from a
    production deletion without seeing it."""
    from app.services.kernel_park_cards import build_card

    card = build_card(_row(), None, "")
    assert "terraform apply -auto-approve" in card["body"]
    assert "plan" not in card["title"].lower(), card["title"]
    labels = " ".join(o["label"] for o in card["options"]).lower()
    assert "call" in labels or "allow" in labels
    assert "plan" not in labels, "the buttons still talk about a plan"


def test_free_text_rides_along_so_not_like_that_is_expressible():
    """"No, use --dry-run first" is neither an approval nor a refusal,
    and it is the most useful answer a person can give."""
    from app.services.kernel_park_cards import build_card

    assert build_card(_row(), None, "")["allow_custom"] is True


def test_the_other_kinds_are_untouched():
    """Every one of these is on the story path. Adding a fourth kind to
    the set must not have changed what the first three render."""
    from app.services.kernel_park_cards import build_card

    plan = build_card(_row("authorization", "approve the plan"), None, "")
    assert plan["title"] == "Plan approval"
    assert {o["value"] for o in plan["options"]} == {"approve", "revise",
                                                    "declined"}

    acc = build_card(_row("acceptance", "done?"), None, "")
    assert "done" in acc["title"].lower()
    assert [o["label"] for o in acc["options"]][0] == "✓ Mark story done"

    direction = build_card(_row("direction", "carry on?"), None, "")
    assert {o["value"] for o in direction["options"]} == {"approve", "revise"}

    clar = build_card(_row("clarification", "which database?"), None, "")
    assert clar["needs_human"] is False, (
        "a clarification is answerable by an assistant; making it "
        "human-only would park every ask_human on a person")
    # IT USED TO ASSERT `options == []`, and that empty list was the
    # bug. `needs_human is False` means the ASSISTANT MAY answer --
    # not that a person may not -- and with no option and no text box
    # a person who could see the question had no way to reply to it.
    # Watched on story dc11d7f2: the assistant answered with a `steer`
    # that a parked run can never apply, told the person to click the
    # card, and the card had nothing to click. The run was cancelled to
    # get out of it.
    #
    # So the property is now BOTH: still relayable by an assistant, and
    # answerable by a person.
    assert clar["options"], "a clarification must be answerable by a person"
    assert clar["allow_custom"] is True, "and a clarification is free text"


def test_every_kind_a_human_must_answer_has_a_title_of_its_own():
    """An escalation once arrived titled "Plan approval" — a person
    reading the title read the wrong question."""
    from app.services.kernel_park_cards import HUMAN_ONLY_KINDS, build_card

    seen = {}
    for kind in HUMAN_ONLY_KINDS:
        title = build_card(_row(kind, "the question text"), None, "")["title"]
        # NON-EMPTY, not merely different. `.get(kind, fallback)` returns
        # a registered empty string rather than the fallback, so a blank
        # title in the map passes "is not the question text" while
        # rendering a card with no heading at all.
        assert title.strip(), f"{kind} renders with a blank title"
        assert title != "the question text", f"{kind} has no title of its own"
        assert title not in seen, (
            f"{kind} and {seen[title]} share the title {title!r}")
        seen[title] = kind
