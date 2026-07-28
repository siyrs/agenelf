import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AgenelfAgent } from "../packages/core/src/agent.ts";
import { AutonomyCycleStore } from "../packages/core/src/autonomy-cycles.ts";
import { SelfOptimizationStore } from "../packages/core/src/self-optimization.ts";
import { TaskStore } from "../packages/core/src/task-store.ts";

async function rootFixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-growth-runtime-"));
  await mkdir(join(root, "local", "self"), { recursive: true });
  await mkdir(join(root, "local", "memory"), { recursive: true });
  await mkdir(join(root, "data", "node-tasks"), { recursive: true });
  await mkdir(join(root, "data", "autonomy-cycles"), { recursive: true });
  await mkdir(join(root, "data", "autonomy-events"), { recursive: true });
  await mkdir(join(root, "data", "runner-health"), { recursive: true });
  return root;
}

test("optimization enforces whitelist, bounds, history and rollback", async () => {
  const root = await rootFixture();
  const store = new SelfOptimizationStore(root, { cooldownSeconds: 0 });
  await assert.rejects(store.apply("unsafe.shell", 1, "bad"), /白名单/);
  await assert.rejects(store.apply("llm.temperature", 2, "bad"), /范围/);
  const applied = await store.apply("llm.temperature", 0.2, "reduce variance", ["validation failed twice"]);
  assert.equal(applied.applied, true);
  assert.equal(await store.effective("llm.temperature"), 0.2);
  const status = await store.status();
  assert.equal((status.parameters as Record<string, { effective: number }>)["llm.temperature"].effective, 0.2);
  assert.equal((status.history as unknown[]).length, 1);
  const rollback = await store.rollback("llm.temperature");
  assert.equal(rollback.rolled_back, true);
  assert.equal(await store.effective("llm.temperature"), 0.6);
  const persisted = JSON.parse(await readFile(join(root, "local", "self", "optimizations.json"), "utf8"));
  assert.equal(persisted.consciousness_claim, false);
  assert.deepEqual(persisted.active, {});
  assert.equal(persisted.history.length, 2);
});

test("Agent applies validated optimization values to memory prompt and model request config", async () => {
  const root = await rootFixture();
  const store = new SelfOptimizationStore(root, { cooldownSeconds: 0 });
  await store.apply("agent.memory_prompt_limit", 10, "bounded memory");
  await store.apply("agent.memory_prompt_max_chars", 2000, "bounded prompt");
  await store.apply("llm.temperature", 0.1, "deterministic maintenance");
  const agent = new AgenelfAgent(root);
  await agent.initialize();
  await agent.memory.add("fact", "Node optimization fixture");
  const reply = await agent.chat("hello", { sessionId: "optimized" });
  assert.match(reply, /Node Runtime/);
  assert.equal(agent.model.config.temperature, 0.1);
  const status = await agent.status();
  assert.equal(((status.optimization as Record<string, unknown>).active as Record<string, { value: number }>)["llm.temperature"].value, 0.1);
});

test("Pi autonomy plan is evidence-backed and plan-only by default", async () => {
  const root = await rootFixture();
  const store = new AutonomyCycleStore(root, new TaskStore(root));
  const snapshot = {
    observed_at: new Date().toISOString(),
    validation: { ready: true },
    compatibility: { legacy_api: false },
    runner_health: { validation: { status: "ok" } },
    capabilities: [], resources: [], prompts: []
  };
  const cycle = await store.create({ goal: "Improve Node docs", applyChanges: false, snapshot });
  assert.equal(cycle.status, "plan_ready");
  assert.equal(cycle.linked_task_id, null);
  const persisted = JSON.parse(await readFile(join(root, "data", "autonomy-cycles", `${cycle.id}.json`), "utf8"));
  assert.equal(persisted.apply_changes, false);
  const events = (await readFile(join(root, "data", "autonomy-events", `${cycle.id}.jsonl`), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(events.map((event) => event.type), ["autonomy.snapshot.created", "autonomy.assessment.completed", "autonomy.plan.created", "autonomy.plan.ready"]);
});

test("Autonomy apply_changes creates a Node Task and requires owner-authorized Self-upgrade", async () => {
  const root = await rootFixture();
  await writeFile(join(root, "protected-source.txt"), "unchanged\n");
  const tasks = new TaskStore(root);
  const store = new AutonomyCycleStore(root, tasks);
  const cycle = await store.create({
    goal: "Eliminate legacy runtime",
    applyChanges: true,
    snapshot: { observed_at: new Date().toISOString(), validation: { ready: true }, compatibility: { legacy_api: true }, runner_health: {} },
    intention: { id: "intent-fixture", status: "proposed" }
  });
  assert.equal(cycle.status, "awaiting_owner_authorized_upgrade");
  assert.match(String(cycle.linked_task_id), /^ntask-/);
  assert.match(String(cycle.next_action), /主人终端/);
  assert.equal(await readFile(join(root, "protected-source.txt"), "utf8"), "unchanged\n");
  const task = await tasks.get(String(cycle.linked_task_id));
  assert.match(task.title, /Autonomy/);
  const events = await readFile(join(root, "data", "autonomy-events", `${cycle.id}.jsonl`), "utf8");
  assert.match(events, /autonomy\.owner_authorization\.required/);
  assert.doesNotMatch(events, /git push|docker\.sock|local\/secrets/);
});
