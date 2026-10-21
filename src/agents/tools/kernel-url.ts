/**
 * WHERE THE KERNEL TOOL SURFACE ANSWERS — resolved, not configured.
 *
 * Every kernel tool on every gateway refused with
 *
 *   {"refused":"E_NO_KERNEL","detail":"KERNEL_ASSISTANT_URL is not set"}
 *
 * because nothing in this repository ever set that variable. It is read
 * in three places (the assistant tools, the GO tools, and the
 * legacy-collision switch in openclaw-tools.ts) and written in none:
 * infra/terraform/modules/openclaw-vm/startup.sh.tpl writes
 * /etc/openclaw/env and has no line for it, so a VM has never had one.
 *
 * It was a SECOND DECLARATION OF A FACT THE GATEWAY ALREADY HOLDS. The
 * kernel surface is a router on the control panel's own FastAPI app --
 * kernel/assistant/asgi.py mounts it at CLOUD_PREFIX = "/internal/kernel"
 * -- because Cloud Run gives a container one port and api.py's standalone
 * server on 8790 cannot deploy. So the address is the control panel's own
 * base URL plus a fixed prefix, and CONTROL_PANEL_URL is already in the
 * env, already refreshed by the refresh-control-panel-url timer whenever
 * the control panel moves. Deriving it means the two cannot drift and a
 * new environment has nothing extra to remember.
 *
 * KERNEL_ASSISTANT_URL still wins when set. E0 and local development
 * point it at 127.0.0.1:8000/internal/kernel, and an explicit value is
 * also the escape hatch if the surface ever moves off the control panel.
 */

/** The kernel surface's path on the control panel's app. Mirrors
 *  CLOUD_PREFIX in kernel/assistant/asgi.py -- the one place the two
 *  repositories have to agree. */
const KERNEL_PREFIX = "/internal/kernel";

function trimSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

/**
 * The base the kernel tools build `<base>/assistant/<goal>/<tool>` and
 * `<base>/go/<goal>/<tool>` on. Empty when neither variable is set,
 * which is what E_NO_KERNEL reports.
 */
export function resolveKernelUrl(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = trimSlash((env.KERNEL_ASSISTANT_URL || "").trim());
  if (explicit) return explicit;
  const panel = trimSlash((env.CONTROL_PANEL_URL || "").trim());
  return panel ? `${panel}${KERNEL_PREFIX}` : "";
}

/** True when the kernel surface is reachable — the switch that decides
 *  whether the kernel owns `cancel_story`, `register_resource` and
 *  `grant_resource` or the legacy control-panel tools keep them. Two
 *  tools with one name is a hard provider rejection on every turn, so
 *  this must answer from the SAME resolution the tools themselves use. */
export function kernelConfigured(env: NodeJS.ProcessEnv = process.env): boolean {
  return resolveKernelUrl(env) !== "";
}

/**
 * The credential the surface expects in `X-Assistant-Token`.
 *
 * Same shape as the URL: an explicit KERNEL_ASSISTANT_TOKEN wins, and
 * otherwise it is the gateway's own shared token — which the control
 * panel already trusts on every other /internal/* route, and which the
 * deployment hands the kernel surface as ASSISTANT_API_TOKEN from the
 * same secret. Deriving it means a new environment cannot come up with
 * the surface open because nobody created a second secret.
 *
 * FOLLOW-UP, deliberately not done here: a credential of its own. The
 * shared token is a skeleton key across /internal/*, and narrowing that
 * is the standing security work — this only stops the kernel surface
 * from being the one route with no key at all.
 */
export function resolveKernelToken(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = (env.KERNEL_ASSISTANT_TOKEN || "").trim();
  if (explicit) return explicit;
  return (env.OPENCLAW_GATEWAY_TOKEN || "").trim();
}

/**
 * Which kernel surface a tool call belongs on, decided by WHO IS CALLING.
 *
 * THE BUG THIS EXISTS FOR. `kernel-assistant-tools.ts` and
 * `kernel-go-tools.ts` both declare `ask_executor`, `steer`,
 * `story_progress`, `story_status` and `cancel_story` — deliberately,
 * because Phase C gave the GO the assistant's own verbs under "shared
 * FUNCTION, separate AUTHORITY", with `kernel/go/drivable.go_may_drive`
 * as the boundary. That reasoning holds inside the kernel and does not
 * survive the trip out here, because AT THE GATEWAY A TOOL'S IDENTITY
 * IS ITS NAME. `openclaw-tools.ts` spreads both sets into one flat
 * array with GO's second, so the GO's definition won every colliding
 * name and `ask_executor` meant `/go/<goal>/ask_executor` for EVERY
 * agent, assistants included.
 *
 * An assistant then called its own tool, landed on the GO surface, and
 * was refused with E_NOT_AUTONOMOUS — a message about the GO, which it
 * read as a rule about itself and wrote into its durable memory. The
 * allowlist was supposed to keep the two apart; an allowlist can grant
 * a NAME, it cannot choose between two things that share one.
 *
 * WHY EQUALITY AND NOT A `go-` PREFIX. The kernel already declares what
 * makes an agent a goal's orchestrator — `app/ledger/actors.py:go_actor`
 * resolves it as `lower(id) = lower('go-' || goal_id)`. Matching that
 * exactly keeps one definition of the fact; a prefix test would be a
 * second one, and would also claim any future agent merely named
 * `go-something`.
 *
 * CASE-INSENSITIVE because the gateway lowercases agent ids, so the GO
 * for goal `AGO01` has the id `go-ago01`.
 *
 * A NON-MATCH IS "assistant", which is the safe direction: the assistant
 * surface has no authority the caller does not already have, while
 * `/go/` is gated per call by `go_may_drive`. Getting this wrong routes
 * a GO to the assistant surface and loses a check; getting it wrong the
 * other way is what produced the bug above.
 */
export function kernelSurfaceFor(agentId: string, goalId: string): "assistant" | "go" {
  const a = (agentId ?? "").trim().toLowerCase();
  const g = (goalId ?? "").trim().toLowerCase();
  return a && g && a === `go-${g}` ? "go" : "assistant";
}

/**
 * A kernel write carries an idempotency key, whichever file declared it.
 *
 * PER STORY, NOT PER INTENT. The kernel's index is (actor, execution,
 * key) precisely because one key reused across three stories wrote ONE
 * proposal while the other two calls reported success having written
 * nothing. A model generating one key for "make all three handle nulls"
 * is being reasonable -- from where it stands that is one intent -- so
 * the story is folded in here rather than left to a prompt.
 *
 * WHY IT MOVED HERE. It lived in `kernel-assistant-tools.ts` and was
 * applied in that file's handler. `kernel-go-tools.ts` only DECLARED
 * `idempotency_key` as a parameter and left the model to supply one.
 * Those two files share five tool names, the gateway keys tools by
 * name, and GO's definitions win registration -- so once
 * `kernelSurfaceFor` started routing assistants to their own surface,
 * their writes arrived there with no key and the kernel refused them:
 *
 *     E_NO_IDEMPOTENCY_KEY -- a write must carry an idempotency key,
 *     the assistant holds no connection and cannot tell a lost reply
 *     from a lost send, so it will retry
 *
 * Fixing the route exposed this; it was always the same defect, hidden
 * behind a refusal that happened earlier. One behaviour for one tool
 * name, in one place, is the only shape that does not keep producing
 * these.
 *
 * A CALLER-SUPPLIED KEY ALWAYS WINS: the GO's spec asks the model for
 * one explicitly, and a model that has thought about idempotency knows
 * something this function cannot.
 */
export function withIdempotencyKey(
  args: Record<string, unknown>,
): Record<string, unknown> {
  if (args.idempotency_key) return args;
  const story = args.story ?? args.question_id ?? "unaddressed";
  const stamp = Math.floor(Date.now() / 1000);
  return { ...args, idempotency_key: `t-${story}-${stamp}` };
}
