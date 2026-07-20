"""Guided IA template authoring.

The "New Template" wizard collects a small structured form (WizardInputs),
asks Gemini to author the role-specific sections (generate_role_sections),
and slots them into a fixed canonical scaffold (assemble). The operator
reviews/edits the result before it's saved as a `templates` row.

Import from this package, not from sub-modules directly.
"""
from app.services.template_authoring.assembler import assemble
from app.services.template_authoring.generator import generate_role_sections
from app.services.template_authoring.models import (
    Archetype,
    GeneratedSections,
    WizardInputs,
)
from app.services.template_authoring.scaffolds import ARCHETYPES, get_archetype

__all__ = [
    "ARCHETYPES",
    "get_archetype",
    "assemble",
    "generate_role_sections",
    "Archetype",
    "GeneratedSections",
    "WizardInputs",
]
