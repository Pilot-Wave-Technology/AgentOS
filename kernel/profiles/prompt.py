"""E6e — compose the executor's system prompt.

Before this, the executor had no system prompt at all: the only `system`
message it ever received was the rehydration brief, and the harness never
read AGENTS.md. The task arrived as a bare `user` message.

Two properties matter and they are tested separately:

  STABLE   the prefix is byte-identical across every leg of a story, so it
           can actually be cached. §8.3 asks for `cache_read_input_tokens
           > 0`, an assertion that could not pass while the only
           stable-position content was a brief that changes every leg.

  PRESENT  AGENTS.md and SOUL.md are in it on EVERY leg, not just the
           first. They are conventions and character; a leg that lost them
           would drift halfway through a story.

What is deliberately NOT here: any instruction about durability, progress
updates, or keeping notes. E3 and E6c make those harness-owned because
three agents told explicitly to keep notes produced 1 commit, 0 and 0. A
prompt line would only make the guarantee look present.
"""

from __future__ import annotations

import logging
import os

from kernel.profiles.record import Profile
from kernel.profiles.skills import discover, format_skill_list

logger = logging.getLogger(__name__)

# A convention file is context, not a document to be reproduced. Past this
# size it is almost certainly not conventions, and it would evict the
# brief and the work itself from the window.
MAX_CONVENTION_BYTES = 32_000


def _read_convention(workspace_path: str, name: str) -> str | None:
    """Read one convention file, or None if it is absent or unusable.

    Absence is normal and silent-ish: most repositories have no SOUL.md.
    Unreadable-but-present is logged, because that is a real problem the
    operator can fix and would otherwise look identical to absence.
    """
    if not workspace_path:
        return None
    path = os.path.join(workspace_path, name)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            body = fh.read(MAX_CONVENTION_BYTES + 1)
    except OSError as e:
        logger.warning("convention file %s present but unreadable: %s", name, e)
        return None

    if len(body) > MAX_CONVENTION_BYTES:
        logger.warning(
            "convention file %s exceeds %d bytes; truncated",
            name, MAX_CONVENTION_BYTES,
        )
        body = body[:MAX_CONVENTION_BYTES] + "\n\n[... truncated by the harness]"
    return body.strip() or None


def compose_system_prompt(profile: Profile, workspace_path: str) -> str:
    """Build the stable system-prompt prefix for a leg.

    Depends only on the profile and the on-disk convention files, never on
    the leg, the generation, the journal or the clock. That is what makes
    it byte-identical across legs — and anything time-varying added here
    silently turns every step into a cache miss, which §8.3 prices at the
    difference between a $60 story and a $314 one.
    """
    sections: list[str] = []

    # 1. The role — profiles/<name>/prompt.md. What this executor IS.
    if profile.prompt_preamble.strip():
        sections.append(profile.prompt_preamble.strip())

    # 2. WHERE it is. The role above says what this executor IS; this
    # says what it has to work with. It was missing entirely -- the only
    # description of the environment in the system was a sentence in the
    # scout's prompt, and the executor was told nothing -- so a model
    # could not know it had a compiler, or that `apt` would be refused.
    #
    # PER IMAGE, AND STILL NOT TIME-VARYING. B7: a goal may name its own
    # base image, and a single constant tells that executor about a
    # machine it is not running on -- a compiler it may not have, an
    # `apt` rule that may not apply. `environment_text` reads the image
    # the conductor resolved at DISPATCH out of the pod's environment,
    # so it is fixed for the life of the pod and the stable prefix stays
    # byte-identical across legs. A database read here would not, and
    # the caching argument in this docstring is why that matters.
    from kernel.base_image import environment_text

    sections.append(environment_text().strip())

    # 3. Skills — names and one-line descriptions only. Bodies load on
    # demand via the `skill` tool, so adding a skill costs the stable
    # prefix one line rather than a page.
    skill_list = format_skill_list(discover(profile.skill_dirs))
    if skill_list:
        sections.append(skill_list)

    # 4. The project's own conventions, read from the WORKSPACE and present
    # on EVERY leg. AGENTS.md is what this repo expects; SOUL.md is the
    # character the operator gave this agent. Losing them at a leg boundary
    # would let an agent drift halfway through a story.
    for name in profile.prompt_files:
        body = _read_convention(workspace_path, name)
        if body:
            sections.append(f"# {name}\n\n{body}")

    return "\n\n---\n\n".join(sections)
