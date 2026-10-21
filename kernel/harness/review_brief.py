"""The brief a reviewer opens on, assembled per leg.

IT IS THE LEG'S TASK, NOT A FILE. Writing `docs/REVIEW_BRIEF.md` into
the workspace would have been the obvious shape and it is wrong twice
over: the reviewer is about to run `git status` and `git diff` over that
tree, so a brief sitting in it becomes a change the review reports on,
and the harness writing into a workspace the phase exists not to write
to is the rule breaking itself. The brief goes where a leg's opening
message already goes.

REGENERATED EVERY LEG, which is what makes the leg counter work. The
preamble notes that the brief "changes every leg by construction"; here
that is load-bearing rather than incidental — the final leg's brief says
so, in the place the model definitely reads, and turns "ran out of
budget" into "submitted a partial".
"""

from __future__ import annotations

import logging

from kernel.harness import review_sources as src

logger = logging.getLogger(__name__)


def _lines_for_claims(claims: list[dict]) -> list[str]:
    if not claims:
        return ["(the executor made no completion claim — unusual; say so)"]
    out = []
    for i, c in enumerate(claims, 1):
        verdict = ("passed" if c["passed"] else "REFUSED"
                   if c["passed"] is not None else "not adjudicated")
        out.append(f"**Claim {i} — {verdict}**")
        out += [f"- {r}" for r in c["reasons"]]
        if c["passed"] is False and c["refusal"]:
            out.append(f"- refusal: {c['refusal']}")
        out.append("")
    if len(claims) > 1:
        out.append(
            f"There were {len(claims)} claims. A run that claimed, was "
            f"refused and claimed again is worth looking at hardest.")
    return out


def _lines_for_knowledge(entries: list[dict]) -> list[str]:
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append(f"**Cycle {e.get('cycle', '?')}**")
        for t in (e.get("techniques") or []):
            out.append(f"- technique: {t}")
        for lr in (e.get("learned") or []):
            if isinstance(lr, dict):
                out.append(f"- learned: {lr.get('fact', '')} "
                           f"({lr.get('why_it_matters', '')})")
        for ab in (e.get("abandoned") or []):
            if isinstance(ab, dict):
                out.append(f"- abandoned: {ab.get('tried', '')} "
                           f"({ab.get('why_not', '')})")
        out.append("")
    return out or ["(nothing recorded)"]


async def build(conn, execution_id: int, story_id: str, goal_id: str,
                leg: int, of: int, *, workspace: str = "",
                mounts=()) -> str:
    """The review brief for one leg. Never raises: a partial brief beats none.

    A SECTION THAT FAILS SAYS SO. The reviewer is about to judge
    somebody's work, and a section that is silently empty reads as
    "there was nothing" rather than "this could not be read" — which is
    precisely how the gateway reviewer came to judge a description
    against a plan it never received.
    """
    final = leg >= of
    head = (f"# Review brief — {'FINAL LEG' if final else 'leg'} "
            f"{leg} of {of}")
    out = [head, ""]
    if final:
        out += [
            "**This is your last leg.** Call `submit_story_review` before "
            "it ends. If you have not read everything, `partial` with an "
            "honest `not_examined` is the correct verdict — a missing "
            "review is worse than an incomplete one.", ""]

    # Sections that FAILED, as opposed to sections that had nothing to
    # say. The difference matters to the verdict and nothing was telling
    # the reviewer which it was looking at.
    broken: list[str] = []

    async def section(title: str, fn):
        out.append(f"## {title}")
        try:
            out.extend(await fn())
        except Exception as exc:                            # noqa: BLE001
            # A BROKEN SECTION IS A PLATFORM FAULT, NOT A FINDING, and
            # saying so is the whole of this change.
            #
            # Story 8499c9ff came back `code_not_reviewed`. The cause was
            # `permission denied for table git_node_stories` -- the leg
            # role was missing a grant -- and the reviewer did the right
            # thing with what it was told: it could not read the diff, so
            # it would not claim to have reviewed one. But the sentence
            # naming the real reason sat inside the body, and the verdict
            # that reached the goal reads as a judgement about the CODE.
            #
            # Every story reviewed in a pod would have returned it, all
            # for the same reason, and each would have looked like a
            # separate review that went badly.
            logger.exception("review brief: %s failed", title)
            broken.append(f"{title}: {exc}")
            out.append(f"_(could not be read: {exc}. Treat this as "
                       f"missing information, not as an empty answer.)_")
        out.append("")

    async def _what_was_asked():
        row = await src.story_row(conn, story_id)
        if row is None:
            return ["_(the story row is gone)_"]
        ac = (row["acceptance_criteria"] or "").strip()
        return [f"**{row['id']} — {row['title']}**", "",
                (row["description"] or "").strip() or "_(no description)_",
                "", "### Acceptance criteria",
                ac or "_(none were written; judge against the description "
                      "and the plan)_"]

    async def _branches():
        bs = await src.branches(conn, story_id)
        if not bs:
            return ["_(no git node — there may be nothing changed here. "
                    "Look, and say so if that is what you find.)_"]
        return [f"- `{b['repo']}` branch `{b['branch']}` "
                f"(node {b['state']}, trunk `{b['trunk']}`)" for b in bs]

    async def _changed():
        """WHAT MOVED — the one thing the workspace cannot tell you.

        Everything else a reviewer needs is in the tree and `read` is
        better at it than a diff. But "the claim says nothing else was
        modified" is only checkable against the trunk, and that is what
        this is here for.
        """
        if not workspace:
            return ["_(no workspace path — the change list could not be "
                    "computed. Treat scope as unverified.)_"]
        repos = await src.changed_files(conn, story_id, workspace, mounts)
        if not repos:
            return ["_(no git mount — nothing to compare.)_"]
        lines = []
        for r in repos:
            lines.append(f"### `{r['at']}` against `{r['trunk'] or '?'}`")
            if not r["readable"]:
                lines += ["_(this repository has no recorded trunk, so what "
                          "changed could not be established. Say so rather "
                          "than assuming nothing did.)_", ""]
                continue
            lines += ["```", r["stat"] or "(no files differ from the trunk)",
                      "```", ""]
            if r["patch"]:
                lines += ["<details><summary>the patch</summary>", "",
                          "```diff", r["patch"], "```", "</details>", ""]
        lines.append(
            "**This is the whole change.** If a file is not listed here, "
            "this story did not touch it — which is how you check a claim "
            "that says nothing else was modified. To read any file in "
            "full, including ones not listed, use `read`.")
        return lines

    async def _plans():
        from kernel.knowledge.read import plans_for

        plans = await plans_for(conn, story_id)
        if not plans:
            return ["_(no plan was registered)_"]
        lines = []
        for p in plans:
            lines.append(f"### Cycle {p.get('cycle')} — {p.get('status')}"
                         + (f", approved by {p.get('approved_by')}"
                            if p.get("approved_by") else ""))
            if p.get("rationale"):
                lines += [str(p["rationale"]).strip(), ""]
            lines += [f"{i}. {s}" for i, s in
                      enumerate(p.get("steps") or [], 1)]
            lines.append("")
        lines.append(
            "**A story with several plans owes you all of them.** Each "
            "cycle after the first exists because somebody read the work, "
            "said it was not finished and asked for more — so the "
            "deliverable is the UNION of the approved cycles, not the last "
            "one. Judging the final state against the latest plan alone "
            "marks a story complete when it delivered only its most recent "
            "instalment.")
        lines.append("")
        lines.append(
            "Read the earlier cycles for dropped promises too: a step that "
            "appeared in cycle 1, vanished from cycle 2 and was never done "
            "is the commonest way scope quietly shrinks, and it is "
            "invisible if you only read the last.")
        lines.append("")
        lines.append(
            "_Numbering: these count PLANS REGISTERED, so a plan sent back "
            "for revision and re-registered takes the next number here. The "
            "`agentos_plans/` files count COMPLETED cycles and a revision "
            "overwrites its file, so the numbers do not line up — there are "
            "usually fewer files than cycles listed above. Match them by "
            "content, not by number._")
        return lines

    async def _settled():
        answers = await src.answered_questions(conn, story_id)
        st = await src.steers(conn, story_id)
        if not answers and not st:
            return ["_(nobody answered a question or steered this story)_"]
        lines = []
        for a in answers:
            verb = "DECLINED" if a["declined"] else "answered"
            lines.append(f"- asked: {a['asked']}")
            lines.append(f"  {verb} by {a['by'] or 'a person'}: {a['answer']}")
        for s in st:
            lines.append(f"- steer ({s['status']}) from {s['by']}: {s['text']}")
        lines.append("")
        lines.append(
            "**These are decisions, not suggestions.** When a person was "
            "asked X or Y and said Y, the code doing Y is correct. Do not "
            "raise a finding that reopens one of these.")
        return lines

    async def _claim():
        return (_lines_for_claims(await src.completion_claims(conn, story_id))
                + ["### What the executor recorded learning", ""]
                + _lines_for_knowledge(await src.knowledge(conn, story_id)))

    async def _goal():
        g = await src.goal_context(conn, goal_id)
        lines = []
        if g["objective"]:
            lines += ["**Objective:** " + g["objective"], ""]
        if g["brief"]:
            lines += [g["brief"], ""]
        elif g["summary"]:
            lines += [g["summary"], ""]
        if g["decisions"]:
            lines += ["### Decisions already taken at goal level"]
            lines += [f"- {d['what']}" + (f" — {d['why']}" if d["why"] else "")
                      for d in g["decisions"]]
            lines.append("")
        if g["siblings"]:
            lines += ["### What sibling stories delivered"]
            lines += [f"- `{s['story']}` {s['title']} ({s['state']})"
                      + (f" — {s['delivered']}" if s["delivered"] else "")
                      for s in g["siblings"]]
        return lines or ["_(no goal context recorded)_"]

    await section("What was asked", _what_was_asked)
    await section("Where the work is", _branches)
    await section("What changed", _changed)
    await section("What was planned", _plans)
    await section("What a person already settled", _settled)
    await section("What the executor claimed", _claim)
    await section("Where the goal is", _goal)

    if broken:
        # AT THE TOP, not buried where the failure happened. A reviewer
        # that meets "permission denied" three sections in has already
        # started forming a verdict about the work.
        out = out[:1] + [
            "", "> **This brief is incomplete because the platform "
            "failed, not because the work is missing anything.** The "
            "sections below could not be built:", "",
        ] + [f"> - {b}" for b in broken] + [
            "",
            "> This is a fault in the review machinery. Do NOT record it "
            "as a finding about the code, and do NOT let it decide the "
            "verdict by itself: a section you were prevented from reading "
            "is not a section that was empty.",
            "",
            "> Review everything you CAN read and say plainly what you "
            "could not. If what is missing is the diff, you cannot claim "
            "to have reviewed a change — reading the tree shows its final "
            "state, not what this story altered — so say that, and say it "
            "is an infrastructure failure somebody has to fix.",
            "",
        ] + out[1:]

    out += ["---", "",
            "Start at step 1 of your instructions. Read this whole brief "
            "before running any tool."]
    return "\n".join(out)
