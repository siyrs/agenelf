import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAgenelfServer } from "../apps/api/src/main.ts";
import { OperationQueue } from "../packages/core/src/operation-queue.ts";

const token = "native-compat-token";
const auth = { "x-agenelf-token": token };
const jsonHeaders = { ...auth, "content-type": "application/json" };

async function setup() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-native-compat-api-"));
  await mkdir(join(root, "web"), { recursive: true });
  await mkdir(join(root, "local"), { recursive: true });
  await mkdir(join(root, "data", "auth-requests"), { recursive: true });
  await writeFile(join(root, "web", "index.html"), "<html>node-native</html>");
  await writeFile(join(root, "local", "profile.yaml"), "owner:\n  display_name: Sirius\n", "utf8");
  await writeFile(join(root, "local", "preferences.yaml"), "response:\n  language: zh-CN\n", "utf8");
  await writeFile(join(root, "local", "models.yaml"), "default:\n  model: mock\n", "utf8");
  await writeFile(join(root, "local", "repositories.yaml"), `schema_version: 1\nrepositories:\n  demo:\n    source_dir: demo\n    description: Demo repository\n    language: python\n    default_test_profile: python-unittest\n    allowed_test_profiles: [python-unittest]\n    protected_paths: [policy/]\n    max_patch_files: 10\n    max_patch_bytes: 262144\ntest_profiles:\n  python-unittest:\n    commands:\n      - [python, -m, unittest, discover, -s, tests, -v]\n    timeout_seconds: 120\n`, "utf8");
  const server = await createAgenelfServer({ root });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing address");
  return { root, server, base: `http://127.0.0.1:${address.port}` };
}

async function withToken<T>(action: () => Promise<T>): Promise<T> {
  const previous = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = token;
  try { return await action(); }
  finally { if (previous === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previous; }
}

test("memory, local context and self-development routes are Node-native", async () => withToken(async () => {
  const { root, server, base } = await setup();
  try {
    const local = await (await fetch(`${base}/local/status`, { headers: auth })).json();
    assert.equal(local.profile_loaded, true);
    assert.equal(local.preferences_loaded, true);
    assert.equal(local.credentials_exposed, false);

    const remember = await fetch(`${base}/memory`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ kind: "preference", content: "喜欢可审计的 Node 架构" }) });
    assert.equal(remember.status, 200);
    assert.match((await remember.json()).id, /^mem-/);
    const recall = await (await fetch(`${base}/memory/search?q=Node&limit=5`, { headers: auth })).json();
    assert.equal(recall.results.length, 1);

    const created = await (await fetch(`${base}/self/intentions`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ title: "移除 legacy API", rationale: "完成 Node 迁移", priority: "P1", acceptance_criteria: ["无自动 Python 代理"] }) })).json();
    assert.equal(created.created, true);
    assert.match(created.intention.id, /^intent-/);
    const pursued = await (await fetch(`${base}/self/intentions/${created.intention.id}/pursue`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ apply_changes: true }) })).json();
    assert.match(pursued.task.id, /^ntask-/);
    assert.equal(pursued.apply_changes_requested, true);
    assert.match(pursued.next_action, /主人授权 Self-upgrade/);

    const reflection = await (await fetch(`${base}/self/reflections`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ note: "Node 原生 API 第一批完成", deep: false }) })).json();
    assert.match(reflection.id, /^reflection-/);
    const development = await (await fetch(`${base}/self/development`, { headers: auth })).json();
    assert.equal(development.reflection_count, 1);
    assert.equal(development.intention_count, 1);
    assert.ok(await readFile(join(root, "local", "self", "intentions.json"), "utf8"));
  } finally { server.close(); }
}));

test("tasks, approvals and operations use existing file protocols without legacy", async () => withToken(async () => {
  const { root, server, base } = await setup();
  try {
    const operation = await new OperationQueue(root).submit({ capability: "server.operations", operation: "service_restart", target: "primary", parameters: { service: "nginx" }, risk: "change", summary: "restart nginx" });
    const approvals = await (await fetch(`${base}/approvals`, { headers: auth })).json();
    assert.equal(approvals.pending.some((item: { operation_id: string }) => item.operation_id === operation.id), true);
    const state = await (await fetch(`${base}/operations/${operation.id}`, { headers: auth })).json();
    assert.equal(state.status, "awaiting_approval");

    const intention = await (await fetch(`${base}/self/intentions`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ title: "task compatibility", rationale: "test", priority: "P2" }) })).json();
    await fetch(`${base}/self/intentions/${intention.intention.id}/pursue`, { method: "POST", headers: jsonHeaders, body: "{}" });
    const tasks = await (await fetch(`${base}/tasks`, { headers: auth })).json();
    assert.equal(tasks.tasks.some((item: { source: string }) => item.source === "node"), true);
    const taskId = tasks.tasks.find((item: { source: string }) => item.source === "node").id;
    const detail = await (await fetch(`${base}/tasks/${taskId}`, { headers: auth })).json();
    assert.equal(detail.source, "node");
  } finally { server.close(); }
}));

test("code repair catalog and request are Node-native immutable queue operations", async () => withToken(async () => {
  const { root, server, base } = await setup();
  try {
    const catalog = await (await fetch(`${base}/code-repair/catalog`, { headers: auth })).json();
    assert.equal(catalog.repositories[0].alias, "demo");
    assert.equal(catalog.credentials_exposed, false);
    const patch = `diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n`;
    const response = await fetch(`${base}/code-repair/requests`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ repository: "demo", unified_diff: patch, summary: "change value" }) });
    assert.equal(response.status, 202);
    const state = await response.json();
    assert.match(state.id, /^repair-[0-9a-f]{16}$/);
    assert.equal(state.status, "queued");
    assert.equal(state.request.patch, undefined);
    const persisted = JSON.parse(await readFile(join(root, "data", "repair-requests", `${state.id}.json`), "utf8"));
    assert.equal(persisted.patch, patch);
    assert.equal(persisted.created_by, "agenelf-node-api");
    const queried = await (await fetch(`${base}/code-repair/requests/${state.id}`, { headers: auth })).json();
    assert.equal(queried.status, "queued");
    assert.equal(queried.request.patch, undefined);
  } finally { server.close(); }
}));

test("migrated routes never proxy and only explicit compatibility allowlist reaches legacy", async () => withToken(async () => {
  const previousLegacy = process.env.AGENELF_LEGACY_API_URL;
  let calls: string[] = [];
  const legacy = createServer((request, response) => {
    calls.push(request.url || "");
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ legacy: true }));
  });
  legacy.listen(0, "127.0.0.1");
  await once(legacy, "listening");
  const legacyAddress = legacy.address();
  if (!legacyAddress || typeof legacyAddress === "string") throw new Error("missing legacy address");
  process.env.AGENELF_LEGACY_API_URL = `http://127.0.0.1:${legacyAddress.port}`;
  const { server, base } = await setup();
  try {
    const roadmap = await fetch(`${base}/self/roadmap`, { headers: auth });
    assert.equal(roadmap.status, 200);
    assert.equal(calls.length, 0);
    const unknown = await fetch(`${base}/legacy-unknown`, { headers: auth });
    assert.equal(unknown.status, 404);
    assert.equal(calls.length, 0);
    const optimization = await fetch(`${base}/self/optimization`, { headers: auth });
    assert.equal(optimization.status, 200);
    assert.deepEqual(calls, ["/self/optimization"]);
    assert.equal(optimization.headers.get("x-agenelf-compatibility"), "legacy-allowlist");
  } finally {
    server.close(); legacy.close();
    if (previousLegacy === undefined) delete process.env.AGENELF_LEGACY_API_URL; else process.env.AGENELF_LEGACY_API_URL = previousLegacy;
  }
}));
