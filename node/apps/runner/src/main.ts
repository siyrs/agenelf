import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { NodeRunner } from "../../../packages/core/src/node-runner.ts";

export async function runLoop(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const runner = new NodeRunner(resolve(root));
  const interval = Math.max(100, Number(process.env.AGENELF_NODE_RUNNER_INTERVAL_MS || 1000));
  console.log(`Agenelf Node Runner started; interval=${interval}ms`);
  while (true) {
    try { await runner.processOnce(); }
    catch (error) { console.error(error); }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, interval));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) runLoop().catch((error) => { console.error(error); process.exitCode = 1; });
