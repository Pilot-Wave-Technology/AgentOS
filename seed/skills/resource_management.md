---
name: resource_management
title: Resource Management
description: Use this when granting or revoking access to repos, branches, docs, or datasets on a story. Covers the absolute rule (stories never get full repo access) and per-story note conventions.
applicable_to: GO
status: active
version: 4
---
# Resource Management

## How story resources work under the Git Engine

A repo is registered **once, at goal scope** (`kind="repo"`). Making sure the goal's repos are registered is part of planning (see the `story_planning` skill). **Allocating the right repo(s) to each story is your job:**

- **Repo → granted to the story (REQUIRED for coding stories).** Grant **every coding story at least one** repo it works in. This is how the Git Engine learns which repo to cut the story's workstream branch from — **a coding story with no repo grant cannot be accepted.** Grant exactly **ONE** repo per coding story, with `relation="produces"` (the story produces commits in that repo). A story maps to a single workstream branch; a second, different repo grant is REJECTED — split cross-repo work into separate stories linked by a dependency.
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

- `grant_resource(subject_type="story", ...)` — declare: story X **produces** in repo R (grant ≥1 repo to every coding story), **needs** doc A. Always set `notes=`. Never grant a branch (the engine links those).
- `revoke_resource(subject_type="story", ...)` — when a story's plan changes and it no longer needs a previously-granted resource (e.g. it no longer touches a second repo). Don't leave stale allocations.
- `list_allocations(subject_type="story", subject_id=<sid>)` — see what a story currently has access to.
- When an assistant requests a resource via the orchestrator channel, evaluate and grant/deny with `grant_resource`.
- Track what each story **produces** — downstream stories need these outputs.

## Task-level allocations are the Assistant's job, not yours

If a specific IA task needs a resource the story doesn't have (or needs to drop one), the Assistant handles it via `grant_resource(subject_type="task", ...)` / `revoke_resource(subject_type="task", ...)`. You allocate at the story layer and let it inherit.
