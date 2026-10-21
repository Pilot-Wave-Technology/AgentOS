"""Unit tests for `app/services/model_registry/dropdowns.py`.

Cycle-01 of story `5dafee43`. Covers the four public helpers that
shape the v2 Model Registry into the view-models the three UI
creation-flow pickers consume:

  - build_go_model_tree_v2
  - list_model_ids_for_dropdown_v2
  - is_dropdown_empty
  - is_model_registered_v2

The v2 primitives (list_base_models_v2, synthesize_all_variants) are
already covered by test_management_v2.py and test_synthesizer.py.
These tests assert only that `dropdowns.py` composes them correctly.
The FakePool test double is the same offline pattern the rest of the
model_registry test suite uses.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from app.services.model_registry import dropdowns as d


# ---------------------------------------------------------------------------
# Test doubles for asyncpg — one row source, always returned by `fetch()`.
# ---------------------------------------------------------------------------


class _CannedResponse:
    def __init__(self, exc: BaseException):
        self.exc = exc


class FakePool:
    """Minimal async-pool stub: `fetch` pops from a queue; `raises(exc)`
    entries surface the given exception at call time. Only `fetch` is
    exercised — `list_base_models_v2` (the sole primitive dropdowns.py
    calls into a pool) uses `pool.fetch(...)`."""

    def __init__(self, fetch_queue: list[Any] | None = None):
        self.fetch_queue: list[Any] = list(fetch_queue or [])
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.fetch_calls.append((sql, args))
        if not self.fetch_queue:
            return []
        value = self.fetch_queue.pop(0)
        if isinstance(value, _CannedResponse):
            raise value.exc
        return value


def raises(exc: BaseException) -> _CannedResponse:
    return _CannedResponse(exc)


# THE TOP RUNG IS THE CAP, NOT ABOVE IT. `GO_MAX_CONTEXT` bounds what
# the goal-setup picker offers (500K), so fixtures that used to top out
# at 1M were rewritten to top out at the cap: every relationship these
# tests are about -- ordering, native-vs-synthetic collision, shape --
# is preserved, and none of them is secretly asserting that an
# over-cap window is offered. The cap itself has tests of its own at the
# end of this file.
#
# ---------------------------------------------------------------------------
# Row builders — full column shape returned by
# list_base_models_with_provider_v2 (models JOIN model_providers).
# ---------------------------------------------------------------------------


def _base(**over) -> dict:
    row = {
        "id": "google/gemini-3.8-flash",
        "provider_id": "google",
        "model_name": "gemini-3.8-flash",
        "wire_id": "google/gemini-3.8-flash",
        "label": "Gemini 3.8 Flash",
        "tier": "fast",
        "context_window": 500_000,
        "provider_shorthand": "flash",
        "available_context_windows": [50_000, 100_000, 200_000],
        "status": "active",
        "created_at": "t0",
        "updated_at": "t0",
        # Provider-status columns from the JOIN — default to a fully
        # healthy, enabled provider so existing tests that don't care
        # about this dimension keep passing unchanged.
        "provider_runtime_status": "provisioned",
        "provider_status": "active",
        "provider_credential_status": "healthy",
        "provider_credential_error": None,
    }
    row.update(over)
    return row


# ===========================================================================
# build_go_model_tree_v2
# ===========================================================================


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_happy_path_shape_matches_v1_contract():
    """Three seeded base rows produce the expected keys + variant grouping.

    Shape must be byte-identical to `llm_models.go_model_tree` so the
    picker's DOM + JS keeps working: keys are `bases`, `variants`,
    `default_base`, `default_context`, `default_id`; `bases` is a list
    of {key,label} dicts; `variants` is keyed by the same `key` and
    each entry is a list of {tokens,label,id} dicts sorted ascending.
    """
    pool = FakePool([[
        # sorted by id ascending — matches list_base_models_v2's ORDER BY.
        _base(
            id="anthropic/claude-sonnet-4-5",
            label="Claude Sonnet 4.5",
            provider_shorthand="sonnet",
            available_context_windows=[100_000, 200_000],
        ),
        _base(),  # google/gemini-3.8-flash — flash / [50K, 100K, 200K]
        _base(
            id="google/gemini-2.5-pro",
            model_name="gemini-2.5-pro",
            label="Gemini 2.5 Pro",
            provider_shorthand="pro",
            available_context_windows=[200_000, 500_000],
        ),
    ]])

    tree = await d.build_go_model_tree_v2(pool)

    assert set(tree.keys()) == {
        "bases", "variants", "default_base", "default_context", "default_id",
    }
    assert isinstance(tree["bases"], list)
    assert isinstance(tree["variants"], dict)

    # base_key = native model name (right-hand side of the base id).
    base_keys = [b["key"] for b in tree["bases"]]
    assert base_keys == ["claude-sonnet-4-5", "gemini-3.8-flash", "gemini-2.5-pro"]

    # Every base has a variants list; every entry has {tokens,label,id}.
    # Flash's own native context_window (1M, from _base()'s default) is
    # always included alongside its synthesized 50K/100K/200K variants.
    flash = tree["variants"]["gemini-3.8-flash"]
    assert [v["tokens"] for v in flash] == [50_000, 100_000, 200_000, 500_000]
    assert flash[0]["label"] == "50K"
    assert flash[1]["label"] == "100K"
    assert flash[2]["label"] == "200K"
    assert flash[3]["label"] == "500K"
    assert flash[0]["id"] == "google-flash-50k/gemini-3.8-flash-50k"
    assert flash[1]["id"] == "google-flash-100k/gemini-3.8-flash-100k"
    assert flash[3]["id"] == "google/gemini-3.8-flash"

    # Whole-million labels use `M`.
    pro_1m = next(v for v in tree["variants"]["gemini-2.5-pro"] if v["tokens"] == 500_000)
    assert pro_1m["label"] == "500K"

    # default_base = preferred Gemini 3.8 Flash (offered here), not the first
    # base by id — the GO goal-creation default.
    #
    # THE FIXTURE SAID 2.5 UNTIL 2026-10-19 and so did dropdowns.py, so
    # this passed while asserting nothing about the preference: once the
    # catalogue dropped every pre-3 Gemini, the real picker preferred a
    # base nobody offered and fell through to `bases[0]` -- still a
    # default, just not a chosen one. Keeping the fixture on a model the
    # platform actually offers is what makes the assertion mean
    # something. `test_..._falls_back_when_the_preferred_base_is_absent`
    # below covers the other side.
    assert tree["default_base"] == "gemini-3.8-flash"
    # default_context = 100_000 requested, and Flash has a 100K variant.
    assert tree["default_context"] == 500_000  # GO default moved
    # 500K NOW, because that is the default a goal is set up with and
    # this call takes it. On this fixture 500K is also flash's native
    # window, so the id is the bare base rather than a synthetic one.
    assert tree["default_id"] == "google/gemini-3.8-flash"


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_default_falls_back_to_smallest_window():
    """When the requested default_context is not offered by the default base,
    fall back to the smallest window of that base."""
    pool = FakePool([[
        _base(available_context_windows=[400_000, 500_000]),
    ]])
    tree = await d.build_go_model_tree_v2(pool, default_context=100_000)
    # default_context still echoed even if no variant matches. An
    # EXPLICIT argument, so the new 500K default does not apply here --
    # that is the whole point of the test.
    assert tree["default_context"] == 100_000
    # default_id resolves to the smallest available window (500K).
    assert tree["default_id"] == "google-flash-400k/gemini-3.8-flash-400k"


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_default_matches_when_available():
    """When the requested default_context IS offered by the default base,
    default_id points at that specific variant."""
    pool = FakePool([[
        _base(available_context_windows=[100_000, 200_000, 400_000]),
    ]])
    tree = await d.build_go_model_tree_v2(pool, default_context=200_000)
    assert tree["default_context"] == 200_000
    assert tree["default_id"] == "google-flash-200k/gemini-3.8-flash-200k"


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_empty_db_returns_empty_sentinel():
    """Zero registered models → empty tree with all keys still present."""
    pool = FakePool([[]])
    tree = await d.build_go_model_tree_v2(pool)
    assert tree == {
        "bases": [],
        "variants": {},
        "default_base": "",
        "default_context": 0,
        "default_id": "",
    }


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_native_only_row_still_included():
    """A base row with `available_context_windows=[]` still contributes
    its native entry (its own real context_window, keyed by its bare
    id) — only the synthesized-variant expansion is skipped."""
    pool = FakePool([[
        _base(id="google/gemini-3.8-flash", available_context_windows=[100_000]),
        _base(id="claude-cli/opus", provider_shorthand=None,
              available_context_windows=[], context_window=200_000,
              label="Claude (CLI)", model_name="opus"),
        _base(id="anthropic/claude-opus-4-7", provider_shorthand="opus",
              available_context_windows=[], context_window=400_000,
              label="Claude Opus 4.7", model_name="claude-opus-4-7"),  # shorthand present, but no windows
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert [b["key"] for b in tree["bases"]] == [
        "gemini-3.8-flash", "opus", "claude-opus-4-7",
    ]
    # Native-only bases get exactly one entry: their own context_window,
    # keyed by their bare base id — no synthesized ids.
    assert tree["variants"]["opus"] == [
        {"tokens": 200_000, "label": "200K", "id": "claude-cli/opus"},
    ]
    assert tree["variants"]["claude-opus-4-7"] == [
        {"tokens": 400_000, "label": "400K", "id": "anthropic/claude-opus-4-7"},
    ]


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_null_shorthand_gets_native_entry_only():
    """A base row with a non-empty windows array but a NULL shorthand
    (which the DB CHECK would normally prevent — but guard defensively)
    skips synthesized-variant expansion (`synthesize_variant` requires a
    shorthand) but still contributes its native entry."""
    pool = FakePool([[
        _base(provider_shorthand=None, available_context_windows=[100_000]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert [b["key"] for b in tree["bases"]] == ["gemini-3.8-flash"]
    assert tree["variants"]["gemini-3.8-flash"] == [
        {"tokens": 500_000, "label": "500K", "id": "google/gemini-3.8-flash"},
    ]


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_variants_sorted_ascending_by_tokens():
    """Variant list within each base is sorted ascending by token count
    even if the DB row's windows array happens to be out of order —
    including the native entry mixed in among the synthesized ones."""
    pool = FakePool([[
        _base(available_context_windows=[200_000, 50_000, 100_000]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    tokens = [v["tokens"] for v in tree["variants"]["gemini-3.8-flash"]]
    assert tokens == [50_000, 100_000, 200_000, 500_000]


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_size_equal_to_native_resolves_to_the_variant():
    """A configured window equal to the model's own native window must yield ONE
    entry at that size, and it must be the synthesized variant — not the bare
    native id.

    Regression guard. Previously both were emitted at the same `tokens`, so the
    size dropdown showed two identically-labelled options and
    `_go_model_picker.html`'s `.find(v => v.tokens === tokens)` took the first —
    always the native one, because it is appended before the synthesized entries
    and the sort is stable. The synthesized variant was therefore unreachable
    through the UI, and re-saving an agent that was stored against the variant
    silently moved it onto the base model.

    Owner's decision (2026-08-16): selecting a size means the VARIANT.
    """
    pool = FakePool([[
        # 1M duplicates this model's own native context_window.
        _base(available_context_windows=[100_000, 500_000]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    variants = tree["variants"]["gemini-3.8-flash"]

    tokens = [v["tokens"] for v in variants]
    assert tokens == sorted(set(tokens)), "a size must appear exactly once"

    at_native = [v for v in variants if v["tokens"] == 500_000]
    assert len(at_native) == 1
    assert at_native[0]["id"] == "google-flash-500k/gemini-3.8-flash-500k", (
        "the size must resolve to the synthesized variant, not the bare native id"
    )


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_native_entry_kept_when_no_size_collides():
    """The de-duplication must touch ONLY exact token collisions: a model whose
    configured windows differ from its native size keeps its native entry."""
    pool = FakePool([[
        _base(available_context_windows=[50_000, 100_000]),  # native 1M, no clash
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    variants = tree["variants"]["gemini-3.8-flash"]
    assert [v["tokens"] for v in variants] == [50_000, 100_000, 500_000]
    native = [v for v in variants if v["tokens"] == 500_000]
    assert native[0]["id"] == "google/gemini-3.8-flash", (
        "with no collision the native entry must survive untouched"
    )


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_pool_exception_returns_sentinel_and_logs(caplog):
    """A raised exception on `pool.fetch` never propagates; the caller
    sees the empty sentinel + a WARNING in the log."""
    pool = FakePool([raises(RuntimeError("db proxy down"))])
    with caplog.at_level(logging.WARNING, logger="app.services.model_registry.dropdowns"):
        tree = await d.build_go_model_tree_v2(pool)
    assert tree["bases"] == []
    assert tree["default_id"] == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log record"
    assert "build_go_model_tree_v2" in warnings[0].message
    assert "db proxy down" in warnings[0].message


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_returns_fresh_sentinel_each_call():
    """The empty-tree constant is copied on return, so a caller mutating
    its result doesn't leak into the next call."""
    pool_a = FakePool([[]])
    pool_b = FakePool([[]])
    a = await d.build_go_model_tree_v2(pool_a)
    a["bases"].append({"key": "attacker", "label": "attacker"})
    b = await d.build_go_model_tree_v2(pool_b)
    assert b["bases"] == []


# ===========================================================================
# list_model_ids_for_dropdown_v2
# ===========================================================================


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_base_models_only():
    """Every active base becomes a `kind='base'` entry. Synthesized
    context-window variants are deliberately excluded — sized entries
    were only ever designed as a GO-specific size cap
    (`build_go_model_tree_v2`), not a general template/agent choice.
    The flat list is sorted by id ascending."""
    pool = FakePool([[
        _base(),  # google/gemini-3.8-flash, has non-empty windows
        _base(
            id="anthropic/claude-haiku-4-5",
            model_name="claude-haiku-4-5",
            label="Claude Haiku 4.5",
            provider_shorthand=None,
            available_context_windows=[],
        ),
    ]])
    rows = await d.list_model_ids_for_dropdown_v2(pool)

    # Every row is a dict with the four expected keys, always kind='base'.
    for r in rows:
        assert set(r.keys()) == {"id", "label", "kind", "credential_warning"}
        assert r["kind"] == "base"
        assert r["credential_warning"] is None  # no provider_* fields on these fixture rows

    ids = [r["id"] for r in rows]
    assert ids == ["anthropic/claude-haiku-4-5", "google/gemini-3.8-flash"]
    # No synthesized variant appears, even though the flash row has windows.
    assert "google-flash-50k/gemini-3.8-flash-50k" not in ids
    assert "google-flash-100k/gemini-3.8-flash-100k" not in ids

    # Sorted ascending by id.
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_empty_db_returns_empty_list():
    pool = FakePool([[]])
    assert await d.list_model_ids_for_dropdown_v2(pool) == []


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_pool_exception_returns_empty_and_logs(caplog):
    pool = FakePool([raises(ConnectionError("proxy dead"))])
    with caplog.at_level(logging.WARNING, logger="app.services.model_registry.dropdowns"):
        rows = await d.list_model_ids_for_dropdown_v2(pool)
    assert rows == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "list_model_ids_for_dropdown_v2" in warnings[0].message


# ===========================================================================
# is_dropdown_empty
# ===========================================================================


def test_is_dropdown_empty_accepts_dict_tree_shapes():
    assert d.is_dropdown_empty({"bases": [], "variants": {}}) is True
    assert d.is_dropdown_empty({"bases": [{"key": "x", "label": "X"}]}) is False


def test_is_dropdown_empty_accepts_list_shapes():
    assert d.is_dropdown_empty([]) is True
    assert d.is_dropdown_empty([{"id": "x", "label": "X", "kind": "base"}]) is False
    assert d.is_dropdown_empty(()) is True


def test_is_dropdown_empty_treats_unknown_shape_as_empty():
    """Defensive: None or a bare string should not crash the template."""
    assert d.is_dropdown_empty(None) is True
    assert d.is_dropdown_empty("not a tree") is True
    assert d.is_dropdown_empty(42) is True


# ===========================================================================
# is_model_registered_v2
# ===========================================================================


@pytest.mark.asyncio
async def test_is_model_registered_v2_true_for_known_base_id():
    pool = FakePool([[_base()]])
    assert await d.is_model_registered_v2(pool, "google/gemini-3.8-flash") is True


@pytest.mark.asyncio
async def test_is_model_registered_v2_true_for_synthesized_variant_id():
    pool = FakePool([[_base()]])
    assert await d.is_model_registered_v2(
        pool, "google-flash-100k/gemini-3.8-flash-100k",
    ) is True


@pytest.mark.asyncio
async def test_is_model_registered_v2_false_for_unknown_id():
    pool = FakePool([[_base()]])
    assert await d.is_model_registered_v2(pool, "unknown/id") is False


@pytest.mark.asyncio
async def test_is_model_registered_v2_false_for_empty_id():
    """Empty string / None short-circuits without touching the pool."""
    pool = FakePool([[_base()]])
    assert await d.is_model_registered_v2(pool, "") is False
    # Pool was not consulted.
    assert pool.fetch_calls == []


@pytest.mark.asyncio
async def test_is_model_registered_v2_false_on_pool_exception_and_logs(caplog):
    pool = FakePool([raises(RuntimeError("db down"))])
    with caplog.at_level(logging.WARNING, logger="app.services.model_registry.dropdowns"):
        result = await d.is_model_registered_v2(pool, "google/gemini-3.8-flash")
    assert result is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "is_model_registered_v2" in warnings[0].message


@pytest.mark.asyncio
async def test_is_model_registered_v2_walks_all_bases_before_returning_false():
    """A miss on the first base still checks subsequent bases + variants."""
    pool = FakePool([[
        _base(id="anthropic/claude-sonnet-4-5", model_name="claude-sonnet-4-5",
              label="Claude Sonnet 4.5", provider_shorthand="sonnet",
              available_context_windows=[100_000]),
        _base(),
    ]])
    # A variant of the second base — must still resolve True.
    assert await d.is_model_registered_v2(
        pool, "google-flash-100k/gemini-3.8-flash-100k",
    ) is True


# ---------------------------------------------------------------------------
# resolve_model_id
# ---------------------------------------------------------------------------


class _FetchvalFakePool:
    """Minimal pool stub for resolve_model_id — only `fetchval` is used."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if not self.responses:
            return None
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_resolve_model_id_none_for_empty_input():
    pool = _FetchvalFakePool([])
    assert await d.resolve_model_id(pool, None) is None
    assert await d.resolve_model_id(pool, "") is None
    assert pool.calls == []


@pytest.mark.asyncio
async def test_resolve_model_id_direct_match_for_base_id():
    """A plain base id (or unsized native/CLI id) resolves on the first
    query — no second lookup needed."""
    pool = _FetchvalFakePool(["google/gemini-3.8-flash"])
    result = await d.resolve_model_id(pool, "google/gemini-3.8-flash")
    assert result == "google/gemini-3.8-flash"
    assert len(pool.calls) == 1


@pytest.mark.asyncio
async def test_resolve_model_id_falls_back_to_base_for_variant():
    """A context-window variant id has no row of its own — resolves to
    its base model's row instead, via derive_base_id."""
    pool = _FetchvalFakePool([
        None,                          # direct match on the variant id misses
        "google/gemini-3.8-flash",     # match on the derived base id
    ])
    result = await d.resolve_model_id(
        pool, "google-flash-100k/gemini-3.8-flash-100k",
    )
    assert result == "google/gemini-3.8-flash"
    assert len(pool.calls) == 2
    assert pool.calls[1][1] == ("google/gemini-3.8-flash",)


@pytest.mark.asyncio
async def test_resolve_model_id_none_for_genuinely_unregistered_model():
    """Direct match misses, and the string doesn't even look like a
    variant (or its derived base doesn't exist either) — None."""
    pool = _FetchvalFakePool([None])
    result = await d.resolve_model_id(pool, "totally-made-up/model-xyz")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_model_id_none_when_derived_base_also_missing():
    """The string looks like a variant, but even its derived base isn't
    a registered row — still None, not the (nonexistent) base id."""
    pool = _FetchvalFakePool([
        None,   # direct match on the variant id misses
        None,   # derived base id also doesn't resolve
    ])
    result = await d.resolve_model_id(
        pool, "ghost-flash-100k/ghost-model-100k",
    )
    assert result is None


# ===========================================================================
# Provider-disabled exclusion (availability.py wiring) — every entry
# point routes through _fetch_selectable_base_models.
# ===========================================================================


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_excludes_model_from_disabled_provider():
    pool = FakePool([[
        _base(),  # google/gemini-3.8-flash — provider healthy/active
        _base(
            id="anthropic/claude-sonnet-4-5",
            model_name="claude-sonnet-4-5",
            label="Claude Sonnet 4.5",
            provider_shorthand="sonnet",
            available_context_windows=[100_000],
            provider_status="disabled",
        ),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    base_keys = [b["key"] for b in tree["bases"]]
    assert base_keys == ["gemini-3.8-flash"]
    assert "claude-sonnet-4-5" not in tree["variants"]


@pytest.mark.asyncio
async def test_build_go_model_tree_v2_does_not_exclude_on_not_provisioned():
    """runtime_status is bookkeeping-only and must never gate
    selectability (see management.py's `_RUNTIME_STATUSES` comment) —
    a not_provisioned provider's models must still appear."""
    pool = FakePool([[
        _base(provider_runtime_status="not_provisioned"),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert [b["key"] for b in tree["bases"]] == ["gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_excludes_disabled_provider_model():
    pool = FakePool([[
        _base(),
        _base(
            id="anthropic/claude-sonnet-4-5",
            model_name="claude-sonnet-4-5",
            label="Claude Sonnet 4.5",
            provider_status="disabled",
        ),
    ]])
    rows = await d.list_model_ids_for_dropdown_v2(pool)
    assert [r["id"] for r in rows] == ["google/gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_surfaces_revoked_credential_warning():
    pool = FakePool([[
        _base(provider_credential_status="revoked"),
    ]])
    rows = await d.list_model_ids_for_dropdown_v2(pool)
    assert len(rows) == 1  # still selectable — soft warning, not hard-excluded
    assert rows[0]["credential_warning"] is not None
    assert "revoked" in rows[0]["credential_warning"]


@pytest.mark.asyncio
async def test_list_model_ids_for_dropdown_v2_surfaces_unhealthy_credential_warning_with_error():
    pool = FakePool([[
        _base(
            provider_credential_status="unhealthy",
            provider_credential_error="permission denied",
        ),
    ]])
    rows = await d.list_model_ids_for_dropdown_v2(pool)
    assert rows[0]["credential_warning"] == (
        "This provider's credential is unhealthy: permission denied — requests may fail."
    )


@pytest.mark.asyncio
async def test_is_model_registered_v2_false_for_disabled_provider_model():
    pool = FakePool([[
        _base(provider_status="disabled"),
    ]])
    result = await d.is_model_registered_v2(pool, "google/gemini-3.8-flash")
    assert result is False


@pytest.mark.asyncio
async def test_is_model_registered_v2_true_for_not_provisioned_provider_model():
    """Same rule as the GO tree test above: not_provisioned must not
    gate this either — it's the check `change_agent_model`/`create_goal`
    call to decide whether a submission is accepted."""
    pool = FakePool([[
        _base(provider_runtime_status="not_provisioned"),
    ]])
    result = await d.is_model_registered_v2(pool, "google/gemini-3.8-flash")
    assert result is True


# ── which models a GOAL may be set up on, and how big a window ───────
#
# Asked for on 2026-09-24: Gemini 3.8 Flash, Opus 5.5 and Sonnet 5, at
# 50K-500K, defaulting to 500K. Neither half deactivates anything: the
# other bases stay assignable everywhere else, and the 1M rung stays in
# the catalogue because four Council RA templates run on it.

@pytest.mark.asyncio
async def test_only_go_selectable_models_are_offered():
    """A TAG AND NOT A LIST IN CODE. `models` is the catalogue the
    gateway pulls, so which of them a goal is offered is data -- a tuple
    here would need a deploy to change and would be a third place that
    has to agree with the other two."""
    pool = FakePool([[
        _base(id="google/gemini-3.8-flash", model_name="gemini-3.8-flash",
              provider_shorthand="flash38", tags=["go-selectable"]),
        _base(id="google/gemini-3.8-flash", model_name="gemini-3.8-flash",
              provider_shorthand="flash", tags=["reasoning"]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert [b["key"] for b in tree["bases"]] == ["gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_an_untagged_catalogue_offers_everything():
    """DEGRADE, DO NOT BLOCK. If the migration has not landed, or the
    last tag is removed, the honest failure is the old dropdown -- not
    an empty one that stops goal creation with nothing on screen saying
    why."""
    pool = FakePool([[
        _base(id="google/gemini-3.8-flash", model_name="gemini-3.8-flash"),
        _base(id="google/gemini-3.8-flash", model_name="gemini-3.8-flash",
              provider_shorthand="flash38"),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert len(tree["bases"]) == 2


@pytest.mark.asyncio
async def test_nothing_over_the_cap_is_offered():
    """500K is the ceiling for the PICKER. The 1M variant stays
    synthesizable and stays assignable -- removing the rung would leave
    the Council RA templates naming a variant that no longer exists."""
    pool = FakePool([[
        _base(context_window=1_000_000,
              available_context_windows=[200_000, 500_000, 1_000_000],
              tags=["go-selectable"]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    sizes = [v["tokens"] for v in tree["variants"]["gemini-3.8-flash"]]
    assert sizes == [200_000, 500_000]
    assert d.GO_MAX_CONTEXT == 500_000


@pytest.mark.asyncio
async def test_a_base_whose_native_window_is_over_the_cap_is_still_bounded():
    """The native entry is built on the "always offered" path, so the
    cap is applied after it -- otherwise a 1M model would slip its own
    window through while its variants were filtered."""
    pool = FakePool([[
        _base(context_window=1_000_000, available_context_windows=[],
              provider_shorthand=None, tags=["go-selectable"]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert tree["bases"] == [], (
        "a base with nothing under the cap must drop out rather than "
        "appear with an empty window list")


@pytest.mark.asyncio
async def test_a_goal_defaults_to_500k():
    pool = FakePool([[
        _base(available_context_windows=[100_000, 500_000],
              tags=["go-selectable"]),
    ]])
    tree = await d.build_go_model_tree_v2(pool)
    assert tree["default_context"] == 500_000


@pytest.mark.asyncio
async def test_go_tree_falls_back_when_the_preferred_base_is_absent():
    """No preferred base offered -> first base by id, and that is a bug
    when it happens silently.

    This is the shape of the defect found on 2026-10-19:
    `_PREFERRED_DEFAULT_BASE` named `gemini-2.5-flash` the day after the
    catalogue stopped offering any pre-3 Gemini, so the preference
    matched nothing and the goal-creation picker defaulted to whatever
    sorted first. It still had a default, every test passed, and the
    only symptom was goals quietly starting on the wrong model.

    The fallback itself is correct -- a picker with no default is worse
    than one with an arbitrary default. What was missing was anything
    asserting that the preference is REACHABLE, which the happy-path
    test above now does.
    """
    pool = FakePool([[
        _base(
            id="anthropic/claude-sonnet-4-5",
            model_name="claude-sonnet-4-5",
            label="Claude Sonnet 4.5",
            provider_shorthand="sonnet",
            available_context_windows=[100_000, 200_000],
        ),
        _base(
            id="anthropic/claude-opus-4-6",
            model_name="claude-opus-4-6",
            label="Claude Opus 4.6",
            provider_shorthand="opus",
            available_context_windows=[100_000],
        ),
    ]])

    tree = await d.build_go_model_tree_v2(pool)

    assert d._PREFERRED_DEFAULT_BASE not in [b["key"] for b in tree["bases"]]
    assert tree["default_base"] == tree["bases"][0]["key"]


def test_the_preferred_base_is_one_the_catalogue_offers():
    """The preference must name a model `app/llm_models.py` still lists.

    Without this, the two can drift apart again in exactly the way they
    did: a one-line edit in the catalogue silently retires the picker's
    default, and nothing anywhere fails.
    """
    from app import llm_models

    # A base key is the right-hand side of a wire id, and the catalogue
    # carries both plain entries (`google/gemini-3.8-flash`) and
    # synthetic ones (`google-flash38-100k/gemini-3.8-flash-100k`). The
    # preference names the plain base, so match on it exactly.
    catalogue = {m["id"].split("/")[-1] for m in llm_models.SUPPORTED_MODELS}
    assert d._PREFERRED_DEFAULT_BASE in catalogue, (
        f"{d._PREFERRED_DEFAULT_BASE!r} is not offered by "
        f"llm_models.SUPPORTED_MODELS: {sorted(catalogue)}")
