import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { atomicWriteJson } from "../../../packages/core/src/fs-store.ts";
import { ValidationRunner } from "../../../packages/core/src/validation.ts";

function intervalMs(): number {
  const parsed = Number(process.env.AGENELF_VALIDATION_INTERVAL_MS ?? 1_000);
  return Math.max(200, Math.min(Number.isFinite(parsed) ? parsed : 1_000, 60_000));
}

async function heartbeat(root: string, counts: Record<string, number>, status = "ok"): Promise<void> {
  await atomicWriteJson(resolve(root, "data", "runner-health", "validation-runner.json"), {
    schema_version: 1,
    name: "validation-runner",
    runtime: "node-typescript",
    status,
    pid: process.pid,
    updated_at: new Date().toISOString(),
    counts
  });
}

export async function runLoop(root = process.env.AGENELF_ROOT || process.cwd()): Promise<void> {
  const resolvedRoot = resolve(root);
  const runner = new ValidationRunner(resolvedRoot, process.env.AGENELF_VALIDATION_FILE);
  await runner.initialize();
  while (true) {
    try {
      await heartbeat(resolvedRoot, await runner.processOnce());
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      await heartbeat(resolvedRoot, { failed: 1 }, message.slice(0, 1_000));
      console.error(message);
    }
    await new Promise((done) => setTimeout(done, intervalMs()));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  runLoop().catch((error) => { console.error(error); process.exitCode = 1; });
}
