import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { OperationQueue } from "../packages/core/src/operation-queue.ts";
import { NodeRunner } from "../packages/core/src/node-runner.ts";

async function tempRoot() { return mkdtemp(join(tmpdir(), "agenelf-runner-test-")); }

test("operation queue emits Python-compatible immutable request", async () => {
  const root = await tempRoot();
  const queue = new OperationQueue(root);
  const first = await queue.submit({ capability: "server.operations", operation: "inspect", target: "demo", risk: "read", summary: "inspect", parameters: { detail: true } });
  const second = await queue.submit({ capability: "server.operations", operation: "inspect", target: "demo", risk: "read", summary: "inspect", parameters: { detail: true } });
  assert.match(first.id, /^op-[0-9a-f]{16}$/);
  assert.equal(second.id, first.id);
  assert.equal(second.reused_existing, true);
  assert.equal((await queue.get(first.id)).status, "queued");
});

test("node runner processes runtime info and allowlisted command", async () => {
  const root = await tempRoot();
  await mkdir(join(root, "local"), { recursive: true });
  await writeFile(join(root, "local", "node-runner.json"), JSON.stringify({ commands: { nodeVersion: [process.execPath, "--version"] } }));
  const runner = new NodeRunner(root);
  const runtime = await runner.submit("runtime_info");
  const command = await runner.submit("allowlisted_command", { alias: "nodeVersion" });
  assert.equal(await runner.processOnce(), 2);
  const runtimeResult = JSON.parse(await readFile(join(root, "data", "node-runner-results", `${runtime.id}.json`), "utf8"));
  const commandResult = JSON.parse(await readFile(join(root, "data", "node-runner-results", `${command.id}.json`), "utf8"));
  assert.equal(runtimeResult.status, "succeeded");
  assert.match(commandResult.output.stdout, /^v\d+/);
});
