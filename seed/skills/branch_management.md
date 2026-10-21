---
name: branch_management
title: Branch Management
description: Use this when creating feature branches for coding stories, deciding shared-vs-per-story strategy, or setting up paired review stories. You (GO) are the branch manager.
applicable_to: 
status: active
version: 4
---
> **PRECEDENCE NOTE (git engine).** When the Git Engine is enabled — the normal
> case — grant the **repo** (`kind='repo'`) to a coding story, NOT a branch. The
> engine cuts the workstream branch and links it to the story automatically, one
> branch per repo. A story may be granted several repos; each gets its own branch and
> its own pull request. The "grant the branch only, never the repo" guidance below
> applies ONLY to legacy, non-engine goals. Never grant a repo's `role='trunk'` branch
> to a story under either model.

# Branch Management

You are the **branch manager**. Assistants and coding agents do NOT create branches.

## Branch setup is part of story creation — do it immediately

**When stories are approved and you create them, branch setup happens in the SAME turn.** Do not wait for the team to ask "GO, can you create the branch?" — that question should never be necessary. Check resources, confirm, create, and link as part of Phase 6 (CREATE).

## Default: all stories share one branch

**Unless the team explicitly tells you otherwise, every coding story in this goal works on the SAME feature branch.** Simpler mental model, avoids most integration pain, matches how small teams actually ship.

**Protocol — run this immediately after `create_story` calls:**

1. **Check for existing repo resources.** Call `resource_list` and look for `kind: repo` or `kind: branch` resources.

   - **If no repo resource exists at all:** do NOT use `register_resource` to create one — repo resources must be added by a human via the goal's Resources panel. Post a message in the goal thread telling the team which repository you need and asking them to add it. Then wait; do not proceed with story creation or branch creation until a real repo resource is registered.

   - **If a repo resource exists and already has a feature branch** (e.g. someone pre-created `feat/goal-xyz`), use that branch — skip to step 3.

   - **If one repo resource exists but has no feature branch yet:** propose the branch name (e.g. `feat/<goal-slug>`) and the trunk it will cut from, then ask:
     > "I'll create branch `feat/<goal-slug>` from `<trunk>` on `<repo-name>` and link it to all coding stories — does that look right?"
     Create immediately on confirmation.

   - **If multiple repo resources exist:** ask explicitly before doing anything:
     > "There are N repos registered — which one should the feature branch live on, and which trunk branch should it cut from?"
     Wait for the answer, then create on confirmation.

2. Create the feature branch **once**, passing the first coding story so the branch resource is granted automatically:
   ```
   create_branch(resource_id="<repo-resource-id>", branch_name="feat/<goal-slug>", story_id=<first-coding-story-id>)
     → returns {resource_id: "r-branch-id"}
   ```

3. For **every** coding story (including the one you passed to `create_branch`), grant the **branch resource only** — do NOT also grant the parent repo. The branch carries the repo's clone URL, so granting just the branch gives the assistant everything it needs. Granting both produces a RESOURCES.md with two entries for the same repo and conflicting working branches (main vs feat/foo), which causes assistants to commit to main.
   - Use `relation="produces"` for coding stories — they push commits to the branch.
   - Use `relation="needs"` ONLY for read-only stories (e.g. a review story that reads the diff).
   ```
   grant_resource(
     subject_type="story", subject_id="<story-id>",
     resource_id="r-branch-id",
     relation="produces",   -- coding story pushes commits to this branch
     notes="Work only under <subfolder>/. Don't touch <other-subfolder>/. …",
   )
   ```

4. Grant reference docs / specs / designs to every story that needs them, with a `notes=` value explaining *why* this resource is attached to *this* story.

> **Why this matters — multi-instance reality:** when a human imports a story to their assistant, a *new conversation thread* and a *new Assistant gateway instance* are created for that story. Each instance has its own RESOURCES.md. Linking a resource to story 1 only refreshes story 1's instance. Story 2's assistant never sees the resource until you link it to story 2 too. Link, link, link — every story, every resource it needs.

## When to diverge from the shared-branch default

Create a separate branch per story ONLY when one of these is explicitly requested:
- The human asks for isolated branches ("give each story its own branch", "I want clean per-story PRs").
- The plan has genuinely parallel, conflict-prone workstreams (e.g. backend API + frontend UI + database schema, expected to merge independently).
- A story modifies shared infrastructure that must not break the mainline during development.

If diverging, explain why in the goal thread before creating branches, and wait for approval.

## Review stories (mandatory for every coding story)

Every batch of coding work needs a paired review story assigned to a human reviewer. **The merge target is the trunk LANE the story lands on.** One repository can carry SEVERAL lanes in one goal — a backend branch and a frontend branch of one app — each its own merge target, and a story lands on the lane it was granted, not on "the repo's branch". `code_state` lists the lanes per repo and, for every live branch, the `lands_on` lane; RESOURCES.md names them under each repository. Never assume `main`: on a goal that declared lanes there is no repo-wide branch to fall back to.

```
Coding story: "Implement Backend Auth" → feat/backend-auth
  assigned_to: Alice
  produces: feat/backend-auth
  cut from: <trunk>          (passed as the source resource to create_branch)

Review story: "Review & merge feat/backend-auth → <trunk>"
  assigned_to: goal admin (or designated reviewer)
  depends_on: [coding_story_id]
  needs: feat/backend-auth (read)
  description: "Review the branch diff on GitHub. Either merge to <trunk>, request changes, or reject."
```

The reviewer can:
- **Merge as-is** → mark review story done
- **Request changes** → tells you in the goal thread what needs to change → you evaluate, propose to update acceptance criteria or create a follow-up story, wait for approval
- **Reject** → mark review story cancelled → propose alternative plan, wait for approval

> Integration story dependencies (depend on review stories, not coding stories) and the cancelled-story lifecycle live in the `story_planning` skill, since they're about story state, not branch state.
