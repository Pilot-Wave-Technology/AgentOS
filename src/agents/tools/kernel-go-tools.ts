/**
 * The Goal Orchestrator's kernel tools.
 *
 *   list_requests    what the assistants have asked for
 *   grant_resource   give a story access to one of the goal's resources
 *   answer_request   close an ask, so the assistant stops waiting
 *
 * SEPARATE FROM THE ASSISTANT'S, deliberately. The assistant asks and GO
 * decides, and that split is only real if the two have different
 * surfaces — an assistant that could reach `grant_resource` by naming it
 * would make the arrangement a convention rather than a boundary. The
 * kernel serves them on `/go/<goal>/<tool>` and asserts that set against
 * its own package at startup.
 *
 * WHAT GO STILL DOES NOT DO. It does not dispatch, does not approve a
 * plan, does not verify. Granting a resource is authority over what a
 * story may touch; it is not an act of execution.
 *
 * A GRANT REACHES A RUNNING EXECUTOR. The repository appears in the live
 * workspace immediately and the executor is told — readable at once,
 * writable from its next leg, because the write scope is fixed for the
 * leg in flight. The tool returns what happened per run, so GO can say
 * which is which rather than reporting a flat "granted".
 */
import { Type } from "@sinclair/typebox";

import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult } from "./common.js";
import {
  kernelSurfaceFor,
  resolveKernelToken,
  resolveKernelUrl,
  withIdempotencyKey,
} from "./kernel-url.js";

function kernelUrl(): string {
  return resolveKernelUrl();
}

function kernelToken(): string {
  return resolveKernelToken();
}

/**
 * The CALLER's agent id, which this file did not read and now must.
 *
 * `agent:{agentId}:{goalId}-{threadId}` — the same key
 * `resolveGoalIdFromSession` below already parses, one field earlier.
 * It is needed because the five tools this file shares with
 * `kernel-assistant-tools.ts` must route to the caller's own surface:
 * the gateway keys tools by NAME, so whichever of the two definitions
 * is registered last serves every agent, and a hard-coded `/go/` here
 * sent assistants to the GO surface to be refused.
 */
function resolveAgentIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  const parts = sessionKey.split(":");
  return parts.length >= 3 ? parts[1] : "";
}

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  const parts = sessionKey.split(":");
  if (parts.length < 3) return "";
  const goalThread = parts.slice(2).join(":");
  const dash = goalThread.indexOf("-");
  return dash > 0 ? goalThread.substring(0, dash) : goalThread;
}

type Spec = {
  name: string;
  label: string;
  method: "GET" | "POST";
  description: string;
  parameters: ReturnType<typeof Type.Object>;
};

const SPECS: Spec[] = [
  {
    name: "goal_state",
    label: "Goal State",
    method: "GET",
    description:
      "The board in one call: every story with state, assignee, and — for " +
      "blocked stories — WHO they wait on; per-person open-story load; " +
      "recent decisions; asks still pending. Each story carries `landed` " +
      "separately from `state`: done means the work sits on a branch, " +
      "landed means the team has it on trunk. Never report done as " +
      "delivered — `done_not_landed` counts exactly that gap.",
    parameters: Type.Object({}),
  },
  {
    name: "goal_members",
    label: "Goal Members",
    method: "GET",
    description:
      "Who is on this goal: every member with their role, when they " +
      "joined, and their assistant. `admins` is lifted out separately, " +
      "because that is the list `decide_plan` and `cancel_story` need — " +
      "both refuse a uid that is not a goal admin. " +
      "Call it before naming anybody: the roster is no longer sent with " +
      "every message, and a colleague you did not read here may not be " +
      "on this goal at all. " +
      "No open tasks — those are stories, and `goal_state` has them " +
      "with their assignees.",
    parameters: Type.Object({}),
  },
  {
    name: "goal_knowledge",
    label: "Goal Knowledge",
    method: "GET",
    description:
      "What was DECIDED and LEARNED, as opposed to what is happening — " +
      "`goal_state` is the board, this is why the board looks like that. " +
      "Returns each story's live decisions by cycle (with who approved " +
      "them and the plan hash), what the work taught, and the goal-level " +
      "decisions. " +
      "DECISIONS ARE APPROVED PLANS. A decision here was signed by a " +
      "person, so it is evidence, not opinion — cite one by its address " +
      "(`cycle2:d1`) and anyone can open the plan and check. A decision " +
      "no later plan supersedes is STILL LIVE, however many cycles ago " +
      "it was taken; `superseded` is listed separately and is how you " +
      "answer 'why did we stop doing X'. " +
      "`about` searches by what a story TOUCHED — pass a resource id, a " +
      "repo name or a path and get findings from the stories that worked " +
      "on it, which is how you ask 'has anyone solved this already'. " +
      "Every fact is tagged: `observed` came from a row or an approval, " +
      "`unproven` is something a model said. Report them differently.",
    parameters: Type.Object({
      story: Type.Optional(Type.String({ description: "One story's record." })),
      about: Type.Optional(
        Type.String({
          description:
            "A resource id, repo name, path or phrase. Answers 'what do " +
            "we know about this'.",
        }),
      ),
      cycle: Type.Optional(
        Type.Number({ description: "Only this plan cycle." }),
      ),
      limit: Type.Optional(Type.Number({ description: "Default 25, max 100." })),
    }),
  },
  {
    name: "update_brief",
    label: "Update Brief",
    method: "POST",
    description:
      "Rewrite the one paragraph somebody reads to know where this goal " +
      "stands. REWRITE, never append — a brief that grows is a log, and " +
      "a log is the thing nobody reads. " +
      "Write it after anything that changes the picture: a completion " +
      "report, a decision, a resolved alignment question. What is done, " +
      "what is in flight, what is waiting, and what it means for the " +
      "objective. The detail lives in decisions and knowledge, which are " +
      "queryable; this is the reading of them.",
    parameters: Type.Object({
      text: Type.String({ description: "The whole brief. Replaces the last." }),
    }),
  },
  {
    name: "retire_decision",
    label: "Retire Decision",
    method: "POST",
    description:
      "Take a superseded GOAL decision out of the picture. It leaves " +
      "every default read and `goal_state`; the row survives so 'why did " +
      "we stop doing X' stays answerable. Story decisions are not " +
      "retired this way — they live in plans, and a later plan " +
      "supersedes exactly the ones it changes.",
    parameters: Type.Object({
      decision: Type.String({ description: "The decision id." }),
      reason: Type.Optional(Type.String({ description: "What replaced it." })),
    }),
  },
  {
    name: "raise_alignment",
    label: "Raise Alignment",
    method: "POST",
    description:
      "Ask ONE question about work that looks like it has drifted from " +
      "the objective. " +
      "EVIDENCE IS REQUIRED and the database enforces it: name the " +
      "decision address, story or plan you are looking at. A flag " +
      "without evidence is an opinion, and the first one of those teaches " +
      "the team to skip the next ten. " +
      "It is a QUESTION, not a correction — the person usually has a " +
      "reason you do not have. Ask what they intended, do not tell them " +
      "they are wrong. " +
      "The same observation cannot be raised twice while it is open, so " +
      "raise it once and wait for the answer.",
    parameters: Type.Object({
      subject: Type.String({ description: "Story id, or a person's uid." }),
      subject_kind: Type.Optional(
        Type.String({ description: "'story' (default) or 'person'." }),
      ),
      question: Type.String({ description: "What you are asking them." }),
      evidence: Type.Array(
        Type.Object({
          kind: Type.Optional(Type.String()),
          id: Type.String({ description: "e.g. `cycle2:d1`, a story id." }),
          says: Type.Optional(Type.String({ description: "What it says." })),
        }),
        { minItems: 1, description: "At least one. This is the constraint." },
      ),
    }),
  },
  {
    name: "resolve_alignment",
    label: "Resolve Alignment",
    method: "POST",
    description:
      "Record what a person said about an alignment question, so planning " +
      "reads the answer instead of asking again. Do this as soon as they " +
      "answer — an open flag is one you are still allowed to raise.",
    parameters: Type.Object({
      flag: Type.Number({ description: "The flag id." }),
      outcome: Type.String({
        description:
          "How it resolved, in their terms — 'deliberate, the vendor " +
          "changed', 'not deliberate, correcting it'.",
      }),
      note: Type.Optional(Type.String({ description: "Anything worth keeping." })),
    }),
  },
  {
    name: "code_state",
    label: "Code State",
    method: "GET",
    description:
      "What the code is doing, without reading any of it. Per TRUNK LANE " +
      "-- a repo can carry several, each its own merge target: the lane's " +
      "branch, its observed HEAD and when it last moved. Per live " +
      "branch: which lane it `lands_on`, the stories riding it in " +
      "handoff order, who is CURRENT, what it last synced to and whether " +
      "it is stale against THAT LANE (a branch is never stale because a " +
      "lane it does not land on moved), mergeability, conflicting " +
      "paths, PR state. Use `conflict_paths` " +
      "as a sequencing signal: two branches touching the same files is a " +
      "decision waiting for you. No file contents — for one targeted " +
      "question about a file, use read_resource.",
    parameters: Type.Object({}),
  },
  {
    name: "plan_preview",
    label: "Plan Preview",
    method: "GET",
    description:
      "What the Git Engine WOULD derive from the current story graph — " +
      "branches, workstreams, merge order. Nothing is created. Pass `add` " +
      "(JSON list of {id, title, assigned_to, resource_id, depends_on}) " +
      "to layer hypothetical stories on top and see the consequence of a " +
      "plan BEFORE creating it. If the preview shows three merges where " +
      "you expected one, fix the dependencies, not the engine.",
    parameters: Type.Object({
      add: Type.Optional(
        Type.String({
          description:
            "JSON array of hypothetical stories to layer onto the preview.",
        }),
      ),
    }),
  },
  {
    name: "resource_brief",
    label: "Resource Brief",
    method: "GET",
    description:
      "What a resource IS: an identity card (purpose, stack, layout, " +
      "interfaces) plus an activity digest of what landed on it — " +
      "maintained by the system on merge events, so reading it costs " +
      "bytes. Honestly stamped: `as_of` is the card's sha, " +
      "`landings_since` counts what happened after it was written, and " +
      "`identity_stale` means a landing touched the card's own subject. " +
      "Pass refresh: true to queue a rebuild (never blocks). " +
      "code_state = what changed; this = what it is; read_resource = one " +
      "targeted question.",
    parameters: Type.Object({
      resource_id: Type.String({ description: "Which resource, by id." }),
      refresh: Type.Optional(
        Type.Boolean({ description: "Queue an identity rebuild." }),
      ),
    }),
  },
  {
    name: "update_goal",
    label: "Update the Goal",
    method: "POST",
    description:
      "Rewrite the goal's description and/or objective from what the " +
      "conversation established. The record a goal starts with is a " +
      "human's first guess; you are the one whose talks refine it, so " +
      "you keep it true. The change is auto-recorded as a decision — " +
      "every assistant sees the goal moved. Use it when understanding " +
      "changes, never to quietly widen scope: say in the thread what you " +
      "changed and why.",
    parameters: Type.Object({
      description: Type.Optional(
        Type.String({ description: "The new description (full text)." }),
      ),
      objective: Type.Optional(
        Type.String({ description: "The new objective (full text)." }),
      ),
    }),
  },
  {
    name: "list_requests",
    label: "Assistant Requests",
    method: "GET",
    description:
      "What the assistants in this goal have asked you for — a story, a " +
      "resource, a decision. Oldest first, because a request nobody " +
      "answered is the one that has been costing somebody time.",
    parameters: Type.Object({
      status: Type.Optional(
        Type.String({ description: "PENDING (default), ANSWERED, or empty for all." }),
      ),
    }),
  },
  {
    name: "register_resource",
    label: "Register a Resource",
    method: "POST",
    description:
      "Put a DOCUMENT in this goal's catalogue, so stories can be granted " +
      "it. A repository is a person's act — registering one installs " +
      "the GitHub App and declares which branch or branches of it this " +
      "goal treats as trunk lanes, which this tool does not do; ask in " +
      "the goal thread and grant it afterwards. REGISTERING IS NOT GRANTING: this says the goal " +
      "owns the thing and whether a story may EVER write it; " +
      "`grant_resource` then attaches it to one story. " +
      "`writable` here is the ceiling, not an grant — a grant cannot " +
      "exceed it, which is what keeps read-only-by-policy meaningful. " +
      "Set it true only where changing that repository is genuinely part " +
      "of the goal's work.",
    parameters: Type.Object({
      resource_id: Type.String({
        description: "Short id the assistant will name, e.g. 'api'.",
      }),
      source: Type.String({
        description: "Clone URL or path. Without it there is nothing to mount.",
      }),
      kind: Type.Optional(
        Type.String({ description: "doc (default). `repo` is refused: a "
          + "repository needs a person to install the App and name its "
          + "trunk branch(es) -- one repo may carry several lanes." }),
      ),
      name: Type.Optional(
        Type.String({ description: "Human-readable name. Defaults to the id." }),
      ),
      writable: Type.Optional(
        Type.Boolean({
          description: "May ANY story in this goal write it? Default false.",
        }),
      ),
    }),
  },
  {
    name: "read_resource",
    label: "Read a Resource",
    method: "GET",
    // THE PLANNER'S BLINDFOLD COMES OFF, one page at a time. The senses
    // (goal_state, code_state, resource_brief) stay the cheap default;
    // this is for the question a brief cannot answer -- the rule in an
    // uploaded spec, the one interface the next story hinges on.
    //
    // For a DOCUMENT this fetches the live stored file, which matters
    // when the document is the contract: resource_brief serves a cached
    // scan and can be stale after an upload, so a question about what a
    // document says NOW is this tool's, not the brief's.
    description:
      "Read one page of one file from a resource this goal owns. For a " +
      "repo or branch, `path` is the file and `ref` an optional branch " +
      "or sha. For a document, file or dataset, it reads the stored " +
      "content — PDFs, .docx and .xlsx are extracted to text, so a " +
      "spreadsheet reads as a table rather than as bytes. Paged: " +
      "`start_line` and `max_lines` (200 default, 400 max), so a " +
      "targeted question costs a page and inhaling a tree is visibly " +
      "the wrong tool. Prefer `resource_brief` for \"what is this\"; " +
      "use this for \"what does it SAY\", and always for a document " +
      "whose content may have changed — the brief is a cached scan and " +
      "this is the live read.",
    parameters: Type.Object({
      resource_id: Type.String({
        description:
          "Which resource — its NAME (VALIDATION_RULES.md, " +
          "myrepo:mybranch) or its id. The name is what resource_brief " +
          "and the goal thread call it, so it is usually what you have.",
      }),
      path: Type.Optional(
        Type.String({
          description:
            "File path within a repo or branch. Omit for a document, " +
            "which has only one file.",
        }),
      ),
      start_line: Type.Optional(
        Type.String({ description: "First line to return (1-based)." }),
      ),
      max_lines: Type.Optional(
        Type.String({ description: "Lines to return; 200 default, 400 max." }),
      ),
      ref: Type.Optional(
        Type.String({
          description: "Branch or sha, for a repo. Defaults to the " +
                       "resource's own branch.",
        }),
      ),
    }),
  },
  {
    name: "grant_resource",
    label: "Grant a Resource",
    method: "POST",
    description:
      "Give a story access to one of the goal's resources, with a " +
      "RELATION — the one vocabulary for access. `needs` = read-only " +
      "context. `produces` = the story's own output. `modifies` = it " +
      "changes something that exists. Recorded against the STORY, so a " +
      "re-run inherits it. Works mid-story: the repository appears in a " +
      "live run's workspace at once — readable immediately, writable " +
      "from its next leg. Write relations are refused on resources that " +
      "are not git-backed: an edit to an untracked file dies with the " +
      "container, and a refusal is honest where silent loss is not. " +
      "A CLOUD IDENTITY IS GRANTED HERE TOO, and always with `needs`: " +
      "a story ACTS AS an identity and never edits one. It needs BOTH " +
      "its environment (which project) and the identity itself (who it " +
      "acts as) -- an environment alone grants no power. One identity " +
      "per story: two means every command has two and the record cannot " +
      "say which it used. " +
      "A WRITE RELATION REQUIRES `why`: it becomes the executor's " +
      "scoping instruction, and a story writing two branches cannot " +
      "tell them apart without one.",
    parameters: Type.Object({
      story: Type.String({ description: "The story being granted access." }),
      resource_id: Type.String({ description: "Which resource, by id." }),
      relation: Type.Optional(
        Type.String({
          description: "needs (default) | produces | modifies.",
        }),
      ),
      // REQUIRED FOR WRITES, and the kernel refuses E_NO_REASON without
      // it. It was missing from this schema entirely, so GO could not
      // pass it however the prompt was worded -- and a two-lane story
      // shipped with both write grants unexplained. The executor's
      // brief lists one line per write target; this is that line.
      why: Type.Optional(
        Type.String({
          description:
            "Why this story touches this resource — one line naming " +
            "what belongs here (e.g. 'the FastAPI service and its " +
            "tests'). REQUIRED for produces/modifies; it reaches the " +
            "executor as the scoping instruction for this place, and " +
            "on a story writing more than one branch it is the only " +
            "thing telling it which half goes where. Optional for " +
            "needs.",
        }),
      ),
      at: Type.Optional(
        Type.String({ description: "Override the mount path. Rarely needed." }),
      ),
    }),
  },
  {
    name: "answer_request",
    label: "Answer a Request",
    method: "POST",
    description:
      "Close an assistant's request with your decision. Do this even " +
      "when the answer is no: a request you acted on but never closed " +
      "looks identical to one you never saw, and the assistant will " +
      "either re-ask or report a settled decision as still pending.",
    parameters: Type.Object({
      request_id: Type.String({ description: "Which request." }),
      text: Type.String({ description: "The decision, and why." }),
      status: Type.Optional(
        Type.String({ description: "ANSWERED (default) or REJECTED." }),
      ),
    }),
  },
  {
    name: "read_artifact",
    label: "Read Artifact",
    method: "GET",
    description:
      "THE CONTENTS of a stored artifact, by id — the ids come from " +
      "`list_artifacts`. Use it before you describe what an artifact " +
      "says. You held the index and not the contents, which made " +
      "answering from what you already know about the subject the path " +
      "of least resistance: asked what an errand had found, the right " +
      "answer from memory and the right answer from the report are " +
      "indistinguishable to whoever reads you, until the report says " +
      "something surprising. " +
      "Never the whole object: pass `pattern` to get matching lines, or " +
      "`offset`/`length` for a byte range.",
    parameters: Type.Object({
      artifact_id: Type.String({
        description: "From `list_artifacts`, or an emitted output's footer.",
      }),
      pattern: Type.Optional(
        Type.String({ description: "Return only lines matching this." }),
      ),
      offset: Type.Optional(Type.Number({ description: "Byte offset." })),
      length: Type.Optional(Type.Number({ description: "Bytes to return." })),
    }),
  },
  {
    name: "list_artifacts",
    label: "List Artifacts",
    method: "GET",
    description:
      "WHAT AN ERRAND PRODUCED — names, roles (report / dataset / method / " +
      "log / figure), sizes and the executor's own one-line summary of " +
      "each. An errand's commits never reach trunk and its branch is deleted " +
      "when it is accepted, so its artifacts ARE its result: knowing the " +
      "run finished tells you " +
      "nothing about what it FOUND. `read_artifact` needs an id and this " +
      "is where ids come from. Filter by `story`, `role` or `contains`. " +
      "Summaries only, never bytes.",
    parameters: Type.Object({
      story: Type.Optional(
        Type.String({ description: "Only this story's artifacts." }),
      ),
      role: Type.Optional(
        Type.String({ description: "report, dataset, method, log, figure." }),
      ),
      contains: Type.Optional(
        Type.String({ description: "Match the name or the summary." }),
      ),
      history: Type.Optional(
        Type.Boolean({
          description:
            "Include superseded versions. Default false: the current " +
            "version of each is almost always what you want.",
        }),
      ),
      limit: Type.Optional(Type.Number({ description: "Cap the rows." })),
    }),
  },
  {
    name: "inbox",
    label: "What Is Waiting",
    method: "GET",
    description:
      "EVERY OPEN QUESTION IN THIS GOAL — the run it parked, what it " +
      "asked, and `needs_human`. This is how you watch an errand instead " +
      "of guessing why it has been running for an hour: a run that looks " +
      "slow is usually a run that is parked. Most cards here are somebody " +
      "else's to answer; `needs_human: true` means you cannot settle it " +
      "whatever you do.",
    parameters: Type.Object({}),
  },
  {
    name: "relay_answer",
    label: "Answer a Parked Run",
    method: "POST",
    description:
      "Unblock a CLARIFICATION on work you planned — \"which of those two " +
      "column names did you mean\". You planned it, so routing that to a " +
      "person costs a day for an answer you already hold. " +
      "YOU CANNOT APPROVE ANYTHING with this and should not try: a plan, " +
      "a completion and a destructive command all refuse you, because " +
      "approving is a person's act and you have no human uid to give. " +
      "When you hit one, say in the goal thread what is waiting and why " +
      "it matters — a card is already with them. " +
      "Three answers per run, then it is a person's: an errand needing a " +
      "fourth clarification from the agent that planned it is one whose " +
      "task text was wrong.",
    parameters: Type.Object({
      question_id: Type.Number({
        description: "From `inbox` or the [ERRAND PARKED] wake.",
      }),
      text: Type.String({ description: "The answer, in full sentences." }),
      idempotency_key: Type.String({
        description:
          "Unique per answer. You hold no connection to the run, so a " +
          "retry without one un-parks it twice.",
      }),
      verdict: Type.Optional(
        Type.String({ description: "revise or declined. Never approve." }),
      ),
    }),
  },
  {
    name: "accept_errand",
    label: "Carry an Errand Approval",
    method: "POST",
    description:
      "AUTONOMOUS ERRANDS ONLY. Carry a person's approval onto the errand " +
      "proposal waiting in this goal, which creates its story WITH its " +
      "grant and starts it -- there is no second step, because an errand " +
      "has nobody waiting to press Start. Call it when somebody says " +
      "`approved` or `yes, go ahead` in the thread. YOU DO NOT NEED A " +
      "STORY ID AND THERE WILL NOT BE ONE: an errand has no story until " +
      "it is approved, so a bare approval resolves to the proposal " +
      "waiting here. If more than one is waiting it refuses and lists " +
      "them -- ask which, then pass `proposal_id`; if they meant all of " +
      "them, pass `all_pending`, because that was one decision and not " +
      "three. IT REFUSES AN ORDINARY STORY: those are accepted and " +
      "started by the person they belong to, from the board. You cannot " +
      "approve on your own reading of what somebody would want -- the " +
      "decision is never yours, only the carrying of it.",
    parameters: Type.Object({
      human_uid: Type.String({
        description: "Who approved it. Required.",
      }),
      proposal_id: Type.Optional(Type.String({
        description:
          "Which proposal, when more than one is waiting and they have " +
          "said which.",
      })),
      all_pending: Type.Optional(Type.Boolean({
        description:
          "They approved every proposal waiting. One decision about a " +
          "set, not one approval each.",
      })),
      story: Type.Optional(Type.String({
        description:
          "Rarely needed: an errand whose story already exists.",
      })),
    }),
  },
  {
    name: "dispatch_errand",
    label: "Run an Errand Again",
    method: "POST",
    description:
      "Start another run of an errand somebody has ALREADY approved. " +
      "NOTHING SCHEDULES AN ERRAND: the first run goes out when a person " +
      "approves it, and every run after that is one somebody asks for. " +
      "Use this when a run ended without producing the work -- cancelled, " +
      "failed, or killed while its pod was still being scheduled -- and " +
      "the errand is still wanted. It needs no new approval: the grant is " +
      "what authorises running and it survives a run that went nowhere. " +
      "`human_uid` IS REQUIRED AND IS NOT A FIELD YOU FILL IN. A run " +
      "spends the approver's budget and carries their name, so somebody " +
      "has to have asked for this one. Only the FIRST run is automatic, " +
      "because approving the errand was that decision. If nobody has " +
      "asked, say on the thread that it can be run again and let them " +
      "decide. Refused while a run is already going -- an errand runs one " +
      "at a time.",
    parameters: Type.Object({
      story: Type.String({
        description: "The errand's story id.",
      }),
      human_uid: Type.String({
        description:
          "Whose decision this is. Theirs, not yours -- and only when " +
          "they have actually asked for another run.",
      }),
    }),
  },
  {
    name: "propose_errand",
    label: "Propose an Autonomous Errand",
    method: "POST",
    description:
      "Ask the people in this goal to let an errand run WITHOUT being " +
      "accepted each time. Normally an errand waits for a person twice — " +
      "once to approve its plan, once to accept it is finished; this asks " +
      "to remove the second wait, and only that. " +
      "ASK WHEN THE ANSWER IS WORTH HAVING AND NOBODY IS WAITING FOR IT: " +
      "a weekly error-rate report, a nightly check that a backlog is " +
      "empty. The test is not \"is this safe\" — the plan approval still " +
      "happens — it is \"would a person say yes, obviously, every time?\" " +
      "If the right answer varies, run it as an ordinary errand instead. " +
      "IT IS BOUNDED OR IT IS REFUSED. Whoever approves owns every run — " +
      "their thread, their ledger principal, their cost — so how many " +
      "runs they are agreeing to own is part of what you are asking. " +
      "THE DECISION IS NEVER YOURS: you cannot approve your own " +
      "proposal, and nothing runs until somebody does. Carrying their " +
      "decision once they have made it is a different act -- that is " +
      "`accept_errand`, and it needs their uid.",
    parameters: Type.Object({
      task: Type.String({
        description: "What it should do, in the words the executor gets.",
      }),
      rationale: Type.String({
        description:
          "WHY. Somebody is being asked to let work they did not think " +
          "of run without them; the task text alone does not tell them " +
          "whether that is a good idea.",
      }),
      // REQUIRED, and deliberately not Optional. An autonomous errand's
      // story is created at APPROVAL -- by somebody else, possibly hours
      // later -- so there is no second call to grant in and nothing
      // downstream asks. Whatever is not named here, the run never has.
      //
      // Declared because a parameter the gateway does not declare is
      // STRIPPED before it reaches the kernel: the backend gained
      // `resources`, the schema did not, and GO's grants vanished in
      // transit while both halves looked correct. Same defect this
      // file's specs exist to prevent, and the reason E0 mounts this
      // directory to compare the two surfaces.
      resources: Type.Array(
        Type.Object({
          resource_id: Type.String({
            description: "Catalogue id, from create_story's `grantable`.",
          }),
          relation: Type.String({
            description:
              "`needs` to read, `produces`/`modifies` to write. Cloud " +
              "reach is always `needs`.",
          }),
          why: Type.Optional(
            Type.String({
              description:
                "Required on a write grant: one line naming what " +
                "belongs there. It reaches the executor's brief.",
            }),
          ),
        }),
        {
          description:
            "WHAT THE ERRAND IS GRANTED. Its story is created when a " +
            "person approves, so this is the only moment the need can " +
            "be stated -- an errand told to check a repository and " +
            "granted nothing spends its legs hunting for a checkout it " +
            "was never given. Pass an EMPTY ARRAY when it genuinely " +
            "needs no workspace (counting stories, reading the goal's " +
            "record) and say so in the rationale.",
        },
      ),
      name: Type.Optional(
        Type.String({ description: "Short label for the card and board." }),
      ),
      cron: Type.Optional(
        Type.String({
          description:
            "Five-field cron for a recurring one, e.g. `0 9 * * 1`. " +
            "Omit for a single run.",
        }),
      ),
      max_runs: Type.Optional(
        Type.Number({ description: "Ceiling on unattended runs. Max 100." }),
      ),
      // ON THE PROPOSAL, because an errand's story is created at
      // approval and dispatched immediately -- there is no later window
      // in which to change it, the way a story has its own page.
      // REQUIRED, like create_story's. An errand's story is created at
      // approval and dispatched immediately, so this is the only moment
      // the rung can be chosen -- and a field a model may omit is the
      // field a model omits.
      sandbox_size: Type.String({
          description:
            "REQUIRED. small, medium or large -- the machine this " +
            "errand runs on, and part of what the person is approving. " +
            "A TIER IS SOMEBODY'S MONEY: leave it out unless you have a " +
            "reason, and say the reason in the rationale. small is 2.5 " +
            "CPU / 9 GiB and fits a log read, a report, most " +
            "investigations. Ask for medium (6.5 / 23) when the errand " +
            "runs a suite or a build known to need it, and large (14 / " +
            "52) only for something measured -- a large pod can make " +
            "the cluster add a node. It is FIXED for the life of a run: " +
            "a recurring errand keeps this machine, and to change it you " +
            "cancel the run and set a new one.",
      }),
      expires_at: Type.Optional(
        Type.String({
          description: "ISO 8601, at most 90 days out.",
        }),
      ),
    }),
  },

  // ── DRIVING AN AUTONOMOUS ERRAND ────────────────────────────────────
  //
  // The tools below act on ONE run. The first five are the same
  // functions the assistant calls; the last two are the narrow end of
  // the kill switch. They are here because GO now holds a thread per
  // live errand and was expected to drive them while able only to see
  // that one existed.
  //
  // `story` IS REQUIRED ON ALL FIVE, unlike the assistant's versions
  // where it is optional and inferred from the conversation. GO's errand
  // threads each name their errand in the turn context, so there is
  // nothing to infer -- and the kernel refuses a call with no story
  // anyway, because it cannot check a permission against a run it has
  // not been told about.
  //
  // THEY REFUSE ANY STORY THAT IS NOT AN AUTONOMOUS ERRAND. The kernel
  // checks per call, not per tool: an errand with a live grant, or
  // E_NOT_AN_ERRAND / E_NOT_AUTONOMOUS. Stories and hand-started errands
  // belong to the assistant, because that is where the person working on
  // them is.
  {
    name: "story_status",
    label: "Errand Status",
    method: "GET",
    description:
      "The machine state of one autonomous errand: where it is, the " +
      "legal next steps from that run's own state machine, and every " +
      "fact tagged with how it is known. Report an unproven fact as " +
      "unproven; a model's claim is not a verdict. Use story_progress " +
      "when you want to know what it has DONE. " +
      "AUTONOMOUS ERRANDS ONLY: a story, or an errand somebody started " +
      "by hand, is refused and belongs to that person's assistant.",
    parameters: Type.Object({
      story: Type.String({ description: "The errand's story id." }),
    }),
  },
  {
    name: "story_progress",
    label: "Errand Progress",
    method: "GET",
    description:
      "What one autonomous errand has actually DONE, and what is in its " +
      "way. Files changed per branch, commits, the checks that ran and " +
      "the output of any that failed, and the last few steps. When the " +
      "run is stuck it returns `stuck` with the signals and `remedies`, " +
      "each marked with WHO can apply it: `nobody` (it retries itself; " +
      "waiting beats acting), `you` (a person must act outside the " +
      "kernel), or `assistant` (holdable with a tool). THIS IS THE READ " +
      "TO REACH FOR FIRST when somebody asks how an errand is going, and " +
      "before you ask the executor anything -- state, plan, checks and " +
      "verdicts are all on the record and cost nothing. Do not report a " +
      "`you` remedy as something you will handle. " +
      "AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      story: Type.String({ description: "The errand's story id." }),
    }),
  },
  {
    name: "steer",
    label: "Steer an Errand",
    method: "POST",
    description:
      "The next instruction to an autonomous errand already in flight. " +
      "Applies at the executor's next step boundary and supersedes " +
      "earlier guidance where they conflict. It is NOT limited to small " +
      "things: if carrying it out changes what done means, the executor " +
      "folds it into a new plan and parks for approval -- so a change of " +
      "approach does land, as a plan a person then approves rather than " +
      "a silent rewrite. You cannot approve that plan yourself. " +
      "AUTONOMOUS ERRANDS ONLY: to change a story, talk to the person " +
      "whose assistant is running it.",
    parameters: Type.Object({
      instruction: Type.String({ description: "What to do differently." }),
      story: Type.String({ description: "The errand's story id." }),
    }),
  },
  {
    name: "ask_executor",
    label: "Ask an Errand",
    method: "POST",
    description:
      "Ask a question ABOUT the work and change nothing by asking. The " +
      "only tool here that asks rather than tells -- steer and cancel " +
      "both give an order, and an executor forty steps into doing as it " +
      "is told reads a question in that position as one more thing to " +
      "do. Use it for anything needing the code read or the reasoning " +
      "behind a choice. TRY story_progress FIRST: state, plan, checks " +
      "and verdicts are on the record and cost nothing. Where the answer " +
      "comes from depends on the run: a live one answers for itself " +
      "without stopping, and one that is not running is answered beside " +
      "it by something with no way to write. If the answer needs a " +
      "decision, the errand parks and asks the person -- that is the " +
      "only thing it can do to the work, and answering undoes it. " +
      "AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      question: Type.String({
        description: "The question, in the person's own words where you can.",
      }),
      story: Type.String({ description: "The errand's story id." }),
      asked_by: Type.Optional(
        Type.String({ description: "Who is asking, for the record." }),
      ),
    }),
  },
  {
    name: "cancel_story",
    label: "Cancel an Errand",
    method: "POST",
    description:
      "Stop an autonomous errand, CARRYING A PERSON'S DECISION. " +
      "`human_uid` is required and recorded, and the kernel refuses " +
      "without it: this ends work somebody approved, so relay a " +
      "decision, never take one. If you think an errand should stop, say " +
      "so on its thread and ask; do not call this until somebody has " +
      "answered. AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      reason: Type.String({ description: "Why it is stopping." }),
      human_uid: Type.String({
        description: "Who decided. Not you -- a person, by uid.",
      }),
      story: Type.String({ description: "The errand's story id." }),
    }),
  },
  {
    name: "show_card",
    label: "Put the Card Back",
    method: "POST",
    description:
      "Post an open question's card into the thread again, when " +
      "somebody asks where it went. A card that has scrolled out of " +
      "reach is a real problem; a duplicate is not -- both carry the " +
      "same question id, and whichever is clicked settles it while the " +
      "other reports it decided. " +
      "It is a RESET, not a second posting path: it clears the poster's " +
      "claim and the poster does it again within a tick, so nothing " +
      "arrives instantly. Reach for it when a person says they cannot " +
      "find the card -- and if they are asking you to DECIDE rather " +
      "than to look, use decide_plan instead. " +
      "AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      story: Type.String({ description: "The errand's story id." }),
      question_id: Type.Number({
        description: "The open question whose card to put back.",
      }),
    }),
  },
  {
    name: "decide_plan",
    label: "Carry a Decision on a Parked Plan",
    method: "POST",
    description:
      "Decide the question an autonomous errand is parked on -- " +
      "approve the plan, ask for changes, or decline it. " +
      "THIS IS WHAT MAKES AN AUTONOMOUS ERRAND AUTONOMOUS: its first " +
      "run parks on its plan, and the person who approved the errand " +
      "has gone away by design. " +
      "DECIDE IT YOURSELF when you can. They approved this errand " +
      "knowing it would run unattended; sending every plan back to them " +
      "puts back the wait that consent removed. Read the plan against " +
      "the errand's own brief and the goal, and say yes or send it " +
      "back. " +
      "ASK A PERSON when you genuinely cannot tell -- the plan spends " +
      "real money, touches production, does something irreversible, or " +
      "changes what the errand is FOR. Then pass their `human_uid` and " +
      "their words, and the record shows both names. Being unsure is a " +
      "reason to ask; being uncomfortable deciding is not, or nothing " +
      "runs unattended at all. " +
      "`said` IS REQUIRED EITHER WAY. Relaying, it is what they told " +
      "you. Deciding, it is your reasoning -- the executor reads it as " +
      "the answer, and on a revise it is the only thing telling it what " +
      "to change. " +
      "AFTER YOU DECIDE ALONE, SAY SO ON THE GOAL THREAD: what you " +
      "approved or sent back and why, in a line or two, so somebody can " +
      "disagree before the work is done rather than after. " +
      "THREE CYCLES PER RUN is the ceiling on deciding alone. Past it " +
      "the run stays parked for a person -- re-planning that many times " +
      "is a loop, not progress. " +
      "Done-or-more-work at the end is NEVER yours; that stays a " +
      "person's. AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      story: Type.String({ description: "The errand's story id." }),
      question_id: Type.Number({
        description:
          "The parked question, from `inbox` or `story_progress`.",
      }),
      verdict: Type.String({
        description:
          "approve, revise or decline. `revise` sends the plan back to " +
          "be rewritten and is the right answer to \"nearly, but...\"; " +
          "`decline` rejects the approach outright.",
      }),
      human_uid: Type.Optional(Type.String({
        description:
          "ONLY when you are relaying a person's decision: their uid, " +
          "and they must be an admin of this goal. Leave it out when " +
          "the decision is yours -- passing your own id is refused, and " +
          "a uid nobody can check is refused too.",
      })),
      said: Type.String({
        description:
          "Relaying: what they actually said, their words. Deciding: " +
          "your reasoning. The executor reads this as the answer.",
      }),
    }),
  },
  {
    name: "run_control",
    label: "Pause, Resume or Cancel a Run",
    method: "POST",
    description:
      "PAUSE, RESUME or CANCEL the run in flight -- exactly what the " +
      "buttons on the thread do, and through the same mechanism, so a " +
      "person and you cannot disagree about whether something is " +
      "paused. " +
      "`human_uid` IS REQUIRED on all three and refused without: these " +
      "stop or alter work somebody approved, so relay a decision, never " +
      "take one. If you think a run should stop, say so on the thread " +
      "and ask. " +
      "PAUSE stops it at the next step boundary, never mid-step, so a " +
      "step in flight finishes first -- and only works while it is " +
      "RUNNING; a run parked on a plan is already waiting for a person. " +
      "RESUME releases the hold, and a `message` rides along as a steer " +
      "the executor reads BEFORE it carries on. " +
      "CANCEL ends THIS RUN from any live state -- a recurring errand's " +
      "next run still comes, so to stop the errand itself ask for the " +
      "grant to be revoked. " +
      "AUTONOMOUS ERRANDS ONLY.",
    parameters: Type.Object({
      action: Type.String({
        description: "pause, resume or cancel.",
      }),
      story: Type.String({ description: "The errand's story id." }),
      human_uid: Type.String({
        description: "Who decided. Not you -- a person, by uid.",
      }),
      message: Type.Optional(
        Type.String({
          description:
            "Why, for the record. On RESUME this is stronger than a " +
            "note: it is delivered as a steer and read before the run " +
            "continues, so put the instruction here rather than sending " +
            "a separate steer afterwards.",
        }),
      ),
    }),
  },
];

export const KERNEL_GO_TOOL_NAMES = SPECS.map((s) => s.name);

function build(spec: Spec, opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: spec.label,
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    execute: async (_toolCallId, args, _signal) => {
      const goalId =
        opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      const agentId = resolveAgentIdFromSession(opts?.agentSessionKey);
      const surface = kernelSurfaceFor(agentId, goalId);
      if (!goalId) {
        return jsonResult({
          refused: "E_NO_GOAL",
          detail: "could not determine which goal this thread belongs to",
        });
      }
      const base = kernelUrl();
      if (!base) {
        return jsonResult({
          refused: "E_NO_KERNEL",
          detail: "KERNEL_ASSISTANT_URL is not set, so no tool can reach the kernel",
        });
      }

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        // DECIDED BY THE CALLER, not by which file the spec lives in.
        // An assistant reaching a tool declared here — which happens
        // whenever this file's copy of a shared name wins registration —
        // must be recorded as the assistant it is, and routed to its own
        // surface below. `go:` on an assistant's write is how the kernel
        // loses track of who asked.
        "X-Assistant-Actor": surface === "go"
          ? `go:${goalId}`
          : agentId
            ? `assistant:${agentId}`
            : `assistant:${goalId}`,
      };
      const token = kernelToken();
      if (token) headers["X-Assistant-Token"] = token;

      const path = `${base}/${surface}/${encodeURIComponent(goalId)}/${spec.name}`;
      const body = (args ?? {}) as Record<string, unknown>;

      try {
        const res =
          spec.method === "GET"
            ? await fetch(
                `${path}?${new URLSearchParams(
                  Object.entries(body)
                    .filter(([, v]) => v !== undefined && v !== null)
                    .map(([k, v]) => [k, String(v)]),
                )}`,
                { headers },
              )
            : await fetch(path, {
                method: "POST",
                headers,
                body: JSON.stringify(withIdempotencyKey(body)),
              });
        // A typed refusal arrives as 200 with a `refused` code and is
        // passed through untouched; only a transport failure is an error.
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({
          refused: "E_KERNEL_UNREACHABLE",
          detail: e?.message ?? String(e),
        });
      }
    },
  };
}

export function kernelGoTools(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool[] {
  return SPECS.map((spec) => build(spec, opts));
}
