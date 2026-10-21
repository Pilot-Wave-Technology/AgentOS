"""Who made this commit — the human, and the agent that did the work.

A commit attributed to nobody is the git-level version of a ledger event
attributed to `system`, and it goes wrong the same way: months later
somebody needs to know whose intent a change was and the record does not
say. `story_commit` passed no identity at all and relied on whatever git
config happened to be on the worker.

AUTHORSHIP IS NOT AUTHORISATION, and conflating them is what makes this
look harder than it is.

    authorisation   the credential that lets you PUSH — short-lived,
                    minted per operation, worker-side, never on disk
    authorship      who is RECORDED as having made the commit — no
                    credential involved at all

E0 has no remote, so there is nothing to authorise and nothing should be
minted. What is missing is the second, and it needs no secret.

THE SHAPE. Git's author/committer split already means what we need, and
it is what the patch workflow has always used it for:

    Author      the human whose story this is       — their intent
    Committer   the executor agent that ran git     — who made the object
    Co-Authored-By  every agent that participated   — the executor, and
                    the assistant when one was in the loop

The human is the AUTHOR rather than a co-author trailer, so `git
shortlog` attributes work to the person who asked for it instead of to a
fleet of execution ids. The agents are co-authors, so both show up as
contributors and neither disappears.

IDENTITY COMES FROM THE RUN, NEVER FROM AMBIENT CONFIG. Passed as
`GIT_AUTHOR_*` / `GIT_COMMITTER_*` on the one subprocess that needs it,
and nothing is written into `.git/config` — which two concurrent stories
would share, and which is on disk inside the workspace the sandbox can
read. Same argument as the Verifier taking its command from the contract
and never from the tree.

NOTHING IS INVENTED. When the human cannot be determined the commit says
`unattributed` and says it in the trailers too. A plausible-looking name
in an audit trail is worse than an honest gap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DOMAIN = "agentos.local"
UNATTRIBUTED = "unattributed"

# Actors the harness creates for itself. They are never the human.
_MACHINE = re.compile(r"^(worker|corpus|adjudicator|harness|system)\b")
_ASSISTANT = re.compile(r"assistant")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-") \
        or UNATTRIBUTED


@dataclass
class Participant:
    id: str
    role: str          # human | assistant | executor
    email: str = ""

    def __post_init__(self):
        if not self.email:
            self.email = f"{_slug(self.id)}@{DOMAIN}"

    def __str__(self) -> str:
        return f"{self.id} <{self.email}>"


@dataclass
class CommitIdentity:
    """Everyone who is on this commit, and the trailers that record it."""

    human: Participant
    executor: Participant
    assistant: Participant | None = None
    execution_id: int = 0
    generation: int = 0
    goal_id: str = ""
    contract_id: int | None = None
    extra: dict = field(default_factory=dict)

    def env(self) -> dict:
        """`GIT_*` for exactly one subprocess. Nothing touches the repo."""
        return {
            "GIT_AUTHOR_NAME": self.human.id,
            "GIT_AUTHOR_EMAIL": self.human.email,
            "GIT_COMMITTER_NAME": self.executor.id,
            "GIT_COMMITTER_EMAIL": self.executor.email,
        }

    def trailers(self) -> list[str]:
        """Machine-readable provenance, in the message.

        Trailers rather than git notes: notes do not travel on a push by
        default and are lost by most tooling, while trailers survive
        rebase and cherry-pick and are readable with
        `git log --format='%(trailers:key=AgentOS-Execution)'`.
        """
        out = []
        # The human already has author credit; co-authoring them again
        # would be a duplicate line every tool dedupes anyway.
        for who in (self.assistant, self.executor):
            if who is not None:
                out.append(f"Co-Authored-By: {who}")
        out.append(f"AgentOS-Execution: {self.execution_id}")
        out.append(f"AgentOS-Generation: {self.generation}")
        if self.goal_id:
            out.append(f"AgentOS-Goal: {self.goal_id}")
        if self.contract_id:
            out.append(f"AgentOS-Contract: {self.contract_id}")
        out.append(f"AgentOS-Human: {self.human.id}")
        for k, v in sorted(self.extra.items()):
            out.append(f"AgentOS-{k}: {v}")
        return out

    def message(self, summary: str) -> str:
        body = summary.rstrip()
        return body + "\n\n" + "\n".join(self.trailers()) + "\n"


def executor_for(execution_id: int, kind: str = "story",
                 domain: str = "coding") -> Participant:
    return Participant(id=f"agentos-{kind}-{domain}-{execution_id}",
                       role="executor",
                       email=f"exec-{execution_id}@{DOMAIN}")


async def resolve(conn, execution_id: int,
                  human: str | None = None) -> CommitIdentity:
    """Who is on this run, from what the run recorded.

    `human` overrides everything and is what PRODUCTION will pass: there
    is a real user id there, and guessing when one exists would be
    absurd. The resolution below is the fallback for E0, which has no
    user table — and it is a reading of real authorisation acts rather
    than a guess:

        the person who APPROVED THE PLAN authorised this work
        an actor that is neither the worker nor an assistant is a person
        anything matching `assistant` is the assistant

    Nothing here invents a name. An undetermined human is recorded as
    `unattributed`, in the author field and in the trailers.
    """
    row = await conn.fetchrow(
        "SELECT kind, domain, generation, goal_id, contract_id "
        "FROM executions WHERE id = $1", execution_id)
    kind = row["kind"] if row else "story"
    domain = row["domain"] if row else "coding"

    approver = await conn.fetchval(
        "SELECT answered_by FROM questions WHERE execution_id = $1 "
        "AND kind = 'authorization' AND status = 'ANSWERED' "
        "AND verdict = 'approve' ORDER BY id DESC LIMIT 1", execution_id)
    actors = [r["actor_id"] for r in await conn.fetch(
        "SELECT DISTINCT actor_id FROM proposals WHERE execution_id = $1",
        execution_id)]

    assistant_id = next((a for a in actors if _ASSISTANT.search(a or "")),
                        None)
    people = [a for a in actors
              if a and not _MACHINE.match(a) and a != assistant_id]
    who = (human or (approver if approver and not _MACHINE.match(approver)
                     else None) or (people[0] if people else None))
    if who is None:
        logger.info("execution %s: no human identity on record", execution_id)

    return CommitIdentity(
        human=Participant(id=who or UNATTRIBUTED, role="human"),
        executor=executor_for(execution_id, kind, domain),
        assistant=(Participant(id=assistant_id, role="assistant")
                   if assistant_id else None),
        execution_id=execution_id,
        generation=row["generation"] if row else 0,
        goal_id=(row["goal_id"] if row else "") or "",
        contract_id=row["contract_id"] if row else None)
