"""The id of the gateway agent an assistant conversation runs on.

Historically one thing: `{agent}-{goal}-{thread}`. That id is also the
workspace directory name, so a thread in it is a HOME — its own MEMORY.md,
SOUL.md, USER.md, its own copy of every skill. While an assistant could
only hold one story at a time nobody noticed, because a second thread
almost never existed. The KER12 assistant has two workspaces and two
memory files today; that is not a future risk, it is the current state.

Stage K's decision: **one agent per (goal, user), N sessions — one per
thread.** The two properties come apart cleanly because the gateway
already separates them:

    session key   agent:{gateway_agent_id}:{goal}-{thread}    per thread
    workspace     resolved from the agent id                  per agent

So dropping the thread from the AGENT id gives a shared home and keeps
separate contexts — which is the requirement in both directions. Sessions
must not be shared (one context across several stories means every
compaction mixes unrelated work, and a noisy story evicts another story's
plan out of the window). The home must be shared, or "what am I working
on?" has no single place to be answered from.

Only the kernel assistant folds. The operator being replaced keeps the
per-thread id it has always had, so nothing already provisioned moves.

WHY THIS IS A FUNCTION AND NOT AN F-STRING. The literal was written out
at five call sites — the chat-connect provisioner, story provisioning,
workspace deletion, the kickoff freshness check, and story metrics — and
they compare against each other. `stories.shca_instance_id` is a
staleness guard: a kickoff that computed the id one way and a
provisioning that computed it the other would re-provision on every
single pass, forever, and look like a workspace that would not settle.
"""
from __future__ import annotations


def compose(agent_id: str, goal_id: str, thread_id: str, *,
            shared_home: bool) -> str:
    """The gateway agent id, given whether this assistant shares a home."""
    return (f"{agent_id}-{goal_id}" if shared_home
            else f"{agent_id}-{goal_id}-{thread_id}")


async def resolve(pool, agent_id: str, goal_id: str, thread_id: str) -> str:
    """The gateway agent id for this (agent, goal, thread).

    One query, cached nowhere on purpose: an assistant's template does not
    change under a running conversation, and a stale cached answer here
    would point a story at a workspace that is not the one being written.
    """
    from app.services.assistant_kind import is_kernel_assistant
    return compose(agent_id, goal_id, thread_id,
                   shared_home=await is_kernel_assistant(pool, agent_id))


def base_agent_candidates(instance_id: str, goal_id: str) -> list[str]:
    """Template `agents.id` values this instance id could fold back to.

    A LIST, not an answer, because the string alone cannot decide. Both
    forms end in the goal id:

        abhi-shca-ker14-9f2c…   operator, thread suffix
        abhi-shca-ker14         shared home, no thread

    and so does GO's own bare `agents.id`, `go-{goal}`. Folding that one
    would turn `go-ker14` into `go` — an id that resolves to nothing, or
    worse, to some other agent. The first version of this function did
    exactly that, and the Stage K gate caught it.

    So it returns every plausible fold and the caller looks them all up.
    A candidate that is not a real agent simply does not join; a wrong
    single answer would have shown one agent's cost under another's name.
    Ordered most-specific first. Never includes `instance_id` itself —
    callers already have that.
    """
    import re
    if not instance_id or not goal_id:
        return []
    out: list[str] = []
    for pattern in (
        # Thread form: the goal id followed by a hex/uuid tail.
        "-" + re.escape(goal_id.lower()) + "-[0-9a-f-]+$",
        # Shared-home form: the goal id and nothing after it.
        "-" + re.escape(goal_id.lower()) + "$",
    ):
        folded = re.sub(pattern, "", instance_id, flags=re.I)
        if folded != instance_id and folded not in out:
            out.append(folded)
    return out
