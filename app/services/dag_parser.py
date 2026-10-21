"""Plan-file Task DAG parser (SHCA Phase 4 Stage 3-f).

The planner Claude session writes a markdown plan to
`<story-workspace>/docs/plans/<name>.md` with a required
`## Task DAG` section containing a fenced YAML block. This module
locates and validates that block.

Once the human approves a plan, the control panel:
  1. Reads the plan file from the gateway (shca.workspace.readFile)
  2. Parses the DAG with parse_dag_from_markdown()
  3. Validates phases against the active claude_phases set
  4. Inserts a parent "DAG task" row + N subtask rows in one transaction
     (see app/services/dag_ingest.py)

This module ONLY parses + validates. It does not touch the DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import yaml  # noqa: F401  (validated as runtime dep in requirements.txt)


# Heading line + fenced YAML block immediately following it (optionally
# separated by blank lines). DOTALL so `.` matches newlines.
_DAG_BLOCK_RE = re.compile(
    r"^##\s+Task\s+DAG\s*$\s*```(?:yaml|yml)?\s*\n(.*?)\n```",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Permitted task-id shape — keeps id values safe to inline in messages
# and into our hierarchical-id naming scheme (`<dag_id>-T1`, etc.).
_TASK_ID_RE = re.compile(r"^T\d+$")


class DagParseError(ValueError):
    """Plan file is malformed or missing the required DAG block."""


# Canonical Task DAG schema example. Single source of truth — referenced
# by the PreToolUse hook's rejection messages so Claude always gets a
# concrete template to copy when it produces an invalid plan.
CANONICAL_DAG_EXAMPLE = """## Task DAG

```yaml
tasks:
  - id: T1
    phase: coder
    title: One-line title
    description: |
      Multi-line description of what this task does.
      Concrete, scoped, executable in one Claude turn.
    acceptance: "One-line acceptance criterion (string, not list)."
    depends_on: []
    # `repo` is OPTIONAL for a single-repo story and REQUIRED for every task
    # when the story spans multiple repositories (there is no default then).
    # Use the repo directory name shown in your workspace docs.
    repo: repos/acme__web

  - id: T2
    phase: unit-tester
    title: Add tests for the engine
    description: |
      Cover happy path + edge cases.
    acceptance: "All tests pass under node --test."
    depends_on: [T1]
```

Hard rules (the parser will reject otherwise):
- Heading exactly `## Task DAG` (h2, case-insensitive, no trailing colon)
- Fence is triple-backtick + `yaml` (or `yml`) language tag
- Top-level mapping with a `tasks:` key whose value is a non-empty list
- Each task has all five required fields non-empty: id, phase, title, description, acceptance
- `id` must match `T<number>` (T1, T2, T10) — never `1`, `task-1`, `T-1`
- `phase` must be one of the registered phases (current set surfaced in the error)
- `depends_on` must be a YAML list (`[]`, `[T1]`, `[T1, T2]`) — never a string
- depends_on entries must reference existing task ids in the same DAG
- No self-reference, no cycles
- Flat list only — no nested subtasks
- `repo` is required on EVERY task when the story spans multiple repositories;
  omit it only when the story has exactly one repo
"""


@dataclass
class DagTask:
    id: str
    phase: str
    title: str
    description: str
    acceptance: str
    depends_on: list[str] = field(default_factory=list)
    # Which repo this task works in (multi-repo plan M4.3). Empty ⇒ the story's sole
    # write repo, which is the only unambiguous default; a multi-write story MUST set
    # it per task (validated in parse_dag_from_markdown when allowed_repos is given).
    # Matches a story_workspace_nodes subdir or a repo name — resolution is the
    # caller's job, since the parser is deliberately DB-free.
    repo: str = ""


@dataclass
class ParsedDag:
    tasks: list[DagTask]

    @property
    def task_ids(self) -> set[str]:
        return {t.id for t in self.tasks}


_BACKTICKED_VALUE_RE = re.compile(
    # A YAML key: value pair where the value starts and ends with a
    # backtick (markdown inline-code). Match the key indent + key + colon
    # then the backtick-wrapped value until end of line.
    r"^(\s*[A-Za-z_][A-Za-z0-9_-]*:\s*)`([^`\n]*)`(\s*)$",
    re.MULTILINE,
)


def _strip_inline_backticks(yaml_body: str) -> str:
    """Strip backticks from values like ``acceptance: `some text` ``.

    Claude tends to wrap inline code in backticks even inside YAML scalars
    (markdown formatting habit). Backticks are reserved in YAML and break
    parsing. Replace ``text`` -> "text" (double-quoted so YAML treats it
    as a flow scalar regardless of contents).
    """
    def _sub(m: re.Match) -> str:
        key_part = m.group(1)
        value = m.group(2)
        trailing = m.group(3)
        # Escape any embedded double quotes in the value.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key_part}"{escaped}"{trailing}'

    return _BACKTICKED_VALUE_RE.sub(_sub, yaml_body)


def parse_dag_from_markdown(
    plan_md: str, *, allowed_phases: set[str],
    repo_aliases: Optional[dict] = None,
) -> ParsedDag:
    """Parse the Task DAG section out of a plan markdown file.

    Returns a ParsedDag with validated tasks. Raises DagParseError on:
      * Missing `## Task DAG` section
      * YAML inside the section won't parse
      * `tasks` is not a list, or any entry has missing/wrong fields
      * Task ids aren't unique or don't match `T<number>`
      * A task references a phase not in `allowed_phases`
      * `depends_on` references a task id that doesn't exist
      * Topological cycle in the dependency graph
      * `repo` names a repo the story wasn't granted, or is MISSING on a
        multi-repo story (see below)

    `repo_aliases` (multi-repo plan M4.3) maps every identifier this story's plan may
    use for a repo → that repo's canonical workspace subdir. Several aliases resolve to
    the same repo on purpose (the directory `repos/acme__web`, the bare name `web`, and
    `acme/web`), because the planner sees more than one form in its workspace docs.

    It is a MAPPING, not a set, specifically so the alias count can never be mistaken
    for the repo count: passing a set of aliases made a one-repo story look like a
    three-repo story and demanded `repo:` on every task, rejecting every plan. The repo
    count is therefore always derived here, from the distinct subdirs.

      * None / empty  — single-repo story (or an un-migrated caller). `repo:` is
        ignored entirely and the DAG parses exactly as it always did.
      * one repo      — `repo:` is optional; that repo is the unambiguous default.
      * several repos — `repo:` is REQUIRED on every task. There is no sensible default
        when a story writes to several repos, and silently guessing would run a task
        against the wrong checkout (plan finding R9).
    """
    # Null bytes (\x00) can appear when the gateway's readFile RPC reads a
    # plan file that has binary-padding artifacts from the filesystem/CLI.
    # They are never intentional content; strip before any parsing.
    plan_md = plan_md.replace('\x00', '')

    match = _DAG_BLOCK_RE.search(plan_md)
    if not match:
        raise DagParseError(
            "plan is missing a `## Task DAG` section with a fenced YAML block. "
            "Add `## Task DAG` followed by a ```yaml ... ``` block listing tasks."
        )

    yaml_body = match.group(1).strip()
    # Pre-process: Claude commonly wraps inline code in backticks even
    # inside YAML scalar values (markdown muscle memory). Backtick is a
    # YAML reserved character so this breaks the parser. Strip leading
    # /trailing backticks on bare scalar values — only if they're
    # paired and the rest of the line doesn't have YAML structural
    # characters that suggest a complex value.
    yaml_body = _strip_inline_backticks(yaml_body)

    try:
        parsed = yaml.safe_load(yaml_body)
    except yaml.YAMLError as exc:
        raise DagParseError(f"YAML inside the Task DAG block didn't parse: {exc}")

    if not isinstance(parsed, dict) or "tasks" not in parsed:
        raise DagParseError(
            "Task DAG YAML must be a mapping with a top-level `tasks:` list."
        )

    raw_tasks = parsed["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise DagParseError("`tasks:` must be a non-empty list.")

    seen_ids: set[str] = set()
    tasks: list[DagTask] = []

    for idx, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise DagParseError(f"tasks[{idx}] must be a mapping, got {type(raw).__name__}")

        # Required fields
        required = ("id", "phase", "title", "description", "acceptance")
        for field_name in required:
            if field_name not in raw or raw[field_name] in (None, ""):
                raise DagParseError(
                    f"tasks[{idx}] is missing required field `{field_name}`"
                )

        tid = str(raw["id"]).strip()
        if not _TASK_ID_RE.match(tid):
            raise DagParseError(
                f"tasks[{idx}].id must match T<number> (e.g. T1, T2); got {tid!r}"
            )
        if tid in seen_ids:
            raise DagParseError(f"duplicate task id {tid!r}")
        seen_ids.add(tid)

        phase = str(raw["phase"]).strip()
        if phase not in allowed_phases:
            raise DagParseError(
                f"tasks[{idx}] ({tid}) phase {phase!r} is not in the active phase "
                f"set: {sorted(allowed_phases)}"
            )

        deps_raw = raw.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise DagParseError(
                f"tasks[{idx}] ({tid}) depends_on must be a list, got {type(deps_raw).__name__}"
            )
        deps = [str(d).strip() for d in deps_raw]
        for d in deps:
            if not _TASK_ID_RE.match(d):
                raise DagParseError(
                    f"tasks[{idx}] ({tid}) depends_on entry {d!r} must match T<number>"
                )

        # `repo` (multi-repo M4.3). Validated only when the caller told us which repos
        # exist — the parser never touches the DB, so with no allowed_repos it cannot
        # distinguish a typo from a legitimate name and stays out of the way.
        repo = str(raw.get("repo") or "").strip()
        if repo_aliases:
            # Distinct SUBDIRS = repositories. Never len(repo_aliases), which counts
            # aliases (3 per repo) and would treat every single-repo story as multi-repo.
            distinct_repos = sorted(set(repo_aliases.values()))
            if repo and repo not in repo_aliases:
                raise DagParseError(
                    f"tasks[{idx}] ({tid}) repo {repo!r} is not one of this story's "
                    f"repos: {distinct_repos}"
                )
            if not repo and len(distinct_repos) > 1:
                raise DagParseError(
                    f"tasks[{idx}] ({tid}) is missing required field `repo`. This "
                    f"story spans multiple repositories ({distinct_repos}), so every "
                    f"task must name the repo it works in — there is no default."
                )
            # Deliberately NOT auto-filled for the single-repo case. An empty `repo`
            # already means "the story's sole write repo" (see DagTask.repo), and
            # filling it in made tasks.repo_subdir non-NULL for ordinary single-repo
            # stories — which changed every dispatch prompt with the feature flag off
            # and contradicted what tasks_repo_subdir.sql documents.

        tasks.append(DagTask(
            id=tid,
            phase=phase,
            title=str(raw["title"]).strip(),
            description=str(raw["description"]).rstrip(),
            acceptance=str(raw["acceptance"]).strip(),
            depends_on=deps,
            repo=repo,
        ))

    # Validate dependency references AFTER all tasks are seen so forward
    # references would also be caught — but our id pattern + ordering
    # convention means deps should reference earlier ids.
    all_ids = {t.id for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            if dep not in all_ids:
                raise DagParseError(
                    f"{t.id} depends_on references unknown task {dep!r}"
                )
            if dep == t.id:
                raise DagParseError(f"{t.id} depends on itself")

    # Cycle detection (Kahn's algorithm, simple topo sort).
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    edges: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            edges[dep].append(t.id)
            in_degree[t.id] += 1

    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    sorted_count = 0
    while queue:
        tid = queue.pop(0)
        sorted_count += 1
        for successor in edges[tid]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if sorted_count != len(tasks):
        # Names of nodes still in a non-zero in-degree state
        cycle_nodes = [tid for tid, deg in in_degree.items() if deg > 0]
        raise DagParseError(
            f"dependency cycle detected involving: {', '.join(sorted(cycle_nodes))}"
        )

    return ParsedDag(tasks=tasks)


def topological_order(dag: ParsedDag) -> list[str]:
    """Return task ids in dependency-satisfying execution order.

    Ties are broken by original list order (stable Kahn's). Assumes the
    DAG has already been validated (no cycles).
    """
    in_degree: dict[str, int] = {t.id: 0 for t in dag.tasks}
    edges: dict[str, list[str]] = {t.id: [] for t in dag.tasks}
    order_index: dict[str, int] = {t.id: i for i, t in enumerate(dag.tasks)}

    for t in dag.tasks:
        for dep in t.depends_on:
            edges[dep].append(t.id)
            in_degree[t.id] += 1

    ready = sorted(
        [tid for tid, deg in in_degree.items() if deg == 0],
        key=lambda x: order_index[x],
    )
    result: list[str] = []
    while ready:
        tid = ready.pop(0)
        result.append(tid)
        # Sort newly-ready successors by their original list position so
        # the order matches what the planner wrote.
        new_ready: list[str] = []
        for successor in edges[tid]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                new_ready.append(successor)
        new_ready.sort(key=lambda x: order_index[x])
        # Merge into the queue, preserving order
        ready = new_ready + ready

    return result
