/**
 * Default IsolateEngine implementation using Node's built-in `node:vm` module.
 *
 * Trade-offs (documented so the future replacer knows what to preserve):
 *   ✓ Zero new dependencies — node:vm is built in
 *   ✓ codeGeneration: { strings: false, wasm: false } blocks eval/new Function
 *   ✓ vm.runInContext({ timeout }) bounds synchronous execution
 *   ✗ Shares heap with the parent process — no hard memory cap
 *   ✗ Cannot terminate async work once started (Promise.race leaves orphans)
 *
 * To migrate to isolated-vm or worker_threads later, implement IsolateEngine
 * in a new file under engines/ and register it via defaults.ts (or pass
 * explicitly to executeTool).
 */

import vm from "node:vm";

import type { IsolateEngine, IsolateRunOptions } from "../engine.js";

export function makeNodeVmEngine(): IsolateEngine {
  return {
    run: async (
      wrappedSource: string,
      contextObj: Record<string, unknown>,
      options: IsolateRunOptions,
    ): Promise<unknown> => {
      const context = vm.createContext(contextObj, {
        name: options.name,
        codeGeneration: { strings: false, wasm: false },
      });

      vm.runInContext(wrappedSource, context, {
        timeout: options.timeout_ms,
        breakOnSigint: true,
        displayErrors: true,
      });

      const resultPromise = contextObj.__tool_result as Promise<unknown> | undefined;
      if (!resultPromise || typeof resultPromise.then !== "function") {
        throw new Error("tool source did not define an async function execute(args)");
      }

      return await Promise.race([
        resultPromise,
        new Promise<never>((_, reject) =>
          setTimeout(
            () => reject(new Error(`tool timed out after ${options.timeout_ms}ms`)),
            options.timeout_ms,
          ).unref(),
        ),
      ]);
    },
  };
}
