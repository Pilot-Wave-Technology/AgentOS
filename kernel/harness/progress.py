"""Progress memory — E6c increment of Stage E.

The harness prompts the model to write completion state to a sibling file
`stories/<id>/plans/cycle-NN-<slug>.progress.md`. The prompt is the harness's,
not the model's — three agents told explicitly to keep notes produced 1 commit,
0 and 0. No prompt instruction about updating is load-bearing.

Shape constraints:
  - The file is a SIBLING of the plan, never inside it.
  - register_plan(ref, hash) binds approval to the plan body; the progress
    file is separate so the hash is unaffected.
  - writable_scope must include the plan directory path, or the chain refuses
    the write.
  - The harness seeds an empty progress file when there is none.

The prompt is issued:
  - Approaching a leg boundary (when a predicate is about to fire).
  - On a step interval (every N steps, configurable).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Progress prompt (harness-issued, not model-volunteered) ──────────────────

PROGRESS_PROMPT = """Update your progress. Write the current state of the task to the progress file.

Use this exact format:
## Progress
- [x] Step completed: description
- [ ] Step not done: description
## Current focus
What you are working on right now — one line.

Write the progress file now using the write tool. The file path is: {progress_path}
"""


@dataclass
class ProgressBlock:
    """Parsed progress state from a progress.md file."""
    steps_done: list[str] = field(default_factory=list)
    steps_todo: list[str] = field(default_factory=list)
    current_focus: Optional[str] = None
    raw_text: str = ""


def derive_progress_path(plan_path: str) -> str:
    """Derive the progress file path from the plan file path.

    The progress file is a sibling: same directory, same base name with
    .progress.md suffix instead of .md.

    Example:
      plan:     stories/abc123/plans/cycle-02-rehydration.md
      progress: stories/abc123/plans/cycle-02-rehydration.progress.md
    """
    if plan_path.endswith(".md"):
        return plan_path[:-3] + ".progress.md"
    return plan_path + ".progress.md"


def seed_progress_file(progress_path: str) -> None:
    """Seed an empty progress file if it doesn't exist.

    Also the errand case — not scaffolding to be thrown away.
    """
    if not os.path.exists(progress_path):
        os.makedirs(os.path.dirname(progress_path), exist_ok=True)
        with open(progress_path, "w") as f:
            f.write("## Progress\n\n## Current focus\n")


def parse_progress_file(content: str) -> ProgressBlock:
    """Parse a progress.md file into a ProgressBlock.

    Returns an empty ProgressBlock for None or empty content — never raises,
    never returns None.
    """
    if not content or not content.strip():
        return ProgressBlock(raw_text=content or "")

    block = ProgressBlock(raw_text=content)

    # Parse checked items: - [x] ...
    for match in re.finditer(r'^\s*-\s*\[x\]\s*(.+)$', content, re.MULTILINE):
        block.steps_done.append(match.group(1).strip())

    # Parse unchecked items: - [ ] ...
    for match in re.finditer(r'^\s*-\s*\[\s\]\s*(.+)$', content, re.MULTILINE):
        block.steps_todo.append(match.group(1).strip())

    # Parse current focus
    focus_match = re.search(
        r'##\s*Current focus\s*\n(.+?)(?=\n##|\Z)',
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if focus_match:
        focus = focus_match.group(1).strip()
        if focus:
            block.current_focus = focus

    return block


def format_progress_for_brief(progress: ProgressBlock) -> str:
    """Render progress state for inclusion in the brief.

    Marked [UNVERIFIED] — the last thing a killed leg wrote is by definition
    what was in flight, and one run's stated next step named work already done.
    The tree stays authoritative.

    Returns a safe default for None — never raises.
    """
    if progress is None:
        return "## Completion state [UNVERIFIED — tree is authoritative]\nNo progress recorded yet."

    lines = ["## Completion state [UNVERIFIED — tree is authoritative]"]

    if progress.steps_done:
        for s in progress.steps_done:
            lines.append(f"- [x] {s}")
    if progress.steps_todo:
        for s in progress.steps_todo:
            lines.append(f"- [ ] {s}")

    if progress.current_focus:
        lines.append(f"\nCurrent focus: {progress.current_focus}")

    if not progress.steps_done and not progress.steps_todo:
        lines.append("No progress recorded yet.")

    return '\n'.join(lines)


def make_progress_prompt(progress_path: str) -> str:
    """Build the progress update prompt with the file path filled in."""
    return PROGRESS_PROMPT.format(progress_path=progress_path)


def should_prompt_progress(step_index: int, step_interval: int = 5) -> bool:
    """Whether to issue a progress prompt at this step.

    Issued every step_interval steps (default 5). The leg boundary prompt
    is handled separately by the caller.
    """
    return step_index > 0 and step_index % step_interval == 0
