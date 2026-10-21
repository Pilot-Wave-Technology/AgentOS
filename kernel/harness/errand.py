"""Eb2 — how an errand ends. R3, as code.

**The errand's declared command exits, and its exit status is the verdict.**

An errand's verdict source already exists and beats any Verifier: a deploy
succeeds or fails, a suite passes or fails. Adding an LLM judge to grade
something that reports its own result deterministically is the pattern this
refactor exists to remove.

  exit 0        -> DONE
  exit non-zero -> FAILED, with the code and a bounded tail of the output
  budget / step ceiling reached first -> FAILED, with which one fired

The disagreement case is RECORDED, NOT ADJUDICATED. If the command exits 0
while the model insists it failed, the exit code wins and both facts are
kept. Letting the model's dissent change the outcome would make it a
verdict source after all, and R3 resolves the other way.

Errands never enter VERIFYING, so R2's leak cannot occur on this profile.
Machine version 2 adds RUNNING -> DONE for exactly this; the guard on who
may take it is the PROFILE's has_verifier flag, not the machine's — a
second machine would make "one executor, three profiles" false again.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TAIL_CHARS = 2000


class ErrandVerdict:
    """The outcome of an errand, and which predicate produced it."""

    def __init__(self, state: str, reason: str, exit_code: int | None = None,
                 output_tail: str = ""):
        self.state = state          # "DONE" | "FAILED"
        self.reason = reason        # errand_terminal | budget | step_ceiling
        self.exit_code = exit_code
        self.output_tail = output_tail

    def __repr__(self) -> str:
        return (f"ErrandVerdict({self.state}, reason={self.reason!r}, "
                f"exit={self.exit_code})")


def verdict_from_exit(exit_code: int, output: str) -> ErrandVerdict:
    """The terminal command finished. Its status is the verdict."""
    tail = (output or "")[-TAIL_CHARS:]
    if exit_code == 0:
        return ErrandVerdict("DONE", "errand_terminal", 0, tail)
    return ErrandVerdict("FAILED", "errand_terminal", exit_code, tail)


def verdict_from_predicate(predicate: str) -> ErrandVerdict:
    """Budget or step ceiling reached before the command finished.

    FAILED with distinct reasons, per R3 — an errand that runs out of budget
    has not succeeded, and saying only "failed" loses which limit was hit.
    """
    return ErrandVerdict("FAILED", predicate, None, "")


async def apply_verdict(conn, execution_id: int, profile,
                        verdict: ErrandVerdict, command: str) -> None:
    """Write the verdict. Refuses if the profile has a Verifier.

    The machine allows RUNNING -> DONE; this is what stops a STORY taking
    it. A story reaching DONE without adjudication is the failure the
    transition would otherwise open up.
    """
    if getattr(profile, "has_verifier", True):
        raise ValueError(
            f"profile {profile.kind}/{profile.domain} has a Verifier; it may "
            "not take the errand_terminal transition to a terminal state"
        )

    await conn.execute(
        """
        UPDATE executions
        SET state = $2,
            terminal_command = $3,
            terminal_exit_code = $4,
            terminal_output = $5
        WHERE id = $1
        """,
        execution_id, verdict.state, command, verdict.exit_code,
        verdict.output_tail,
    )
    logger.info("errand %s -> %s (%s, exit=%s)", execution_id,
                verdict.state, verdict.reason, verdict.exit_code)


def dissent_note(verdict: ErrandVerdict, model_said: str | None) -> str | None:
    """R3's open question, resolved: record the disagreement, do not judge it.

    Returns a line for the journal when the model's account contradicts the
    exit code. Nothing reads it to decide anything — that is the point.
    """
    if not model_said:
        return None
    said = model_said.lower()
    claims_failure = any(w in said for w in ("fail", "error", "broken", "did not work"))
    claims_success = any(w in said for w in ("success", "passed", "works", "complete"))

    if verdict.exit_code == 0 and claims_failure and not claims_success:
        return ("DISSENT: the terminal command exited 0 but the model reported "
                "failure. The exit code is the verdict; this is recorded, not "
                "adjudicated.")
    if verdict.exit_code not in (0, None) and claims_success and not claims_failure:
        return (f"DISSENT: the terminal command exited {verdict.exit_code} but "
                "the model reported success. The exit code is the verdict; "
                "this is recorded, not adjudicated.")
    return None
