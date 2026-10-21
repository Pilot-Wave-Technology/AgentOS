---
name: resource_management
title: Resource Management
description: Use this when granting or revoking access to repos, branches, docs, or datasets on a story. Covers the absolute rule (stories never get full repo access) and per-story note conventions.
applicable_to: GO
status: active
version: 8
---
# Resource Management

## How story resources work under the Git Engine

A repo is registered **once, at goal scope** (`kind="repo"`). Making sure the goal's repos are registered is part of planning (see the `story_planning` skill). **Allocating the right repo(s) to each story is your job:**

- **Repo → granted to the story (REQUIRED for coding stories).** Grant **every coding story at least one** repo it works in. This is how the Git Engine learns which repo to cut the story's workstream branch from — **a coding story with no repo grant cannot be accepted.** Grant **one** repo for an ordinary single-repo story; grant **two or more** when the story genuinely spans repos (each gets its own engine-cut branch). Use `relation="produces"` for a repo the story COMMITS to, and `relation="needs"` for one it only READS for context. Only a second write branch in the SAME repo is rejected.
  - **Which TRUNK, when a repo has more than one.** Some repos register several long-lived trunks (e.g. `agentos_platform` and `agentos_openclaw` on one repo). Call `resource_describe(resource_id)` before granting: it returns `trunks` and `trunk_choice_required`.
    - **One trunk** — grant normally, say nothing. There is nothing to decide.
    - **Several trunks** — you MUST pass `trunk_branch=...`, and you MUST ask the human which one first. `grant_resource` refuses the grant without it and names the options. **Do not pick one yourself**, and do not reuse a previous story's answer — ask again for each story.
    - **A story may target BOTH trunks of one repo**: grant the same repo twice, once per `trunk_branch`. Each gets its own engine-cut branch and its own PR.
  - **Decide this from what the story will actually DO, never from the default.** `grant_resource`'s `relation` parameter defaults to `needs` (read-only) when omitted. A generic request to grant/assign repos ("assign repos as well", "add these resources") is NOT itself a signal that every repo should be read-only — re-read the story's own description first. If it says a repo will get new files, edits, or a report committed to it, grant that repo `produces` explicitly. **If the split genuinely isn't clear, ask the human which repos need write access before granting** — don't let the tool's default silently decide it for you. This applies just as much when `update_story` changes an existing story's scope to touch a new repo, or a later standalone `grant_resource` call adjusts one mid-story — not only right after the story is first created.
  - Granting the repo does **not** expose the trunk: the engine cuts the workstream branch from the trunk and the assistant only ever sees/commits *that branch* in its RESOURCES.md — never the trunk and never other branches.
- **Branch → NEVER granted by you.** The Git Engine cuts each workstream's feature branch from the trunk and **automatically links it** to that workstream's stories, so the assistant sees its working branch with no action from you. You declare the **repo**; the engine owns the **branch**.
- **Other resources (docs, datasets, specs, configs)** — grant the ones a story needs with `relation="needs"`.

Grant a repo to a coding story (do this for **every** coding story):
```
grant_resource(
  subject_type="story", subject_id="<story-id>",
  resource_id="<repo-resource-id>",   # a kind='repo' goal resource — NOT a branch
  relation="produces",
  notes="What this story builds in this repo. …",
)
```

Grant a non-branch resource:
```
grant_resource(
  subject_type="story", subject_id="<story-id>",
  resource_id="<doc-or-dataset-resource-id>",
  relation="needs",
  notes="Which sections / columns / files matter for this story. …",
)
```

A story can need multiple resources — grant each separately. **Never grant a branch** (the engine owns those).

## Always set `notes=`

The `notes=` value on every `grant_resource` call is rendered inline in the assistant's RESOURCES.md as a **GO note:** line under that resource. This is your chance to tell the assistant *how* to use this specific resource in this specific story — which sections of a spec matter, which columns of a dataset to look at, what NOT to touch.

```
grant_resource(
  subject_type="story", subject_id="s-abc",
  resource_id="r-doc-spec",
  relation="needs",
  notes="Implement against sections 3-4 of the spec. Ignore the legacy appendix.",
)
```

If you don't know yet, pass an empty note — but leaving them all empty is a missed opportunity; the assistant ends up asking questions you could have pre-answered.

## Standard operations

- `grant_resource(subject_type="story", ...)` — declare: story X **produces** in repo R (grant ≥1 repo to every coding story), **needs** doc A. Decide produces vs needs from the story's actual work, or ask if unclear — never rely on the `needs` default for a repo that will be committed to. Always set `notes=`. Never grant a branch (the engine links those).
- `revoke_resource(subject_type="story", ...)` — when a story's plan changes and it no longer needs a previously-granted resource (e.g. it no longer touches a second repo). Don't leave stale allocations.
- `list_allocations(subject_type="story", subject_id=<sid>)` — see what a story currently has access to.
- When an assistant requests a resource via the orchestrator channel, evaluate and grant/deny with `grant_resource`.
- Track what each story **produces** — downstream stories need these outputs.

## Task-level allocations are the Assistant's job, not yours

If a specific IA task needs a resource the story doesn't have (or needs to drop one), the Assistant handles it via `grant_resource(subject_type="task", ...)` / `revoke_resource(subject_type="task", ...)`. You allocate at the story layer and let it inherit.


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
