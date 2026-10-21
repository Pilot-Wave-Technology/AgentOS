/**
 * H10b — the assistant's eight tools, pointed at the kernel.
 *
 *   READ    story_status . inbox . read_artifact
 *   WRITE   ask_go . steer . amend . relay_answer . cancel_story
 *   ASK     ask_executor
 *
 * The assistant does no work. It reads projections and writes intents,
 * and every capability it appears to have beyond those two is one of
 * them wearing a different hat.
 *
 * WHY THESE ARE BUILT FROM A TABLE. The kernel serves
 * `/assistant/<goal>/<tool>` and asserts `set(ROUTES) == set(TOOLS)` at
 * startup; this file is the other half of the same statement. Declaring
 * each tool by hand would let the two drift silently, which is the exact
 * defect the surface was designed to make impossible — the live system
 * being replaced has five allowlist entries with no catalog row,
 * surviving only because nothing compares them.
 *
 * WHAT IS ABSENT, and enforced by there being no entry rather than by a
 * rule in a prompt: dispatch, provision, approve, verify, commit, and
 * create-a-story. The assistant asks; something else decides.
 *
 * A TYPED REFUSAL IS A RESULT, NOT AN ERROR. The kernel answers 200 with
 * a `refused` code for "which story did you mean" and "you may not
 * approve that". They are handed back verbatim so the model can act on
 * them; turning them into failures would invite a retry that buries the
 * answer.
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

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  // agent:{agentId}:{goalId}-{threadId}
  const parts = sessionKey.split(":");
  if (parts.length < 3) return "";
  const goalThread = parts.slice(2).join(":");
  const dash = goalThread.indexOf("-");
  return dash > 0 ? goalThread.substring(0, dash) : goalThread;
}

/**
 * WHICH assistant is calling — from the same string the goal comes from.
 *
 * The actor header used to be `assistant:{goalId}`, which names the goal
 * and not the caller: every assistant in a goal sent the identical
 * string. So `assistant_requests.actor_id` recorded who asked as
 * "assistant:KER14" for all of them, and the kernel had no way to tell
 * one person's assistant from another's — which meant a write could not
 * be scoped to the stories its caller had actually worked.
 *
 * The agent id was in the session key the whole time, one field along
 * from the goal id this file already parses out of it.
 */
function resolveAgentIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  // agent:{agentId}:{goalId}-{threadId}
  const parts = sessionKey.split(":");
  return parts.length >= 3 ? parts[1] : "";
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
    name: "story_status",
    label: "Story Status",
    method: "GET",
    description:
      "What is happening. With no story, every story in flight, briefly. " +
      "With a story, the full picture: its state, the legal next steps read " +
      "from that run's own state machine, and every fact tagged with how it " +
      "is known — observed, derived, or unproven. Report an unproven fact as " +
      "unproven; a model's claim is not a verdict.",
    parameters: Type.Object({
      story: Type.Optional(Type.String({ description: "Execution id. Omit for all." })),
    }),
  },
  {
    name: "story_progress",
    label: "Story Progress",
    method: "GET",
    // WHAT CHANGED, not what state. story_status reads the state
    // machine and answers "RUNNING, 27 steps", which is true and thin.
    // This reads the work itself, and when a run is stuck it is the
    // only way to see the evidence -- git_nodes.last_error in
    // particular was recorded and surfaced to nobody, so a story whose
    // push was refused just stopped, silently.
    description:
      "What this story has actually DONE, and what (if anything) is in " +
      "its way. Files changed per branch with counts, commits made, the " +
      "checks that ran and the OUTPUT of any that failed, and the last " +
      "few steps. When the run is stuck it also returns `stuck`, with " +
      "the signals (the run's own stall reason, git engine errors, " +
      "verification strikes) and `remedies` — each marked with WHO can " +
      "apply it: `nobody` (it retries itself; waiting beats acting), " +
      "`you` (a person must act outside the kernel), or `assistant` " +
      "(you can, with a tool you hold). Use this for \"what's " +
      "happening\" and for anything that looks stuck; use story_status " +
      "when you only need the state. Do not report a `you` remedy as " +
      "something you will handle.",
    parameters: Type.Object({
      story: Type.String({ description: "Story id, or a run number." }),
    }),
  },
  {
    name: "goal_state",
    label: "Goal State",
    method: "GET",
    description:
      "The whole board in one call — what this goal is FOR (its name, " +
      "description and objective), every story in it with state, who it " +
      "is assigned to and what it waits on, who is on the team and how " +
      "much each person is carrying, the decisions recorded so far, and " +
      "the asks pending with GO. Goal-wide: it includes work assigned to " +
      "other people, and stories nobody has started yet — neither of " +
      "which `story_status` can show you, because that reads RUNS and a " +
      "story with no run has none. " +
      "Read it when a question is about the goal rather than about one " +
      "story: what are we trying to do, what else is happening, is this " +
      "already covered, who is this blocked on. `story_status` stays the " +
      "cheap default for what is running right now. " +
      "`landed` is separate from `state` on purpose: done means the work " +
      "happened, landed means the team has it. Never report done as " +
      "delivered. " +
      "This is orientation, not authority. You cannot create a story, " +
      "assign one, or change what the goal is for — seeing the board does " +
      "not make planning yours. Anything it shows you that needs doing, " +
      "`ask_go`.",
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
    name: "record_finding",
    label: "Record Finding",
    method: "POST",
    description:
      "Write down something the WORK revealed that nobody knew when the " +
      "plan was written — 'the vendor rate-limits at 5/s', 'the fixture " +
      "loader ignores a trailing newline'. Not what was done (the diff " +
      "says that) and not what was planned (the plan says that). " +
      "Use it the moment a constraint turns up, not at the end: the " +
      "completion report carries what the executor knew when it " +
      "finished, and anything discovered at leg 3 has to survive until " +
      "then in a conversation nobody will re-read. " +
      "It is recorded against the story, which is what makes it findable " +
      "again by what that story touched.",
    parameters: Type.Object({
      story: Type.String({ description: "The story this was learned on." }),
      fact: Type.String({ description: "What is true, in one line." }),
      why_it_matters: Type.Optional(
        Type.String({ description: "What it changes for whoever comes next." }),
      ),
    }),
  },
  {
    name: "inbox",
    label: "Inbox",
    method: "GET",
    description:
      "Everything waiting on a person, grouped as cards. `needs_human` marks " +
      "a plan approval: you may relay a revise or a decline on one, never an " +
      "approval. Say so rather than attempting it.",
    parameters: Type.Object({}),
  },
  {
    name: "list_resources",
    label: "List Resources",
    method: "GET",
    description:
      "What this goal owns — repositories, the TRUNK BRANCHES declared " +
      "on them (one repo may carry several, each its own merge " +
      "target), and documents — and whether " +
      "this story already holds each one. You cannot attach a resource " +
      "yourself: anything the story needs and does not have, ask GO for. " +
      "`held: false` is the thing to act on. " +
      "`writable` is what the story holds NOW; `can_write` is what the " +
      "catalogue would ever allow. Never ask for write access on a " +
      "resource whose `can_write` is false — it is read-only by policy " +
      "and the ask will be refused. More importantly, never write a " +
      "criterion about a path the story cannot write: it will fail " +
      "honestly forever and no dry-run can tell you why.",
    parameters: Type.Object({
      story: Type.Optional(
        Type.String({ description: "Show what THIS story holds." }),
      ),
    }),
  },
  {
    name: "read_artifact",
    label: "Read Artifact",
    method: "GET",
    description:
      "Read part of a stored file — the lines matching a pattern, or a byte " +
      "range. You hold no workspace, so this is how you read anything at all: " +
      "a file someone attached, or an executor's oversized output. Never the " +
      "whole object.",
    parameters: Type.Object({
      artifact: Type.String({ description: "Artifact id." }),
      pattern: Type.Optional(Type.String({ description: "Regex; returns matching lines." })),
      offset: Type.Optional(Type.String({ description: "Byte offset; negative reads from the end." })),
      length: Type.Optional(Type.String({ description: "Bytes to read." })),
    }),
  },
  {
    name: "start_story",
    label: "Start a Run",
    method: "POST",
    description:
      "Start a run of a story GO has already created. You are not " +
      "creating a story — GO did that — you are starting one attempt at " +
      "it, and a story can be run more than once. " +
      "Do this only once you know what the work is, which repositories " +
      "AND WHICH TRUNK BRANCH IN EACH it touches — the story must " +
      "already hold that BRANCH resource (`list_resources`, then " +
      "`ask_go`); on a repo carrying two lanes, holding the repo is " +
      "not holding a lane, and the work lands wherever the fallback " +
      "points. and what must be true when it is done. Criteria " +
      "go in as PROSE, never as a shell command: the executor sees the " +
      "tree and turns them into runnable checks. A good criterion fails " +
      "now and passes when the work is finished.",
    parameters: Type.Object({
      story: Type.String({ description: "The story GO created." }),
      task: Type.String({
        description: "The work in prose. Include the why, not only the what.",
      }),
      criteria_prose: Type.Optional(
        Type.Array(Type.String(), {
          description: "What must be true when done, and what must not break.",
        }),
      ),
      kind: Type.Optional(
        Type.String({ description: "story (default) or errand." }),
      ),
    }),
  },
  {
    name: "ask_go",
    label: "Ask the Goal Orchestrator",
    method: "POST",
    description:
      "Ask GO for something you cannot do: a story created, a resource " +
      "granted, work sequenced. This is the ONLY creation path — you cannot " +
      "create a story, and asking is not a lesser version of doing it.",
    parameters: Type.Object({
      kind: Type.String({
        description:
          "story | resource | sequence | question | report. `report` is the " +
          "only one that asks for nothing: it files the assistant's account " +
          "of a FINISHED story, stores it as that story's result_summary so " +
          "GO can read it back weeks later, and needs no reply. It requires " +
          "`story`.",
      }),
      body: Type.String({ description: "What is being asked for, and why." }),
      story: Type.Optional(Type.String({ description: "The story this concerns." })),
    }),
  },
  {
    name: "steer",
    label: "Steer",
    method: "POST",
    description:
      "The next instruction to work already in flight. Applies at the " +
      "executor's next step boundary and supersedes earlier guidance where " +
      "they conflict. It is NOT limited to small things: if carrying it out " +
      "changes what done means, the executor folds it into a new plan and " +
      "parks for approval — so 'use SQLite instead' does land, as a plan a " +
      "person then approves rather than a silent rewrite. Reach for `amend` " +
      "instead when you want the change RECORDED as a contract, or when the " +
      "story is already finished — amend reopens one, a steer cannot.",
    parameters: Type.Object({
      instruction: Type.String({ description: "What to do differently." }),
      story: Type.Optional(Type.String({ description: "Execution id." })),
      focus: Type.Optional(Type.String({ description: "The story under discussion." })),
      hint: Type.Optional(Type.String({
        description:
          "A FRAGMENT OF WHAT THE PERSON SAID, when more than one " +
          "run could be meant and `story` is not known — 'the " +
          "rename one', 'the migration'. Matched against the " +
          "candidates; turning what was said into this fragment is " +
          "your job, and without it an ambiguous target is refused " +
          "rather than guessed.",
      })),
    }),
  },
  {
    name: "ask_executor",
    label: "Ask the Story",
    method: "POST",
    description:
      "Ask a question ABOUT the work and change nothing by asking. The only " +
      "tool here that asks rather than tells: steer, amend and cancel all " +
      "give an order, so a person who merely wanted to know something had to " +
      "phrase it as a steer — and asked why it chose in-memory storage, an " +
      "executor went and changed the storage. Use this for anything needing " +
      "the CODE read or the reasoning behind a choice: 'why this shape?', " +
      "'does it handle nulls?', 'what did you assume about the spec?'. " +
      "TRY story_progress FIRST — state, plan, checks and verdicts are all " +
      "on the record and cost nothing to read. Nothing here reaches the " +
      "executor's prompt: the question is answered beside the run by " +
      "something with no way to write, so a running leg is not interrupted " +
      "and a parked one is not woken. If the answer turns out to need a " +
      "decision — the plan assumes something untrue, or the question was a " +
      "preference wearing a question mark — the story PARKS and asks the " +
      "person. That is the only thing it can do to the work, and answering " +
      "undoes it. It can never start work or approve anything.",
    parameters: Type.Object({
      question: Type.String({
        description: "The question, in the person's own words where you can.",
      }),
      asked_by: Type.Optional(
        Type.String({ description: "Who is asking, for the record." }),
      ),
      story: Type.Optional(Type.String({ description: "Execution id." })),
      focus: Type.Optional(Type.String({ description: "The story under discussion." })),
      hint: Type.Optional(Type.String({
        description:
          "A FRAGMENT OF WHAT THE PERSON SAID, when more than one " +
          "run could be meant and `story` is not known — 'the " +
          "rename one', 'the migration'. Matched against the " +
          "candidates; turning what was said into this fragment is " +
          "your job, and without it an ambiguous target is refused " +
          "rather than guessed.",
      })),
    }),
  },
  {
    name: "amend",
    label: "Amend the Story",
    method: "POST",
    description:
      "Change what the story is FOR. Works from any state, including a " +
      "finished one, which reopens it. Criteria go in as PROSE and never as " +
      "a shell command: you know what was meant and cannot see the tree, the " +
      "executor can see the tree and must not decide what done means. An " +
      "amendment always withdraws the plan approval, because a plan approved " +
      "for the old scope is not approval for the new one.",
    parameters: Type.Object({
      scope: Type.Optional(Type.String({ description: "Added to the task." })),
      criteria_prose: Type.Optional(
        Type.Array(Type.String(), {
          description: "The COMPLETE new set, in prose. Not an addition.",
        }),
      ),
      story: Type.Optional(Type.String({ description: "Execution id." })),
      focus: Type.Optional(Type.String({
        description: "The story under discussion.",
      })),
      hint: Type.Optional(Type.String({
        description:
          "A FRAGMENT OF WHAT THE PERSON SAID, when more than one " +
          "run could be meant and `story` is not known — 'the " +
          "rename one', 'the migration'. Matched against the " +
          "candidates; turning what was said into this fragment is " +
          "your job, and without it an ambiguous target is refused " +
          "rather than guessed.",
      })),
    }),
  },
  {
    name: "relay_answer",
    label: "Relay an Answer",
    method: "POST",
    description:
      "Carry a person's answer to a parked executor. You may relay a " +
      "`revise` or a `declined` on a plan — 'nearly right, change step 3' is " +
      "exactly what this is for — but never an approval. Approval is a " +
      "person's act and the vocabulary has no word for it.",
    parameters: Type.Object({
      question_id: Type.String({ description: "The question being answered." }),
      text: Type.String({ description: "The answer, in the person's words." }),
      verdict: Type.Optional(Type.String({ description: "revise | declined" })),
    }),
  },
  {
    name: "show_card",
    label: "Show a Card Again",
    method: "POST",
    // A card scrolled out of reach is a real problem; a duplicate card
    // is not. The buttons carry the question id and every write is
    // guarded on status='OPEN', so whichever copy is clicked settles
    // the one question and the other reports it decided.
    description:
      "Put an open question's card back at the bottom of the thread, " +
      "with its buttons live. Use it when someone asks to see the plan " +
      "again, or has scrolled past a decision that is still waiting. " +
      "Refuses on a question already answered — say what was decided " +
      "instead of showing live buttons on a settled decision. The card " +
      "reappears within a few seconds; it is re-posted by the same " +
      "poster that posted it the first time, not by you.",
    parameters: Type.Object({
      question_id: Type.String({
        description: "The question whose card to show again.",
      }),
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
    name: "cancel_story",
    label: "Cancel a Story",
    method: "POST",
    description:
      "Stop a run, carrying a person's decision. `human_uid` is required and " +
      "recorded: this ends somebody's work, so relay a decision, never take " +
      "one.",
    parameters: Type.Object({
      reason: Type.String({ description: "Why it is stopping." }),
      human_uid: Type.String({ description: "Who decided." }),
      story: Type.Optional(Type.String({ description: "Execution id." })),
      // CANCELLING WAS THE ONE WRITE THAT COULD NOT BE AIMED. The
      // dispatcher reads `focus` for this tool like every other, and the
      // spec declared neither it nor `hint` -- so the single most
      // destructive write was also the only one that had to guess, or
      // refuse, when more than one run could be meant.
      focus: Type.Optional(Type.String({
        description: "The story under discussion.",
      })),
      hint: Type.Optional(Type.String({
        description:
          "A FRAGMENT OF WHAT THE PERSON SAID, when more than one run " +
          "could be meant and `story` is not known. Matters most here: " +
          "cancelling is not undoable.",
      })),
    }),
  },
  {
    name: "list_artifacts",
    label: "List Artifacts",
    method: "GET",
    description:
      "WHAT AN ERRAND PRODUCED — names, roles (report / dataset / method / " +
      "log / figure), sizes and the executor's own one-line summary of " +
      "each. An errand's branch never lands and is deleted when the errand " +
      "is accepted, " +
      "so its artifacts ARE its result: knowing the run finished tells you " +
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
];

export const KERNEL_ASSISTANT_TOOL_NAMES = SPECS.map((s) => s.name);

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
        // `assistant:{agentId}` — the CALLER, not the goal. The goal is
        // already in the path below; repeating it here told the kernel
        // nothing and cost it the one fact it needed to scope a write.
        // Falls back to the old shape only when the session key is
        // unavailable, which the kernel detects and says so about.
        // THE SURFACE AND THE ACTOR MUST AGREE, and are both decided
        // by the CALLER rather than by which file this spec lives in.
        // This file and `kernel-go-tools.ts` both declare `ask_executor`
        // and four of its siblings; the gateway keys tools by name, so
        // one of the two definitions serves every agent. Deriving the
        // route here makes that collision harmless — whichever one wins,
        // it routes the caller to its own surface.
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
        // A refusal arrives as 200 with a `refused` code and is passed
        // through untouched; only a transport failure is an error.
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

export function kernelAssistantTools(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool[] {
  return SPECS.map((spec) => build(spec, opts));
}
