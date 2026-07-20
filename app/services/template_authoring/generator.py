"""Gemini-backed generation of the role-specific template sections.

Given the operator's structured answers + the chosen archetype, ask Gemini to
author ONLY the GENERATED slots (role_bullets, working_section,
personality_bullets) as JSON. The fixed scaffold is shown for context but the
model is told NOT to reproduce it.

Degrades gracefully: if GEMINI_API_KEY is unset, or the call/parse fails, we
return deterministic fallbacks derived from the operator's input so the wizard
stays fully usable offline. The operator then completes the sections by hand in
the review step.
"""
import json
import logging

from google import genai
from google.genai import types

from app.config import get_settings
from app.services.template_authoring.models import GeneratedSections, WizardInputs
from app.services.template_authoring.scaffolds import get_archetype

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"

_SYSTEM_INSTRUCTION = """You author the role-specific sections of an IA (Independent Agent) template for a multi-agent software platform. An IA is a task-scoped agent dispatched to do one job and report back.

You are shown a FIXED canonical scaffold for context. DO NOT reproduce, rewrite, or reference the scaffold — it is already written. Produce ONLY the role-specific sections, as a single JSON object with EXACTLY these keys:

- "role_bullets": a markdown bullet list (4-7 bullets) describing concretely what this agent does, in second person ("Read ...", "Write ..."). No heading.
- "working_section": the agent's main domain section. START with a markdown "## " heading, then the body (bullets or short prose). This is the heart of the agent — its standards, method, or checklist.
- "personality_bullets": EXACTLY 4 markdown bullets, each "- <Trait> — <short description>", matching the house voice.

Rules:
- Match the voice and structure of the reference example.
- Output raw JSON only — no markdown code fences, no commentary.
- Keep it tight and concrete. No filler.
"""


def _build_user_prompt(archetype: dict, inputs: WizardInputs) -> str:
    return f"""ARCHETYPE: {archetype['label']} — {archetype['blurb']}

OPERATOR'S DESCRIPTION OF THE AGENT:
- Name: {inputs.name}
- Role (one line): {inputs.role_oneliner or '(not given)'}
- Domain / specialization: {inputs.domain or '(not given)'}
- What its main section should cover: {inputs.working_specifics or '(not given)'}
- Personality hint: {inputs.personality_hint or '(not given)'}
- Output artifact: {inputs.effective_output_artifact()}

REFERENCE EXAMPLE (house voice — match this style, do NOT copy verbatim):
{archetype['fewshot']}

Now produce the JSON object with keys role_bullets, working_section, personality_bullets."""


def _fallback(inputs: WizardInputs) -> GeneratedSections:
    """Deterministic sections from operator input when Gemini is unavailable."""
    oneliner = inputs.role_oneliner.strip() or f"Acts as {inputs.name}."
    working = inputs.working_specifics.strip()
    hint = inputs.personality_hint.strip()
    bullets = (
        "\n".join(f"- {w.strip()}" for w in hint.split(",") if w.strip())
        if hint
        else "- Focused — does exactly what's asked\n- Thorough — handles edge cases\n"
        "- Clear — readable, well-structured output\n- Communicative — reports progress"
    )
    return GeneratedSections(
        role_bullets=f"- {oneliner}",
        working_section=(
            "## Approach\n\n> TODO: describe how this agent works.\n\n"
            f"Operator notes: {working}" if working else
            "## Approach\n\n> TODO: describe how this agent works."
        ),
        personality_bullets=bullets,
        ai_generated=False,
    )


def _parse(text: str) -> dict:
    """Parse Gemini output to a dict, tolerating stray code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip a leading ```json / ``` fence and the trailing fence.
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return json.loads(cleaned)


async def generate_role_sections(
    archetype_name: str, inputs: WizardInputs
) -> GeneratedSections:
    """Generate the GENERATED slots via Gemini, or fall back deterministically."""
    archetype = get_archetype(archetype_name)
    settings = get_settings()

    if not settings.gemini_api_key:
        logger.warning(
            "template_authoring: GEMINI_API_KEY not set — using fallback sections for %s",
            inputs.template_id,
        )
        return _fallback(inputs)

    prompt = _build_user_prompt(archetype, inputs)
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
            ),
        )
        data = _parse(response.text or "")
        sections = GeneratedSections(
            role_bullets=str(data.get("role_bullets", "")).strip(),
            working_section=str(data.get("working_section", "")).strip(),
            personality_bullets=str(data.get("personality_bullets", "")).strip(),
            ai_generated=True,
        )
        if not (sections.role_bullets and sections.working_section and sections.personality_bullets):
            logger.warning(
                "template_authoring: Gemini returned incomplete sections for %s — using fallback",
                inputs.template_id,
            )
            return _fallback(inputs)
        logger.info("template_authoring: generated sections via Gemini for %s", inputs.template_id)
        return sections
    except Exception as exc:
        logger.warning(
            "template_authoring: Gemini generation failed for %s (%s) — using fallback",
            inputs.template_id, exc,
        )
        return _fallback(inputs)
