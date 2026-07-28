import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { atomicWriteJson } from "../../../packages/core/src/fs-store.ts";
import { ReadOnlyOpsRunner } from "../../../packages/core/src/read-ops.ts";

function intervalMs(): number {
  const value = Number(process.env.AGENELF_READ_OPS_INTERVAL_MS ?? 1_000);
  return Math.max(200, Math.min(Number.isFinite(value) ? value : 1_000, 60_000));
}

async function heartbeat(root: string, counts: Record<string, number>, status = "ok"): Promise<void> {
  await atomicWriteJson(resolve(root, "data", "runner-health", "read-ops-runner.json"), {
    schema_version: 1,
    name: "read-ops-runner",
    runtime: "node-typescript",
    status,
    pid: process.pid,
    updated_at: new Date().toISOString(),
    counts
  });
}

export async function runOnce(root = process.env.AGENELF_ROOT || process.cwd()): Promise<Record<string, number>> {
  const resolved = resolve(root);
  const runner = new ReadOnlyOpsRunner(resolved);
  await runner.initialize();
  try {
    const counts = await runner.processOnce();
    await heartbeat(resolved, counts);
    return counts;
  } catch (error) {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    await heartbeat(resolved, { failed: 1 }, message.slice(0, 1_000));
    throw error;
  }
}

export async function runLoop(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const resolved = resolve(root);
  const runner = new ReadOnlyOpsRunner(resolved);
  await runner.initialize();
  while (true) {
    try {
      const counts = await runner.processOnce();
      await heartbeat(resolved, counts);
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      await heartbeat(resolved, { failed: 1 }, message.slice(0, 1_000));
      console.error(message);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, intervalMs()));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const once = process.argv.includes("--once");
  const execution = once ? runOnce().then((counts) => console.log(JSON.stringify(counts))) : runLoop();
  execution.catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
