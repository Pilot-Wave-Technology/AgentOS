/**
 * IsolateEngine — the abstraction over "execute JavaScript in a sandboxed context".
 *
 * Today's implementation is node:vm (engines/node-vm.ts). Tomorrow's could be
 * isolated-vm (true V8 isolates with hard heap limits + thread isolation),
 * worker_threads (separate event loops, terminable), or wasmtime (WASM modules).
 *
 * The contract is intentionally narrow:
 *   - Accept a wrapped source string and a context-globals object.
 *   - Inject the globals into the sandbox.
 *   - Run the source (which leaves a Promise on `contextObj.__tool_result`).
 *   - Return that promise's eventual value, honoring the timeout.
 *
 * Engines must enforce code-generation restrictions (no eval, no new Function,
 * no dynamic WASM) so a tool cannot reintroduce capabilities via runtime
 * code generation.
 */

export interface IsolateRunOptions {
  /** Wall-clock timeout, applied to both sync compilation and the async result. */
  timeout_ms: number;
  /** Human-readable label for diagnostics (often `tool:<name>`). */
  name: string;
}

export interface IsolateEngine {
  /**
   * Execute `wrappedSource` against `contextObj` as the only globals.
   * Returns whatever `contextObj.__tool_result` resolves to.
   */
  run: (
    wrappedSource: string,
    contextObj: Record<string, unknown>,
    options: IsolateRunOptions,
  ) => Promise<unknown>;
}
