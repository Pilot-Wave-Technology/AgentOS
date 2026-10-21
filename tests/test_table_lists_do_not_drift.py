"""The two hardcoded table lists, checked against the real schema.

Table names inside a query are fine -- that is what raw SQL is. A table
name inside a PYTHON LIST is different: the list has to stay in step
with a schema that changes underneath it, and nothing makes it.

Both lists here have already drifted once.

  * THE BASELINE SEED. The squashed OSS baseline was built from a
    hand-listed set of tables, and silently dropped the static
    data-seed migrations -- memory_prompts, claude_phases,
    model_pricing and actors all had to be added back after the fact,
    because a fresh install came up missing them and nothing said so.
  * `goal_rollback._CHILDREN`. Six (table, column) pairs deleted when
    goal creation fails. A renamed column makes the DELETE raise; a new
    child table creation can write makes it leave orphans.

Neither test asserts a list is COMPLETE, because neither list is meant
to be. They assert that every table is CLASSIFIED -- so adding one
fails here and someone decides, instead of the decision being made by
omission months later.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

import db_or_skip

asyncpg = pytest.importorskip("asyncpg")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSERT = re.compile(r"INSERT\s+INTO\s+((?:kernel\.)?[a-zA-Z_][a-zA-Z0-9_]*)",
                     re.IGNORECASE)

# Carried into the public baseline by `publish/extract_seed.py`. A fresh
# OSS install has these rows or it does not work.
SEEDED = {
    "templates", "template_files", "skills", "tools", "tool_profiles",
    "tool_profile_tools", "memory_prompts", "claude_phases",
    "model_pricing", "actors", "goals", "platform_recreate_state",
    "kernel.run_state_machine", "kernel.admission_buckets",
}

# Written by a migration but deliberately NOT part of the baseline:
# one-off data repairs, runtime tables that fill themselves, caches,
# and registries reconciled at startup by catalog_sync / bootstrap.
NOT_SEEDED = {
    "agent_configs", "agent_files", "agent_gateway_bindings", "agents",
    "council_review_runs", "council_review_stages", "criterion_results",
    "dag_ceiling_config", "dashboard_cache_meta", "execution_transitions",
    "gateway_cache", "git_repo_trunks", "goal_agent_contexts",
    "goal_agent_memberships", "goal_documents",
    "goal_platform_integrations", "model_aliases", "model_context_windows",
    "model_providers", "model_tags", "models", "platform_channel_bindings",
    "platform_integrations", "platform_schedule_state", "platform_settings",
    "platform_user_map", "shca_template_files", "shca_templates",
    "skills_history", "stories", "story_reviews", "tags",
    "template_history", "template_tool_profiles", "thread_participants",
    "threads", "tool_credentials", "user_shca_configs",
}


def _migration_insert_targets() -> set[str]:
    out: set[str] = set()
    for path in sorted((_ROOT / "migrations").rglob("*")):
        if not path.is_file() or path.suffix not in (".sql", ".py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _INSERT.finditer(text):
            name = match.group(1).lower()
            # Scratch tables a migration makes and drops in the same
            # file -- never part of the schema anyone ships.
            if name.startswith("_") or name.split(".")[-1].startswith("_"):
                continue
            out.add(name)
    return out


def test_every_seeded_table_is_classified() -> None:
    """A migration that seeds a new table forces a baseline decision."""
    found = _migration_insert_targets()
    assert len(found) > 40, (
        f"only {len(found)} INSERT targets found in migrations/; the "
        "scanner has stopped seeing them rather than the migrations "
        "having lost their seeds")
    unclassified = found - SEEDED - NOT_SEEDED - {"this"}
    assert not unclassified, (
        "a migration INSERTs into tables that are in neither list:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nDecide for each, and add it to SEEDED or NOT_SEEDED in "
          "this file:\n"
          "  SEEDED     -- a fresh OSS install needs these rows, so\n"
          "                publish/extract_seed.py must carry it.\n"
          "  NOT_SEEDED -- a one-off repair, a cache, or a runtime\n"
          "                table that fills itself.\n"
          "Getting this wrong is how the baseline shipped without\n"
          "memory_prompts, claude_phases, model_pricing and actors.")


_COLUMN_EXISTS = """
SELECT 1 FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
"""

_GOAL_CHILDREN = """
SELECT tc.table_name, kcu.column_name
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON kcu.constraint_name = tc.constraint_name
   AND kcu.table_schema = tc.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name
   AND ccu.table_schema = tc.table_schema
 WHERE tc.constraint_type = 'FOREIGN KEY'
   AND tc.table_schema = 'public'
   AND ccu.table_name = 'goals'
"""

# A goal-child table that `undo_goal_creation` deliberately does NOT
# delete, because goal CREATION cannot have written it: a goal that
# never finished being created has no stories, tasks, git nodes,
# council runs or Slack bindings yet.
NOT_ROLLED_BACK = {
    "council_findings", "council_review_runs", "git_merge_groups",
    "git_nodes", "go_conversation_summaries", "go_memory",
    "goal_decisions", "goal_documents", "goal_platform_integrations",
    "goal_summaries", "goal_summary", "join_requests",
    "platform_channel_bindings", "resource_access", "resources",
    "slack_config", "slack_story_channels", "slack_thread_map",
    "slack_user_goal_channels", "stories", "tasks",
}


def _dsn() -> str | None:
    return (os.environ.get("INTERLOCK_TEST_DSN")
            or os.environ.get("DATABASE_URL") or None)


def test_goal_rollback_children_are_real_and_complete() -> None:
    dsn = _dsn()
    if not dsn:
        pytest.skip("no INTERLOCK_TEST_DSN / DATABASE_URL")

    from app.services.goal_rollback import _CHILDREN

    async def go():
        conn, unused = await db_or_skip.connect_pair(asyncpg)
        await unused.close()
        try:
            missing = [f"{t}.{c}" for t, c in _CHILDREN
                       if not await conn.fetchval(_COLUMN_EXISTS, t, c)]
            children = {r["table_name"] for r in await conn.fetch(_GOAL_CHILDREN)}
        finally:
            await conn.close()
        return missing, children

    missing, children = asyncio.run(go())

    # 1. Every pair it deletes must exist, or the rollback raises and
    #    the goal is left behind for a person -- which is what the
    #    except in `undo_goal_creation` reports.
    assert not missing, (
        "goal_rollback._CHILDREN names columns that do not exist: "
        + ", ".join(missing))

    # 2. Every OTHER goal-child table must be a deliberate omission.
    #    `agent_instances` is in _CHILDREN without an FK, so it will
    #    not appear here; that is fine and the reason this compares
    #    one way only.
    named = {t for t, _ in _CHILDREN}
    unclassified = children - named - NOT_ROLLED_BACK
    assert not unclassified, (
        "these tables reference goals and are in neither "
        "goal_rollback._CHILDREN nor NOT_ROLLED_BACK:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nIf goal CREATION can write it, add it to _CHILDREN "
          "(children before parents). If it can only appear later, add "
          "it to NOT_ROLLED_BACK here.")
