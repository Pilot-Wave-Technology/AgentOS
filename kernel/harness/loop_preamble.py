"""What a leg opens with, and the ORDER it opens with, which is the point.

Nothing here is difficult on its own. Every function is a best-effort
read that appends one thing to `messages`. The file exists because the
sequence is load-bearing and was, until it was collected here, spread
over 180 lines of `run_leg` where no reader could see it whole:

    [system prompt]  stable, byte-identical across every leg of a story
    [brief]          the prior leg, via kernel/harness/loop_rehydrate.py
    [task]           the caller's own messages
    [human answer]   the reply to a question THIS run asked
    [refusal]        the verifier's verdict on the last claim
    [steers]         standing amendments to the task

WHY THE SYSTEM PROMPT IS FIRST AND EVERYTHING ELSE FOLLOWS. It is the
only part of the request that can be cached -- the brief changes every
leg by construction, and anything placed BEFORE it inherits that
instability. Section 8.3 prices the difference at $60 against $314 on
the same story. It is also re-read every leg rather than inherited
through the brief, so AGENTS.md and SOUL.md cannot drift out of context
mid-story.

WHY THE AMENDMENTS ARE LAST. A steer is the most recent thing anyone
said about what to do, so it reads last. `take_proposals` injects one at
a step boundary too, but that injection lives in a single leg's message
list and dies with it -- a steer taken in leg 2 was gone in leg 3 even
when leg 2 ended cleanly, because the brief is model prose and does not
carry it. Reloading here is what makes it standing.

WHY THE REFUSAL IS ITS OWN MESSAGE AND NOT PART OF THE BRIEF (F2b). The
brief's `next step` is R4's field: the model's own unverified opinion. A
refusal is the harness's MEASURED fact, and mixing the two puts the one
thing the next leg should trust in the place it should not. Without it
the model re-claims the identical thing, and E5's fence then refuses the
re-claim by zero rowcount -- so the run is stuck twice over, once not
knowing what was wrong and again because the mechanism that would have
said so now blocks it.

EVERY READ IS BEST-EFFORT AND MUST STAY SO. A leg that cannot read its
mailbox still has work to do. Do not "simplify" the handlers into
raises.
"""

from __future__ import annotations

import logging

from kernel.harness.intake import format_steers_for_prompt, standing_steers
from kernel.harness.loop_rehydrate import rehydrated
from kernel.harness.park import answered_since, format_answer_for_prompt
from kernel.profiles.prompt import compose_system_prompt

logger = logging.getLogger(__name__)


async def opening_messages(conn, execution_id: int, generation: int,
                           messages: list[dict], *, rehydrate: bool,
                           profile, tool_executor) -> list[dict]:
    """The full opening of a leg, assembled in the order above."""
    messages = await rehydrated(conn, execution_id, generation, messages,
                                rehydrate)
    messages = await _with_human_answer(conn, execution_id, generation,
                                        messages)
    messages = await _with_refusal(conn, execution_id, generation, messages)
    messages = await _with_steers(conn, execution_id, messages)
    return _with_system_prompt(messages, profile, tool_executor)


async def _with_human_answer(conn, execution_id: int, generation: int,
                             messages: list[dict]) -> list[dict]:
    # ── E6i/E6h: a human's reply reaches the model as a reply ────────────
    # Ahead of the task and after the brief, so the model reads the answer
    # to its own question rather than inferring it from a changed
    # workspace. Scoped to prior generations: an answer to THIS leg's
    # question would mean the leg never parked.
    if generation > 0:
        try:
            replied = await answered_since(conn, execution_id, generation)
            if replied:
                messages = list(messages) + [{
                    "role": "user",
                    "content": format_answer_for_prompt(replied),
                }]
                logger.info("leg resumed with a %s answer (request %s)",
                            replied["kind"], replied["id"])
        except Exception as e:
            # Never block the leg on reading an answer.
            logger.warning("could not read a human answer: %s", e)

    return messages


async def _with_refusal(conn, execution_id: int, generation: int,
                        messages: list[dict]) -> list[dict]:
    # ── F2b: a refusal is the harness's VERIFIED fact ────────────────────
    # Read fresh each leg, ahead of the assistant's amendments and behind
    # the brief. Never inside `next step`: R4 makes that the model's own
    # unverified field, and mixing a measured refusal into it puts the
    # one thing the next leg should trust in the place it should not.
    #
    # Without this the model claims the identical thing again — and E5's
    # fence then refuses the re-claim by zero rowcount, so the run is
    # stuck twice over: once not knowing what was wrong, and again
    # because the mechanism that would have said so now blocks it.
    try:
        from kernel.verifier.refusal import format_for_prompt, latest_refusal
        verdict = await latest_refusal(conn, execution_id, generation)
        if verdict:
            messages = list(messages) + [{
                "role": "user",
                "content": format_for_prompt(verdict),
            }]
            logger.info("leg carries a refusal of claim %s (%d reason(s))",
                        verdict["claim_seq"], len(verdict["refusal"]))
    except Exception as e:                                  # noqa: BLE001
        # Never block the leg on reading a verdict.
        logger.warning("could not read a refusal: %s", e)

    return messages


async def _with_steers(conn, execution_id: int,
                       messages: list[dict]) -> list[dict]:
    # ── H1a: standing amendments to the task, reloaded every leg ─────────
    # A steer is not a one-shot message. `take_proposals` injects one at a
    # step boundary, but that injection lives in this leg's message list
    # and dies with it — so a steer taken in leg 2 was gone in leg 3 even
    # when leg 2 ended cleanly, because the brief is model prose and does
    # not carry it. Reloaded here so it stays in front of the model for
    # the rest of the run.
    #
    # Placed after the brief and the human's answer, and before the
    # system prompt is prepended: the amendment is the most recent thing
    # anyone said about what to do, so it reads last.
    try:
        steers = await standing_steers(conn, execution_id)
        if steers:
            messages = list(messages) + [{
                "role": "user",
                "content": format_steers_for_prompt(steers),
            }]
            logger.info("leg carries %d standing steer(s)", len(steers))
    except Exception as e:                                  # noqa: BLE001
        # Never block the leg on reading the mailbox.
        logger.warning("could not read standing steers: %s", e)

    return messages


def _with_system_prompt(messages: list[dict], profile,
                        tool_executor) -> list[dict]:
    # ── E6f: the stable system prompt, ahead of everything volatile ──────
    # Prepended AFTER rehydration so it lands in front of the brief:
    # [profile prompt] [brief] [task]. That order is the whole point.
    # The prompt is byte-identical across every leg of a story, so it is
    # the only part of the request that can be cached; the brief changes
    # every leg by construction, and anything placed before it inherits
    # that instability. §8.3 prices the difference at $60 versus $314.
    #
    # It is re-read every leg rather than inherited through the brief, so
    # AGENTS.md and SOUL.md cannot drift out of context mid-story.
    if profile is not None:
        workspace = getattr(getattr(tool_executor, "_envelope", None),
                            "workspace_path", "") or ""
        system_prompt = compose_system_prompt(profile, workspace)
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + list(messages)
            logger.info(
                "System prompt composed for %s/%s: %d chars, %d skills",
                profile.kind, profile.domain, len(system_prompt),
                len(profile.skill_dirs),
            )

    return messages
