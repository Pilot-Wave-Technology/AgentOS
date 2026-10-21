"""The refresh itself: plan, or plan and then apply.

WHAT THIS DOES NOT DO IS RESTART THE GATEWAY, and that is the design
rather than an omission. A refresh writes three things with three
different activation rules:

  prompts   inert until the gateway restarts, because it caches
            bootstrap files per session
  tools     live on the next turn
  skills    read on demand

They must go live TOGETHER -- the prompt is what names the tools, so a
tool renamed live while its prompt still teaches the old name means every
call is refused. The restart is what makes the prompts live, so the
restart is what makes the set coherent, and until it happens the fleet is
mid-change.

So this prints, at the end, exactly what the operator must do next -- and
REFUSES TO RECOMMEND THE RESTART if any agent came back PARTIAL. A whole
agent failing is harmless; it stays as it was. One agent half-succeeding
is not: after a restart it runs new prompts describing tools it does not
have. That case wants a person, not a default.

`--goal` FIRST. Prove a refresh on one goal, read the result, then run
the fleet. The scoping is in `refreshable_agents` so that the scoped and
unscoped runs cannot disagree about who is eligible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.agent_refresh.apply import Outcome, apply_to_agent
from app.services.agent_refresh.eligibility import refreshable_agents
from app.services.agent_refresh.plan import plan_for, render_report
from app.services.agent_refresh.quiesce import backlog, is_quiet
from app.services.agent_refresh.render import desired_for

logger = logging.getLogger(__name__)


@dataclass
class Result:
    report: str
    outcomes: list[Outcome] = field(default_factory=list)
    applied: bool = False
    refused: str = ""
    # How many agents the plan covers. Carried rather than parsed back
    # out of `report` -- the caller needs it for a button label, and
    # reading it off rendered prose would break the moment the wording
    # changes.
    eligible: int = 0

    @property
    def partial(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.partial]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.failures and not o.partial]


async def _reconcile_from_disk(conn) -> tuple[bool, list[str]]:
    """Pull the repository's prompts into `template_files` first.

    WITHOUT THIS THE PAGE CANNOT SEE A PROMPT CHANGE AT ALL, and it took
    a live fleet refresh to find out. `render.desired_for` reads
    `template_files`; the only thing that ever wrote the repository's
    AGENTS.md / SOUL.md into that table was `sync_all`, called from the
    boot resync in `app/main.py` -- which was switched OFF on
    2026-09-23, with a comment claiming this page "now does the same
    push". It does the push. It did not do the reconcile.

    So on 2026-09-24 a fleet refresh reported `12 ok, 0 failed, 0
    PARTIAL` and delivered the prompts of the previous build: every GO
    missing the section written that morning, every assistant still
    telling people they had four stories. Nothing failed, and nothing
    anywhere could have said so -- the table was self-consistent, and a
    push of stale rows is indistinguishable from a push of fresh ones.

    IT RUNS ON THE PLAN TOO, not only on apply. The plan is what a
    person reads before deciding, and a plan rendered from rows the
    apply is about to replace describes a refresh that will not happen.

    Cheap and idempotent: two files per kernel template, rewritten only
    when the bytes differ. Never raises -- an unreadable prompt file
    must not take the page down, and the rows already in the table are
    still serviceable.

    RETURNS (ok, changed), because an empty list has TWO meanings and
    conflating them reproduces this bug in miniature: "the repository
    already matched" and "the reconcile blew up" would be the same `[]`,
    and the reassuring line printed for the first would then be printed
    for the second.
    """
    try:
        from kernel.assistant.prompts import sync_all

        changed = []
        for template_id, result in (await sync_all(conn)).items():
            for name in (result or {}).get("changed_files", ()):
                changed.append(f"{template_id}/{name}")
        if changed:
            logger.info("agent refresh: reconciled from disk first -- %s",
                        ", ".join(changed))
        return True, changed
    except Exception:                                       # noqa: BLE001
        logger.exception(
            "agent refresh: could not reconcile prompts from disk; the "
            "plan below is rendered from whatever `template_files` "
            "already holds, which may predate this build")
        return False, []


async def refresh(conn, gateway_admin, *, apply: bool = False,
                  goal_id: str = "", agent_type: str = "") -> Result:
    """Plan a refresh, and optionally carry it out.

    `apply` defaults to False and must be asked for. A tool that writes
    to the whole fleet by default is one whose first run is an accident.
    """
    ok, reconciled = await _reconcile_from_disk(conn)
    agents = await refreshable_agents(
        conn, goal_id=goal_id, agent_type=agent_type)
    desired_by_id = {
        a.gateway_agent_id: await desired_for(conn, a) for a in agents}
    plans = await plan_for(conn, gateway_admin, agents, desired_by_id)
    report = render_report(plans)

    # SAY WHETHER THE SOURCE MOVED. The failure this page had was
    # invisible precisely because a push of stale rows renders
    # identically to a push of fresh ones -- "2 file(s), ok" either way.
    # Naming what the reconcile pulled in, or saying plainly that
    # nothing moved, is the one line that distinguishes them.
    if not ok:
        report += ("\n\nCOULD NOT RECONCILE from the repository — see the "
                   "logs. What follows is rendered from whatever "
                   "`template_files` already holds, which may predate this "
                   "build. Do not read a clean plan as proof the prompts "
                   "are current.")
    elif reconciled:
        report += ("\n\nReconciled from the repository first: "
                   + ", ".join(reconciled)
                   + "\n(the table now matches this build; applying is what "
                     "sends it to the agents)")
    else:
        # IT SAYS WHAT IT KNOWS, NOT WHAT IT GUESSES. This used to end
        # "...so this refresh delivers what the agents may well already
        # hold" -- an inference about the GATEWAY drawn from the state of
        # a TABLE, in the same report that says there is no read-back.
        # The ordinary case is a SECOND plan click: the first one did the
        # reconcile, so there is nothing left to pull in, and the agents
        # are still holding what they were created with. Read as written,
        # the old sentence said "don't bother applying" at exactly the
        # moment applying was the whole point.
        report += ("\n\nNothing to reconcile: `template_files` already "
                   "matches the repository. That is a fact about the "
                   "TABLE and not about the agents — nothing here can "
                   "read what they hold. If an earlier plan did the "
                   "reconcile, applying is still what delivers it.")

    pending = await backlog(conn)
    if pending:
        # Shown, not enforced. Worth a person seeing -- a long queue can
        # mean something is stuck -- but neither queued work nor a
        # running leg is a reason to refuse a write that touches no
        # memory and stops no process.
        report += ("\n\nAlso in flight (does not block): " + pending)

    if not apply:
        return Result(report=report, eligible=len(agents))

    # ── no turn may straddle the apply ───────────────────────────────
    #
    # NOT the memory guard, which an earlier version of this comment
    # called it. Nothing here touches a memory file or stops a process,
    # so nothing here can interrupt a flush -- that danger belongs to
    # the restart, and lives in its instructions below. What this
    # prevents is narrower: tools go live on the next turn while prompts
    # wait for the restart, so a turn running through the apply would
    # see new tools described by an old prompt.
    quiet = await is_quiet(conn)
    if not quiet:
        return Result(report=report, refused=quiet.reason,
                      outcomes=[], eligible=len(agents))

    outcomes: list[Outcome] = []
    for agent in agents:
        outcomes.append(await apply_to_agent(
            gateway_admin, agent, desired_by_id[agent.gateway_agent_id]))

    result = Result(report=report + "\n" + _applied_report(outcomes),
                    outcomes=outcomes, applied=True, eligible=len(agents))
    logger.info("agent refresh: applied to %d agent(s); %d failed, "
                "%d PARTIAL", len(outcomes), len(result.failed),
                len(result.partial))
    return result


def _applied_report(outcomes: list[Outcome]) -> str:
    lines = ["", "── applied ──", ""]
    lines += [f"  {o.summary()}" for o in outcomes]
    partial = [o for o in outcomes if o.partial]
    failed = [o for o in outcomes if o.failures and not o.partial]
    lines.append("")
    lines.append(f"  {len(outcomes) - len(partial) - len(failed)} ok, "
                 f"{len(failed)} failed, {len(partial)} PARTIAL")
    lines.append("")

    if partial:
        # The one outcome that makes a restart unsafe rather than merely
        # incomplete. Named, not counted, because the operator has to go
        # and look at each one.
        lines += [
            "  DO NOT RESTART YET.",
            "",
            "  These agents took some of the change and not the rest:",
        ]
        lines += [f"    - {o.gateway_agent_id}: {'; '.join(o.failures)}"
                  for o in partial]
        lines += [
            "",
            "  After a restart each of them would run new prompts that "
            "describe",
            "  tools it does not have. Re-run the refresh for those agents "
            "until",
            "  they come back clean, then restart.",
            "",
        ]
        return "\n".join(lines)

    if failed:
        lines += [
            "  The agents above were not touched at all, so they are "
            "unchanged",
            "  rather than half-changed -- restarting is safe, they simply "
            "stay",
            "  stale until you re-run.",
            "",
        ]

    lines += [
        "  NEXT: restart the gateway. Until then these prompts are on disk",
        "  and no running session can see them.",
        "",
        "  The usual way is to run the openclaw gateway deploy, which ends",
        "  in `redeploy.sh`: pnpm build, configure, systemctl restart. It",
        "  leaves /home/openclaw/.openclaw alone, so memory and every",
        "  session transcript survive -- that is what makes this safe.",
        "",
        "  CHECK ONE THING FIRST. That pipeline begins with terraform, and",
        "  terraform RECREATES THE VM if `startup.sh.tpl` has changed. A",
        "  recreated VM takes the data directory with it: every agent's",
        "  MEMORY.md and every transcript, unrecoverable. If that file has",
        "  moved since the last deploy, snapshot the data directory first,",
        "  or restart the process directly instead of running the build.",
        "",
        "  Afterwards, ask an agent something only its own transcript would",
        "  tell it. The chat UI renders history from Postgres and will look",
        "  intact either way, so it proves nothing on its own -- whether the",
        "  AGENT still knows is the real check.",
        "",
        "  The plan will not confirm the tools: the gateway does not report",
        "  them, before or after.",
    ]
    return "\n".join(lines)
