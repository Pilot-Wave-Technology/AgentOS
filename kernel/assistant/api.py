"""H10 — the eight tools over HTTP. The seam the gateway calls.

The assistant is an OpenClaw agent and stays one: the model loop is the
gateway's and is not rebuilt here. What was missing is the other side.
H8's eight functions are in-process Python and the gateway has no way to
reach them.

THE PATH SEGMENT IS THE TOOL NAME, and that is the whole design of this
module. `/assistant/<goal>/steer` serves `steer`; there is no route that
is not a tool and no tool that is not a route. So "what can be reached
over the wire" is not a claim anybody has to audit — it is
`set(ROUTES) == set(TOOLS)`, asserted by the gate. Adding a route that
dispatches, provisions or approves means inventing a ninth tool name,
and the assertion fails on the name before anyone reads the handler.

That matters because the surface is the security model. The live system
this replaces has five tools in its assistant allowlist with no row in
the tools catalog, surviving only because nothing validates them; the
lesson taken from that is to make the surface checkable rather than
carefully curated.

WHY ITS OWN SERVER AND NOT THE CONSOLE'S. The console is an ops view and
says so — "this is not a product surface". The gateway must be able to
reach the assistant whether or not anyone is watching a console, and
turning the console off must not turn the assistant off with it.

WHAT IT DOES NOT DO. No model, no loop, no thread state. Every request
is one function call against the database and returns what the function
returns, including the typed refusals: an ambiguous target still
refuses, and `relay_answer` still cannot approve a plan.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from kernel.assistant import (TOOLS, amend, ask_executor, ask_go,
                              cancel_story, inbox, list_artifacts,
                              list_resources, read_artifact, relay_answer,
                              start_story, steer, story_status)
from kernel.assistant.context import Refused
from kernel.go import TOOLS as GO_TOOLS
from kernel.assistant.errand_dispatch import dispatch_errand
from kernel.go import (accept_errand, answer_request, code_state,
                       goal_state,
                       read_brief, read_resource,
                       resource_brief, retire_decision, update_brief,
                       grant_resource, list_requests, plan_preview,
                       propose_errand,
                       register_resource, update_goal)
from kernel.knowledge import (goal_knowledge, raise_alignment, record_finding,
                              resolve_alignment)
from kernel.loopbridge import LoopRuntime
from kernel.obs import configure

logger = logging.getLogger(__name__)

PORT = int(os.environ["ASSISTANT_API_PORT"]
           if "ASSISTANT_API_PORT" in os.environ else 8790)

# READS are GET, WRITES are POST, and the split is not decoration: a GET
# that changes something is invisible to every proxy, log and retry
# policy that assumes otherwise.
READS = {"story_status", "story_progress", "inbox", "read_artifact",
         # Ec2g — the INDEX. `read_artifact` needs an id, and an id comes
         # from a bounded output the assistant may never have seen: an
         # errand runs unwatched and its artifacts are the whole result.
         "list_artifacts",
         "list_resources", "goal_state", "goal_knowledge"}
WRITES = {"start_story", "ask_go", "steer", "amend", "show_card",
          "relay_answer", "cancel_story", "ask_executor",
          # A run of an ALREADY-APPROVED errand. It grants nothing new:
          # the grant is what authorises running, and this asks for one
          # more run under it, carrying a person's decision.
          "dispatch_errand",
          "record_finding"}
ROUTES = READS | WRITES

# ── GO's surface, alongside and separate ─────────────────────────────
#
# Served by the same process because there is one kernel and one port,
# and kept as its own route set because the authority differs: the
# assistant asks, GO decides. Sharing a set would make that a naming
# convention rather than a boundary, and the startup assertion below
# checks each against its own package.
GO_READS = {"goal_state", "code_state", "plan_preview",
            "resource_brief", "read_resource", "list_requests",
            "goal_knowledge",
            # WHO IS ON THE GOAL, and with what role. Replaces a roster
            # that was rebuilt and prepended to EVERY message -- and so
            # written into the transcript once per turn, for ever. A
            # read, because it changes nothing and GO needs it exactly
            # when it is working out who may decide something.
            "goal_members",
            # I3c — what an errand produced. GO plans work whose
            # deliverable is an artifact rather than a branch, and
            # `goal_state` could only say the story finished.
            "list_artifacts",
            # D2 — and the contents. See kernel/go/tools.py.
            "read_artifact",
            # I5e — what is parked and waiting. The same goal-scoped read
            # the assistant makes; GO watching an errand needs to see the
            # question, not be told a run is slow.
            "inbox",
            # PHASE C — DRIVING AN AUTONOMOUS ERRAND. The same two reads
            # the assistant uses to answer "how is it going": the machine
            # state, and the work. GO holds a thread per live errand now
            # and was expected to drive them while able only to see that
            # one existed.
            #
            # Route membership is a coarse yes-or-no per TOOL and cannot
            # say "yes, but only for this run" -- so every one of these
            # five is gated per call by `kernel/go/drivable.go_may_drive`,
            # which refuses anything that is not an errand with a live
            # grant. Stories and hand-started errands stay with the
            # assistant, because that is where the person is.
            "story_status", "story_progress"}
GO_WRITES = {"register_resource", "grant_resource",
             "answer_request", "update_goal",
             # I5e — GO unblocks a clarification on work it planned. The
             # approval kinds refuse it in `resolve_request`, which is the
             # only seam where that rule can be true (H8a).
             "relay_answer",
             # I5f — a WRITE that grants nothing. It writes a row a person
             # must act on; `errand_schedules` INSERT stays out of reach.
             "propose_errand",
             "update_brief", "retire_decision",
             "raise_alignment", "resolve_alignment",
             # PHASE C — the three writes that drive one. `steer` is the
             # instruction channel; `ask_executor` asks without ordering
             # (it routes to the live model or the answerer by run state);
             # `cancel_story` already refuses without a `human_uid`, so
             # GO can only ever carry a named person's decision -- two
             # independent gates on the most destructive verb, which is
             # right rather than redundant.
             #
             # `amend` is deliberately NOT here. It withdraws an approval
             # and writes a new contract version, which is the one thing
             # in this family that changes what a person agreed to.
             "steer", "ask_executor", "cancel_story",
             # PHASE C.4 — pause / resume / cancel THE RUN, one tool.
             # Same mechanisms the button bar uses, because they are the
             # same control surface. Carries a `human_uid` and refuses
             # without one.
             "run_control",
             # A person's decision on a parked plan, carried. Not an
             # agent approving: the kernel's gate is a human uid, and
             # this verifies the uid is a member of the goal before it
             # uses one.
             "decide_plan", "show_card",
             # CARRYING AN APPROVAL, and carrying a request to run.
             # `accept_errand` turns a person's "approved" into the
             # errand's story and grant; `dispatch_errand` asks for one
             # more run under a grant that already exists. Both refuse
             # without a `human_uid`, and neither is a decision GO may
             # take -- only the FIRST run is automatic, because
             # approving the errand was that decision.
             "accept_errand", "dispatch_errand"}
GO_ROUTES = GO_READS | GO_WRITES

# ── VIEWS: served here, but not tools ────────────────────────────────
#
# `workspace` is the file explorer's read of the executor's tree,
# addressed by STORY. It belongs on this surface because this is the
# process that deploys and has the volume -- but it is NOT an agent
# tool, and putting it in READS would have widened the assistant's tool
# set as a side effect of fixing a broken panel. The startup assertion
# that ROUTES == TOOLS exists to catch exactly that, and it did.
#
# The control panel's explorer used to call the E0 console instead. The
# console parses its id with `int()`, the page holds a story id, and so
# the first live story turned every open of the panel into
# `invalid literal for int() with base 10: 'de5512b2'` behind a 502. A
# product surface may not depend on a test harness.
VIEWS = {"workspace"}


def route_table() -> tuple:
    """Every (method, path, who, tool) the surface serves.

    PURE, and deliberately free of any web framework, because it is the
    one description both transports derive from -- the standalone server
    here and the router the control panel mounts for cloud. Writing the
    second transport's routes out by hand would be a third declaration
    of the tool surface, and the first two have already drifted from
    each other this week.

    It is importable without FastAPI so the gate can check it in the E0
    container, which has no web framework installed. A check that cannot
    run where the code runs is not a check.
    """
    rows = []
    for who, routes, reads in (("assistant", ROUTES, READS),
                               ("go", GO_ROUTES, GO_READS),
                               ("view", VIEWS, VIEWS)):
        for tool in sorted(routes):
            rows.append(("GET" if tool in reads else "POST",
                         f"/{who}/{{goal}}/{tool}", who, tool))
    return tuple(rows)


def _encode(value):
    """Serialise what the tools return, and refuse to guess at the rest.

    Projections carry real timestamps -- `last_activity` is
    `max(step_journal.ts)` and a card has an `opened_at` -- and
    `json.dumps` refuses them. ISO 8601 rather than `str`: the assistant
    reads these back and reasons about elapsed time, and a format that
    is nearly ISO is the kind of thing that parses everywhere except the
    one place it matters.

    Anything else unserialisable raises, deliberately. A `default=str`
    here would turn a bug -- a dataclass or a Decimal leaking into a
    payload -- into a plausible-looking string that reaches the model.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON: a tool returned "
                    f"something the surface cannot carry")


def _token() -> str:
    return os.environ["ASSISTANT_API_TOKEN"] \
        if "ASSISTANT_API_TOKEN" in os.environ else ""


# The local-development opt-out, named so that finding it in a process
# environment is finding the answer. Deliberately not a default: an
# unset token used to mean "open", which is the failure mode where a
# deployment is insecure because nobody typed anything.
OPEN_ENV = "AGENTOS_ASSISTANT_API_OPEN"


def _open_by_request() -> bool:
    return (os.environ[OPEN_ENV] if OPEN_ENV in os.environ else "") == "1"


def _require_auth_decision() -> bool:
    """True when the surface is deliberately open. Raises when it is not.

    REFUSED AT START, not at the first write, for the reason the
    credential sidecar gives: a service that accepts connections and
    only then discovers it cannot authenticate anybody is one somebody
    has already pointed an agent at.

    This surface carries `start_story`, `steer`, `amend`, `cancel_story`
    and every GO write, addressed by a goal id in the URL path with no
    other scoping -- so "open" means any caller that can reach the port
    can drive any goal.
    """
    if _token():
        return False
    if _open_by_request():
        return True
    raise RuntimeError(
        "ASSISTANT_API_TOKEN is not set. This surface can start, steer, "
        "amend and cancel work on any goal, so it will not start without a "
        f"way to authenticate its caller. Set the token, or set {OPEN_ENV}=1 "
        "if this is a local development stack and you mean it.")


async def canonical_goal(conn, goal: str) -> str:
    """The goal id as the database spells it.

    The gateway LOWERCASES ids in its session keys, so every tool call
    arrives with goal 'ker04' while the rows say 'KER04'. That one case
    fold made the assistant see an empty catalogue, made GO tell a human
    the repo was 'not registered', and left the ask unrouted -- three
    different symptoms, one boundary. Canonicalize ONCE here; everything
    downstream compares exactly.
    """
    if not goal:
        return goal
    return await conn.fetchval(
        "SELECT id FROM goals WHERE lower(id) = lower($1)", goal) or goal


def _flag(value) -> bool:
    """A BOOLEAN THAT CROSSED A GET, where every value is a string.

    `list_artifacts` is served over GET and the gateway maps each
    parameter through `String(v)`, so a model passing `history: false`
    sends the four characters "false" — and `bool("false")` is True. The
    caller asking to HIDE superseded versions got all of them, with
    `superseded_hidden: 0` reported alongside, which reads as "there
    were none" rather than "the flag was ignored".

    `refresh` in this same dispatcher already parsed it this way; the
    fix is to stop having two readings of one wire format.
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _artifact_id(args: dict) -> str:
    """BOTH SPELLINGS, because two specs disagreed and the refusal named
    a third state of affairs.

    The assistant's gateway spec declares `artifact`; GO's declares
    `artifact_id`; and `read_artifact` refuses with "needs an
    artifact_id". So an assistant that read its own refusal and retried
    with the name the refusal gave it got the SAME refusal, for ever —
    an unbreakable loop on the one tool whose absence had already caused
    an agent to describe a report it never opened.

    `read_resource` has accepted `resource`/`resource_id` for exactly
    this reason; this is the same fix on the same shape.
    """
    return str(args.get("artifact_id") or args.get("artifact") or "").strip()


async def _dispatch(conn, tool: str, goal: str, actor: str, args: dict):
    goal = await canonical_goal(conn, goal)
    """One tool call. Returns whatever the tool returns."""
    if tool == "story_status":
        story = args.get("story")
        # Story ids and run numbers both pass; the resolver inside
        # story_status decides. int() here is how the surface once
        # crashed on the id every other system uses.
        return await story_status(conn, goal, story=story or None)
    if tool == "story_progress":
        # WHAT CHANGED, not what state. `story_status` reads the machine;
        # this reads the work -- files, commits, checks -- and, when the
        # run is stuck, the evidence and the remedies. Kept apart from
        # story_status because they answer at different depths and a
        # person asking "how is it going" should not pay for a git walk.
        from kernel.assistant.progress import story_progress
        return await story_progress(conn, goal, args.get("story") or "")
    if tool == "goal_state":
        # THE SAME FUNCTION GO CALLS, not a second one shaped like it.
        # Nothing in it is privileged — every field is on the story board
        # any member of the goal can open — and two implementations of
        # "the board" would answer differently the first time one grew a
        # column. What separates the assistant from GO here is the WRITE
        # side: GO alone can create a story, assign one, or grant a
        # resource. Reading the board was never the boundary; it was
        # just missing.
        from kernel.go.state import goal_state
        return await goal_state(conn, goal)
    if tool == "goal_knowledge":
        # THE SAME FUNCTION GO CALLS. Decisions are approved plans, and a
        # second reader of them would be a second reading of them — the
        # first time one grew a field the two would answer differently
        # about what is still live.
        return await goal_knowledge(
            conn, goal, story=args.get("story") or "",
            about=args.get("about") or "",
            cycle=int(args.get("cycle") or 0),
            limit=int(args.get("limit") or 25))
    if tool == "inbox":
        return await inbox(conn, goal)
    if tool == "list_resources":
        return await list_resources(conn, goal, args.get("story") or "")
    if tool == "list_artifacts":
        return await list_artifacts(
            conn, goal, story=args.get("story") or "",
            role=args.get("role") or "",
            contains=args.get("contains") or "",
            history=_flag(args.get("history")),
            limit=int(args.get("limit") or 0))
    if tool == "read_artifact":
        return read_artifact(_artifact_id(args),
                             pattern=args.get("pattern"),
                             offset=int(args.get("offset") or 0),
                             length=int(args.get("length") or 0))

    key = args.get("idempotency_key") or ""
    if not key:
        return Refused("E_NO_IDEMPOTENCY_KEY",
                       "a write must carry an idempotency key — the "
                       "assistant holds no connection and cannot tell a "
                       "lost reply from a lost send, so it will retry")

    if tool == "start_story":
        return await start_story(
            conn, goal, actor, args.get("story") or "",
            args.get("task") or "", key,
            criteria_prose=args.get("criteria_prose"),
            kind=args.get("kind") or "story",
            domain=args.get("domain") or "")
    if tool == "steer":
        return await steer(conn, goal, actor, args.get("instruction") or "",
                           key, story=args.get("story"),
                           focus=args.get("focus"), hint=args.get("hint"))
    if tool == "amend":
        return await amend(conn, goal, actor, key,
                           scope=args.get("scope") or "",
                           criteria_prose=args.get("criteria_prose"),
                           story=args.get("story"), focus=args.get("focus"),
                           hint=args.get("hint"))
    if tool == "relay_answer":
        return await relay_answer(conn, goal, actor,
                                  int(args.get("question_id") or 0),
                                  args.get("text") or "", key,
                                  verdict=args.get("verdict") or "")
    if tool == "show_card":
        # Not a re-post: it clears the poster's claim and the poster
        # does it again. The kernel does not own `messages` and should
        # not learn to.
        from kernel.assistant.write import show_card
        return await show_card(conn, goal, actor,
                               int(args.get("question_id") or 0), key)
    if tool == "cancel_story":
        return await cancel_story(conn, goal, actor, args.get("reason") or "",
                                  args.get("human_uid") or "", key,
                                  story=args.get("story"),
                                  focus=args.get("focus"))
    if tool == "dispatch_errand":
        return await dispatch_errand(
            conn, goal, str(args.get("story") or ""),
            args.get("human_uid") or "", actor_id=actor)
    if tool == "record_finding":
        return await record_finding(
            conn, goal, actor, args.get("story") or "",
            args.get("fact") or "",
            args.get("why_it_matters") or "")
    if tool == "ask_executor":
        # A POST because it writes a proposal, not because it changes the
        # work: the run is untouched unless the answer needs a decision.
        return await ask_executor(conn, goal, actor,
                                  args.get("question") or "", key,
                                  asked_by=args.get("asked_by") or "",
                                  story=args.get("story"),
                                  focus=args.get("focus"))
    return await ask_go(conn, goal, actor, args.get("kind") or "",
                        args.get("body") or "", key, story=args.get("story"))


async def _dispatch_view(conn, tool: str, goal: str, args: dict):
    """A view. Reads only, and never a write -- see VIEWS."""
    if tool == "workspace":
        from kernel.workspace.browse import browse
        return await browse(conn, str(args.get("story") or ""),
                            mount=args.get("mount") or "",
                            rel=args.get("path") or "",
                            want=args.get("view") or "tree")
    raise RuntimeError(f"no view {tool}")


# The five that act on ONE run, and so are gated per call rather than by
# route membership. See `kernel/go/drivable.py`.
GO_PER_STORY = ("story_status", "story_progress",
                "steer", "ask_executor", "cancel_story",
                "run_control", "decide_plan", "show_card")


async def _dispatch_go(conn, tool: str, goal: str, actor: str, args: dict):
    """One GO tool call."""
    goal = await canonical_goal(conn, goal)

    # THE GATE, BEFORE THE TOOL AND NOT INSIDE IT. Every one of these
    # functions is shared with the assistant and takes `actor_id` as an
    # opaque string; putting the check inside would either change the
    # assistant's behaviour or need a caller flag, and a flag is a thing
    # somebody passes wrongly. One list, one predicate, one place to add
    # a sixth tool -- which is the failure mode here: a tool shipped
    # without the gate.
    if tool in GO_PER_STORY:
        from kernel.go.drivable import go_may_drive
        refused = await go_may_drive(conn, goal, str(args.get("story") or ""))
        if refused is not None:
            return refused

    if tool == "goal_members":
        # NOT gated by `go_may_drive`: it names no story, changes
        # nothing, and GO needs it precisely when it is working out who
        # may be asked to decide something.
        from kernel.go.members import goal_members
        return await goal_members(conn, goal)
    if tool == "story_status":
        return await story_status(conn, goal, story=args.get("story") or None)
    if tool == "story_progress":
        from kernel.assistant.progress import story_progress
        return await story_progress(conn, goal, args.get("story") or "")
    # ── THE KEY IS MINTED HERE, NOT ASKED OF THE MODEL ───────────────
    #
    # The assistant's branch is guarded: the gate further up refuses an
    # empty `idempotency_key` before dispatch, naming the argument. THIS
    # branch sits below that gate, so an omitted key used to travel all
    # the way to `validate_proposal` and come back as the mailbox's
    # `E_NO_IDEMPOTENCY_KEY` -- a refusal about a database invariant,
    # returned to a model that was never told it had to supply one. Errand
    # 282: a person asked the GO to steer a running executor, and the
    # steer simply never landed.
    #
    # Guarding this branch too would have made the refusal readable and
    # the steer would still not have landed. A key exists so a RETRY does
    # not land twice; it has to be STABLE across a retry and DIFFERENT for
    # a new intent, and the model is the one participant that cannot tell
    # which it is doing. `derive_key` reads both off the call itself --
    # see `kernel/go/idem.py` for why the fence is per-verb.
    #
    # A MODEL-SUPPLIED KEY STILL WINS. A caller doing its own retry
    # bookkeeping keeps it; this only fills the gap.
    async def _key(tool_name: str, payload: dict) -> str:
        given = args.get("idempotency_key") or ""
        if given:
            return given
        from kernel.go.idem import derive_key           # noqa: PLC0415
        return await derive_key(conn, tool_name, goal,
                                str(args.get("story") or ""), payload)

    if tool == "steer":
        # THE SAME FUNCTION THE ASSISTANT CALLS. A second implementation
        # of "the next instruction" would drift from the first the moment
        # one learned something about how an executor reads it.
        instruction = args.get("instruction") or ""
        return await steer(conn, goal, actor, instruction,
                           await _key("steer", {"instruction": instruction}),
                           story=args.get("story"),
                           focus=args.get("focus"), hint=args.get("hint"))
    if tool == "ask_executor":
        question = args.get("question") or ""
        return await ask_executor(conn, goal, actor, question,
                                  await _key("ask_executor",
                                             {"question": question}),
                                  asked_by=args.get("asked_by") or "",
                                  story=args.get("story"),
                                  focus=args.get("focus"))
    if tool == "cancel_story":
        # `human_uid` is required inside and refused without: GO carries
        # a person's decision here, it does not make one.
        reason = args.get("reason") or ""
        return await cancel_story(conn, goal, actor, reason,
                                  args.get("human_uid") or "",
                                  await _key("cancel_story",
                                             {"reason": reason}),
                                  story=args.get("story"),
                                  focus=args.get("focus"))
    if tool == "show_card":
        # `story` IS REQUIRED HERE AND NOT ON THE ASSISTANT'S VERSION.
        # The function itself takes only a question id, so without a
        # story `go_may_drive` has nothing to check -- and GO could put
        # a STORY's card back, which is an assistant's conversation.
        from kernel.assistant.write import show_card
        question_id = int(args.get("question_id") or 0)
        return await show_card(conn, goal, actor, question_id,
                               await _key("show_card",
                                          {"question_id": question_id}))
    if tool == "decide_plan":
        from kernel.go.decide_plan import decide_plan
        return await decide_plan(
            conn, goal, str(args.get("story") or ""),
            int(args.get("question_id") or 0),
            str(args.get("verdict") or ""),
            args.get("human_uid") or "",
            said=args.get("said") or "")
    if tool == "run_control":
        from kernel.go.run_control import run_control
        return await run_control(
            conn, goal, str(args.get("story") or ""),
            str(args.get("action") or ""),
            args.get("human_uid") or "",
            message=args.get("message") or "",
            # WHO IS CALLING, so the proposal records GO as the actor and
            # the human only as whose decision is being carried. Omitted,
            # `_submit` stamped `human:<uid>` and GO disappeared from its
            # own cancellation (run 345).
            actor_id=actor)
    if tool == "dispatch_errand":
        return await dispatch_errand(
            conn, goal, str(args.get("story") or ""),
            args.get("human_uid") or "", actor_id=actor)
    if tool == "accept_errand":
        return await accept_errand(
            conn, goal, actor, str(args.get("story") or ""),
            args.get("human_uid") or "",
            proposal_id=args.get("proposal_id"),
            all_pending=bool(args.get("all_pending")))
    if tool == "goal_state":
        return await goal_state(conn, goal)
    if tool == "code_state":
        return await code_state(conn, goal)
    if tool == "list_artifacts":
        # I3c — the SAME function the assistant calls. Two
        # implementations of "what did this goal produce" answer
        # differently the first time one grows a field, and the read is
        # goal-scoped either way, so there is nothing here that needs to
        # differ by caller.
        return await list_artifacts(
            conn, goal, story=args.get("story") or "",
            role=args.get("role") or "",
            contains=args.get("contains") or "",
            history=_flag(args.get("history")),
            limit=int(args.get("limit") or 0))
    if tool == "goal_knowledge":
        return await goal_knowledge(
            conn, goal, story=args.get("story") or "",
            about=args.get("about") or "",
            cycle=int(args.get("cycle") or 0),
            limit=int(args.get("limit") or 25))
    if tool == "read_artifact":
        # THE SAME FUNCTION the assistant calls, for the same reason
        # `list_artifacts` is shared: the content store is goal-blind and
        # addressed by hash, so there is nothing about reading an
        # artifact that differs by caller.
        return read_artifact(_artifact_id(args),
                             pattern=args.get("pattern"),
                             offset=int(args.get("offset") or 0),
                             length=int(args.get("length") or 0))
    if tool == "inbox":
        # I5e — THE SAME FUNCTION, for the same reason `list_artifacts` is
        # shared: it is goal-scoped, and two implementations of "what is
        # waiting" would answer differently the first time one grows a
        # field. `needs_human` is already on every card, so GO reads which
        # of these are not its to settle before trying.
        return await inbox(conn, goal)
    if tool == "relay_answer":
        # ACTOR IS `go`, NOT A PERSON, and nothing here pretends
        # otherwise. `resolve_request` takes no human uid from this path,
        # so an approval, an acceptance and a destructive-command
        # permission all refuse — which is the design, not a limitation
        # to work around.
        #
        # THE KEY IS STILL REQUIRED, AND STILL MINTED RATHER THAN ASKED.
        # GO holds no connection to the run: it submits an intent and a
        # worker applies it, so it cannot tell a lost reply from a lost
        # send and will retry. The other GO writes are idempotent by
        # their own shape (an upsert, a status transition); an answer is
        # not — a retry without a key un-parks a run twice.
        #
        # None of which argues for asking the MODEL for the key. It is
        # the one participant that cannot tell a retry from a new intent,
        # and a random string from it satisfies neither half of what a
        # key is for. The fenced hash does: same answer to the same
        # question is a retry, a different answer is not.
        key = await _key("relay_answer",
                         {"question_id": args.get("question_id"),
                          "text": args.get("text") or ""})
        # I5h — THE PER-RUN CAP, on GO's branch and not inside
        # `relay_answer`. The assistant relays a PERSON's answer through
        # that same function; capping there would ration how often
        # somebody may answer their own story's questions, which is a
        # change to how story executions behave and this stage may not
        # make one.
        from kernel.go.answers import may_answer

        capped = await may_answer(conn, int(args.get("question_id") or 0),
                                  actor)
        if capped is not None:
            return capped
        return await relay_answer(conn, goal, actor,
                                  int(args.get("question_id") or 0),
                                  args.get("text") or "", key,
                                  verdict=args.get("verdict") or "")
    if tool == "update_brief":
        return await update_brief(conn, goal, actor, args.get("text") or "")
    if tool == "retire_decision":
        return await retire_decision(conn, goal, actor,
                                     args.get("decision") or "",
                                     args.get("reason") or "")
    if tool == "raise_alignment":
        evidence = args.get("evidence")
        if isinstance(evidence, str):
            import json as _json
            try:
                evidence = _json.loads(evidence)
            except ValueError:
                return Refused("E_BAD_ARGS", "`evidence` must be JSON")
        return await raise_alignment(
            conn, goal, actor,
            subject_kind=args.get("subject_kind") or "story",
            subject_id=args.get("subject") or args.get("subject_id") or "",
            question=args.get("question") or "",
            evidence=evidence)
    if tool == "resolve_alignment":
        return await resolve_alignment(
            conn, goal, actor, flag_id=int(args.get("flag") or 0),
            outcome=args.get("outcome") or "", note=args.get("note") or "")
    if tool == "plan_preview":
        # `add` arrives as a JSON list of hypothetical stories. Over GET
        # it would be a query-string contortion; plan_preview is a READ
        # that takes structure, so the GET handler parses a JSON `add`
        # param when present.
        add = args.get("add")
        if isinstance(add, str):
            import json as _json
            try:
                add = _json.loads(add)
            except ValueError:
                return Refused("E_BAD_ARGS", "`add` must be JSON")
        return await plan_preview(conn, goal, add=add)
    if tool == "read_resource":
        # `resource_id` is what the gateway spec declares and what
        # `resource_brief` takes three lines below. This read `resource`,
        # so every call from GO arrived with an EMPTY id -- which the
        # prefix match then resolved to the goal's oldest resource
        # instead of refusing. `resource` stays as an alias: the console
        # and older transcripts use it.
        return await read_resource(
            conn, goal,
            str(args.get("resource_id") or args.get("resource") or ""),
            path=args.get("path") or "",
            start_line=int(args.get("start_line") or 1),
            max_lines=int(args.get("max_lines") or 200),
            ref=args.get("ref") or "")
    if tool == "resource_brief":
        return await resource_brief(
            conn, goal, args.get("resource_id") or "",
            refresh=str(args.get("refresh", "")).lower() in ("true", "1"))
    if tool == "list_requests":
        return await list_requests(conn, goal, args.get("status", "PENDING"))
    if tool == "propose_errand":
        return await propose_errand(
            conn, goal,
            task=args.get("task") or "",
            rationale=args.get("rationale") or "",
            name=args.get("name") or "",
            domain=args.get("domain") or "ops",
            cron=args.get("cron") or "",
            max_runs=args.get("max_runs"),
            expires_at=args.get("expires_at") or "",
            # OMITTED IS NOT EMPTY, and the default must preserve that.
            # `args.get("resources") or []` would turn "GO forgot" into
            # "GO said none" and re-create the bug this parameter exists
            # to fix -- a story granted nothing, discovered by a run
            # hunting for a repository at its first leg.
            resources=args.get("resources"),
            # WHICH MACHINE, chosen on the PROPOSAL because an errand's
            # story is created at approval and dispatched immediately --
            # there is no later window. Blank means nobody chose, which
            # falls through to the goal and then the profile.
            sandbox_size=args.get("sandbox_size") or "",
            # THE ASKER IS RECORDED, not assumed. "Never answer a question
            # on an errand you proposed" needs something to compare
            # against, and a hardcoded 'go' would make that rule
            # unenforceable the day a second orchestrator exists.
            proposed_by=actor)
    if tool == "update_goal":
        return await update_goal(
            conn, goal, actor,
            description=args.get("description"),
            objective=args.get("objective"))
    if tool == "register_resource":
        return await register_resource(
            conn, goal, args.get("resource_id") or "",
            args.get("source") or "", kind=args.get("kind") or "repo",
            name=args.get("name") or "",
            writable=bool(args.get("writable")), registered_by=actor)
    if tool == "grant_resource":
        # `relation` is the vocabulary. A legacy `writable` bool still
        # maps (true -> modifies) so an old caller degrades to a coarser
        # grant rather than a crash -- but nothing current sends it.
        relation = args.get("relation") or (
            "modifies" if args.get("writable") else "needs")
        return await grant_resource(
            conn, goal, args.get("story") or "",
            args.get("resource_id") or "",
            # `notes` is the name GO's own tool spec has advertised all
            # along ("tactical guidance for how to use this resource in
            # this specific story"); the kernel simply never read it, so
            # every why GO wrote was dropped on the floor. Accept that
            # name first and keep `why` as an alias.
            relation=relation,
            why=args.get("notes") or args.get("why") or "",
            at=args.get("at") or "", granted_by=actor)
    return await answer_request(
        conn, goal, int(args.get("request_id") or 0),
        args.get("text") or "", status=args.get("status") or "ANSWERED")


class Handler(BaseHTTPRequestHandler):
    def __init__(self, runtime, *a, **kw):
        self.runtime = runtime
        super().__init__(*a, **kw)

    # ── plumbing ──────────────────────────────────────────────────────
    def _send(self, code: int, body: dict):
        raw = json.dumps(body, default=_encode).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        want = _token()
        return not want or self.headers.get("X-Assistant-Token") == want

    def _route(self):
        """(who, goal, tool) from /<who>/<goal>/<tool>, or (None, None, None)."""
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) != 3:
            return None, None, None
        who, goal, tool = parts
        if who == "assistant" and tool in ROUTES:
            return who, goal, tool
        if who == "go" and tool in GO_ROUTES:
            return who, goal, tool
        if who == "view" and tool in VIEWS:
            return who, goal, tool
        return None, None, None

    def _serve(self, allowed: set, args: dict):
        if not self._authorised():
            return self._send(401, {"refused": "E_UNAUTHORISED"})
        who, goal, tool = self._route()
        if tool is None:
            return self._send(404, {"refused": "E_NO_SUCH_TOOL",
                                    "detail": f"assistant: {sorted(ROUTES)}; "
                                              f"go: {sorted(GO_ROUTES)}"})
        reads = (READS if who == "assistant"
                 else VIEWS if who == "view" else GO_READS)
        if (tool in reads) != (allowed is READS):
            return self._send(405, {"refused": "E_WRONG_METHOD",
                                    "detail": f"{tool} is a "
                                              f"{'read' if tool in reads else 'write'}"})
        actor = self.headers.get("X-Assistant-Actor") or who
        try:
            out = self.runtime.call(self._call(who, tool, goal, actor, args))
        except Exception as e:                              # noqa: BLE001
            logger.exception("assistant api: %s", tool)
            return self._send(500, {"refused": "E_TOOL_FAILED",
                                    "detail": f"{type(e).__name__}: {e}"})
        if isinstance(out, Refused):
            # 200, not 4xx. A typed refusal is a RESULT the assistant is
            # meant to read and act on — "which story did you mean" is an
            # answer, not a transport failure — and an HTTP error code
            # invites the gateway's retry logic to bury it.
            return self._send(200, {"refused": out.code,
                                    "detail": out.message,
                                    "candidates": list(out.candidates)})
        self._send(200, out)

    async def _call(self, who, tool, goal, actor, args):
        async with self.runtime.pool.acquire() as conn:
            if who == "go":
                return await _dispatch_go(conn, tool, goal, actor, args)
            if who == "view":
                return await _dispatch_view(conn, tool, goal, args)
            return await _dispatch(conn, tool, goal, actor, args)

    # ── verbs ─────────────────────────────────────────────────────────
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        self._serve(READS, {k: v[0] for k, v in q.items()})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            args = json.loads(raw)
        except UnicodeDecodeError as e:
            # A Windows shell sends an em-dash as one cp1252 byte and a
            # crashed handler thread was the reply. Bad bytes are a 400,
            # never a dead connection.
            return self._send(400, {"refused": "E_BAD_ENCODING",
                                    "detail": f"body is not UTF-8: {e}"})
        except json.JSONDecodeError as e:
            return self._send(400, {"refused": "E_BAD_JSON", "detail": str(e)})
        self._serve(WRITES, args if isinstance(args, dict) else {})

    def log_message(self, fmt, *args):
        logger.info("assistant api: " + fmt, *args)


def serve(dsn: str, port: int = PORT):
    if set(GO_ROUTES) != set(GO_TOOLS):
        raise RuntimeError(
            f"GO's surface and its tool set disagree: "
            f"{sorted(set(GO_ROUTES) ^ set(GO_TOOLS))}")
    if set(ROUTES) != set(TOOLS):
        # Refused at start, not reported at review. The surface and the
        # tool set are the same statement written twice, and the moment
        # they disagree one of them is a lie.
        raise RuntimeError(
            f"the HTTP surface and the tool set disagree: "
            f"{sorted(set(ROUTES) ^ set(TOOLS))}")
    is_open = _require_auth_decision()

    runtime = LoopRuntime(dsn)
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), lambda *a: Handler(runtime, *a))
    logger.info("assistant api: %d assistant + %d GO tools on 0.0.0.0:%d%s",
                len(ROUTES), len(GO_ROUTES), port,
                "" if not is_open else "  (OPEN — no token, by request)")
    if is_open:
        logger.warning(
            "%s=1: every write on this surface is unauthenticated. Local "
            "development only.", OPEN_ENV)
    server.serve_forever()


if __name__ == "__main__":
    configure(component="assistant-api")
    serve(os.environ["DATABASE_URL"])
