"""The legacy templates are retired, and nothing live depends on them.

`go` and `default-shca` were replaced by `go-v2` and `kernel-assistant`;
the IA templates belonged to the retired task-controller flow. They
stayed in the template list looking like choices.
"""
from __future__ import annotations

import io
import pathlib

from app.services.tests.shipped_tree import IS_DISTRIBUTION, migration_text

# The distribution ships squashed, numbered migrations; the same change is
# `0009_retire_legacy_templates` there.
_MIGRATION = ("0009_retire_legacy_templates" if IS_DISTRIBUTION
              else "retire_legacy_templates")

ROOT = pathlib.Path(__file__).resolve().parents[3]
RETIRED = ("go", "default-shca", "planner-ia", "backend-dev-ia",
           "frontend-dev-ia", "code-test-ia", "code-review-ia",
           "task-reviewer-ia")


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_a_template_in_use_is_never_deleted():
    """The rule that makes the migration safe on any database: a
    template some non-deleted agent still points at stays."""
    sql = migration_text(_MIGRATION)
    assert sql, f"migrations/{_MIGRATION}.sql is missing"
    assert "a.template_id = t.id AND a.status <> 'deleted'" in sql
    assert "NOT EXISTS (SELECT 1 FROM agents a" in sql
    for tid in RETIRED:
        assert f"'{tid}'" in sql, f"{tid} is not retired"
    assert _MIGRATION in _read("migrations/SEQUENCE").split()


def test_the_council_profiles_the_templates_in_use():
    inv = _read("app/services/platform_council/inventory.py")
    assert 'GO_TEMPLATE_ID = "go-v2"' in inv
    assert 'SHCA_TEMPLATE_ID = "kernel-assistant"' in inv
    reg = _read("app/services/platform_council/registry.py")
    assert '{"GO": "go-v2", "SHCA": "kernel-assistant"}' in reg


def test_nothing_is_auto_assigned_to_the_retired_planner():
    tc = _read("app/task_controller.py")
    assert "SET template_id = 'planner-ia'" not in tc


def test_local_hydration_reads_the_current_assistant():
    assert '_SHCA_TEMPLATE_ID = "kernel-assistant"' in _read(
        "app/services/local_hydrate.py")
