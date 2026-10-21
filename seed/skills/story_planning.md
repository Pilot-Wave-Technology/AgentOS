---
name: story_planning
title: Story Planning Protocol
description: Use this when the team asks you to plan, propose, or revise stories. Walks through the 6-phase protocol from understanding the objective to writing acceptance criteria.
applicable_to: GO
status: active
version: 11
---
# Story Planning Protocol

**NEVER rush to create stories.** Follow this process every time the team asks you to plan work.

## Phase 1: UNDERSTAND
- Ask clarifying questions about the objective
- Who are the stakeholders? What are the constraints? Timeline?
- What does success look like?

## Phase 2: RESEARCH
- Use `web_search` for tech landscape, best practices.
- Inspect registered repos via:
  - `resource_list()` — see what repos/branches are registered.
  - `resource_summary(resource_id)` — cached summary if a scan has already been run. Prefer this — it's cheap.
  - `resource_scan(resource_id)` — fresh structured summary (file tree, key files, language breakdown). **Expensive — only when summary is stale or missing.**
  - `resource_read(resource_id, path)` — read a specific file. **Expensive — only when you need to verify acceptance-criteria-level detail.**
- You do NOT have shell or git access, and you do NOT create tasks. Task creation belongs to the user's Assistant after the human accepts a story. If you need deeper code inspection than `resource_*` can provide:
  - Ask the team in the goal thread to describe what they want to know.
  - Surface specific files you've already read so the human can tell you whether you've seen enough.
  - If they confirm deeper exploration is needed, propose it as a story for an assistant to pick up. The assistant then creates an exploration task.

## Phase 3: GATHER
- **Confirm the repo(s) are registered as goal resources.** The Git Engine can only place a story that resolves to a registered repo (`kind: repo` in `resource_list`). You **cannot** add a repo yourself — if one the work needs is missing, ask the team to (1) install the GitHub App on it, then (2) add it via the goal's Resources panel, and **wait**. Don't plan coding stories against a repo that isn't registered.
- **Multiple repos:** if the goal spans more than one repo, make sure every repo a story will touch is registered. **Most stories work in a single repo** — when you write a story, decide which repo(s) it genuinely needs rather than assuming it needs all of them. A story may work in two or more repos when it genuinely spans them. Prefer one repo when the work fits in one; use several when the change is inherently cross-repo.
- If other resources are missing (docs, designs, datasets), ask the team to add them.
- Wait for resources before planning — don't speculate.

## Phase 4: PROPOSE
- **Know who's on the goal first.** Read the `## Team` block in your context — the live roster (display names + uids) of who's actually on this goal. You plan *for these people*; never propose a story for someone who isn't a current member.
- Present stories as a **markdown plan** in the goal thread (natural text, not tool calls). For **each story, show the proposed assignee by display name**, the **repo it works in**, its dependencies, and its acceptance criteria. For example:
  > **Story 2 — Implement calculator logic** · _Assignee: Alice_ · _Repo: calculator-web_ · depends on Story 1
  > Acceptance: add/subtract/multiply/divide work in the UI and call the backend.
- Group the proposal **by assignee** so it's obvious who owns what and in what order.
- Explain the WHY behind both the **sequencing** and the **assignment** (why this person — capacity, expertise, who owns the adjacent work).
- Flag risks and unknowns.

## Phase 5: DISCUSS
- Wait for human feedback. Iterate.
- **Assignments are a proposal, not a decision.** The team can reassign any story in conversation ("give story 3 to Bob instead") — update the plan accordingly.
- **When an assignment changes, re-evaluate the dependencies/branching** (see "Dependencies & the Git Engine"): moving a story to a different owner usually means pulling it out of one person's sequential chain and into another's — which changes how the engine will branch and merge it. Don't leave the dependency graph reflecting the old owner.

## Phase 6: CREATE
Only after **explicit approval** from the team:

**Before you assign — refresh the team roster.** The `## Team` block in your per-turn context is the live source of truth (joined from `goal_members + users`). Read it *immediately before* every `create_story` call so you assign to a `uid` that is *currently* on the goal. Never assign from a roster you saw earlier in the conversation — members can join or leave between turns. If you're not sure a person is still on the goal, say so in the goal thread and ask before assigning.

- `create_story` for each story. **Every story MUST have:**
  1. A clear, specific **title** — what's being built, not what domain it's in.
  2. A **description** covering WHAT to build, WHY, the expected user outcome, and any non-obvious constraints. Minimum ~2 sentences. Never empty or one-word.
     > This description becomes the **Story brief** at the top of the assistant's RESOURCES.md. Keep it *tactical* — what to build, hard constraints, boundaries. Not marketing copy.
  3. **Acceptance criteria** — explicit, checkable conditions that define "done" (what the human/assistant verifies before completing the story). Don't leave this implicit; a story without clear acceptance criteria isn't ready to create.
  4. **Accurate `depends_on`** — the stories whose output this one needs. This is how you shape the git flow (see "Dependencies & the Git Engine" below), so get it right.
  5. **Repo allocation — MANDATORY for every coding story.** Immediately after creating a coding story, grant it the repo(s) it works in:
     `grant_resource(subject_type="story", subject_id=<story-id>, resource_id=<repo-id>, relation="produces", notes=...)`.
     **If the repo has SEVERAL trunks, name one.** Call `resource_describe(resource_id)` first. When it reports `trunk_choice_required: true`, pass `trunk_branch=...` — and ask the human which trunk before you do; the grant is REFUSED without it. When creating SEVERAL stories at once, propose a trunk per story from what the conversation implies, list them for the human, and wait for explicit confirmation — they may answer in bulk ("all use `agentos_platform`", or "first three on A, the rest on B"). Never assume, and never carry a previous story's answer forward.

      This is **required** — it's how the Git Engine knows which repo to cut the story's branch from, and **a coding story with no repo grant cannot be accepted.** Grant **one** repo for the common single-repo story; grant **two or more** when the story genuinely spans repos. Each write repo gets its own engine-cut branch and its own pull request. Only a second write branch in the SAME repo is rejected. **Never grant a branch** — the engine cuts and links the branch itself. (Granting the repo does not expose the trunk; the assistant only ever sees its engine-cut feature branch.) See the `resource_management` skill.

     **Decide `produces` vs `needs` from what the story will actually DO in each repo — never default.** Read your OWN story description before granting: if it says a repo will get new files, edits, fixes, or a report committed to it, that repo is `produces`, even when a later human request ("assign repos", "grant the repos", "add resources as well") doesn't spell out read vs write per repo — `grant_resource`'s relation defaults to `needs` (read-only), and using that default across the board silently strands a story that was always meant to commit to all of them. **If it's genuinely unclear** which repos should be editable — the description doesn't say, or the request spans several repos with mixed intent — **ask the human directly** (e.g. "Which of these should be write access — the story will commit to them — versus read-only for context?") before granting anything. Don't guess and don't silently default every repo to read-only just because the request didn't spell it out. **This applies every time you grant or change a repo's access, not only right after `create_story`** — the exact same reasoning (and the same "ask if unclear" rule) applies when `update_story` changes a story's scope to touch a new repo, or when a follow-up `grant_resource` call adds/adjusts a repo mid-story.
  6. **Other resource allocations** — grant any documents, datasets, or specs the story needs via `grant_resource(subject_type="story", ..., relation="needs")`.
  7. An `assigned_to` that you confirmed against the live `## Team` block this turn.
- Record key decisions with `record_decision`.

## Plan Evolution

After every story completion, you receive a detailed review including the assistant's progress_summary.
- Compare actual output vs acceptance criteria.
- Check if remaining stories still make sense.
- Propose changes in the goal thread, wait for approval, then execute.

## Dependencies & the Git Engine

You shape the git flow **entirely** through each story's `depends_on`, and you set dependencies on **two axes — logical order AND owner**:

1. **Logical order:** a story that needs another story's output `depends_on` it.
2. **Owner:** **one person's stories should be sequential, not parallel.** If you assign Alice three stories, chain them (B depends on A, C depends on B) so they flow on **one branch** — *even if they're logically independent of each other*. One person works one thing at a time, and parallel branches for the same owner just create extra merges for no benefit.

The engine derives branches from the resulting graph (per repo — a story's branch is cut from the repo you allocated it):
- a **linear chain** in one repo → **one** branch, one merge — the target shape for a single owner's work;
- **independent** stories → **parallel** branches, each merged separately;
- a story depending on two or more others → a **merge gate** where they converge.

**Rules of thumb:**
- **Group by assignee, then sequence within each assignee.** All of Alice's stories form one ordered chain; all of Bob's form another → **one branch per person, the fewest possible merges.**
- **Parallel branches only across *different* people building *different* things** — e.g. Alice's backend chain runs in parallel with Bob's frontend chain, and an integration story (owned by whoever) depends on both.
- **Reassigned story → re-chain it.** If story 3 moves from Alice to Bob, take it out of Alice's chain and put it at the right point in Bob's (or make it parallel). The graph must match who's actually doing the work.
- **When unsure, add the dependency** (sequential) rather than leaving stories parallel.

You do **not** create "review" or "merge" stories — the engine surfaces **one merge story per branch** for human approval, and guarantees a producer is on trunk before a dependent's branch is cut. Fewer branches ⇒ fewer merge approvals — which is exactly why same-owner sequencing matters.

## Cancelling a story

If the plan changes and a story isn't going to happen, cancel it via `cancel_story`. Don't leave it in limbo.

Cancelling marks a story as effectively deleted:
- It disappears from the Story Board.
- It no longer blocks any downstream story — dependents become unblocked automatically.
- Its resources (docs, datasets) stay registered at the goal level but are no longer locked to it. (The Git Engine reclaims any branch it had cut for the cancelled work.)


## Multi-repo stories

> **Check this applies first.** Multi-repo stories are a per-deployment
> capability. If granting a second repo to a story is REJECTED ("a story maps
> to exactly ONE repo"), this deployment has multi-repo turned OFF — that
> rejection is expected, not a bug. Keep to one repo per story and split
> cross-repo work into sibling stories with a dependency, as before. You can
> also tell from a story workspace: multi-repo is active when it contains a
> `repos/` directory instead of a single `repo/`.

A coding story MAY work in more than one repository. Grant each repo the story needs.

- **Write repos** — grant with `relation="produces"`. Each one gets its own
  engine-cut branch, its own pull request, and its own merge.
- **Read-only repos** — grant with `relation="needs"` when the story only needs to
  READ another repo's code for context. These are cloned but never branched or
  committed to. Prefer this whenever the story does not actually change that repo.
- **One write branch per repo.** A story may write to many repos, but only ONE branch
  in each. If work genuinely needs two branches of the same repo, split it into
  sibling stories with a dependency.
- **Every task must name its repo.** When a story spans repositories, each task in the
  `## Task DAG` block MUST carry a `repo:` field naming the repo directory it works in
  (e.g. `repo: repos/acme__web`). There is no default with several repos, and a task
  without one is rejected. Omit `repo:` only when the story has exactly one repo.
- **Repos merge independently.** One human approval commits and pushes every write
  repo and opens a pull request for each, but those pull requests are merged
  separately by humans on GitHub. The story completes only once ALL of them land.
  Plan each repo's changes so they are self-consistent — never assume the repos land
  at the same instant.

Prefer a single-repo story when the work genuinely fits in one repo: it is simpler to
review and lands in one step.
