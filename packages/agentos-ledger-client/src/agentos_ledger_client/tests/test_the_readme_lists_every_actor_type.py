"""The README's actor-type list must match the constraint it describes.

THIS IS THE THIRD TIME THE SAME FACT DRIFTED. `actor_type` began as
four values. `014_goal_collaboration.sql` added `goal_orchestrator`;
`ledger_kernel_actors.sql` added `executor` and `verifier`. Each time,
something that restated the list stayed behind:

  1. `schema.sql` shipped the original four, so a consumer creating
     tables from it would reject 29% of the events a live deployment
     produces. Caught by comparing against the migrations.
  2. The guard written to catch exactly that read only the two files
     `schema.sql` was copied FROM, so it compared a copy with its own
     source and passed.
  3. The README's Quick Start said "human | shca | independent_agent |
     system  (CHECK constraint)" — stating as fact a list missing
     `goal_orchestrator`, which is the MOST COMMON value in production.
     It was published to PyPI that way.

A list restated in prose drifts from the list in code unless something
compares them. Nothing did, so here it is.

WHY THE README AND NOT JUST THE SCHEMA: the README is what a consumer
reads before writing their first `append_event`. A wrong constraint in
the schema fails loudly at INSERT; a wrong list in the docs produces
code that is wrong before it runs, and the author has no reason to
doubt it.
"""
from __future__ import annotations

import importlib.metadata
import pathlib
import re
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve()
# .../src/agentos_ledger_client/tests/ -> .../packages/agentos-ledger-client/
_PKG = _HERE.parents[3]
_README = _PKG / "README.md"
_SCHEMA = _HERE.parents[1] / "schema.sql"


def _load_readme_text() -> str:
    """Read the README from disk if present, else from installed metadata.

    Prefers the file on disk when it exists so local edits are verified
    before packaging. In an installed wheel, README.md is not shipped as
    a package data file, but its text is recorded in the distribution
    metadata (METADATA Description or message payload).

    Raises:
        FileNotFoundError: If neither on-disk file nor metadata yields the README.
    """
    if _README.is_file():
        return _README.read_text(encoding="utf-8")

    meta = None
    for name in ("agentos-ledger-client", "agentos_ledger_client"):
        try:
            meta = importlib.metadata.metadata(name)
            break
        except importlib.metadata.PackageNotFoundError:
            continue

    if meta is not None:
        payload = meta.get_payload()
        if payload and isinstance(payload, str) and payload.strip():
            return payload
        description = meta.get("Description")
        if description and isinstance(description, str) and description.strip():
            return description

    raise FileNotFoundError(
        f"README not found at {_README} and could not be loaded from distribution metadata"
    )


def _schema_actor_types() -> set[str]:
    """The values the shipped DDL actually admits."""
    sql = re.sub(r"--[^\n]*", "", _SCHEMA.read_text(encoding="utf-8"))
    m = re.search(r"CHECK\s*\(\s*actor_type\s+IN\s*\((.*?)\)", sql, re.S)
    assert m, "no actor_type CHECK in schema.sql"
    return {v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()}


def _readme_actor_types() -> set[str]:
    """Values named in the README's actor_type table.

    Backticked cells in that table only — prose elsewhere may mention a
    value in passing without claiming to enumerate them.
    """
    text = _load_readme_text()
    start = text.find("### `actor_type`")
    assert start != -1, "the README has no actor_type section"
    section = text[start:]
    end = section.find("\n## ")
    if end != -1:
        section = section[:end]
    return set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))


def test_the_readme_lists_exactly_the_schema_s_actor_types():
    schema, readme = _schema_actor_types(), _readme_actor_types()
    assert readme == schema, (
        f"the README and schema.sql disagree about actor_type.\n"
        f"  in schema.sql, undocumented : {sorted(schema - readme)}\n"
        f"  in the README, not allowed  : {sorted(readme - schema)}\n"
        f"A consumer writes their first append_event from the README.")


def test_the_quick_start_does_not_restate_the_list():
    """It used to, inline, as a comment — and that copy is what went
    stale. One enumeration, in the table, or this drifts again."""
    text = _load_readme_text()
    quick = text[text.find("## Quick Start"):text.find("### `actor_type`")]
    assert "independent_agent | system" not in quick, (
        "the Quick Start is enumerating actor types again; point at the "
        "table instead")


@pytest.mark.parametrize("value", ["goal_orchestrator", "executor", "verifier"])
def test_the_three_that_were_missing_are_present(value):
    """Named explicitly, because these are the ones that were absent and
    the generic comparison above would pass if the table and the schema
    were wrong in the same way."""
    assert value in _schema_actor_types(), f"{value} dropped from schema.sql"
    assert value in _readme_actor_types(), f"{value} dropped from the README"


def test_actor_type_table_found_via_metadata_fallback_when_on_disk_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """When the on-disk README does not exist (as in an installed wheel),
    the test falls back to distribution metadata, finds the actor-type table,
    and compares it against schema.sql.

    SKIPPED WHEN THERE IS NO DISTRIBUTION TO FALL BACK TO. The fallback
    exists for an installed wheel, and in a plain source checkout the
    package is not installed -- so there is no metadata, `_load_readme_text`
    raises, and the failure says nothing about the code. CI installs the
    package, so the path this guards is still exercised where it matters.

    AND A STALE `.egg-info` IS NOT AN INSTALLED WHEEL. `conftest` puts
    `packages/agentos-ledger-client/src` on `sys.path`, so a build artifact
    left in the tree is discovered by `importlib.metadata` as though it were
    a real distribution -- carrying whatever the README said on the day it
    was built. One such artifact (0.1.7) still contained the sentence
    "`actor_type` is constrained to the four", which is failure #3 in this
    module's own docstring: the test was comparing the schema against a
    snapshot of the bug it was written to catch. Deleting the artifact is
    the fix; this check makes the next one say so instead of failing as if
    the README were wrong.
    """
    missing = tmp_path / "NONEXISTENT_README.md"
    monkeypatch.setattr(sys.modules[__name__], "_README", missing)
    assert not _README.is_file(), "precondition: on-disk README must not exist"

    try:
        text = _load_readme_text()
    except FileNotFoundError:
        pytest.skip("agentos-ledger-client is not installed, so there is no "
                    "distribution metadata to fall back to")
    if "### `actor_type`" not in text:
        pytest.fail(
            "distribution metadata was found but predates the README's "
            "actor_type table — this is a STALE build artifact shadowing "
            "the real README, not a documentation bug. Delete "
            "packages/agentos-ledger-client/src/*.egg-info and re-run.")

    readme_types = _readme_actor_types()
    schema_types = _schema_actor_types()
    assert readme_types, "no actor types extracted from metadata README"
    assert readme_types == schema_types, (
        f"metadata README and schema.sql disagree about actor_type.\n"
        f"  in schema.sql, undocumented : {sorted(schema_types - readme_types)}\n"
        f"  in the README, not allowed  : {sorted(readme_types - schema_types)}"
    )


def test_readme_resolution_fails_hard_when_both_disk_and_metadata_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """If neither on-disk file nor metadata yields a README, raise FileNotFoundError.
    It must not skip."""
    missing = tmp_path / "NONEXISTENT_README.md"
    monkeypatch.setattr(sys.modules[__name__], "_README", missing)

    def _raise_missing(_name: str) -> None:
        raise importlib.metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(importlib.metadata, "metadata", _raise_missing)
    with pytest.raises(FileNotFoundError, match="README not found"):
        _load_readme_text()


def test_readme_resolution_uses_description_when_payload_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """Defensively check the legacy Description field fallback when get_payload() is empty."""
    missing = tmp_path / "NONEXISTENT_README.md"
    monkeypatch.setattr(sys.modules[__name__], "_README", missing)

    class FakeMetadata:
        def get_payload(self) -> str:
            return ""

        def get(self, key: str, default: str | None = None) -> str | None:
            if key == "Description":
                return "# Fake\n\n### `actor_type`\n\n| `system` |\n\n## Next"
            return default

    monkeypatch.setattr(importlib.metadata, "metadata", lambda _name: FakeMetadata())
    text = _load_readme_text()
    assert "### `actor_type`" in text
