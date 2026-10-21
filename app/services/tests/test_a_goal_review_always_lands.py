"""Every goal review produces its findings, and never repeats itself.

TWO PROPERTIES, AND THEY PULL AGAINST EACH OTHER. A review must always
arrive -- a cycle that delivers nothing teaches the orchestrator that
reviews are optional, after which the one that mattered gets skimmed
too. But a review that arrives every twelve hours saying what it said
last time teaches exactly the same lesson by a different route.

So the file is in two halves. The first is the delivery floor: whatever
fails, a packet reaches the GO. The second is the ledger: a finding has
an identity, so the packet can carry what is NEW.

The layers that make delivery unconditional:

    D1  each stage's findings land the moment it finishes
    D2  a failed upstream renders as a stated gap, never as silence
    D3  the convener runs even when every upstream failed
    D4  no convener -> the packet is drawn from the ledger in code
    D5  a wedged run is swept, delivered, and its reviewers reaped

D5 exists because the global single-run gate means one wedged run stops
every OTHER goal being reviewed.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.services.tests.shipped_tree import (IS_DISTRIBUTION, seed_model,
                                             seed_template, seed_text)
from app.services.tests.test_a_story_is_always_reviewed import code_only


# ── the ledger: identity, and what it refuses to infer ──────────────────

def test_a_key_seen_twice_is_one_finding():
    """`ON CONFLICT (goal_id, key)` — the row is updated, never doubled."""
    from app.services.council.findings import store

    sql = code_only(inspect.getsource(store.record))
    assert "ON CONFLICT (goal_id, key) DO UPDATE" in sql
    assert "times_seen  = council_findings.times_seen" in sql


def test_the_same_run_does_not_inflate_the_count():
    """Two stages raising one key in one run is one recurrence, not two.

    `times_seen` is the escalation ladder. If `story` and `goal_health`
    both notice the same thing in the same cycle, that is one cycle's
    worth of evidence -- counting it twice would escalate on the first
    run and make the number mean nothing.
    """
    from app.services.council.findings import store

    sql = code_only(inspect.getsource(store.record))
    assert "CASE WHEN council_findings.last_run_id" in sql
    assert "THEN 0 ELSE 1 END" in sql


def test_a_finding_nobody_mentioned_stays_open():
    """Silence is "did not look", which is not "fixed"."""
    from app.services.council.findings import store

    src = code_only(inspect.getsource(store.apply_accounting))
    # It closes only what it was handed, and only what was handed a
    # terminal verdict.
    assert "for key, (status, detail) in verdicts.items()" in src
    assert 'if status == "open":' in src
    assert "continue" in src
    # And it never sweeps the rest.
    assert "status = 'resolved'" not in src.replace(" ", "")


def test_the_parser_drops_what_it_cannot_key():
    """A mangled key is worse than a missing finding: it never matches
    again, so it reappears as new every cycle and the count becomes
    noise."""
    from app.services.council.findings import parse

    text = "## Findings\n- no key here at all\n- [good-key] (high, goal) a — b\n"
    got = parse.findings(text, raised_by="test")
    assert [f["key"] for f in got] == ["good-key"]


def test_one_review_saying_it_twice_is_one_finding():
    from app.services.council.findings import parse

    text = ("## Findings\n"
            "- [dup] (high, goal) first phrasing — why\n"
            "- [dup] (low, goal) second phrasing — why\n")
    assert len(parse.findings(text, raised_by="test")) == 1


def test_accounting_reads_all_three_verdicts():
    from app.services.council.findings import parse

    text = ("## Previously open\n"
            "- [a] still open — nothing changed\n"
            "- [b] resolved — landed on cycle 14\n"
            "- [c] no longer applies — module deleted\n"
            "- [d] (no verdict word in this line)\n")
    got = parse.accounted(text)
    assert got["a"][0] == "open"
    assert got["b"][0] == "resolved"
    assert got["c"][0] == "wont_fix"
    assert "d" not in got, "a line with no verdict must not close anything"


# ── delivery: the floor under every cycle ───────────────────────────────

def test_a_failed_stage_is_a_stated_gap_not_an_empty_file():
    """D2. An empty input reads as "there was nothing"; that is how the
    gateway reviewer came to judge a description against a plan it never
    received."""
    from app.services.council.inputs import stage_output

    gap = stage_output._gap("Progress", "status `failed`", "the agent timed out")
    assert "NOT AVAILABLE" in gap
    assert "coverage gap" in gap
    assert gap.strip(), "a gap must never render as an empty document"


def test_findings_land_before_the_convener_can_crash():
    """D1. Ingest is per-stage, so a convener crash cannot lose them."""
    from app.services.council import dispatcher

    src = code_only(inspect.getsource(dispatcher.CouncilDispatcher._stage))
    assert "_ingest" in src, "a stage must fold its findings in as it finishes"


def test_the_convener_runs_even_when_everything_upstream_failed():
    """D3."""
    from app.services.council import dispatcher

    src = code_only(inspect.getsource(dispatcher.CouncilDispatcher._process_run))
    assert "status='pending'" in src and "blocked_on_deps" in src


def test_no_convener_still_delivers_a_packet():
    """D4. The ledger already holds what the stages found."""
    from app.services.council import dispatcher

    src = code_only(inspect.getsource(dispatcher.CouncilDispatcher._process_run))
    assert src.count("deliver_ledger_packet") >= 2, (
        "the ledger packet must cover BOTH the convener-failed path and the "
        "driver-crashed path -- a crash is exactly when a goal most needs "
        "its review")


def test_the_run_is_done_when_a_packet_landed_not_when_agents_behaved():
    from app.services.council import dispatcher

    src = code_only(inspect.getsource(dispatcher.CouncilDispatcher._process_run))
    assert 'status = "done" if delivered else "failed"' in src


def test_an_empty_ledger_does_not_claim_the_goal_is_healthy():
    """Nothing found because every reviewer failed is not good news."""
    from app.services.council.findings import render

    p = render.packet("G1", [], ["goal_health", "progress", "story"])
    assert "not a statement that the goal is healthy" in p
    assert "goal_health" in p, "the packet must name which stages failed"


def test_an_empty_ledger_does_not_demand_a_reply():
    """Waking the GO for "nothing was found and the reviewers failed"
    trains it to answer reviews with noise."""
    from app.services.council import deliver

    src = code_only(inspect.getsource(deliver.deliver_ledger_packet))
    assert "wake=bool(open_items)" in src


def test_the_packet_separates_new_from_escalating():
    from app.services.council.findings import render

    items = [
        {"key": "fresh", "severity": "high", "title": "t", "times_seen": 1,
         "story_id": None},
        {"key": "old", "severity": "low", "title": "t", "times_seen": 5,
         "story_id": None},
    ]
    p = render.packet("G1", items, [])
    assert p.index("## New this cycle") < p.index("## Escalating")
    assert "[fresh]" in p.split("## Escalating")[0]
    assert "[old]" in p.split("## Escalating")[1]


def test_an_unanswered_go_response_escalates_at_any_age():
    """A GO answer with the finding still open means it did not land."""
    from app.services.council.findings import render

    new, esc, rest = render._bucket(
        [{"key": "k", "times_seen": 2, "go_said": "proposed a story"}])
    assert [f["key"] for f in esc] == ["k"]


# ── the wedge, and the slot it holds ────────────────────────────────────

def test_a_wedged_run_delivers_before_it_closes():
    """D5. Freeing the slot without delivering satisfies the index and
    fails the requirement."""
    from app.services.council import deadline

    src = code_only(inspect.getsource(deadline.sweep))
    assert src.index("deliver_ledger_packet(goal_id, run_id)") < src.index(
        "UPDATE council_review_runs SET status = 'failed'")


def test_a_wedged_run_reaps_its_reviewers():
    """`run_stage` deletes its RA in a `finally`, which does not run when
    the process holding it died."""
    from app.services.council import deadline

    assert "delete_agent" in code_only(inspect.getsource(deadline._reap_agents))


def test_already_gone_is_not_a_cleanup_failure():
    from app.services.council import deadline, provisioning

    for fn in (code_only(inspect.getsource(deadline._reap_agents)),
               code_only(inspect.getsource(provisioning.run_stage))):
        assert '"not found"' in fn


def test_the_deadline_is_shorter_than_the_cycle():
    """A wedge must not survive to block the next scheduled run."""
    from app.services.council import deadline

    assert deadline.DEFAULT_MINUTES < 12 * 60


# ── the prompts, against the tools that actually exist ──────────────────

_MIGRATION = "migrations/council_v2_agent_files.sql"
_STAGES = ("ra-progress", "ra-story", "ra-goal-health", "ra-convener")


def _migration() -> str:
    with open(_MIGRATION, encoding="utf-8") as fh:
        return fh.read()


def _prompt(stage: str) -> str:
    # The distribution has no migrations to read; the prompt it installs
    # is the seeded template file, which is what the reviewer is given.
    if IS_DISTRIBUTION:
        return seed_text(f"templates/{stage}/AGENTS.md")
    body = _migration().split(f"('{stage}', 'AGENTS.md', $ra$", 1)[1]
    return body.split("$ra$", 1)[0]


@pytest.mark.parametrize("stage", _STAGES)
def test_the_prompt_names_no_tool_the_stage_does_not_have(stage):
    """The check that caught `story_diff` still being in a prompt after
    the tool was removed."""
    granted = {"read", "submit_review"}
    called = set(re.findall(r"`([a-z_]{4,})`", _prompt(stage)))
    known_tools = {"glob", "grep", "goal_knowledge", "web_search", "web_fetch",
                   "story_diff", "write", "bash", "artifact_read",
                   "create_story", "record_finding"}
    assert not (called & known_tools - granted), (
        f"{stage} names a tool it was not granted")


@pytest.mark.parametrize("stage", _STAGES)
def test_every_required_heading_is_owned_by_a_step(stage):
    """A heading the prompt never mentions is one Flash leaves out."""
    from app.services.council.validators import REQUIRED_SECTIONS

    key = stage.replace("ra-", "").replace("-", "_")
    prompt = _prompt(stage)
    for heading in REQUIRED_SECTIONS[key]:
        assert heading in prompt, f"{stage} never names {heading!r}"


@pytest.mark.parametrize("stage", _STAGES)
def test_every_prompt_ends_on_the_finish_tool(stage):
    """Flash ends turns without calling it, so the instruction is last
    and unmissable. The LAST step must be the submit step -- not merely
    a mention of the tool somewhere in the middle."""
    p = _prompt(stage)
    # Whitespace-normalised: the sentence line-wraps in three of the four
    # prompts, and a test that fails on where a paragraph broke is
    # checking the formatter, not the instruction.
    flat = " ".join(p.split())
    assert "Never end your turn without calling it" in flat
    last_heading = [ln for ln in p.splitlines() if ln.startswith("## ")][-1]
    assert "submit" in last_heading.lower(), (
        f"{stage} ends on {last_heading!r}, not on the finish step")


@pytest.mark.parametrize("stage", ("ra-progress", "ra-story", "ra-goal-health"))
def test_every_raising_prompt_teaches_key_reuse(stage):
    """The three stages that RAISE findings must be told to reuse a key."""
    p = _prompt(stage).lower()
    assert "open_findings.md" in p
    assert "reuse" in p


def test_the_convener_carries_keys_through():
    """It reads keys rather than minting them, so its rule is different:
    the key must survive into the packet, or the GO cannot cite it and
    the response never attaches to the finding."""
    p = _prompt("ra-convener")
    assert "docs/OPEN_FINDINGS.md" in p
    assert "keeping the key" in p
    assert "(key: kebab-key)" in p


def test_the_council_runs_on_gemini_38_at_a_million():
    if IS_DISTRIBUTION:
        for stage in _STAGES:
            model = seed_template(stage)["model"]
            assert model == "google-flash38-1m/gemini-3.8-flash-1m", (
                f"{stage} is seeded on {model!r}")
        return
    sql = _migration()
    assert "google-flash38-1m/gemini-3.8-flash-1m" in sql
    assert "gemini-2.5" not in sql, "2.5-era models are retired"


def test_the_whole_38_series_is_registered():
    if IS_DISTRIBUTION:
        import json
        # The windows the PRODUCT reads are model_context_windows, not the
        # legacy models column. The distribution chooses its own set; what
        # must hold is that the windows its agents run on are there: 500K
        # (GO and the assistant) and 1M (this council).
        seed_model("google/gemini-3.8-flash")
        windows = {w["tokens"] for w in json.loads(seed_text("model_context_windows.json"))
                   if w["model_id"] == "google/gemini-3.8-flash"}
        for tokens in (500000, 1000000):
            assert tokens in windows, f"gemini-3.8-flash has no {tokens} window"
        owners = [m["id"] for m in json.loads(seed_text("models.json"))
                  if m.get("provider_shorthand") == "flash38"]
        assert owners == ["google/gemini-3.8-flash"], (
            "a new shorthand, not a repoint of an existing one")
        return
    with open("migrations/models_add_gemini_38_series.sql", encoding="utf-8") as fh:
        sql = fh.read()
    for tokens in ("50000", "100000", "200000", "500000", "1000000"):
        assert tokens in sql
    assert "'flash38'" in sql, "a new shorthand, not a repoint of an existing one"


# ── what the stages must never do again ─────────────────────────────────

def test_no_council_input_reads_a_gateway_workspace():
    """The bug this rebuild exists to remove: a pod-executed story has no
    gateway workspace, so those reads returned empty and the reviewer
    judged against nothing."""
    import app.services.council.inputs.compose as compose
    import app.services.council.inputs.story_state as story_state

    for mod in (compose, story_state):
        src = code_only(inspect.getsource(mod))
        assert "shca_instance_id" not in src
        assert "agents.files.get" not in src


def test_decisions_come_from_plans_not_the_empty_table():
    """Measured on CAL05: `goal_decisions` zero live rows, two approved
    plans."""
    from app.services.council.inputs import plans

    src = code_only(inspect.getsource(plans))
    assert "kernel.questions" in src
    assert "goal_decisions" not in src


def test_result_summary_is_not_clipped():
    """CAL05: median 528 chars, max 646. Clipping bought nothing and cost
    the tail of the executor's own account."""
    from app.services.council.inputs import story_state

    assert story_state._MAX_SUMMARY >= 20000


def test_one_story_stage_per_run():
    from app.services.council import runs

    src = code_only(inspect.getsource(runs.enqueue_run))
    # Exactly one INSERT of a story stage. ('story' also appears in the
    # convener's depends_on array, which is correct and unrelated.)
    assert src.count("'story', 'pending'") == 1
    assert "for s in eligible" not in src, "the per-story fan-out is gone"


def test_counts_come_from_sql():
    from app.services.council.inputs import story_state

    src = code_only(inspect.getsource(story_state.ledger))
    assert "count(*)" in src and "GROUP BY" in src


# ── the GO's half of the loop ───────────────────────────────────────────

def test_the_go_reply_attaches_to_the_finding_it_cites():
    from app.services import go_response

    keys = {m.group(1) for m in go_response._FINDING_KEY_RE.finditer(
        "1. [story] (key: drift-billing) — do it — why\n"
        "2. [errand] (key: —) — no finding\n")}
    assert keys == {"drift-billing"}


def test_the_skill_forbids_acting_from_a_review():
    if IS_DISTRIBUTION:
        # The seeded skill's body, after its frontmatter.
        skill = seed_text("skills/review_action_planning.md").split(
            "\n---\n", 1)[1]
    else:
        with open("migrations/review_action_planning_v3.sql",
                  encoding="utf-8") as fh:
            skill = fh.read().split("$skill$")[1]
    assert "Never call `create_story`" in skill
    assert "not before approval, and not after it" in skill
    assert "maximum 5" in skill
    assert "Taking no action is a correct and complete answer" in skill


# ── what the first live run taught ──────────────────────────────────────

def test_engine_git_stories_are_not_asked_for_paperwork():
    """THE FIRST LIVE RUN RAISED A FALSE POSITIVE, and the reviewer was
    not at fault.

    `merge-unrecorded-and-unreviewed` was raised against a merge story
    for having no result summary, no artifacts and no verdict -- which
    is exactly what the prompt says to flag. But engine git stories
    (merge / diff / fix) are excluded from review EVERYWHERE else in the
    system precisely because reviewing them is meaningless, and they
    record none of that by design. Presenting them like goal work
    manufactured a finding that would have recurred every twelve hours
    for the life of the goal -- the exact noise the ledger exists to
    prevent, generated by the ledger's own supplier.
    """
    import inspect

    from app.services.council.inputs import story_state

    src = code_only(inspect.getsource(story_state))
    # It reads all three columns the shared predicate needs -- the
    # version of that bug which mattered was a caller checking only
    # `created_by`.
    for col in ("created_by", "story_kind", "merge_group_id"):
        assert col in src, f"{col} must be selected for is_engine_git_story"
    assert "is_engine_git_story" in src, "use the ONE shared definition"


def test_an_engine_story_is_never_called_unreviewed():
    """Three places in one row could contradict the label, so this
    renders a real merge row and reads what it actually says."""
    from app.services.council.inputs import story_state

    row = {
        "id": "MERGE-07D041C6D89E", "title": "merge lane", "description": "",
        "acceptance_criteria": "", "state": "reviewed", "shape": "story",
        "result_summary": None, "created_by": "engine", "story_kind": "merge",
        "merge_group_id": "g1", "verdict": None, "has_open_finding": False,
    }
    text = chr(10).join(story_state._one(row))

    assert "engine plumbing" in text
    assert "Do not raise a finding about its missing paperwork" in text
    assert "worth flagging" not in text
    assert "unreviewed" not in text
    assert "none recorded" not in text


def test_a_real_story_with_no_summary_is_still_flagged():
    """The engine exemption must not swallow the genuine case."""
    from app.services.council.inputs import story_state

    row = {
        "id": "AB12", "title": "a real story", "description": "d",
        "acceptance_criteria": "", "state": "done", "shape": "story",
        "result_summary": None, "created_by": "abhishek", "story_kind": None,
        "merge_group_id": None, "verdict": None, "has_open_finding": False,
    }
    text = chr(10).join(story_state._one(row))
    assert "worth flagging" in text
    assert "unreviewed" in text
