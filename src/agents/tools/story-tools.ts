/**
 * Story and collaboration tools for SHCA and GO agents.
 *
 * story_list        — list all stories in the goal
 * story_detail      — get full story context
 * story_update      — post a progress update (Gemini-merged)
 * create_story      — GO creates a story
 * update_story      — GO updates a story
 * cancel_story      — GO cancels a story
 * message_go        — SHCA posts to orchestrator channel
 * reply_assistant   — GO replies to a specific story's assistant
 * create_branch     — GO creates a feature branch
 * register_resource — SHCA registers a new resource
 * grant_resource    — GO grants resource to story / SHCA grants to task
 * revoke_resource   — GO revokes from story / SHCA revokes from task
 * list_allocations  — list current allocations for a goal|story|task
 * record_decision   — GO records a decision
 * list_decisions    — read goal decisions
 * critical_path     — GO checks critical path
 */
import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

import { getControlPanelAuth } from "./control-panel-auth.js";

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  // Session key format: agent:{agentId}:{goalId}-{threadId}
  const parts = sessionKey.split(":");
  if (parts.length >= 3) {
    const goalThread = parts.slice(2).join(":");
    const dashIdx = goalThread.indexOf("-");
    return dashIdx > 0 ? goalThread.substring(0, dashIdx) : goalThread;
  }
  return "";
}

// ── story_list ──────────────────────────────────────────────────────────

const StoryListSchema = Type.Object({});

export function storyListTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "List Stories",
    name: "story_list",
    description:
      "List all stories in the current goal with their states, task progress, " +
      "and a preview of the progress summary.",
    parameters: StoryListSchema,
    execute: async (_toolCallId, _args, _signal) => {
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id." });
      }
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!res.ok) {
          return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        }
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── story_detail ────────────────────────────────────────────────────────

const StoryDetailSchema = Type.Object({
  story_id: Type.String({ description: "The story ID to get details for." }),
});

export function storyDetailTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Story Detail",
    name: "story_detail",
    description:
      "Get full details of a specific story including description, acceptance criteria, " +
      "tasks under it, dependencies, and the full progress summary.",
    parameters: StoryDetailSchema,
    execute: async (_toolCallId, args, _signal) => {
      const params = args as Record<string, unknown>;
      const storyId = readStringParam(params, "story_id", { required: true, trim: true });
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id." });
      }
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories/${encodeURIComponent(storyId)}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!res.ok) {
          return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        }
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── story_update ────────────────────────────────────────────────────────

const StoryUpdateSchema = Type.Object({
  story_id: Type.String({ description: "The story ID to update." }),
  update: Type.String({
    description:
      "A detailed progress update. Describe what was done, key decisions made, " +
      "approach changes, blockers resolved, or resources created/modified. " +
      "This will be merged with the existing progress summary using AI summarization.",
  }),
});

export function storyUpdateTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Update Story Progress",
    name: "story_update",
    description:
      "Post a progress update to a story. The update is merged with the existing " +
      "progress summary using AI summarization, keeping it concise and current. " +
      "Use this when something noteworthy happens: task completed, approach decided, " +
      "blocker hit or resolved, deviation from plan.",
    parameters: StoryUpdateSchema,
    execute: async (_toolCallId, args, _signal) => {
      const params = args as Record<string, unknown>;
      const storyId = readStringParam(params, "story_id", { required: true, trim: true });
      const update = readStringParam(params, "update", { required: true });
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id." });
      }
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories/${encodeURIComponent(storyId)}/update`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ update }),
          },
        );
        if (!res.ok) {
          return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        }
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── create_story ────────────────────────────────────────────────────────

const CreateStorySchema = Type.Object({
  title: Type.String({ description: "Clear story title describing the work item." }),
  description: Type.Optional(Type.String({ description: "Detailed description of what needs to be done." })),
  acceptance_criteria: Type.Optional(Type.String({ description: "How we know this story is done." })),
  assigned_to: Type.Optional(Type.String({ description: "Human uid to assign to." })),
  priority: Type.Optional(Type.String({ description: "low, medium, high, or critical." })),
  depends_on: Type.Optional(Type.Array(Type.String(), { description: "Story IDs or titles this depends on." })),
  estimated_effort: Type.Optional(Type.String({ description: "e.g. '2 days', '1 week'" })),
});

export function storyCreateTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Create Story",
    name: "create_story",
    description:
      "Create a new story on the Story Board. Stories are human-level work items. " +
      "Only create stories after the team has approved your plan. " +
      "The story appears as 'proposed' and the assigned human must accept it.",
    parameters: CreateStorySchema,
    execute: async (_toolCallId: string, args: unknown, _signal: unknown) => {
      const params = args as Record<string, unknown>;
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) return jsonResult({ success: false, error: "Could not determine goal_id." });
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });
      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories/create`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify(params),
          },
        );
        if (!res.ok) return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── update_story ────────────────────────────────────────────────────────

const UpdateStorySchema = Type.Object({
  story_id: Type.String({ description: "The story ID to update." }),
  description: Type.Optional(Type.String({ description: "New description." })),
  acceptance_criteria: Type.Optional(Type.String({ description: "New acceptance criteria." })),
  priority: Type.Optional(Type.String({ description: "New priority." })),
  assigned_to: Type.Optional(Type.String({ description: "New assignee uid." })),
  depends_on: Type.Optional(Type.Array(Type.String(), { description: "New dependency list." })),
  estimated_effort: Type.Optional(Type.String({ description: "New effort estimate." })),
});

export function storyModifyTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Update Story",
    name: "update_story",
    description: "Update a story's description, acceptance criteria, priority, assignee, dependencies, or effort estimate.",
    parameters: UpdateStorySchema,
    execute: async (_toolCallId: string, args: unknown, _signal: unknown) => {
      const params = args as Record<string, unknown>;
      const storyId = readStringParam(params, "story_id", { required: true, trim: true });
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) return jsonResult({ success: false, error: "Could not determine goal_id." });
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });
      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories/${encodeURIComponent(storyId)}/modify`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify(params),
          },
        );
        if (!res.ok) return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── cancel_story ────────────────────────────────────────────────────────

const CancelStorySchema = Type.Object({
  story_id: Type.String({ description: "The story ID to cancel." }),
  reason: Type.String({ description: "Why this story is being cancelled." }),
});

export function storyCancelTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Cancel Story",
    name: "cancel_story",
    description: "Cancel a story. The assignee is notified. Only cancel after team approval.",
    parameters: CancelStorySchema,
    execute: async (_toolCallId: string, args: unknown, _signal: unknown) => {
      const params = args as Record<string, unknown>;
      const storyId = readStringParam(params, "story_id", { required: true, trim: true });
      const reason = readStringParam(params, "reason", { required: true });
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) return jsonResult({ success: false, error: "Could not determine goal_id." });
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });
      try {
        const res = await fetch(
          `${url}/internal/goals/${encodeURIComponent(goalId)}/stories/${encodeURIComponent(storyId)}/cancel`,
          {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({ reason }),
          },
        );
        if (!res.ok) return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── Helper for POST tools ───────────────────────────────────────────────────

function makePostTool(
  name: string, label: string, description: string,
  schema: ReturnType<typeof Type.Object>,
  pathFn: (goalId: string, params: Record<string, unknown>) => string,
  bodyFn?: (params: Record<string, unknown>) => unknown,
  opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string },
): AnyAgentTool {
  return {
    label, name, description, parameters: schema,
    execute: async (_tid: string, args: unknown, _sig: unknown) => {
      const params = args as Record<string, unknown>;
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) return jsonResult({ success: false, error: "Could not determine goal_id." });
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });
      try {
        const res = await fetch(`${url}${pathFn(goalId, params)}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(bodyFn ? bodyFn(params) : params),
        });
        if (!res.ok) return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

function makeGetTool(
  name: string, label: string, description: string,
  schema: ReturnType<typeof Type.Object>,
  pathFn: (goalId: string, params: Record<string, unknown>) => string,
  opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string },
): AnyAgentTool {
  return {
    label, name, description, parameters: schema,
    execute: async (_tid: string, args: unknown, _sig: unknown) => {
      const params = args as Record<string, unknown>;
      const goalId = opts?.goalId || resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) return jsonResult({ success: false, error: "Could not determine goal_id." });
      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });
      try {
        const res = await fetch(`${url}${pathFn(goalId, params)}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) return jsonResult({ success: false, error: `HTTP ${res.status}`, body: await res.text() });
        return jsonResult(await res.json());
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message });
      }
    },
  };
}

// ── message_go ──────────────────────────────────────────────────────────

export function messageGoTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "message_go", "Message GO",
    "Post a message to the Goal Orchestrator via the orchestrator channel. " +
    "Use type='question' for questions, 'update' for progress updates (also updates story summary), " +
    "'request' for resource requests.",
    Type.Object({
      story_id: Type.String({ description: "Your current story ID (from IDENTITY.md)." }),
      message: Type.String({ description: "The message to send to the GO." }),
      type: Type.Optional(Type.String({ description: "question | update | request. Defaults to update." })),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/orchestrator/message`,
    (params) => ({
      ...params,
      gateway_agent_id: resolveAgentIdFromSession(opts?.agentSessionKey) || "",
    }),
    opts,
  );
}

// ── reply_assistant ─────────────────────────────────────────────────────

export function replyAssistantTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "reply_assistant", "Reply to Assistant",
    "Reply to a specific story's assistant. The message appears in the orchestrator channel " +
    "AND is delivered to the assistant's conversation thread.",
    Type.Object({
      story_id: Type.String({ description: "The story ID whose assistant you're replying to." }),
      message: Type.String({ description: "Your reply to the assistant." }),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/orchestrator/reply`,
    undefined, opts,
  );
}

// ── create_branch ───────────────────────────────────────────────────────

export function createBranchTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "create_branch", "Create Branch",
    "Create a feature branch from an existing repo/branch resource. " +
    "The new branch is registered as a goal resource linked to the story.",
    Type.Object({
      resource_id: Type.String({ description: "The repo/branch resource ID to branch FROM." }),
      branch_name: Type.String({ description: "New branch name (e.g. feat/story-abc)." }),
      story_id: Type.Optional(Type.String({ description: "Story ID to link this branch to." })),
    }),
    (goalId, params) => `/internal/goals/${encodeURIComponent(goalId)}/resources/${encodeURIComponent(String(params.resource_id))}/branch`,
    undefined, opts,
  );
}

// ── register_resource ───────────────────────────────────────────────────

export function registerResourceTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "register_resource", "Register Resource",
    "Register a new resource you created (branch, document, file). " +
    "It becomes a goal resource visible to the GO and other stories.",
    Type.Object({
      name: Type.String({ description: "Resource name (e.g. feat/auth-middleware)." }),
      kind: Type.String({ description: "Resource kind: branch | document | file | config | other." }),
      uri: Type.Optional(Type.String({ description: "URL or path to the resource." })),
      story_id: Type.Optional(Type.String({ description: "Story that produced this resource." })),
      description: Type.Optional(Type.String({ description: "What this resource is." })),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/resources/register`,
    undefined, opts,
  );
}

// ── grant_resource ──────────────────────────────────────────────────────
//
// Unified resource-allocation tool. Replaces link_story_resource.
// - GO uses it with subject_type="story" to allocate a resource to a
//   story (populates story_resources).
// - SHCAs use it with subject_type="task" to add (or re-include a
//   previously-excluded) resource for a specific task (populates
//   task_resources).
//
// The `notes` field is rendered inline in the subject's RESOURCES.md
// as "**GO note:**" (story-level) or "**SHCA note:**" (task-level),
// giving the downstream agent per-allocation tactical guidance.

export function grantResourceTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "grant_resource", "Grant Resource",
    "Grant access to a goal resource for a story (GO) or task (SHCA). " +
    "The `notes` field is surfaced inline in the target's RESOURCES.md as " +
    "per-allocation tactical guidance — use it to scope the work (which " +
    "subfolder, which sections of a spec, what NOT to touch).",
    Type.Object({
      subject_type: Type.String({ description: "'story' (GO's use) or 'task' (SHCA's use)." }),
      subject_id: Type.String({ description: "The story_id or task_id receiving access." }),
      resource_id: Type.String({ description: "The goal_resources.id being granted." }),
      notes: Type.String({ description: "Tactical guidance for how to use this resource in this specific story/task." }),
      relation: Type.Optional(Type.String({ description: "needs | produces | modifies. Defaults to 'needs'." })),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/allocations/grant`,
    (params) => ({
      ...params,
      actor_id: resolveAgentIdFromSession(opts?.agentSessionKey) || "",
    }),
    opts,
  );
}

// ── revoke_resource ─────────────────────────────────────────────────────
//
// Revokes a resource from a story or task. For stories: deletes the
// story_resources row. For tasks: if the resource was added at task
// level, the addition is removed; if it's inherited from the story,
// a task_resources row with relation='excluded' is inserted so the
// resource is hidden from THIS task only (the story keeps it).

export function revokeResourceTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "revoke_resource", "Revoke Resource",
    "Revoke a story's or task's access to a resource. For tasks, this " +
    "either removes a previously-added resource or (if the resource is " +
    "inherited from the story) hides it from this task's RESOURCES.md " +
    "only — the story still keeps the resource.",
    Type.Object({
      subject_type: Type.String({ description: "'story' (GO's use) or 'task' (SHCA's use)." }),
      subject_id: Type.String({ description: "The story_id or task_id losing access." }),
      resource_id: Type.String({ description: "The goal_resources.id being revoked." }),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/allocations/revoke`,
    (params) => ({
      ...params,
      actor_id: resolveAgentIdFromSession(opts?.agentSessionKey) || "",
    }),
    opts,
  );
}

// ── list_allocations ────────────────────────────────────────────────────

export function listAllocationsTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makeGetTool(
    "list_allocations", "List Allocations",
    "List the current resource allocations for a subject. For subject_type='task', " +
    "the response merges story-inherited allocations with any task-level additions " +
    "or exclusions — i.e. exactly what the task's RESOURCES.md renders.",
    Type.Object({
      subject_type: Type.String({ description: "goal | story | task." }),
      subject_id: Type.Optional(Type.String({ description: "Required for story/task; ignored for goal." })),
    }),
    (goalId, params) => {
      const qs = new URLSearchParams({
        subject_type: String(params.subject_type),
        subject_id: String(params.subject_id ?? ""),
      }).toString();
      return `/internal/goals/${encodeURIComponent(goalId)}/allocations?${qs}`;
    },
    opts,
  );
}

// ── record_decision ─────────────────────────────────────────────────────

export function recordDecisionTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makePostTool(
    "record_decision", "Record Decision",
    "Record a goal decision (e.g. tech choice, architecture decision). " +
    "Decisions are shared with all assistants via IDENTITY.md.",
    Type.Object({
      decision: Type.String({ description: "The decision made." }),
      rationale: Type.Optional(Type.String({ description: "Why this decision was made." })),
      story_id: Type.Optional(Type.String({ description: "Related story if any." })),
    }),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/decisions`,
    undefined, opts,
  );
}

// ── list_decisions ──────────────────────────────────────────────────────

export function listDecisionsTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makeGetTool(
    "list_decisions", "List Decisions",
    "List all recorded decisions for this goal.",
    Type.Object({}),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/decisions`,
    opts,
  );
}

// ── critical_path ───────────────────────────────────────────────────────

export function criticalPathTool(opts?: { config?: OpenClawConfig; goalId?: string; agentSessionKey?: string }): AnyAgentTool {
  return makeGetTool(
    "critical_path", "Critical Path",
    "Calculate the critical path — the longest chain of dependent stories. " +
    "Helps identify bottlenecks and prioritize work.",
    Type.Object({}),
    (goalId) => `/internal/goals/${encodeURIComponent(goalId)}/critical-path`,
    opts,
  );
}

// ── Helper: extract agent ID from session key ───────────────────────────────

function resolveAgentIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  const parts = sessionKey.split(":");
  return parts.length >= 2 ? parts[1] : "";
}
