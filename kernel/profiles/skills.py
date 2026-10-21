"""Skill discovery — names and descriptions in the prompt, bodies on demand.

Progressive disclosure, the way Claude Code and OpenClaw both do it. The
stable prefix lists each skill as one line:

    - commit-hygiene: How to size and word commits so a killed leg loses
      as little as possible

and the body is loaded only when the model calls the `skill` tool. Inlining
a dozen skill bodies would evict the work from the window, and most are
irrelevant to any given story.

A skill is a directory containing SKILL.md with YAML-ish frontmatter:

    ---
    name: commit-hygiene
    description: one line, shown in the prompt
    ---

Frontmatter is parsed with a deliberately small reader rather than a YAML
dependency: two keys are needed, and a full parser would accept documents
this format does not want.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"

# A skill body is instructions, not a manual. Past this it is being used
# as a document store and will not fit alongside the work.
MAX_SKILL_BYTES = 24_000


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (fields, body). Missing or malformed frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, "\n".join(lines[end + 1:]).strip()


def discover(skill_dirs: tuple[str, ...]) -> list[Skill]:
    """Find every skill under the profile's skill directories.

    A skill missing its name or description is skipped with a warning
    rather than raising: one malformed skill should not stop a story, but
    it must not be silent either or it will look like it was never added.
    """
    found: list[Skill] = []
    seen: set[str] = set()

    for root in skill_dirs:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry, SKILL_FILE)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    fields, _ = _parse_frontmatter(fh.read(MAX_SKILL_BYTES + 1))
            except OSError as e:
                logger.warning("skill %s unreadable: %s", path, e)
                continue

            name = fields.get("name") or entry
            description = fields.get("description")
            if not description:
                logger.warning(
                    "skill %s has no description; skipped — the description "
                    "is what the model selects on", path,
                )
                continue
            if name in seen:
                logger.warning("duplicate skill name %r at %s; skipped", name, path)
                continue
            seen.add(name)
            found.append(Skill(name=name, description=description, path=path))

    return found


def load_body(skill_dirs: tuple[str, ...], name: str) -> str | None:
    """Read one skill's instructions. None if it is not a known skill.

    Resolved by discovered NAME, never by joining a caller-supplied string
    onto a path — the name arrives from the model, and a path join would
    make `../../etc/passwd` a skill.
    """
    for skill in discover(skill_dirs):
        if skill.name != name:
            continue
        try:
            with open(skill.path, encoding="utf-8") as fh:
                _, body = _parse_frontmatter(fh.read(MAX_SKILL_BYTES + 1))
        except OSError as e:
            logger.warning("skill %s unreadable at load: %s", skill.path, e)
            return None
        if len(body) > MAX_SKILL_BYTES:
            body = body[:MAX_SKILL_BYTES] + "\n\n[... truncated by the harness]"
        return body
    return None


def format_skill_list(skills: list[Skill]) -> str:
    """The prompt section. Empty string when there are no skills."""
    if not skills:
        return ""
    lines = ["## Skills available"]
    for s in skills:
        lines.append(f"- **{s.name}**: {s.description}")
    lines.append("")
    lines.append("Load one with the `skill` tool before relying on it.")
    return "\n".join(lines)
