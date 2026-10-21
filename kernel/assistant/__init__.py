"""H8 — the assistant's surface, and the absences are the design.

    READ    story_status . inbox . read_artifact . list_resources
            memory_search . memory_get          (gateway-side)
    WRITE   start_story . ask_go . steer . amend . relay_answer
            cancel_story
    ASK     ask_executor

ASK IS ITS OWN COLUMN because it is the only thing here that does not
tell. Every write is imperative, and for a long time that was the whole
surface — so a person who merely wanted to KNOW something had to phrase
it as a steer, and an executor forty steps into doing as it is told
reads anything in that position as one more thing to do. Asked why it
had chosen in-memory storage, one went and changed the storage.

The assistant does no work. It reads projections and writes intents, and
every capability it appears to have beyond those two is one of them
wearing a different hat.

What is deliberately absent, each enforced by there being no function
rather than by a rule in a prompt:

    dispatch        the Conductor, level-triggered, is the only path
    provision       deterministic in the kernel, before the assistant sees it
    approve         a human uid (§1.3, and H8a closed the way round it)
    verify          the Verifier is the sole writer of criterion.passes
    commit          story_commit, from an executor under an approved contract
    create a story  GO — the assistant may only ask. `start_story`
                    creates a RUN of a story GO already made, which is
                    a different object: a story can be run twice.
    grant a resource  GO — `list_resources` reads, `ask_go` requests
"""

from kernel.assistant.artifacts import list_artifacts
from kernel.assistant.context import Refused
from kernel.assistant.progress import story_progress
from kernel.assistant.read import (inbox, list_resources, read_artifact,
                                   story_status)
from kernel.assistant.write import (amend, ask_executor, ask_go, cancel_story,
                                    relay_answer, show_card, start_story,
                                    steer)
# THE FOUR THAT LIVE ELSEWHERE. `TOOLS` named them and `__all__` re-exported
# them, so `from kernel.assistant import *` raised AttributeError on the
# first of them -- while the surface assertion stayed green, because it
# compares NAMES against the route table and never resolves one.
from kernel.assistant.artifacts import list_artifacts
from kernel.go.state import goal_state
from kernel.knowledge.read import goal_knowledge
from kernel.knowledge.write import record_finding

# `memory_search` and `memory_get` are NOT here: they are served by the
# gateway's memory plugin, not by this API, so the surface assertion
# (set(ROUTES) == set(TOOLS)) stays about what the kernel serves.
from kernel.assistant.errand_dispatch import dispatch_errand  # noqa: F401,E402

TOOLS = ("story_status", "story_progress", "goal_state", "goal_knowledge",
         "inbox", "read_artifact", "list_artifacts", "list_resources",
         "start_story", "ask_go",
         "steer", "amend", "relay_answer", "cancel_story", "show_card",
         # ANOTHER RUN OF AN ERRAND SOMEBODY ALREADY APPROVED. Nothing
         # schedules an errand: the first run goes out when a person
         # approves it, and every run after that is one somebody asks
         # for. A run can end without producing the work -- cancelled,
         # failed, or killed while its pod was still being scheduled --
         # and before this there was no way to start another.
         #
         # Requires a `human_uid` and refuses without one: a run spends
         # the approver's budget and carries their name.
         "dispatch_errand",
         "ask_executor", "record_finding")

__all__ = ["Refused", "TOOLS", *TOOLS]
