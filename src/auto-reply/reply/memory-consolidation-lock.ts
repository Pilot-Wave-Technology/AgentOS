/**
 * One consolidation at a time per workspace.
 *
 * WHAT IS SHARED AND WHAT IS NOT. An agent has ONE workspace --
 * `resolveAgentWorkspaceDir` takes an agent id and no session -- and
 * therefore one `MEMORY.md`. It has as many SESSIONS as it has threads,
 * because the session key is `agent:{agent}:{goal}-{thread}`. So N
 * threads share one memory file through N independent turn loops.
 *
 * THE TWO WRITERS ARE NOT ALIKE, which is why this guards only one of
 * them. A memory FLUSH is append-only: it runs with
 * `trigger: "memory"` and a `memoryFlushWritePath`, and `pi-tools`
 * wraps its writes as appends. Two concurrent appends interleave into
 * one file and both survive. A CONSOLIDATION runs with
 * `trigger: "memory_consolidation"` and deliberately REWRITES the whole
 * file to bring it back under the cap. Two of those racing means one
 * agent reads the file, a second rewrites it, and the first writes back
 * a full document built from what it read -- silently reverting
 * everything the second one kept.
 *
 * WHY THE EXISTING COOLDOWN DOES NOT COVER IT.
 * `STANDALONE_CONSOLIDATION_MIN_INTERVAL_MS` is stored on the SESSION
 * entry, so it spaces out consolidations within one thread and says
 * nothing about two threads firing in the same second. That was
 * adequate while a multi-thread agent was rare; the Goal Orchestrator
 * now holds a thread per live autonomous errand, so it is the ordinary
 * case rather than the unlucky one.
 *
 * TRY-ACQUIRE, NOT A QUEUE. A waiter would rewrite the file again
 * immediately after the holder finished -- from a transcript it read
 * before the holder's rewrite existed -- which is the same lost update
 * one step later. Failing to acquire means "somebody is already doing
 * exactly this job"; the right response is to skip the turn entirely.
 * The size trigger is level-triggered, so if the file is still over cap
 * when the holder is done, the next turn fires another attempt.
 *
 * IN-PROCESS IS THE RIGHT SCOPE. The gateway is one node process per
 * host and the workspace is on that host's disk, so every writer to a
 * given `MEMORY.md` is inside this module's memory. A lock file would
 * add a crash-recovery problem to solve a problem that does not exist.
 *
 * NEVER STRANDS THE LOCK. The release is handed back as a function the
 * caller MUST run from a `finally`; `withConsolidationLock` exists so
 * that callers do not have to remember. A stranded entry would disable
 * consolidation for that agent until the gateway restarted, and the
 * symptom -- a MEMORY.md that grows for ever and never consolidates --
 * looks nothing like a lock bug.
 */

const holders = new Set<string>();

/**
 * Take the lock for `workspaceDir`, or return null if it is held.
 *
 * An undefined `workspaceDir` acquires nothing and succeeds: callers
 * that could not resolve a workspace are not consolidating a shared
 * file, and refusing them would turn "I don't know where this is" into
 * "this never runs".
 */
export function acquireConsolidationLock(
  workspaceDir: string | undefined,
): (() => void) | null {
  if (!workspaceDir) {
    return () => {};
  }
  if (holders.has(workspaceDir)) {
    return null;
  }
  holders.add(workspaceDir);
  let released = false;
  return () => {
    // Idempotent: a double release from a retry path must not free a
    // lock a LATER consolidation has since taken.
    if (released) return;
    released = true;
    holders.delete(workspaceDir);
  };
}

/** True while some turn is consolidating this workspace. Tests and logs. */
export function isConsolidating(workspaceDir: string): boolean {
  return holders.has(workspaceDir);
}

/**
 * Run `fn` holding the lock, or return `skipped` without running it.
 *
 * The two outcomes are distinguishable on purpose: a caller that cannot
 * tell "ran" from "skipped" will report a consolidation that never
 * happened, and the session bookkeeping downstream would mark the cycle
 * done.
 */
export async function withConsolidationLock<T>(
  workspaceDir: string | undefined,
  fn: () => Promise<T>,
): Promise<{ ran: true; value: T } | { ran: false; value?: undefined }> {
  const release = acquireConsolidationLock(workspaceDir);
  if (!release) {
    return { ran: false };
  }
  try {
    return { ran: true, value: await fn() };
  } finally {
    release();
  }
}

/** Test-only: forget every holder. Never call this from product code. */
export function __resetConsolidationLocks(): void {
  holders.clear();
}
