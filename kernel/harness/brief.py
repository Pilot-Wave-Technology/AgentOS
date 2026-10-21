"""Brief generator — E6b increment of Stage E.

Two-layer brief generated at leg boundary:

Layer 1 (mechanical): extracted from step_journal rows. Files read, search
targets, files written, refusals, commits. Always available, even on a
mid-edit kill.

Layer 2 (model-authored, harness-prompted): what_mattered (relevance per file),
next_step (intention, UNVERIFIED), summary (accomplishment). These are model
claims — the model is the thing whose claim is in doubt, so UNVERIFIED is
correct for model-authored fields. If the leg was killed before the model
could answer the boundary prompt, layer 2 is absent.

The brief is stored as a checkpoint (kind='leg_brief') and injected into
leg 2's system prompt for rehydration (E6).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RefusalEntry:
    """A tool call that was denied by the interceptor chain."""
    tool: str
    target: Optional[str]
    code: str       # E_ENVELOPE_VIOLATION, E_TOOL_DENIED, etc.
    corrective: str  # what IS allowed


@dataclass
class LegBrief:
    """Two-layer brief for a completed or killed leg.

    Layer 1 fields are always populated (from journal). Layer 2 fields
    are None if the leg was killed before the boundary prompt.
    """
    # Layer 1: mechanical (from journal)
    files_read: list[str] = field(default_factory=list)
    search_targets: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    refusals: list[RefusalEntry] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)  # SHAs

    # Still layer 1: the leg's own narration, taken from the journal rather
    # than from a boundary turn, so it survives a kill like the rest of layer 1.
    # Bounded on read — see NARRATION_KEEP.
    narration: list[str] = field(default_factory=list)

    # A2b -- BACKGROUND JOBS STILL RUNNING AS THIS LEG ENDED. Layer 1,
    # but from the MIRROR rather than the journal: a job id lives in a
    # tool RESULT and the journal keeps calls, so there is nothing in
    # `step_journal` to read it from. Without this the next leg inherits
    # a running suite it cannot name and cannot poll -- the job survives
    # the boundary, only the model's memory of it does not.
    live_jobs: list[dict] = field(default_factory=list)

    # Layer 2: model-authored (harness-prompted, UNVERIFIED)
    what_mattered: Optional[dict] = None   # file -> relevance judgement
    # E6d: approaches tried and abandoned. The highest value per token in a
    # resumption summary — it is what stops leg 2 spending its budget
    # re-walking a path leg 1 already proved wrong. Claude Code's compaction
    # carries the same thing under "errors and fixes"; ours had only typed
    # tool refusals, which say nothing about an approach that merely failed.
    dead_ends: Optional[str] = None
    next_step: Optional[str] = None        # model-stated intention
    summary: Optional[str] = None          # model-stated accomplishment


# ── Layer 1: extract mechanical facts from journal ───────────────────────────

async def _live_jobs(conn, execution_id: int) -> list[dict]:
    """Jobs the mirror says are still going. Never raises.

    NOT FROM THE JOURNAL, which is why this is a second query in a
    function named for the journal. `brief.py` already records the
    limitation for commits -- "the SHA is in the tool result which is in
    the messages, not the journal" -- and a job id is the same shape.

    A brief that fails to mention a running job is worse than one that
    fails to build, so a missing table or an unreadable row answers with
    nothing rather than raising into the leg boundary.
    """
    try:
        rows = await conn.fetch(
            "SELECT job_id, command, produced, "
            "       EXTRACT(EPOCH FROM (now() - last_seen_at))::int AS quiet_s "
            "  FROM kernel.execution_jobs "
            " WHERE execution_id = $1 AND running ORDER BY started_at",
            execution_id)
    except Exception:                                       # noqa: BLE001
        logger.debug("brief: could not read live jobs", exc_info=True)
        return []
    return [{"job": r["job_id"], "command": (r["command"] or "")[:160],
             "bytes": r["produced"], "quiet_s": r["quiet_s"]} for r in rows]


async def extract_journal_facts(
    conn,
    execution_id: int,
    generation: int,
) -> LegBrief:
    """Extract mechanical facts from step_journal for one leg.

    Reads all journal rows for this execution+generation and classifies
    tool calls into files_read, search_targets, files_written, refusals,
    and commits.

    Raises ValueError on invalid input — callers must not pass None or
    negative IDs, which would silently return an empty brief and hide
    a real problem.
    """
    if execution_id is None or execution_id < 0:
        raise ValueError(f"execution_id must be a non-negative int, got {execution_id!r}")
    if generation is None or generation < 0:
        raise ValueError(f"generation must be a non-negative int, got {generation!r}")

    rows = await conn.fetch(
        """
        SELECT tool, args::text, cost::text, model_text
        FROM step_journal
        WHERE execution_id = $1 AND generation = $2
        ORDER BY seq
        """,
        execution_id,
        generation,
    )

    files_read: list[str] = []
    search_targets: list[str] = []
    files_written: list[str] = []
    refusals: list[RefusalEntry] = []
    commits: list[str] = []
    narration: list[str] = []

    def _text(value) -> str:
        """Journalled args come from the MODEL and are not guaranteed to be
        strings. A chaos stub sent {"path": 12345} and the int travelled all
        the way into the brief, where joining the read-set raised
        `sequence item 0: expected str instance, int found` and the boundary
        prompt was abandoned — the brief lost, on the leg whose brief
        matters. Coerced here, at the point untrusted args become facts."""
        return value if isinstance(value, str) else ("" if value is None
                                                     else str(value))

    for row in rows:
        # Prose the model produced on this step. Kept in order; trimmed to the
        # last NARRATION_KEEP entries below so a long leg cannot crowd out the
        # facts underneath it.
        said = (row["model_text"] or "").strip()
        if said:
            narration.append(said[:NARRATION_CHARS])

        tool = row["tool"]
        args_str = row["args"]
        args = json.loads(args_str) if args_str else {}

        if tool == "read":
            path = _text(args.get("path", ""))
            if path and path not in files_read:
                files_read.append(path)

        elif tool == "write":
            path = _text(args.get("path", ""))
            if path and path not in files_written:
                files_written.append(path)

        elif tool == "bash":
            command = _text(args.get("command", ""))
            # Extract search targets from grep/glob/find commands
            if command:
                targets = _extract_search_targets(command)
                for t in targets:
                    if t not in search_targets:
                        search_targets.append(t)

        elif tool == "story_commit":
            # Commits are stored as the output (SHA) — but we only have
            # args here, not the result. The SHA is in the tool result
            # which is in the messages, not the journal.
            # For now, record that a commit was attempted.
            message = _text(args.get("message", ""))
            if message:
                commits.append(message)

    # Refusals: we need to check tool results, but step_journal only stores
    # tool name + args, not the result. Refusals are surfaced when the
    # executor returns exit_code=1 with an E_ code — these appear in the
    # conversation messages, not in step_journal.
    # For the mechanical layer, we record refusals from the journal's
    # result_ref column if available. Since step_journal doesn't have
    # result_ref populated in the current schema, refusals come from
    # a separate query or from the conversation messages passed to the
    # brief generator.

    return LegBrief(
        files_read=files_read,
        search_targets=search_targets,
        files_written=files_written,
        refusals=refusals,
        commits=commits,
        narration=narration[-NARRATION_KEEP:],
        live_jobs=await _live_jobs(conn, execution_id),
    )


# How much of the previous leg's narration travels. Leg 2 starts from a fresh
# conversation — the brief is its entire inheritance — but narration is the
# least reliable thing in it, so it is bounded hard and never crowds out facts.
NARRATION_KEEP = 3
NARRATION_CHARS = 400

_SEARCH_COMMANDS = {"grep", "egrep", "fgrep", "rg", "ack", "find", "ls",
                    "glob", "list_dir"}
_SHELL_SEPARATORS = {"|", "&&", "||", ";", ">", ">>", "2>", "&"}
# find flags whose VALUE is the pattern being searched for.
_FIND_PATTERN_FLAGS = {"-name", "-iname", "-path", "-ipath", "-regex"}
# find flags that take a value which is NOT a search target. Without these,
# `find . -type f -name "*.py"` records `f` as something the leg searched for —
# noise that could later collide with a real target and fake a repeat.
_FIND_VALUE_FLAGS = {
    "-type", "-maxdepth", "-mindepth", "-size", "-perm", "-user", "-group",
    "-newer", "-mtime", "-atime", "-ctime", "-not", "-anewer",
}


def _extract_search_targets(command: str) -> list[str]:
    """Extract search patterns and paths from a bash command.

    Tokenised, not regex-matched, so that **flags never enter the read-set**.
    The regex version captured `-R` from `ls -R` as a search target, because its
    optional flag group matched zero times and the operand group took the flag.
    That silently destroys E6b's orientation-lever measurement: leg 2
    "repeating" the target `-R` says nothing whatever about whether it repeated
    leg 1's search, so the assertion passes while measuring noise.

    Targets are normalised `command:operand` signatures, following LR5's
    `(tool, target)` hashing. A search command with no operand still records
    one entry — `ls:.` for a bare `ls -R` — because "list everything from here"
    IS a locating search, and leg 2 repeating it is precisely what E6b's
    orientation lever must detect. Recording only operands loses that case
    entirely and leaves the read-set empty for a leg that plainly oriented.

    Operands are collected per search command until a shell separator. Flags are
    skipped, except find's pattern-bearing flags whose value IS the pattern.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:          # unbalanced quotes — fall back to whitespace
        tokens = command.split()

    targets: list[str] = []
    i = 0
    while i < len(tokens):
        base = tokens[i].rsplit("/", 1)[-1]
        if base not in _SEARCH_COMMANDS:
            i += 1
            continue
        is_find = base == "find"
        i += 1
        operands: list[str] = []
        while i < len(tokens) and tokens[i] not in _SHELL_SEPARATORS:
            arg = tokens[i]
            if arg.startswith("-"):
                if is_find and arg in _FIND_PATTERN_FLAGS and i + 1 < len(tokens):
                    operands.append(tokens[i + 1])
                    i += 2
                    continue
                if is_find and arg in _FIND_VALUE_FLAGS:
                    i += 2          # skip the flag AND its value
                    continue
                i += 1
                continue
            operands.append(arg)
            i += 1
        # No operand still counts as one search of the current directory.
        targets.extend(f"{base}:{o}" for o in (operands or ["."]))
    return targets




# ── Layer 2: boundary prompt for model-authored fields ───────────────────────

BOUNDARY_PROMPT = """You are about to be paused. Before you stop, write a brief summary for whoever continues this work.

Answer these four questions concisely:

1. **What mattered**: For each file you read, was it relevant to the task? List files and one line each saying why it mattered or why it didn't.

2. **Next step**: What were you about to do next? State your intention, not the last action you took.

3. **Dead ends**: What did you TRY that did not work, and why? An approach you
   abandoned, a file that looked right and wasn't, a command that failed for a
   reason worth knowing. This is the highest-value thing you can leave behind —
   without it whoever continues will walk the same path you already proved wrong.
   Write "none" if there were none.

4. **Summary**: What did you accomplish so far?

Format your response as:
## What mattered
- path/to/file.py — relevant: contains the function being modified
- path/to/other.py — irrelevant: checked but has no connection to the task

## Next step
[Your intention here]

## Dead ends
- tried X — it failed because Y

## Summary
[What you accomplished]
"""


def build_boundary_prompt(brief: "LegBrief") -> str:
    """BOUNDARY_PROMPT with the journal's mechanical facts inlined.

    The facts are restated in the prompt so the model can answer **without
    relying on its conversation history**. A leg that is near context overflow
    (LR2), or whose history was trimmed, must still be able to say what
    mattered — otherwise the one moment we need the brief is the moment the
    brief cannot be written. Restating costs a few hundred tokens and removes
    the dependency entirely.
    """
    lines = [BOUNDARY_PROMPT, "", "---", "For reference, this leg's recorded activity:"]
    if brief.files_read:
        lines.append(f"Files read: {', '.join(brief.files_read)}")
    if brief.files_written:
        lines.append(f"Files written: {', '.join(brief.files_written)}")
    if brief.search_targets:
        lines.append(f"Searches run: {', '.join(brief.search_targets)}")
    if brief.commits:
        lines.append(f"Commits made: {'; '.join(brief.commits)}")
    if brief.refusals:
        lines.append(f"Refusals: {len(brief.refusals)}")
    if len(lines) == 4:                      # nothing recorded
        lines.append("(no tool activity recorded for this leg)")
    return "\n".join(lines)


def parse_model_brief_response(response_text: str) -> dict:
    """Parse the model's response to the boundary prompt.

    Returns dict with what_mattered, next_step, summary — or empty dict
    if the response cannot be parsed or is empty.
    """
    if not response_text or not response_text.strip():
        return {}

    result = {}

    # Extract "What mattered" section
    what_mattered_match = re.search(
        r'##\s*What mattered\s*\n(.*?)(?=##|\Z)',
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    if what_mattered_match:
        what_mattered = {}
        for line in what_mattered_match.group(1).strip().split('\n'):
            line = line.strip().lstrip('- ')
            if ' — ' in line:
                path, judgement = line.split(' — ', 1)
                what_mattered[path.strip()] = judgement.strip()
            elif ': ' in line:
                path, judgement = line.split(': ', 1)
                what_mattered[path.strip()] = judgement.strip()
        if what_mattered:
            result['what_mattered'] = what_mattered

    # Extract "Next step" section
    next_step_match = re.search(
        r'##\s*Next step\s*\n(.*?)(?=##|\Z)',
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    if next_step_match:
        next_step = next_step_match.group(1).strip()
        if next_step:
            result['next_step'] = next_step

    # Extract "Dead ends" section (E6d)
    dead_ends_match = re.search(
        r'##\s*Dead ends\s*\n(.*?)(?=##|\Z)',
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    if dead_ends_match:
        dead_ends = dead_ends_match.group(1).strip()
        # "none" is a real answer and must not become a section that says
        # nothing — an empty heading in every brief trains the reader to
        # skip it, including in the briefs where it is populated.
        if dead_ends and dead_ends.lower().strip(" .-") != "none":
            result['dead_ends'] = dead_ends

    # Extract "Summary" section
    summary_match = re.search(
        r'##\s*Summary\s*\n(.*?)(?=##|\Z)',
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    if summary_match:
        summary = summary_match.group(1).strip()
        if summary:
            result['summary'] = summary

    return result


# ── Generate the full brief ──────────────────────────────────────────────────

async def generate_brief(
    conn,
    execution_id: int,
    generation: int,
    model_response: Optional[str] = None,
) -> LegBrief:
    """Generate a two-layer brief for a completed or killed leg.

    Layer 1 is always extracted from the journal. Layer 2 is parsed from
    model_response if provided (the model's answer to the boundary prompt).
    If model_response is None (killed before prompt), layer 2 fields are None.
    """
    brief = await extract_journal_facts(conn, execution_id, generation)

    if model_response:
        parsed = parse_model_brief_response(model_response)
        brief.what_mattered = parsed.get('what_mattered')
        brief.next_step = parsed.get('next_step')
        brief.dead_ends = parsed.get('dead_ends')
        brief.summary = parsed.get('summary')

    return brief


# ── Format brief for prompt injection ────────────────────────────────────────

def format_brief_for_prompt(brief: LegBrief) -> str:
    """Render the brief as structured text for system prompt injection.

    Refusals render in their own block above the read-set.
    Model-authored fields render with [UNVERIFIED] tag.
    """
    sections = []

    # A2b -- STILL RUNNING, ABOVE EVERYTHING. A job survives a leg
    # boundary; only the model's memory of its id does not. A new leg
    # that does not know a suite is running will start a second one, or
    # sit waiting for work that is already being done. This is the first
    # thing it needs and the cheapest to state.
    if brief.live_jobs:
        lines = ["## Background jobs still running"]
        for job in brief.live_jobs:
            quiet = job.get("quiet_s")
            tail = (f", last output {quiet}s ago"
                    if isinstance(quiet, int) else "")
            lines.append(
                f"- {job['job']} · {job.get('bytes', 0)} bytes{tail} · "
                f"{job.get('command', '')}")
        lines.append(
            "These are STILL GOING from the previous leg. Do not start "
            "them again. Wait for one with bash_wait(job_id=...), or look "
            "at it with bash_output; bash_jobs lists them.")
        sections.append("\n".join(lines))

    # Refusals first (own block, above everything else)
    if brief.refusals:
        lines = ["## Refusals (from previous leg)"]
        for r in brief.refusals:
            lines.append(f"- {r.tool} target={r.target}: {r.code} — {r.corrective}")
        sections.append('\n'.join(lines))

    # Mechanical read-set
    if brief.files_read or brief.search_targets:
        lines = ["## Read-set (from previous leg)"]
        if brief.files_read:
            lines.append("Files read:")
            for f in brief.files_read:
                lines.append(f"  - {f}")
        if brief.search_targets:
            # Say only what is true. This line has now been wrong twice:
            # first as "do not repeat these" (a prohibition — re-reading to
            # re-orient is normal, and E6c makes the tree authoritative, so
            # telling a model not to look contradicts the harness's own rule),
            # then as "their results are above" (false — tool OUTPUT is never
            # persisted; step_journal.result_ref exists and nothing writes it,
            # so what the previous leg saw is gone, not merely uncarried).
            # Until results are journalled, the honest claim is the narrow one:
            # these searches were run, and the reader must decide for itself.
            lines.append(
                "Searches the previous leg ran (output not retained):"
            )
            for t in brief.search_targets:
                lines.append(f"  - {t}")
        sections.append('\n'.join(lines))

    # The leg's own words, last few steps. Marked UNVERIFIED for the same
    # reason next_step is: it is the model describing itself, and one measured
    # run's stated next step named work it had already finished. It sits after
    # the mechanical facts so it can inform them, never replace them.
    if brief.narration:
        lines = ["## What the previous leg was saying [UNVERIFIED — its own words]"]
        for note in brief.narration:
            lines.append(f"  - {note}")
        sections.append(chr(10).join(lines))

    # Files written
    if brief.files_written:
        lines = ["## Files modified (from previous leg)"]
        for f in brief.files_written:
            lines.append(f"  - {f}")
        sections.append('\n'.join(lines))

    # Commits
    if brief.commits:
        lines = ["## Commits (from previous leg)"]
        for c in brief.commits:
            lines.append(f"  - {c}")
        sections.append('\n'.join(lines))

    # Model-authored: what mattered [UNVERIFIED]
    if brief.what_mattered:
        lines = ["## What mattered [UNVERIFIED — check the tree]"]
        for path, judgement in brief.what_mattered.items():
            lines.append(f"- {path} — {judgement}")
        sections.append('\n'.join(lines))

    # Model-authored: next step [UNVERIFIED]
    if brief.next_step:
        sections.append(
            f"## Next step [UNVERIFIED — check the tree before acting]\n"
            f"{brief.next_step}"
        )

    # E6d: dead ends. Rendered BEFORE the summary and after the next step,
    # because it is the part that changes what the next leg does — a
    # summary describes what happened, this says which paths are already
    # known to be wrong. UNVERIFIED like the rest of layer 2: it is the
    # model's account of its own failures.
    if brief.dead_ends:
        sections.append(
            f"## Already tried, did not work [UNVERIFIED — its own account]\n"
            f"{brief.dead_ends}"
        )

    # Model-authored: summary
    if brief.summary:
        sections.append(f"## Summary (from previous leg)\n{brief.summary}")

    if not sections:
        return "## Previous leg brief\nNo information available from previous leg."

    return '\n\n'.join(sections)


# ── Serialisation for checkpoint storage ─────────────────────────────────────

def brief_to_dict(brief: LegBrief) -> dict:
    """Serialise a LegBrief to a dict for checkpoint storage."""
    return {
        "files_read": brief.files_read,
        "search_targets": brief.search_targets,
        "files_written": brief.files_written,
        "refusals": [
            {"tool": r.tool, "target": r.target, "code": r.code, "corrective": r.corrective}
            for r in brief.refusals
        ],
        "commits": brief.commits,
        "narration": brief.narration,
        "what_mattered": brief.what_mattered,
        "dead_ends": brief.dead_ends,
        "next_step": brief.next_step,
        "summary": brief.summary,
    }


def dict_to_brief(data: dict) -> LegBrief:
    """Deserialise a LegBrief from a checkpoint dict."""
    return LegBrief(
        files_read=data.get("files_read", []),
        search_targets=data.get("search_targets", []),
        files_written=data.get("files_written", []),
        refusals=[
            RefusalEntry(**r) for r in data.get("refusals", [])
        ],
        commits=data.get("commits", []),
        narration=data.get("narration", []),
        what_mattered=data.get("what_mattered"),
        dead_ends=data.get("dead_ends"),
        next_step=data.get("next_step"),
        summary=data.get("summary"),
    )
