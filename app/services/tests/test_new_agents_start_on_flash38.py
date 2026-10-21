"""New assistants and new goals start on Gemini 3.8 Flash at 500K.

NEW ONLY, and that is the whole safety story. A live assistant keeps
whatever `agents.model` it holds until somebody re-provisions or
switches it -- nothing is recreated, no workspace is touched, no memory
moves. Asked for exactly that way on 2026-09-24: "I only want new
assistants on 3.8 flash, old ones can use whatever they want."

TWO PLACES HAVE TO AGREE, and either one left behind is silently dead
for half the surfaces. Model resolution is

    explicit user choice  >  templates.model  >  llm_models default

so a template row that still says Sonnet 5 makes the constant
unreachable for every surface that goes through a template, and a stale
constant does the same in reverse for the surfaces that do not. These
tests read both.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib

import asyncpg
import pytest

from app.services.tests.shipped_tree import (IS_DISTRIBUTION, seed_template,
                                             seed_text)

ROOT = pathlib.Path(__file__).resolve().parents[3]
FLASH38 = "google-flash38-500k/gemini-3.8-flash-500k"
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


def test_the_constants_moved():
    from app.llm_models import DEFAULT_GO_MODEL, DEFAULT_SHCA_MODEL

    assert DEFAULT_SHCA_MODEL == FLASH38
    assert DEFAULT_GO_MODEL == FLASH38


@pytest.mark.asyncio
async def test_the_template_rows_agree_with_the_constants():
    """`templates.model` WINS over the constant, so a row left behind
    means the constant is decoration."""
    from app.llm_models import DEFAULT_SHCA_MODEL

    conn = await _connect_or_skip()
    try:
        rows = await conn.fetch(
            "SELECT id, model FROM templates WHERE id = ANY($1::text[])",
            ["kernel-assistant", "go-v2"])
        if not rows:
            pytest.skip("template rows are not in this database")
        for r in rows:
            assert r["model"] == DEFAULT_SHCA_MODEL, (
                f"{r['id']} still provisions on {r['model']!r}")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_no_live_agent_was_moved():
    """THE PROPERTY THAT MAKES THIS SAFE. Changing a default must not
    touch a running agent: a model change is a RECREATE on the gateway
    (`agents.update` is a no-op for it), and a recreate is where
    workspaces and memory are at risk. If this migration had rewritten
    `agents.model`, the next refresh would have quietly rebuilt the
    fleet."""
    if IS_DISTRIBUTION:
        # A fresh install has no live agents to move; what the migration
        # changed -- the template rows -- ships as the seed.
        for tid in ("kernel-assistant", "go-v2"):
            assert seed_template(tid)["model"] == FLASH38, (
                f"{tid} is not seeded on {FLASH38}")
        return
    sql = io.open(ROOT / "migrations/default_models_flash38.sql",
                  encoding="utf-8").read()
    body = "\n".join(line for line in sql.splitlines()
                     if not line.lstrip().startswith("--"))
    assert "UPDATE agents" not in body, (
        "the migration rewrites live agents, which forces a recreate")
    assert "agent_instances" not in body
    assert "UPDATE templates" in body


def test_the_go_default_is_a_model_the_picker_offers():
    """A default the menu cannot show is a goal landing on something
    nobody chose. `gemini-3.7-flash` is no longer go-selectable, which
    is why the GO default had to move as well."""
    from app.llm_models import DEFAULT_GO_MODEL

    if IS_DISTRIBUTION:
        # No `go-selectable` tag is seeded, and an untagged catalogue
        # offers every active base (dropdowns.build_go_model_tree_v2). So
        # the default is offered iff its base is seeded active with the
        # default's window among its sizes.
        base_name, size = DEFAULT_GO_MODEL.split("/")[-1].rsplit("-", 1)
        bases = [m for m in json.loads(seed_text("models.json"))
                 if m["model_name"] == base_name]
        assert bases, f"{base_name!r} is not a seeded model"
        base = bases[0]
        assert base.get("status", "active") == "active"
        tokens = int(size.rstrip("k")) * 1000
        assert tokens in base["available_context_windows"], (
            f"the GO default {DEFAULT_GO_MODEL!r} is a window the picker "
            f"does not offer")
        return
    sql = io.open(ROOT / "migrations/go_model_choices.sql",
                  encoding="utf-8").read()
    # The synthetic id carries its base: `<owner>-<shorthand>-<size>/
    # <native>-<size>`, so the base's model_name is the tail without the
    # size suffix.
    native = DEFAULT_GO_MODEL.split("/")[-1].rsplit("-", 1)[0]
    assert native in sql, (
        f"the GO default is {native!r}, which go_model_choices.sql does "
        f"not make go-selectable")


def test_it_is_in_the_sequence():
    if IS_DISTRIBUTION:
        # The migration "runs" in a fresh install by being in the seed.
        for tid in ("kernel-assistant", "go-v2"):
            assert seed_template(tid)["model"] == FLASH38
        return
    seq = io.open(ROOT / "migrations/SEQUENCE", encoding="utf-8").read()
    assert "default_models_flash38" in seq
