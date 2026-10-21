"""Every execution in the system ran `small`, and nothing could ask.

THE DEFECT WAS AN ABSENT COLUMN, NOT ABSENT CODE. The ladder
(`kernel/sizes.py`), the resolver (`resolve_size`), the nodeSelector and
the node pools all existed and were correct. `contract_config.py` reads
`contract_row["size"]` and `spec_builder.py` resolves it -- against a
column that **did not exist in the schema**. No `profile.toml` set
`size` either. So the size branch was dead code reachable only from
tests, and a monorepo whose suite needs 8 GiB died on 9 GiB of
`small` while a working feature sat one line away.

`sandbox_size_choice.sql` alone makes `spec_builder` live with no code
change. The rest is how a person says which.

SHAPED AS `executor_model`, and the tests below check that likeness
deliberately: it solves the identical problem -- a per-story choice
inheriting from the goal, frozen at dispatch -- and two shapes for one
question drift the first time either grows a case.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from kernel.conductor import size_choice


# ── the ladder is the only list of rungs ──────────────────────────────

def test_a_known_rung_is_accepted_and_normalised():
    assert size_choice.valid("MEDIUM ") == "medium"


def test_nothing_chosen_is_None_and_not_small():
    """"Nobody chose" is a DIFFERENT fact from "small", and the
    difference is load-bearing: a profile naming its own size must beat
    a default nobody picked and lose to a story somebody did pick."""
    assert size_choice.valid(None) is None
    assert size_choice.valid("   ") is None


def test_an_unknown_word_is_refused_with_the_ladder_named():
    """Not rounded down. A story that quietly ran `small` when somebody
    asked for `large` is a story that looks merely slow."""
    with pytest.raises(ValueError) as e:
        size_choice.valid("enormous")
    assert "small" in str(e.value) and "large" in str(e.value)


def test_the_rung_names_come_from_kernel_sizes():
    """`xlarge` was a real rung and was removed when it named a fourth
    tier with no fourth pool. A second copy of the list would still
    accept it."""
    src = inspect.getsource(size_choice)
    assert "from kernel.sizes import SIZES" in src
    assert '"medium"' not in src.split('"""')[2] if '"""' in src else True


# ── precedence ────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, story=None, goal=None, story_id="s1"):
        self.story, self.goal, self.story_id = story, goal, story_id
        self.updates: list = []

    async def fetchval(self, sql, *a):
        if "FROM stories" in sql:
            return self.story
        if "FROM goals" in sql:
            return self.goal
        if "story_id FROM executions" in sql:
            return self.story_id
        return None

    async def execute(self, sql, *a):
        self.updates.append((sql, a))


def _chosen(**kw):
    c = _Conn(**kw)
    return asyncio.run(size_choice.chosen_for_story(c, "s1", "KER12")), c


def test_the_story_beats_the_goal():
    got, _ = _chosen(story="large", goal="medium")
    assert got == "large"


def test_the_goal_applies_when_the_story_says_nothing():
    got, _ = _chosen(story=None, goal="medium")
    assert got == "medium"


def test_nobody_choosing_falls_through_to_the_profile():
    got, _ = _chosen()
    assert got is None


def test_an_errand_with_no_story_still_inherits_the_goal():
    """An errand has a goal and no story, and "this goal's work" is what
    a person would expect that setting to mean."""
    c = _Conn(goal="medium")
    got = asyncio.run(size_choice.chosen_for_story(c, None, "KER12"))
    assert got == "medium"


def test_a_retired_rung_in_the_column_does_not_stop_the_run():
    """Validation belongs at the WRITE. A column that somehow holds a
    rung that has since been removed must fall through to the profile,
    not refuse to dispatch."""
    got, _ = _chosen(story="xlarge")
    assert got is None


# ── frozen at dispatch, like the model ────────────────────────────────

def test_the_rung_is_pinned_only_while_unset():
    c = _Conn()
    asyncio.run(size_choice.freeze(c, 7, "large"))
    assert c.updates, "nothing was pinned"
    sql = c.updates[0][0]
    assert "sandbox_size IS NULL" in sql, (
        "a second dispatch pass could rewrite a running story's machine")


def test_nothing_chosen_pins_nothing():
    """The column means "this run was pinned to a rung". Freezing a
    resolved default would make every run look deliberately configured
    and would override a profile's own size."""
    c = _Conn()
    asyncio.run(size_choice.freeze(c, 7, None))
    assert not c.updates


def test_a_run_with_no_story_pins_nothing():
    """An errand re-resolves from the goal next time, which is correct
    because the goal is the only thing it could have inherited from."""
    c = _Conn(story_id=None)
    asyncio.run(size_choice.freeze(c, 7, "large"))
    assert not c.updates


# ── the wiring, end to end ────────────────────────────────────────────

def test_the_builder_puts_the_choice_between_contract_and_profile():
    from kernel.conductor import spec_builder
    src = inspect.getsource(spec_builder.build_spec)
    assert 'resolve_size(overrides.get("size") or size_choice or profile.size)' \
        in src, "the precedence contract -> story/goal -> profile is broken"


def test_dispatch_resolves_before_it_builds_and_pins_after():
    from kernel.conductor import sandbox_spec
    src = inspect.getsource(sandbox_spec.spec_for_dispatch)
    assert "size_choice=size_choice" in src
    assert src.index("chosen_for_story(\n            conn") < \
        src.index("size_choice=size_choice"), \
        "the spec is built before the size is known"
    assert "_sz.freeze(conn" in src


def test_a_settings_lookup_cannot_take_the_dispatch_down():
    """Refusing to dispatch over a preference turns it into an outage.
    The model block one comment down says the same and is the
    precedent."""
    from kernel.conductor import sandbox_spec
    src = inspect.getsource(sandbox_spec.spec_for_dispatch)
    block = src.split("size_choice = None", 1)[1].split("spec = build_spec")[0]
    assert "except Exception" in block


# ── a person can change it, until they cannot ─────────────────────────

def test_the_override_is_locked_only_while_a_run_holds_it():
    """IT WAS A STATE WHITELIST, copied from `story_executor_model`, and
    it refused the one case people hit: cancel a run that died on
    `small`, try `medium`, and by then the story is not `accepted`. The
    rule is "nothing in flight", which covers proposed and accepted as
    before and covers the retry too."""
    from app.routers import stories
    src = inspect.getsource(stories.story_sandbox_size)
    assert "locked_reason" in src
    assert '("proposed", "accepted")' not in src
    assert "get_goal_role" in src


def test_blank_means_inherit_rather_than_an_empty_rung():
    from app.routers import stories
    src = inspect.getsource(stories.story_sandbox_size)
    assert 'or None' in src


def test_the_override_refuses_an_unknown_rung_rather_than_storing_it():
    from app.routers import stories
    src = inspect.getsource(stories.story_sandbox_size)
    assert "except ValueError" in src and "status_code=400" in src


# ── the GO can suggest one ────────────────────────────────────────────

def test_create_story_carries_the_rung():
    from app.services import go_response
    src = inspect.getsource(go_response)
    assert "sandbox_size" in src
    assert "work_kind, sandbox_size)" in src, (
        "the column is parsed and never written, which is the exact "
        "shape of the F8 bug it is modelled on")


def test_a_bad_rung_from_the_model_becomes_nobody_chose():
    """Rather than a 500 at dispatch. NULL means story-as-usual, which
    is what every row predating the column does."""
    from app.services import go_response
    src = inspect.getsource(go_response)
    assert "except ValueError:" in src
    assert "sandbox_size = None" in src


def test_the_gateway_declares_it_so_the_model_can_send_it():
    """F8 and G5 were both "a field the prompt teaches and the tool
    cannot carry". This is the third instance of that shape and the
    check that stops a fourth."""
    from app.services.gateway_surface import specs_dir
    d = specs_dir()
    if d is None or not (d / "story-tools.ts").exists():
        pytest.skip("the gateway checkout is not beside this one")
    body = (d / "story-tools.ts").read_text(encoding="utf-8")
    assert "sandbox_size: Type.Optional(" in body
    assert "cheapest" in body, "the model is not told a tier costs money"


# ── the ceiling, which stays tier-blind on purpose ────────────────────

def test_the_ceiling_is_not_made_tier_aware():
    """The plan asked whether `SYSTEM_CEILING` should be weighted before
    `large` is offered. It should not, and
    `test_the_ceiling_is_priced_against_the_real_quota` already argues
    why: a story occupies a WHOLE machine, `gke_standard_pools.sh` caps
    each tier's node count, so the pools are the capacity control and
    the ceiling is a backstop against a bug. Sixteen large stories is
    not a load to protect against, it is a load the cluster cannot
    express."""
    from kernel.conductor.admit import SYSTEM_CEILING
    assert isinstance(SYSTEM_CEILING, int)
    src = inspect.getsource(__import__("kernel.conductor.admit",
                                       fromlist=["admit"]))
    assert "TIER IS THE VARIABLE" in src


def test_every_rung_still_has_a_pool_to_land_on():
    """A `medium` story with no medium pool produces a pod that stays
    Pending for ever, because the nodeSelector matches nothing."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    pools = root / "scripts/infra/gke_standard_pools.sh"
    if not pools.exists():                              # pragma: no cover
        pytest.skip("the infra scripts are not in this checkout")
    from kernel.sizes import SIZES
    found = {m.group(1) for m in re.finditer(
        r"^pool\s+(\w+)\s+", pools.read_text(encoding="utf8"), re.M)}
    found.discard("platform")
    assert set(SIZES) <= found, f"tiers with no pool: {set(SIZES) - found}"


# ── revising it, which is the case that actually comes up ─────────────

def test_the_go_can_change_the_rung_before_it_runs():
    """You learn a suite needs a bigger machine when the first run dies
    on the small one. Without this the only fix was to cancel the story
    and write a new one."""
    from app.services import go_response
    src = inspect.getsource(go_response._action_update_story)
    assert '"sandbox_size" in params' in src
    assert "sandbox_size = $" in src


def test_and_is_refused_while_a_run_holds_it():
    """The pod is built from the rung, so it cannot change under a
    running leg -- and a form that looks like it worked and changes
    nothing is worse than a refusal."""
    from app.services import go_response
    src = inspect.getsource(go_response._action_update_story)
    assert "locked_reason" in src
    assert '("proposed", "accepted")' not in src


def test_an_unknown_rung_from_update_is_reported_not_stored():
    from app.services import go_response
    src = inspect.getsource(go_response._action_update_story)
    assert "except ValueError as exc" in src
    assert 'return {"error": str(exc)}' in src


def test_the_change_is_recorded_in_memory_like_the_others():
    """A silent size change is the one nobody can explain later."""
    from app.services import go_response
    src = inspect.getsource(go_response._action_update_story)
    tail = src.split("changed_fields", 1)[1]
    assert '"sandbox_size"' in tail


def test_the_gateway_lets_update_story_carry_it():
    from app.services.gateway_surface import specs_dir
    d = specs_dir()
    if d is None or not (d / "story-tools.ts").exists():
        pytest.skip("the gateway checkout is not beside this one")
    body = (d / "story-tools.ts").read_text(encoding="utf-8")
    # create_story REQUIRES it; update_story keeps it optional, because
    # an update names only what it is changing.
    assert "sandbox_size: Type.String({" in body, "create_story"
    assert "sandbox_size: Type.Optional(" in body, "update_story"


# ── the goal level, which is an ERRAND'S ONLY ROUTE ───────────────────

def test_the_goal_can_set_a_machine_too():
    """WITHOUT THIS AN ERRAND CAN ONLY EVER BE `small`. A story is
    created `proposed` and can be re-sized on its own page before it
    runs; an errand's story is created AT APPROVAL and dispatched
    immediately, so there is no window in which to edit it. The goal is
    the only place its rung can come from -- and `goals.sandbox_size`
    existed with nothing writing it, which is the same defect one level
    up that `contracts.size` was."""
    from app.routers import goals
    src = inspect.getsource(goals.change_kernel_models)
    assert "sandbox_size = $4" in src
    assert "valid_size" in src


def test_it_is_validated_like_the_models_beside_it():
    from app.routers import goals
    src = inspect.getsource(goals.change_kernel_models)
    assert "except ValueError as exc" in src
    assert "status_code=400" in src


def test_blank_is_inherit_at_the_goal_level_too():
    from app.routers import goals
    src = inspect.getsource(goals.change_kernel_models)
    assert '(form.get("sandbox_size") or "").strip() or None' in src


def test_the_form_offers_it_and_prices_it():
    """A dropdown with no cost attached is one somebody sets to `large`
    because bigger sounds safer."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    body = (root / "app/templates/goals/model_config.html").read_text(
        encoding="utf-8")
    assert 'name="sandbox_size"' in body
    assert "real money" in body
    assert "no warm node" in body, (
        "the first medium or large run waits for a machine, and somebody "
        "who is not told reads that as a hang")


def test_the_page_passes_the_current_value_back():
    from app.routers import goals
    src = inspect.getsource(goals)
    assert '"sandbox_size": goal.get("sandbox_size") or "",' in src


# ── changing it: allowed until a run holds it, and again after ────────
#
# THE WHITELIST THIS REPLACED refused the one case people hit. A run
# starts on `small`, dies for want of memory, somebody cancels it and
# wants `medium` -- and by then the story is not `accepted` any more, so
# the form said no. The only way through was to cancel the story and
# write a new one.

class _LockConn:
    def __init__(self, live=None, raises=False):
        self.live, self.raises = live, raises

    async def fetchrow(self, *_a):
        if self.raises:
            raise RuntimeError("no database")
        return self.live


def _locked(**kw):
    from app.services.machine_lock import locked_reason
    return asyncio.run(locked_reason(_LockConn(**kw), "s1"))


def test_it_is_editable_when_nothing_is_running():
    assert _locked(live=None) is None


def test_it_is_editable_again_after_a_cancel():
    """A cancelled run is terminal, so it holds nothing. This is the
    case the state whitelist got wrong."""
    from app.services.machine_lock import TERMINAL
    assert "CANCELLED" in TERMINAL
    assert _locked(live=None) is None


def test_it_is_locked_while_a_run_is_in_flight():
    why = _locked(live={"id": 9, "state": "RUNNING"})
    assert why and "run 9" in why
    assert "Cancel the run" in why, "the refusal does not say what to do"


def test_a_parked_run_still_holds_it():
    """PARKED is not terminal: the pod is built and waiting, and its
    machine is already decided."""
    assert _locked(live={"id": 9, "state": "PARKED_HOT"}) is not None


def test_an_unreadable_table_locks_rather_than_opens():
    """Refusing an edit is recoverable. Letting one through while a pod
    is running produces a setting nobody can explain."""
    assert _locked(raises=True) is not None


def test_both_the_form_and_the_go_use_the_same_rule():
    from app.routers import stories
    from app.services import go_response
    for fn in (stories.story_sandbox_size, go_response._action_update_story):
        assert "locked_reason" in inspect.getsource(fn), fn.__name__


def test_the_freeze_cannot_undo_a_deliberate_change():
    """`freeze` writes `WHERE sandbox_size IS NULL`, so it pins the first
    dispatch and never overwrites what somebody set afterwards."""
    from kernel.conductor import size_choice
    assert "sandbox_size IS NULL" in inspect.getsource(size_choice.freeze)


# ── it is visible where the work is discussed ─────────────────────────

def test_both_threads_show_the_machine():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("app/templates/chat/index.html",
                "app/templates/goal_thread/index.html"):
        body = (root / rel).read_text(encoding="utf-8")
        assert '_shared/machine_row.html' in body, rel


def test_the_row_shows_while_a_run_is_live_too():
    """That is when somebody most wants to know. An element that
    disappears exactly when it matters is worse than no element."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    row = (root / "app/templates/_shared/machine_row.html").read_text(
        encoding="utf-8")
    assert "{% if current_story %}" in row
    assert "machine_locked" in row
    assert "disabled" in row, "a locked row must grey the control, not hide it"


def test_blank_is_labelled_as_inherit_and_not_as_small():
    """Unset and `small` are different facts, and a label that conflates
    them makes the goal-level setting look broken."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    row = (root / "app/templates/_shared/machine_row.html").read_text(
        encoding="utf-8")
    assert "default for this work" in row


def test_both_pages_pass_the_lock_in():
    from app.routers import chat, goal_thread
    for fn in (chat.chat_page if hasattr(chat, "chat_page") else None,
               goal_thread.goal_thread_page):
        if fn is None:
            continue
        assert "machine_locked" in inspect.getsource(fn), fn.__name__


# ── required, not optional ────────────────────────────────────────────

def test_the_gateway_makes_it_required_on_both_creators():
    """A field a model MAY omit is the field a model omits, and then
    every story runs small."""
    from app.services.gateway_surface import specs_dir
    d = specs_dir()
    if d is None:
        pytest.skip("the gateway checkout is not beside this one")
    story = (d / "story-tools.ts").read_text(encoding="utf-8")
    go = (d / "kernel-go-tools.ts").read_text(encoding="utf-8")
    assert "sandbox_size: Type.String({" in story, "create_story"
    assert "sandbox_size: Type.String({" in go, "propose_errand"
    # update_story keeps it optional: an update names what it changes.
    assert "sandbox_size: Type.Optional(" in story


# ── both agents know about it ─────────────────────────────────────────

def _prompt(*rels) -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    return " ".join(" ".join((root / r).read_text(encoding="utf-8").split())
                    for r in rels)


def test_the_go_is_told_it_is_required_and_why():
    go = _prompt("kernel/go/AGENTS.md", "kernel/go/SOUL.md")
    assert "It is REQUIRED" in go
    assert "a field you may omit is the field that gets omitted" in go
    assert "Say `small` when it is small" in go


def test_the_go_is_told_a_cancel_unlocks_it():
    go = _prompt("kernel/go/AGENTS.md")
    assert "Fixed for the life of a RUN, not for ever" in go
    assert "cancel the run and the machine is editable again" in go


def test_the_assistant_is_told_too():
    """It is the agent watching a story die on `small`, and it knew
    nothing about machines at all."""
    a = _prompt("kernel/assistant/AGENTS.md")
    assert "The machine a story runs on" in a
    for rung in ("small", "medium", "large"):
        assert rung in a, rung


def test_the_assistant_is_told_it_cannot_change_it():
    """A prompt that implies a tool it does not hold produces an agent
    promising something and then failing quietly."""
    a = _prompt("kernel/assistant/AGENTS.md")
    assert "You cannot change it" in a
    assert "should not pretend otherwise" in a


def test_the_assistant_knows_the_retry_shape():
    """Cancel, raise, start again. Without it the move somebody reaches
    for is cancelling the whole story and writing a new one."""
    a = _prompt("kernel/assistant/AGENTS.md")
    assert "Cancel the run and it unlocks" in a
    assert "cancel, raise the machine, start again" in a


def test_the_assistant_is_warned_off_suggesting_it_reflexively():
    """A tier is real money, and "try a bigger box" is the advice that
    hides a real bug."""
    a = _prompt("kernel/assistant/AGENTS.md")
    assert "Do not suggest it reflexively" in a
    assert "hides a real bug" in a
