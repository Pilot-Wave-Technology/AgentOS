import type { OpenClawConfig } from "../config/config.js";
import { resolvePluginTools } from "../plugins/tools.js";
import { getActiveRuntimeWebToolsMetadata } from "../secrets/runtime.js";
import type { GatewayMessageChannel } from "../utils/message-channel.js";
import { resolveSessionAgentId, resolveAgentConfig } from "./agent-scope.js";
import type { SandboxFsBridge } from "./sandbox/fs-bridge.js";
import type { SpawnedToolContext } from "./spawned-context.js";
import type { ToolFsPolicy } from "./tool-fs-policy.js";
import { createAgentsListTool } from "./tools/agents-list-tool.js";
import { createBrowserTool } from "./tools/browser-tool.js";
import { createCanvasTool } from "./tools/canvas-tool.js";
import type { AnyAgentTool } from "./tools/common.js";
import { createCronTool } from "./tools/cron-tool.js";
import { createGatewayTool } from "./tools/gateway-tool.js";
import { createImageTool } from "./tools/image-tool.js";
import { createMessageTool } from "./tools/message-tool.js";
import { createNodesTool } from "./tools/nodes-tool.js";
import { createPdfTool } from "./tools/pdf-tool.js";
import { createSessionStatusTool } from "./tools/session-status-tool.js";
import { createSessionsHistoryTool } from "./tools/sessions-history-tool.js";
import { createSessionsListTool } from "./tools/sessions-list-tool.js";
import { createSessionsSendTool } from "./tools/sessions-send-tool.js";
import { createSessionsSpawnTool } from "./tools/sessions-spawn-tool.js";
import { createClaudeCodeCommunicationTool } from "./tools/claude-code-communication-tool.js";
import { createSubagentsTool } from "./tools/subagents-tool.js";
import { createTaskTool, createTaskStatusTool, createTaskActivityTool, createMessageAssistantTool } from "./tools/task-management-tools.js";
import { createTaskReportTool } from "./tools/task-report-tool.js";
import { createSubmitReviewTool } from "./tools/review-tools.js";
import { createSubmitSecurityReviewTool } from "./tools/security-tools.js";
import { createResourceSummaryTool, createResourceScanTool, createResourceReadTool, createResourceListTool } from "./tools/resource-tools.js";
import {
  storyListTool, storyDetailTool, storyUpdateTool, storyCreateTool, storyModifyTool, storyCancelTool,
  messageGoTool, replyAssistantTool, createBranchTool, registerResourceTool,
  grantResourceTool, revokeResourceTool, listAllocationsTool,
  recordDecisionTool, listDecisionsTool, criticalPathTool,
} from "./tools/story-tools.js";
import { kernelAssistantTools } from "./tools/kernel-assistant-tools.js";
import { kernelGoTools } from "./tools/kernel-go-tools.js";
import { kernelConfigured } from "./tools/kernel-url.js";
import {
  storyPhaseTool, storyStateTool, snapshotCheckpointTool, planVersionTool,
  escalateForApprovalTool, workspaceFileTool, gitTool, worktreeTool, sessionControlTool,
} from "./tools/shca-tools.js";
// memory-acp-tools imports removed — SHCA + GO now use the native
// memory-core plugin (memory-tool.ts) for memory_search / memory_get,
// which gives vector + BM25 hybrid search over MEMORY.md + memory/*.md
// directly from the workspace. Writes go through Write/Edit on the
// agent's workspace files; structured event writes go through the
// agentos memory_v2 event_bridge via memory.append RPC.
import { createTtsTool } from "./tools/tts-tool.js";
import { createWebFetchTool, createWebSearchTool } from "./tools/web-tools.js";
import { resolveWorkspaceRoot } from "./workspace-dir.js";

/**
 * `cancel_story` and `grant_resource` exist TWICE in this registry.
 *
 * The legacy story surface declares both, and so does the kernel — the
 * same two words for two different backends. Nothing downstream
 * deduplicates: the allowlist filter returns an array, so an agent
 * permitted `cancel_story` sends the provider two functions with one
 * name, which is a hard rejection on every turn rather than a subtly
 * wrong call. It is not a collision anybody would see by reading either
 * file, because neither file is wrong.
 *
 * Where the kernel is configured it OWNS these two names, and the
 * legacy pair is dropped. Where it is not, nothing changes and the
 * legacy surface is untouched — which is what makes this safe to land
 * before the old surface is retired.
 *
 * This shadows real behaviour, so it is a deliberate switch and not a
 * dedupe-by-position: last-wins would silently repoint the legacy
 * agents, and first-wins would silently break the kernel ones.
 */
const KERNEL_OWNED_NAMES = new Set([
  "cancel_story",
  // register_ and grant_ must resolve to the SAME catalogue. GO had
  // the control panel's register and the kernel's grant sitting side
  // by side describing one workflow, so registering then granting
  // returned E_NO_SUCH_RESOURCE -- the grant looked in the kernel
  // catalogue and the register had written the other one.
  "register_resource",
  "grant_resource",
]);

function kernelSupersedes(tools: AnyAgentTool[]): AnyAgentTool[] {
  // The SAME resolution the kernel tools use. Asking a different
  // question here than build() asks is how one name ends up declared
  // twice -- a hard provider rejection on every turn.
  if (!kernelConfigured()) return tools;
  const seen = new Set<string>();
  // Walk from the END so the kernel's copy — registered last — is the
  // one kept, and drop the earlier legacy twin.
  const kept: AnyAgentTool[] = [];
  for (let i = tools.length - 1; i >= 0; i--) {
    const tool = tools[i];
    if (KERNEL_OWNED_NAMES.has(tool.name)) {
      if (seen.has(tool.name)) continue;
      seen.add(tool.name);
    }
    kept.push(tool);
  }
  return kept.reverse();
}

export function createOpenClawTools(
  options?: {
    sandboxBrowserBridgeUrl?: string;
    allowHostBrowserControl?: boolean;
    agentSessionKey?: string;
    agentChannel?: GatewayMessageChannel;
    agentAccountId?: string;
    /** Delivery target (e.g. telegram:group:123:topic:456) for topic/thread routing. */
    agentTo?: string;
    /** Thread/topic identifier for routing replies to the originating thread. */
    agentThreadId?: string | number;
    agentDir?: string;
    sandboxRoot?: string;
    sandboxFsBridge?: SandboxFsBridge;
    fsPolicy?: ToolFsPolicy;
    sandboxed?: boolean;
    config?: OpenClawConfig;
    pluginToolAllowlist?: string[];
    /** Current channel ID for auto-threading (Slack). */
    currentChannelId?: string;
    /** Current thread timestamp for auto-threading (Slack). */
    currentThreadTs?: string;
    /** Current inbound message id for action fallbacks (e.g. Telegram react). */
    currentMessageId?: string | number;
    /** Reply-to mode for Slack auto-threading. */
    replyToMode?: "off" | "first" | "all";
    /** Mutable ref to track if a reply was sent (for "first" mode). */
    hasRepliedRef?: { value: boolean };
    /** If true, the model has native vision capability */
    modelHasVision?: boolean;
    /** If true, nodes action="invoke" can call media-returning commands directly. */
    allowMediaInvokeCommands?: boolean;
    /** Explicit agent ID override for cron/hook sessions. */
    requesterAgentIdOverride?: string;
    /** Require explicit message targets (no implicit last-route sends). */
    requireExplicitMessageTarget?: boolean;
    /** If true, omit the message tool from the tool list. */
    disableMessageTool?: boolean;
    /** Trusted sender id from inbound context (not tool args). */
    requesterSenderId?: string | null;
    /** Whether the requesting sender is an owner. */
    senderIsOwner?: boolean;
    /** Ephemeral session UUID — regenerated on /new and /reset. */
    sessionId?: string;
    /**
     * Workspace directory to pass to spawned subagents for inheritance.
     * Defaults to workspaceDir. Use this to pass the actual agent workspace when the
     * session itself is running in a copied-workspace sandbox (`ro` or `none`) so
     * subagents inherit the real workspace path instead of the sandbox copy.
     */
    spawnWorkspaceDir?: string;
  } & SpawnedToolContext,
): AnyAgentTool[] {
  const workspaceDir = resolveWorkspaceRoot(options?.workspaceDir);
  const spawnWorkspaceDir = resolveWorkspaceRoot(
    options?.spawnWorkspaceDir ?? options?.workspaceDir,
  );
  const runtimeWebTools = getActiveRuntimeWebToolsMetadata();
  const imageTool = options?.agentDir?.trim()
    ? createImageTool({
        config: options?.config,
        agentDir: options.agentDir,
        workspaceDir,
        sandbox:
          options?.sandboxRoot && options?.sandboxFsBridge
            ? { root: options.sandboxRoot, bridge: options.sandboxFsBridge }
            : undefined,
        fsPolicy: options?.fsPolicy,
        modelHasVision: options?.modelHasVision,
      })
    : null;
  const pdfTool = options?.agentDir?.trim()
    ? createPdfTool({
        config: options?.config,
        agentDir: options.agentDir,
        workspaceDir,
        sandbox:
          options?.sandboxRoot && options?.sandboxFsBridge
            ? { root: options.sandboxRoot, bridge: options.sandboxFsBridge }
            : undefined,
        fsPolicy: options?.fsPolicy,
      })
    : null;
  const webSearchTool = createWebSearchTool({
    config: options?.config,
    sandboxed: options?.sandboxed,
    runtimeWebSearch: runtimeWebTools?.search,
  });
  const webFetchTool = createWebFetchTool({
    config: options?.config,
    sandboxed: options?.sandboxed,
    runtimeFirecrawl: runtimeWebTools?.fetch.firecrawl,
  });
  const messageTool = options?.disableMessageTool
    ? null
    : createMessageTool({
        agentAccountId: options?.agentAccountId,
        agentSessionKey: options?.agentSessionKey,
        config: options?.config,
        currentChannelId: options?.currentChannelId,
        currentChannelProvider: options?.agentChannel,
        currentThreadTs: options?.currentThreadTs,
        currentMessageId: options?.currentMessageId,
        replyToMode: options?.replyToMode,
        hasRepliedRef: options?.hasRepliedRef,
        sandboxRoot: options?.sandboxRoot,
        requireExplicitTarget: options?.requireExplicitMessageTarget,
        requesterSenderId: options?.requesterSenderId ?? undefined,
      });
  const tools: AnyAgentTool[] = [
    createBrowserTool({
      sandboxBridgeUrl: options?.sandboxBrowserBridgeUrl,
      allowHostControl: options?.allowHostBrowserControl,
      agentSessionKey: options?.agentSessionKey,
    }),
    createCanvasTool({ config: options?.config }),
    createNodesTool({
      agentSessionKey: options?.agentSessionKey,
      agentChannel: options?.agentChannel,
      agentAccountId: options?.agentAccountId,
      currentChannelId: options?.currentChannelId,
      currentThreadTs: options?.currentThreadTs,
      config: options?.config,
      modelHasVision: options?.modelHasVision,
      allowMediaInvokeCommands: options?.allowMediaInvokeCommands,
    }),
    createCronTool({
      agentSessionKey: options?.agentSessionKey,
    }),
    ...(messageTool ? [messageTool] : []),
    createTtsTool({
      agentChannel: options?.agentChannel,
      config: options?.config,
    }),
    createGatewayTool({
      agentSessionKey: options?.agentSessionKey,
      config: options?.config,
    }),
    createAgentsListTool({
      agentSessionKey: options?.agentSessionKey,
      requesterAgentIdOverride: options?.requesterAgentIdOverride,
    }),
    createTaskReportTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createTaskTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createTaskStatusTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createTaskActivityTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createMessageAssistantTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createSubmitReviewTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createSubmitSecurityReviewTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createResourceSummaryTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createResourceScanTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createResourceReadTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createResourceListTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyListTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyDetailTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyUpdateTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyCreateTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyModifyTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    storyCancelTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    messageGoTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    replyAssistantTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    createBranchTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    registerResourceTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    grantResourceTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    revokeResourceTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    listAllocationsTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    recordDecisionTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    listDecisionsTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    criticalPathTool({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    // ── H10b: the assistant's eight, pointed at the kernel ──────────────
    //
    // Registered alongside the old surface rather than replacing it, so
    // the two can coexist while assistants are migrated. What decides
    // which an agent gets is its ALLOWLIST, not what is registered here
    // -- and the new assistant template lists exactly these eight.
    ...kernelAssistantTools({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    // GO's three, on their own kernel surface. Registered here like
    // everything else; which agent GETS them is decided by its
    // allowlist, and the assistant template does not list them.
    ...kernelGoTools({
      config: options?.config,
      agentSessionKey: options?.agentSessionKey,
    }),
    // ── SHCA operator tools (Phase 2 of SHCA refactor) ──────────────────
    storyPhaseTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    storyStateTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    snapshotCheckpointTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    planVersionTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    escalateForApprovalTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    workspaceFileTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    gitTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    worktreeTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    sessionControlTool({ config: options?.config, agentSessionKey: options?.agentSessionKey }),
    createClaudeCodeCommunicationTool({
      agentSessionKey: options?.agentSessionKey,
      agentChannel: options?.agentChannel,
    }),
    createSessionsListTool({
      agentSessionKey: options?.agentSessionKey,
      sandboxed: options?.sandboxed,
    }),
    createSessionsHistoryTool({
      agentSessionKey: options?.agentSessionKey,
      sandboxed: options?.sandboxed,
    }),
    createSessionsSendTool({
      agentSessionKey: options?.agentSessionKey,
      agentChannel: options?.agentChannel,
      sandboxed: options?.sandboxed,
    }),
    createSessionsSpawnTool({
      agentSessionKey: options?.agentSessionKey,
      agentChannel: options?.agentChannel,
      agentAccountId: options?.agentAccountId,
      agentTo: options?.agentTo,
      agentThreadId: options?.agentThreadId,
      agentGroupId: options?.agentGroupId,
      agentGroupChannel: options?.agentGroupChannel,
      agentGroupSpace: options?.agentGroupSpace,
      sandboxed: options?.sandboxed,
      requesterAgentIdOverride: options?.requesterAgentIdOverride,
      workspaceDir: spawnWorkspaceDir,
    }),
    createSubagentsTool({
      agentSessionKey: options?.agentSessionKey,
    }),
    createSessionStatusTool({
      agentSessionKey: options?.agentSessionKey,
      config: options?.config,
      sandboxed: options?.sandboxed,
    }),
    ...(webSearchTool ? [webSearchTool] : []),
    ...(webFetchTool ? [webFetchTool] : []),
    ...(imageTool ? [imageTool] : []),
    ...(pdfTool ? [pdfTool] : []),
  ];

  // memory_search / memory_get / memory_save: native memory-core plugin
  // owns these names now (registered via resolvePluginTools below). The
  // SHCA-specific ACP wrappers that used to shadow them have been
  // removed — they pointed at the legacy PG-backed memory system that
  // memory_v2 replaces. Agents now get vector+BM25 hybrid search over
  // MEMORY.md + memory/*.md directly from the workspace.

  const pluginTools = resolvePluginTools({
    context: {
      config: options?.config,
      workspaceDir,
      agentDir: options?.agentDir,
      agentId: resolveSessionAgentId({
        sessionKey: options?.agentSessionKey,
        config: options?.config,
      }),
      sessionKey: options?.agentSessionKey,
      sessionId: options?.sessionId,
      messageChannel: options?.agentChannel,
      agentAccountId: options?.agentAccountId,
      requesterSenderId: options?.requesterSenderId ?? undefined,
      senderIsOwner: options?.senderIsOwner ?? undefined,
      sandboxed: options?.sandboxed,
    },
    existingToolNames: new Set(tools.map((tool) => tool.name)),
    toolAllowlist: options?.pluginToolAllowlist,
  });

  // Applied to the WHOLE list, plugins included: a plugin that
  // declares one of these names collides exactly as the legacy
  // surface does.
  return kernelSupersedes([...tools, ...pluginTools]);
}
