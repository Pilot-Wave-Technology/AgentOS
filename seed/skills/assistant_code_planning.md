---
name: assistant_code_planning
title: Assistant Code Planning (unified)
description: Single skill for ALL planning situations on a code story — first plan, edit-in-place revisions, and new-cycle rework. Picks mode from state snapshot. Points Claude to CLAUDE.md for the live phase registry; enforces YAML DAG schema and absolute paths.
applicable_to: SHCA
status: active
version: 12
---
# Code Planning

You're the planning skill for **code stories**. One skill, three modes.

---

## 🚨 NON-NEGOTIABLE RULES (relay these to Claude in every briefing)

### 1. Valid phases ONLY. No invented names.

Every task in the DAG must use a `phase` value from the active phase registry. The current
valid phases are listed in Claude's **CLAUDE.md** under the `## The N phases` section —
Claude reads CLAUDE.md automatically every turn.

When briefing Claude, include this instruction verbatim:
> "Read the `## The N phases` section in CLAUDE.md for the current list of valid `phase`
> values before writing any task. Using a phase not in that list will cause the DAG parser
> to reject your plan."

Names like `scaffold`, `markup`, `styling`, `verification`, `developer`, `qa` etc. are
**NOT** valid. Bundle work that doesn't fit a registered phase into a `coder` task instead.

### 2. The Task DAG is YAML inside a fenced code block. NOT text/ascii.

The control panel parses the DAG by finding a `## Task DAG` heading followed by a ```yaml ... ``` block. Text diagrams (`T1 → T2`), prose lists, and ASCII art are ALL rejected.

Exact required format:

````
## Task DAG

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

  - id: T2
    phase: unit-tester
    title: Add tests for the engine
    description: |
      Cover happy path + edge cases.
    acceptance: "All tests pass under node --test."
    depends_on: [T1]
```
````

Hard rules (the parser rejects anything else):
- Heading exactly `## Task DAG` (h2, case-insensitive, no trailing colon)
- Fence triple-backtick + `yaml` language tag
- Top-level mapping with a `tasks:` key whose value is a non-empty YAML list
- Each task: `id`, `phase`, `title`, `description`, `acceptance` — all required, non-empty
- `id` matches `T<number>` (T1, T2, T10) — never `1`, `task-1`, `T-1`
- `phase` must be one of the active phases listed in CLAUDE.md
- `depends_on` is a YAML list (`[]`, `[T1]`, `[T1, T2]`) — never a string
- depends_on entries reference existing task ids
- Flat list — NO nested subtasks
- Inline backticks in YAML values break the parser — use plain or double-quoted strings

### 3. Workspace structure — cwd is the workspace root, not the repo

```
<workspace_root>            ← Claude's cwd ($CLAUDE_PROJECT_DIR)
├── repo/                    ← single-branch git checkout (the actual project)
│   ├── stories/<story_id>/
│   │   ├── scope.md         ← living story description (created once, edited rarely)
│   │   └── plans/
│   │       └── cycle-NN-<slug>.md   ← one plan per cycle (NN=01,02,…), frozen after approval
│   ├── src/, tests/, etc.   ← the project's own code
│   └── ...
├── IDENTITY.md              ← workspace metadata (Assistant reads, Claude can read)
├── RESOURCES.md             ← project resources + git credentials
├── .claude/settings.json    ← permissions + hooks
└── skills/<skill>/SKILL.md  ← Assistant reads these (Claude shouldn't)
```

Use **absolute paths** with `$CLAUDE_PROJECT_DIR` for ALL plan writes:

- ✅ `$CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/cycle-01-foo.md`
- ❌ `repo/stories/<story_id>/plans/cycle-01-foo.md` (breaks if cwd drifts into `repo/`)
- ❌ `stories/<story_id>/plans/cycle-01-foo.md` (only works from `repo/`)

The PreToolUse validator hook rejects writes that would create a nested `repo/repo/` directory. Avoid the round-trip — use absolute paths up front.

---

## Decision tree (run at the start of every planning turn)

```
1. Does `current_plan_id` exist in the snapshot?
     NO   → MODE A: FIRST PLAN
     YES  → go to 2

2. Has any subtask of the current DAG ever left state='pending'?
   (Check next_action + recent_progress: if any subtask has dispatched or finished, you are in MODE C.)
     NO   → MODE B: EDIT IN PLACE
     YES  → MODE C: NEW CYCLE
```

If you can't tell from the snapshot, call `task_activity` for the DAG's subtask states.

## Your role (all modes)

You drive planning WITH Claude, but you are still an assistant: answer the human's
status/meta questions yourself from state; send Claude ONLY genuine planning input or
plan-revision feedback, and check claude_code is not busy first (see SOUL triage). Claude
writes the plan files; you handle the human relationship.

- Send genuine planning input to Claude via `claude_code_communication({message})` — first triage per SOUL: answer status/meta questions yourself, forward only real planning input/feedback, and check claude_code is not busy
- Relay Claude output → chat (verbatim, optional one-line prefix)
- Surface plan-ready moments with a concise paraphrase
- **Never write plan files yourself**
- **Never invent phase names or paraphrase the schema** — pass the rules above verbatim into Claude's briefing
- Don't transition phase or dispatch tasks outside the transitions AGENTS.md gives you

You talk to Claude Code ONLY via `claude_code_communication({message})` — it resolves and resumes the right session for this story automatically. The snapshot's `claude_code` field is an opaque status, not an id: `ready` (send now), `not_started` (your first message starts the session), `provisioning` (workspace still being set up — wait, tell the human), `busy` (a phase or another turn holds the session — tell the human to resend later; do NOT retry).

## Reads vs writes

Multiple stories share the repo. Each writes only its own scope + plans; all read each other's.

- **Writes**: ONLY into `$CLAUDE_PROJECT_DIR/repo/stories/<this_story_id>/` (its `scope.md` and `plans/`). Never touch other stories' files.
- **Reads**: Free across the entire `$CLAUDE_PROJECT_DIR/repo/stories/` tree.

### Typical multi-story flow

```
repo/stories/
  a9520866/   ← shipped story "Calculator core"
    scope.md
    plans/
      cycle-01-impl.md
      cycle-02-modulo.md
  c4e7f231/   ← in-progress story "Calculator history"
    scope.md
    plans/
      cycle-01-history-pane.md
  <new_id>/   ← THIS story
```

Claude reads sibling scope/cycle files for context, writes only into its own dir.

---

## MODE A — FIRST PLAN

No `current_plan_id`. Phase is `planning`. Two files to produce: scope.md + cycle-01-<slug>.md.

### What to tell Claude (pass as the `message` to `claude_code_communication`)

```json
{
  "message": "First planning turn for this story (story_id=<from snapshot>).\n\n=== WORKSPACE STRUCTURE ===\nYour cwd is the workspace root ($CLAUDE_PROJECT_DIR). The single-branch git checkout is at $CLAUDE_PROJECT_DIR/repo/. Your story's scope + plans live at $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/ (scope.md at the story root, cycle plans under plans/). USE ABSOLUTE PATHS for all plan reads/writes — relative paths will break if your shell cwd drifts into repo/.\n\n=== STEP 1: explore ===\nList $CLAUDE_PROJECT_DIR/repo/stories/ to see if sibling stories have planned things before. Read their scope.md + plans/cycle-*.md for context — codebase conventions, prior decisions, what's already been built. These are frozen history; DO NOT modify or delete them.\n\nThen scan $CLAUDE_PROJECT_DIR/repo/ to understand the code itself (relevant files, conventions).\n\n=== STEP 2: ask clarifying questions ===\nBefore proposing a plan, ask the human 2-4 focused scoping questions. Iterate until the scope is clear.\n\n=== STEP 3: write two files ===\n\n1. $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/scope.md — 2-3 paragraphs describing the overall story scope, user-visible behaviour, story-level acceptance criteria. Lives across all cycles.\n\n2. $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/cycle-01-<slug>.md — the executable plan for cycle 1. <slug> is short kebab-case from the story title.\n\nThe cycle file MUST contain:\n  - Goal (one paragraph)\n  - Scope (in/out lists)\n  - Approach (file changes, design choices)\n  - Test strategy\n  - Risks + mitigations\n  - A `## Task DAG` section at the end (see schema below)\n\n=== TASK DAG SCHEMA (parser rejects anything else) ===\n\nThe cycle file MUST end with EXACTLY this structure (substitute your task content):\n\n## Task DAG\n\n```yaml\ntasks:\n  - id: T1\n    phase: coder\n    title: One-line title\n    description: |\n      Multi-line description. Concrete, scoped, one Claude turn.\n    acceptance: \"One-line acceptance criterion as a string.\"\n    depends_on: []\n  - id: T2\n    phase: unit-tester\n    title: ...\n    description: |\n      ...\n    acceptance: \"...\"\n    depends_on: [T1]\n```\n\nRules — the parser WILL reject:\n  - Heading exactly `## Task DAG` (h2, not h3, no trailing colon).\n  - Fence triple-backtick + `yaml` language tag.\n  - phase MUST be one of the phases listed in CLAUDE.md under '## The N phases'. Read that section NOW before writing any task. NO INVENTED NAMES — bundle non-phase work into `coder` tasks instead.\n  - id matches T<number>. depends_on is a YAML list (never a string).\n  - All 5 fields required: id, phase, title, description, acceptance.\n  - NO text diagrams (T1 -> T2). NO prose lists (T1 - foo, T2 - bar). YAML ONLY.\n\nTypical DAG: 3-8 tasks. Always end with at least one `code-reviewer` task depending on all the coder/tester tasks.\n\n=== STEP 4: surface to human ===\nAfter writing both files, summarize in chat: what the plan builds, the DAG shape (N tasks, phases breakdown), key design choices. The human will click ✓ Approve plan."
}
```

### When the plan lands

Surface to the human:

> *"Plan written:*
> *- scope.md → `<path>`*
> *- Cycle 1 → `<path>`*
>
> *Summary: <2-4 sentences. Cover what the plan builds, DAG shape (N tasks + phases), notable design choices.>*
>
> *Open the file viewer to review. Click ✓ Approve plan when ready, or tell me what to revise."*

---

## MODE B — EDIT IN PLACE

A plan exists; no subtask has dispatched. Revise the same cycle file.

### What to tell Claude

```json
{
  "message": "Plan revision (in place — execution hasn't started). Human feedback: <one-paragraph synthesis>.\n\nRe-read `$CLAUDE_PROJECT_DIR/<plan_path from snapshot>` and revise the SAME file (don't write a new cycle file — execution hasn't started, this is just iteration). Use the absolute path with $CLAUDE_PROJECT_DIR; relative paths break if cwd drifts.\n\nThe `## Task DAG` YAML schema is unchanged — id matches T<number>, phase must be one of the phases listed in CLAUDE.md under '## The N phases' (no others), depends_on is a YAML list. If you change tasks, keep the schema intact.\n\nWhen done, summarize what changed."
}
```

### When Claude responds

> *"Plan updated in place at `<path>`. What changed: <2-4 sentences>. Click ✓ Approve plan or tell me more to change."*

---

## MODE C — NEW CYCLE

Execution has happened. Old cycle file is frozen. Write a new cycle file.

Covers: replan after Replan button, replan after diff-review rejection, post-push "add X" requests.

### Determine the next cycle number

Snapshot's `plan_path` is `repo/stories/<story_id>/plans/cycle-NN-<slug>.md`. Next is `cycle-<NN+1>-<new-slug>.md` in the SAME `stories/<story_id>/plans/` dir.

If you can't determine NN, ask Claude to list `$CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/` and pick the next number.

### What to tell Claude

```json
{
  "message": "New planning cycle for story <story_id>. The previous cycle (<cycle-NN-prev>.md) is frozen. Human feedback for this cycle: <one-paragraph synthesis>.\n\n=== ABSOLUTE PATHS ===\nUse $CLAUDE_PROJECT_DIR/ prefix for ALL plan file reads/writes. Relative paths will create nested repo/repo/ if cwd has drifted.\n\n=== STEP 1: re-read prior cycles ===\nRe-read $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/cycle-*.md and scope.md to refresh what's been built. You may also skim sibling stories' plans under $CLAUDE_PROJECT_DIR/repo/stories/<other_id>/plans/ if they touch related code.\n\nReads free; writes scoped to $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/ ONLY — do NOT modify prior cycles or other stories' files.\n\n=== STEP 2: write ===\n\n1. Update $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/scope.md ONLY if the story scope has genuinely broadened (new capability area). Append a brief section; don't rewrite. Skip otherwise.\n\n2. Write $CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/cycle-<NN+1>-<slug>.md — a fresh plan containing ONLY this cycle's tasks. Don't carry over prior cycles' completed tasks. Reference prior cycle file for context (\"builds on cycle-01's engine API\").\n\nNumber tasks starting from T1 again (each DAG is independent).\n\n=== TASK DAG SCHEMA (parser rejects anything else) ===\n\nSame schema as cycle 1:\n\n## Task DAG\n\n```yaml\ntasks:\n  - id: T1\n    phase: coder\n    title: ...\n    description: |\n      ...\n    acceptance: \"...\"\n    depends_on: []\n```\n\n- phase must be one of the phases listed in CLAUDE.md under '## The N phases'. No inventions.\n- depends_on is YAML list. id matches T<number>.\n- YAML only — NO text diagrams, NO prose lists.\n\n=== STEP 3: surface ===\nSummarize what's in this cycle. The human will approve or revise."
}
```

### When the new-cycle plan lands

> *"New cycle plan written: `<path>`.*
> *<scope.md updated|scope.md unchanged>.*
>
> *Summary: <2-4 sentences>. <N> tasks in the DAG.*
>
> *Click ✓ Approve plan when ready."*

---

## After approval (all modes)

Control panel handles:
- MODE A → creates `plan_versions` row, ingests DAG, sets phase=`executing`
- MODE B → updates existing `plan_versions` row, replaces DAG subtasks, sets phase=`executing`
- MODE C → creates new `plan_versions` row (parent_plan_id = old), creates NEW DAG, sets phase=`executing`

You don't drive any of this. The next state snapshot will reflect the transition; follow AGENTS.md's inline "During execution" doctrine (you relay dispatched-phase output; the control panel drives execution).

## Don't

- Don't write plan files yourself — Claude writes them.
- Don't append to a frozen cycle file (any cycle where a subtask has dispatched is frozen).
- Don't tell Claude to throw away its session context — the same session knows prior cycles.
- Don't try to spawn, resume, or pass a session id — `claude_code_communication` manages the session for you; you only send `message`.
- Don't dispatch tasks, commit, push, or modify files outside `repo/stories/<story_id>/`.
- Don't acknowledge every Claude turn — surface only on plan-ready moments.
- Don't transition phase yourself — control panel owns planning ↔ executing.
- Don't paraphrase the schema or phase list when briefing Claude — paste verbatim.

## If the session isn't ready

If the snapshot's `claude_code` status is `not_started`, just send your MODE A briefing with `claude_code_communication({message})` anyway — it starts a fresh session automatically. If it's `provisioning`, the workspace is still being set up — wait and tell the human. If it's `busy`, a phase or another turn is running — tell the human to resend in a moment.

## Closing the story instead of planning (human may bail mid-cycle)

At any point during discovery / planning / awaiting_plan_approval the human may decide the (new) feature isn't needed and want to close the story — "actually let's not", "never mind", "drop this", "just mark it done". This is expected, especially on a second cycle where an earlier DAG already shipped.

- If the intent is clear ("mark it done", "close the story"), point them to the **✓ Mark story done** button in the phase banner. It is available DURING planning too — so they do NOT have to approve the pending plan first.
- If the intent is ambiguous, ask one short confirming question before pointing them there.
- Do NOT call `story_state` / `story_phase('done')` yourself — marking done is a deliberate human button action (see SOUL: never auto-close). Do NOT pressure the human to approve the plan.
- Any half-written plan file is harmless — it just stays unused under `repo/stories/<story_id>/plans/`. Nothing to clean up.


## Multi-repo workspaces

Some stories span several repositories. You can tell from your workspace: instead of a
single `repo/` checkout, there is a `repos/` directory containing one checkout per
repository (e.g. `repos/acme__web/`, `repos/acme__api/`). CLAUDE.md and RESOURCES.md
list them and mark each **write** or **read-only**.

When that is the case:

- **Plans and scope still live in the PRIMARY write repo**, at
  `<primary-repo>/stories/<story_id>/`. Do not scatter plan files across repos.
- **Every task in the `## Task DAG` block MUST include a `repo:` field** naming the
  checkout that task works in — use the directory name shown in CLAUDE.md, e.g.
  `repo: repos/acme__web`. This is REQUIRED for every task when the workspace has more
  than one repo; there is no default and the plan will be rejected without it. For a
  single-repo workspace, omit `repo:` entirely.
- **Never plan write work in a read-only repo.** Those are cloned for reference only.
- **Each repo merges on its own.** Plan each repo's changes so they are coherent
  independently — do not write a plan that only works if all repos land together.

Example task in a multi-repo plan:

```yaml
  - id: T3
    phase: coder
    title: Add the /health endpoint
    description: |
      Implement the endpoint and register its route.
    acceptance: "GET /health returns 200 with {status: ok}."
    depends_on: [T1]
    repo: repos/acme__api
```
