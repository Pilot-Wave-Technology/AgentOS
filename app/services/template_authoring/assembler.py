"""Assemble the final AGENTS.md / SOUL.md from scaffold + slot values.

Assembly is a SINGLE regex pass over ``%%slot%%`` tokens. We deliberately do
NOT use ``str.format``: the scaffolds contain literal ``{...}`` JSON examples
(the task-report payloads) that would make ``.format`` raise. A single
pass also means a slot VALUE that happens to contain a ``%%token%%`` substring
is never re-expanded — each token is resolved exactly once.
"""
import logging
import re

from app.services.template_authoring.models import GeneratedSections, WizardInputs
from app.services.template_authoring.scaffolds import get_archetype

logger = logging.getLogger(__name__)

_SLOT_RE = re.compile(r"%%([a-z_]+)%%")


def _resolve_operator_slots(archetype: dict, inputs: WizardInputs) -> dict[str, str]:
    """Map each OPERATOR slot name to its value pulled from WizardInputs.

    The op_slots map values are either a plain attribute name or the name of a
    zero-arg method on WizardInputs (the ``effective_*`` helpers).
    """
    values: dict[str, str] = {}
    for slot, accessor in archetype["op_slots"].items():
        attr = getattr(inputs, accessor)
        values[slot] = attr() if callable(attr) else (attr or "")
    return values


def _build_value_map(
    archetype: dict, inputs: WizardInputs, sections: GeneratedSections
) -> dict[str, str]:
    values = _resolve_operator_slots(archetype, inputs)
    for slot in archetype["gen_slots"]:
        values[slot] = getattr(sections, slot, "") or ""
    return values


def _fill(scaffold: str, values: dict[str, str]) -> str:
    """Single-pass replace of every ``%%slot%%`` token. Unknown tokens are
    left intact (visible in the review step rather than silently dropped)."""
    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        logger.warning("template_authoring: unmapped scaffold slot %%%%%s%%%%", name)
        return match.group(0)

    return _SLOT_RE.sub(repl, scaffold)


def assemble(
    archetype_name: str, inputs: WizardInputs, sections: GeneratedSections
) -> dict[str, str]:
    """Return ``{"AGENTS.md": ..., "SOUL.md": ...}`` for the chosen archetype."""
    archetype = get_archetype(archetype_name)
    values = _build_value_map(archetype, inputs, sections)
    result = {
        "AGENTS.md": _fill(archetype["agents_scaffold"], values),
        "SOUL.md": _fill(archetype["soul_scaffold"], values),
    }
    logger.info(
        "template_authoring: assembled files for archetype=%s id=%s "
        "(AGENTS.md=%d chars, SOUL.md=%d chars)",
        archetype_name, inputs.template_id,
        len(result["AGENTS.md"]), len(result["SOUL.md"]),
    )
    return result
