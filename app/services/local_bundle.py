"""Local Execution Mode — the assistant bundle served to the extension (D.4).

The extension materializes a native Claude Code `.claude/` bundle into the local
checkout so the human's session boots AS the single-role AgentOS assistant. This
module builds that bundle's CONTENT (persona + `CLAUDE.md`), story-interpolated,
and hands it to `build_hydration_bundle` (M2). Content lives in `local_prompts/`
as plain markdown so it can be iterated on without touching code.

THE ROLE COMES FROM THE PROFILE, the mechanics from the templates. A local
story resolves a profile in `profiles-local/` (see
`kernel.profiles.local_registry`), and that profile's `prompt.md` becomes
`.claude/agents/assistant.md` -- who the agent is. `claude.md` here stays what
it should be: THIS story, THIS workspace, THIS memory path, THIS tool reality.
Two profiles then produce two genuinely different sessions rather than the
same prose with a different story id in it.

`skills` is `{}` for v0: the profile tree supports a skills/ directory and the
local lane does not read it yet.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.database import get_pool
from app.services.story_workspace_layout import resolve_workspace_clones, write_clones

logger = logging.getLogger(__name__)

STORY_DELIVERY = "## Git\n\nCommit each coherent change as you go with your own `git commit`. A tree with\nten unrelated changes in it is a diff nobody can review, and reviewing it is\nthe next thing that happens.\n\n**Do not push, and do not open a pull request.** When the person submits the\ndiff from the AgentOS panel, the extension pushes the branch with a short-lived\ntoken minted for that one push, and the git engine opens the merge PR. A push\nfrom here would use whatever credential is on the machine, against a branch the\nengine believes it owns.\n\n## Finishing\n\nWhen the work meets the plan's `goal` checks, say so and stop. Tell the person\nwhat you did and what you checked. **You do not submit, approve, push, or mark\nanything done** -- they submit the diff, review it, and mark the story complete.\n\nIf they want more work after a cycle lands, that is a **new plan**: register it\nthe same way and wait for approval again. Do not continue from the old one."

ERRAND_DELIVERY = "## Git\n\nYou have a clone because an ops errand is almost always ABOUT a repository --\nread the config, run the script, check the output. **You do not deliver through\nit.**\n\n**There is nothing to push and nothing to submit.** No branch of yours lands,\nno diff is reviewed, no pull request is opened. This is enforced, not just\nasked: the control plane refuses to mint a push token for an errand and refuses\na diff submission from one. If you find yourself reaching for either, the\nerrand has turned into a story -- say so and let the person decide.\n\nCommit locally if it helps you work. It goes nowhere.\n\n## Finishing\n\n**The answer is the deliverable.** Report it in the conversation: what you ran,\nwhat came back, and what you conclude. That text is recorded into the story's\nthread, which is where anyone looking for the result will read it.\n\nIf the answer is a file -- a report, an extract, a chart -- write it into the\nworkspace and say where it is, so the person can keep it before the checkout\ngoes away.\n\nThen stop. The person marks the errand done."

_PROMPTS_DIR = Path(__file__).parent / "local_prompts"


def _read(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _delivery_section(kind: str) -> str:
    """How this run delivers -- the one part of CLAUDE.md that differs in kind.

    A story delivers a BRANCH: commit, submit, the extension pushes, the git
    engine opens the PR. An errand delivers an ANSWER and must not push at all
    -- `local_diff` refuses both the push token and the diff submission for
    one, and the prompt says so in the same words rather than leaving the agent
    to discover a refusal.

    Shared prose here was a real defect waiting: the single section told every
    local session "the extension pushes the branch when you submit the diff",
    which for an errand describes something that cannot happen.
    """
    return ERRAND_DELIVERY if kind == "errand" else STORY_DELIVERY


async def _profile_context(pool, story_id: str) -> dict[str, str]:
    """The story's LOCAL executor profile, rendered for the templates.

    REPLACES `_phase_context`, which read the `claude_phases` registry and
    rendered the six-phase table the Task DAG was built on. That registry
    describes a dispatch model the local lane no longer has, and the table was
    handed to every local session as doctrine -- telling it to emit a
    `## Task DAG` the parser no longer accepts and to call `claim_next_task`,
    which no longer exists.

    Degrades rather than raises. Hydration runs AFTER the import already
    resolved this profile and refused if it could not, so a failure here is
    unexpected -- but a story that cannot state its role is better served by a
    bundle that says so than by one that quietly omits it.
    """
    from kernel.profiles.record import UnknownProfileError
    from app.services.local_tool_surface import (ProfileNotRunnableLocally,
                                                 bridge_tools_for,
                                                 local_substitutes)
    from app.services.story_profile import resolve_local_for_story

    try:
        prof = await resolve_local_for_story(pool, story_id)
    except (UnknownProfileError, ProfileNotRunnableLocally, KeyError) as e:
        logger.warning("local bundle for %s: no local profile (%s)", story_id, e)
        return {
            "PROFILE_KEY": "unresolved",
            "PROFILE_ROLE": (
                "Your executor profile could not be resolved, so this file "
                "cannot tell you what role you are playing. Say so, and ask "
                "the person before doing anything."),
            "BRIDGE_TOOLS": "",
            "SUBSTITUTES": "",
            "PLAN_GATE": "",
            "PROMPT_FILES_NOTE": "",
            "DELIVERY": STORY_DELIVERY,
        }

    key = f"{prof.kind}/{prof.domain}" + (f"/{prof.phase}" if prof.phase else "")
    try:
        bridge = bridge_tools_for(prof.tools)
    except ProfileNotRunnableLocally:
        bridge = []
    subs = local_substitutes(prof.tools)

    plan_gate = (
        "This story REQUIRES AN APPROVED PLAN before implementation. Register "
        "it with `register_plan`, tell the person it is ready, and wait -- they "
        "approve it in the AgentOS panel."
        if prof.requires_plan else
        "This story does not require plan approval, but say what you intend to "
        "do before you do it."
    )
    prompt_files_note = (
        "Read " + ", ".join(f"`{f}`" for f in prof.prompt_files)
        + " from the repository root if present -- they are this project's own "
          "conventions and character, and they outrank your general habits."
        if prof.prompt_files else ""
    )
    return {
        "PROFILE_KEY": key,
        "PROFILE_ROLE": prof.prompt_preamble,
        "BRIDGE_TOOLS": "\n".join(f"- `{t}`" for t in bridge),
        "SUBSTITUTES": "\n".join(f"- **{t}** -- {why}" for t, why in subs),
        "PLAN_GATE": plan_gate,
        "PROMPT_FILES_NOTE": prompt_files_note,
        "DELIVERY": _delivery_section(prof.kind),
    }


async def _repo_context(pool, story_id: str) -> dict[str, str]:
    """Multi-repo awareness for `claude.md` (research/NREPO_LOCAL_EXECUTION_PLAN.md).

    Before this, a local multi-repo story's agent had NO indication from its own
    persona that a second repo existed at all — `build_local_bundle` never looked
    at `resolve_workspace_clones` — so it authored a single-repo-shaped plan and
    discovered the second repo only via the DAG parser's rejection once it tried to
    submit. Mirrors `operator_provisioning._build_claude_md_for_story`'s multi-repo
    section, adapted to local's actual (different) layout: cwd is the PRIMARY repo
    itself, not the checkout root — and memory lives ABOVE cwd for a genuine
    multi-repo checkout (D-3), not inside it, so the plain single-repo instruction
    below would point the agent at a MEMORY.md that doesn't exist there.

    Returns "" for every placeholder in the single-repo case (`clones` has 0 or 1
    entries) — the template renders byte-identical to before this existed.
    """
    clones = await resolve_workspace_clones(pool, story_id)
    if len(clones) <= 1:
        sole = clones[0]["subdir"] if clones else "repos/<owner>__<name>"
        return {
            "REPO_LAYOUT_NOTE": _layout_note(clones),
            "MEMORY_PATH": ".agentos-memory/MEMORY.md",
            "REPO_PLAN_NOTE": "",
            "PLAN_PATH_NOTE": (
                f"\n**Write your plan inside the repository you are "
                f"changing**, at `{sole}/agentos_plans/<story-id>/plan-NN.md`. "
                f"It is a git-tracked artifact of that repo: it rides the "
                f"story's commits and shows up in the PR diff for whoever "
                f"merges. Your durable memory is NOT -- it is per-STORY and "
                f"stays at the checkout root." + "\n"
            ),
        }

    # `_layout_note` draws every clone, marking each write or READ-ONLY, so the
    # primary/sibling split this used to build by hand is gone: under a
    # root-anchored layout no clone is "the" one the others hang off.
    repo_layout_note = _layout_note(clones)

    write_subdirs = [c["subdir"] for c in write_clones(clones)]
    repo_values = ", ".join(f"`{s}`" for s in write_subdirs)
    # REPLACES the Task DAG `repo:` field note. A plan has no per-task repo
    # field -- what carries the same information is the `cd <mount> &&` prefix
    # on a command check, which `kernel/tools/check_paths` refuses a plan for
    # omitting. Same defect it always guarded: with two repositories a command
    # with no `cd` resolves the first one alphabetically, and the failure reads
    # as "the tests are broken".
    repo_plan_note = (
        f"\n**This story spans {len(clones)} repositories, so every command check "
        f"must say which one it runs in** -- `cd <repo-dir> && <command>`, where "
        f"<repo-dir> is one of: {repo_values}. There is no default: without the "
        f"prefix the first directory alphabetically wins, which is silently the "
        f"wrong repository. The control plane refuses a plan whose checks name a "
        f"repo directory without it.\n"
    )
    # 2026-08-20 fix: the plan path was left for the assistant to reason about
    # each session, with no explicit anchor — confirmed live, SAME primary repo,
    # two different sessions: one wrote the plan at the checkout root, the other
    # inside the primary repo's own tree. Only the latter is a real git-tracked
    # artifact (captureDiff/commitWork operate on the primary repo's working
    # tree) and only the latter is what boundary.js's C1 gate allows — so state
    # it explicitly rather than leaving it inferred, the same fix already
    # applied to the per-repo check prefix via REPO_PLAN_NOTE.
    #
    # 2026-10-01: the folder is now `agentos_plans/<story>/`, the kernel's own
    # (`kernel.harness.planfiles.FOLDER`), not the operator lane's
    # `stories/<id>/plans/`. SCOPE.MD IS DELIBERATELY NO LONGER MENTIONED — it
    # is an operator-lane artifact the kernel has no concept of, and what it
    # held is the plan's own rationale and steps.
    plan_path_note = (
        "\n**Write your plan INSIDE each repository you are changing**, "
        "at `<repo-dir>/agentos_plans/<story-id>/plan-NN.md` -- not at the "
        "checkout root. A plan is a git-tracked artifact of the repository it "
        "describes: it rides that repo's commits and shows up in the PR diff "
        "for whoever merges, and a check scoped to one repository has nowhere "
        "else to find it. Every WRITE repository gets the same file, "
        "byte-for-byte: " + ", ".join(f"`{x}/`" for x in write_subdirs)
        + ". Your durable memory is the opposite case -- it is per-STORY, so "
        "it stays at the checkout root and is never copied into a repository."
        + "\n"
    )
    return {
        "REPO_LAYOUT_NOTE": repo_layout_note,
        "MEMORY_PATH": ".agentos-memory/MEMORY.md",
        "REPO_PLAN_NOTE": repo_plan_note,
        "PLAN_PATH_NOTE": plan_path_note,
    }


def _layout_note(clones: list[dict]) -> str:
    """The story workspace, drawn. ONE layout, single repo or many.

    cwd is the CHECKOUT ROOT, not a repository. The governance files, the
    shared memory, the resources and the artifacts all sit there, and each
    repository is a directory beneath it -- which is what the pod has always
    done. Before this the files lived inside the PRIMARY repo and everything
    else was addressed relative to it (`../../.agentos-memory/`), which was
    both fragile and different from the other lane for no reason.

    Drawn rather than described because a path the model infers is a path it
    gets wrong: two sessions on the same story once wrote the plan to two
    different places, and only one of them was git-tracked.
    """
    lines = [
        "",
        "## Your workspace",
        "",
        "Your cwd is the CHECKOUT ROOT. It holds everything that belongs to "
        "the story; each repository is a directory beneath it.",
        "",
        "```",
        ".                      <- cwd, the checkout root",
        "|- CLAUDE.md           this file",
        "|- .claude/            your persona and hooks   (never edit)",
        "|- .mcp.json           the AgentOS tool server  (never edit)",
        "|- .agentos/           session coordinates      (never edit)",
        "|- .agentos-memory/    your durable memory, per STORY",
        "|- docs/               resources served to you  (READ-ONLY)",
        "|- artifacts/          what this story produces (write here)",
        "`- repos/",
    ]
    for c in clones:
        leaf = (c["subdir"] or "").split("/")[-1]
        how = "write" if c.get("is_write") else "READ-ONLY context, never commit"
        lines.append(
            f"   |- {leaf}/  {c['repo_owner']}/{c['repo_name']} "
            f"@ {c['branch_name']}  ({how})")
    if not clones:
        lines.append("   `- <owner>__<name>/   your repository")
    lines += [
        "```",
        "",
        "`cd` into a repository before running git against it. Commit inside "
        "the repository; never commit the checkout root, which is not a git "
        "repository at all.",
        "",
    ]
    return "\n".join(lines)


def _apply(template: str, values: dict[str, str]) -> str:
    """Simple `[[TOKEN]]` substitution — chosen over str.format so the YAML/code
    examples in the templates keep their literal `{ }` without escaping."""
    out = template
    for key, val in values.items():
        out = out.replace(f"[[{key}]]", val)
    return out


async def build_local_bundle(story_id: str) -> dict:
    """Return {agent_md, claude_md, skills} — the local assistant bundle for one
    story, or {} if the story is gone (caller omits the field)."""
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT title, goal_id FROM stories WHERE id = $1", story_id,
    )
    if not story:
        return {}
    goal_id = story["goal_id"] or ""
    goal = await pool.fetchrow("SELECT name FROM goals WHERE id = $1", goal_id)
    values = {
        "STORY_ID": story_id,
        "STORY_TITLE": story["title"] or "(untitled story)",
        "GOAL_ID": goal_id,
        "GOAL_TITLE": (goal and goal["name"]) or "(unknown goal)",
        **await _profile_context(pool, story_id),
        **await _repo_context(pool, story_id),
    }
    return {
        "agent_md": _apply(_read("assistant.md"), values),
        "claude_md": _apply(_read("claude.md"), values),
        "skills": {},
    }
