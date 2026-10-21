"""Pydantic models for the guided template-creation wizard.

`WizardInputs` is what the operator submits in step 1 (the structured form).
`GeneratedSections` is what Gemini returns in step 2 (the role-specific prose
that gets slotted into the fixed scaffold). Keeping these as explicit models
means the router, the generator, and the assembler all agree on the contract.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


# Archetypes the wizard can produce. Coding-pipeline mirrors the 5 existing IA
# templates (repo / working-branch / PLAN.md workflow); general-purpose is a
# minimal task-scoped agent with no git machinery; code-reviewer is the RA
# variant (clones repo, reads diff, emits structured JSON verdict).
Archetype = Literal["coding-pipeline", "general-purpose", "code-reviewer"]

AgentType = Literal["IA", "RA"]


class WizardInputs(BaseModel):
    """Step-1 form fields.

    Identity fields flow straight into the `templates` row. The OPERATOR slot
    fields are substituted verbatim into the scaffold. The LLM-input fields
    (domain / personality_hint / working_specifics) are fed to Gemini and are
    NOT substituted directly — they only steer generation.
    """

    # ── Identity (→ templates row) ──────────────────────────────────────────
    template_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    emoji: str = Field(default="", max_length=8)
    model: str = Field(default="google/gemini-3.8-flash", min_length=1)
    description: str = Field(default="", max_length=500)
    # agent_type is selected by the operator in the form; archetype is derived
    # from it automatically (IA → coding-pipeline, RA → code-reviewer).
    agent_type: AgentType = "IA"
    archetype: Archetype = "coding-pipeline"

    # ── OPERATOR slots (substituted verbatim into the scaffold) ─────────────
    role_oneliner: str = Field(default="", max_length=300)
    # role_slug is used in the PROGRESS.md / REVIEW.md header + re-plan match
    # line. Defaults from template_id when the operator leaves it blank.
    role_slug: str = Field(default="", max_length=64)
    # previous_agent: who precedes this agent in the chain. For IA this names
    # the agent whose PROGRESS entry to focus on; for RA it names the agent
    # being reviewed. Falls back to "the previous agent" when blank.
    previous_agent: str = Field(default="", max_length=64)
    # next_agent: names the successor in the chain, written into the IA's
    # PROGRESS "Next agent" section. Not applicable to RA (verdict-driven).
    next_agent: str = Field(default="", max_length=64)
    # output_artifact: used in the general-purpose workflow frame only.
    output_artifact: str = Field(default="", max_length=200)

    # ── LLM-input fields (steer generation; never substituted directly) ─────
    domain: str = Field(default="", max_length=2000)
    personality_hint: str = Field(default="", max_length=500)
    working_specifics: str = Field(default="", max_length=2000)

    def effective_role_slug(self) -> str:
        """role_slug, falling back to template_id when blank."""
        return (self.role_slug or self.template_id).strip()

    def effective_previous_agent(self) -> str:
        return (self.previous_agent or "the previous agent").strip()

    def effective_next_agent(self) -> str:
        return (self.next_agent or "the next agent").strip()

    def effective_output_artifact(self) -> str:
        return (self.output_artifact or "the requested output").strip()


class GeneratedSections(BaseModel):
    """The role-specific prose Gemini fills in (step 2).

    Each field maps to a GEN slot in the scaffold. All are plain markdown
    fragments — no surrounding code fences, no JSON. `personality_bullets`
    must be exactly 4 bullet lines.
    """

    role_bullets: str = ""
    working_section: str = ""
    personality_bullets: str = ""

    # True when these came from Gemini; False when they are deterministic
    # fallbacks (no API key / generation failed). The UI surfaces this so the
    # operator knows to complete the sections by hand.
    ai_generated: bool = False
