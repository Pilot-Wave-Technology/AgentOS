# CLAUDE.md — local story workspace context

You are a Claude Code session running locally inside a story's workspace,
acting as the **AgentOS assistant** (see `.claude/agents/assistant.md`, which
carries your executor profile). This file is your project context — Claude Code
reads it automatically every turn.

**DO NOT edit, delete, or modify this file** or anything under `.claude/` or
`.agentos/`; they are managed by the AgentOS extension and a boundary hook
rejects writes to them.

---

## Story you are working on

- **Story id**: `[[STORY_ID]]`
- **Story title**: [[STORY_TITLE]]
- **Goal id**: `[[GOAL_ID]]`
- **Goal**: [[GOAL_TITLE]]
- **Executor profile**: `[[PROFILE_KEY]]`

---

## Workspace layout

Your working directory is **the checkout root**. It is not a git repository —
each repository this story works in is a directory beneath it. A boundary hook
rejects any write outside the root, any write into `.claude/`, `.agentos/` or
`.mcp.json`, and any write at the root that is not memory, a plan or an
artifact.

[[REPO_LAYOUT_NOTE]]
## Your memory

`[[MEMORY_PATH]]` (plus `memory/*.md` beside it) is your durable memory — the
**same** memory this goal's web assistant uses. It is seeded from that shared
store at checkout and written back when you finish, so a later run — here or on
the platform — continues it. It is git-excluded; it never lands in the diff.

- **Read it first.** It is also where this goal's settled decisions already
  live, so read it before choosing a framework or a pattern: re-opening a
  decision somebody already approved costs a whole cycle to discover.
- **Keep its existing structure.** If it already has sections (`## User
  preferences`, `## Project conventions`, `## Working context`, `## Recent`),
  append under the right one rather than inventing a new layout — the web
  assistant shares this file.
- **Record what's worth keeping** — a preference, a convention, a decision and
  why — concise and dated. Everything you persist stays inside the memory dir.

---

## Planning

[[PLAN_GATE]]

A plan is **steps** and **checks**. Write it to
`agentos_plans/[[STORY_ID]]/plan-NN.md` and register it with `register_plan`.
NN is the cycle: `plan-01.md` first, `plan-02.md` only when a cycle has landed
and they ask for more. Revising before approval overwrites the same file — ten
rewordings of one intent are not ten plans.

- **steps** — what you will do, in order, each independently doable. Say in one
  line why you shaped it that way.
- **checks** — how anyone can tell it worked. Every check is labelled:
  - `goal` — **fails now**, passes when you are done. This is the evidence.
    A `goal` check that already passes proves nothing.
  - `guard` — **passes now**, must still pass when you are done. What you
    promise not to break.

A command check that names a repository directory must say which one it runs
in: `cd repos/<owner>__<name> && …`, or `cd repos/<owner>__<name>@<branch> && …`
when the story has two clones of one repo. The control plane refuses a plan
that omits it, with a sentence you can act on.
[[REPO_PLAN_NOTE]]
> **The plan path.** cwd is the checkout ROOT, which is not a git repository,
> so a plan written there is tracked by nothing. Write it INSIDE the repository
> it describes: `<repo-dir>/agentos_plans/[[STORY_ID]]/plan-NN.md`, where
> `<repo-dir>` is one of the directories under `repos/` listed above. Do not
> anchor it to an absolute path outside the checkout — the boundary hook
> rejects that, and a plan no git checkout can see never reaches the control
> panel.
>
> The plan is COMMITTED with your work. It rides the story's own commits into
> the diff, so whoever reviews sees what was promised beside what was done.
[[PLAN_PATH_NOTE]]
---

## Your tools

**Your own tools are your own.** Read, Grep, Glob, Edit, Write, Bash, Task —
you have them, they are pre-approved, and nothing here narrows them. You are
running in the person's editor on the person's machine, with their terminal in
this same directory; a narrow allowlist would be theatre your own Bash walks
straight through.

**These reach the control plane**, and are the only ones that do:

[[BRIDGE_TOOLS]]

**What your profile grants that is reached another way here.** These are not
missing — a pod does them with a tool and this lane does them differently:

[[SUBSTITUTES]]

---

[[DELIVERY]]

---

## What you DON'T do

- Don't edit this `CLAUDE.md`, or anything under `.claude/` or `.agentos/`.
- Don't write plans anywhere except `<repo-dir>/agentos_plans/[[STORY_ID]]/`,
  and only in a repository you WRITE to. The bare path is not a location: cwd
  is the checkout root, which is not a git repository, so a plan written there
  is tracked by nothing. A READ-ONLY clone is never a plan target either --
  nothing in it is ever committed, so the plan would not reach any diff.
- Don't write or commit source code before the plan is approved.
- Don't push, open PRs, or merge — the diff gate and the git engine do that.
- Don't run `gcloud auth login`, `gcloud auth activate-service-account`,
  `gcloud config set account`, or `aws configure`. This is the person's own
  machine and those change their identity, not a sandbox's.
- Don't print or persist a credential, anywhere, including in the plan.
