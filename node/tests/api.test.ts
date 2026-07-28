import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { createAgenelfServer } from "../apps/api/src/main.ts";

async function setup() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-api-test-"));
  await mkdir(join(root, "web"), { recursive: true });
  await mkdir(join(root, "local"), { recursive: true });
  await mkdir(join(root, "node", "prompts"), { recursive: true });
  await writeFile(join(root, "web", "index.html"), "<html>node-ui</html>");
  await writeFile(join(root, "node", "prompts", "plan.md"), "---\nname: plan\ndescription: plan test\n---\nPlan this: {{input}}\n");
  await writeFile(join(root, "local", "validation.yaml"), [
    "checks:",
    "  api-health:",
    "    type: http",
    "    description: API health",
    "    url: http://127.0.0.1:1/health",
    "    expected_status: [200]",
    "suites:",
    "  smoke:",
    "    checks:",
    "      - api-health",
    ""
  ].join("\n"));
  const server = await createAgenelfServer({ root });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing address");
  return { root, server, base: `http://127.0.0.1:${address.port}` };
}

test("API fails closed without token", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  const previousInsecure = process.env.AGENELF_API_ALLOW_INSECURE;
  delete process.env.AGENELF_API_TOKEN;
  delete process.env.AGENELF_API_ALLOW_INSECURE;
  const { server, base } = await setup();
  try {
    assert.equal((await fetch(`${base}/health`)).status, 200);
    assert.equal((await fetch(`${base}/status`)).status, 503);
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
    if (previousInsecure === undefined) delete process.env.AGENELF_API_ALLOW_INSECURE; else process.env.AGENELF_API_ALLOW_INSECURE = previousInsecure;
  }
});

test("API supports sync chat and real lifecycle SSE", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "test-token";
  const { server, base } = await setup();
  const headers = { "x-agenelf-token": "test-token", "content-type": "application/json" };
  try {
    const chat = await fetch(`${base}/chat`, { method: "POST", headers, body: JSON.stringify({ message: "hello", session_id: "api" }) });
    assert.equal(chat.status, 200);
    assert.match((await chat.json()).reply, /Node Runtime/);

    const created = await fetch(`${base}/v1/chat/runs`, { method: "POST", headers, body: JSON.stringify({ message: "status", session_id: "events" }) });
    assert.equal(created.status, 202);
    const run = await created.json();
    const events = await fetch(`${base}${run.events}`, { headers: { "x-agenelf-token": "test-token" } });
    const text = await events.text();
    assert.match(text, /event: run.started/);
    assert.match(text, /event: tool.started/);
    assert.match(text, /event: message.completed/);
    assert.match(text, /event: run.settled/);
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});

test("API exposes native prompt templates and validation control plane", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "native-token";
  const { server, base } = await setup();
  const headers = { "x-agenelf-token": "native-token", "content-type": "application/json" };
  try {
    const prompts = await (await fetch(`${base}/prompts`, { headers })).json();
    assert.equal(prompts.prompts[0].command, "/plan");
    const expandedResponse = await fetch(`${base}/prompts/plan/expand`, { method: "POST", headers, body: JSON.stringify({ input: "Node migration" }) });
    assert.equal(expandedResponse.status, 200);
    assert.match((await expandedResponse.json()).prompt, /Plan this: Node migration/);

    const catalog = await (await fetch(`${base}/validation/catalog`, { headers })).json();
    assert.equal(catalog.checks[0].name, "api-health");
    assert.equal(Object.hasOwn(catalog.checks[0], "url"), false);
    const submitted = await fetch(`${base}/validation/checks/api-health`, { method: "POST", headers, body: JSON.stringify({ summary: "native API" }) });
    assert.equal(submitted.status, 202);
    const request = await submitted.json();
    assert.match(request.id, /^val-[0-9a-f]{16}$/);
    const state = await (await fetch(`${base}/validation/results/${request.id}`, { headers })).json();
    assert.equal(state.status, "queued");
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});

test("legacy Web stream endpoint preserves status/message/done compatibility", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "web-stream-token";
  const { server, base } = await setup();
  try {
    const response = await fetch(`${base}/chat/stream`, {
      method: "POST",
      headers: { "x-agenelf-token": "web-stream-token", "content-type": "application/json" },
      body: JSON.stringify({ message: "hello-web", session_id: "web-stream" })
    });
    assert.equal(response.status, 200);
    const text = await response.text();
    assert.match(text, /event: status/);
    assert.match(text, /event: message/);
    assert.match(text, /event: done/);
    assert.doesNotMatch(text, /event: run\.started/);
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});

test("API exposes and clears Node-native session history", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "history-token";
  const { server, base } = await setup();
  const headers = { "x-agenelf-token": "history-token", "content-type": "application/json" };
  try {
    await fetch(`${base}/chat`, { method: "POST", headers, body: JSON.stringify({ message: "history-message", session_id: "history" }) });
    const historyResponse = await fetch(`${base}/chat/history?session_id=history&limit=10`, { headers: { "x-agenelf-token": "history-token" } });
    assert.equal(historyResponse.status, 200);
    const history = await historyResponse.json();
    assert.deepEqual(history.history.map((item: { role: string }) => item.role), ["user", "assistant"]);
    assert.equal(history.history[0].content, "history-message");

    const clearedResponse = await fetch(`${base}/chat/history?session_id=history`, { method: "DELETE", headers: { "x-agenelf-token": "history-token" } });
    assert.equal(clearedResponse.status, 200);
    assert.equal((await clearedResponse.json()).cleared > 0, true);
    const empty = await (await fetch(`${base}/chat/history?session_id=history`, { headers: { "x-agenelf-token": "history-token" } })).json();
    assert.deepEqual(empty.history, []);
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});

test("API proxies unmigrated compatibility routes to internal legacy API", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  const previousLegacy = process.env.AGENELF_LEGACY_API_URL;
  process.env.AGENELF_API_TOKEN = "proxy-token";
  let forwardedToken = "";
  const legacy = (await import("node:http")).createServer((request, response) => {
    forwardedToken = String(request.headers["x-agenelf-token"] || "");
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ roadmap: [{ title: "legacy-compatible" }] }));
  });
  legacy.listen(0, "127.0.0.1");
  await once(legacy, "listening");
  const legacyAddress = legacy.address();
  if (!legacyAddress || typeof legacyAddress === "string") throw new Error("missing legacy address");
  process.env.AGENELF_LEGACY_API_URL = `http://127.0.0.1:${legacyAddress.port}`;
  const { server, base } = await setup();
  try {
    const response = await fetch(`${base}/self/roadmap`, { headers: { "x-agenelf-token": "proxy-token" } });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).roadmap[0].title, "legacy-compatible");
    assert.equal(forwardedToken, "proxy-token");
  } finally {
    server.close(); legacy.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
    if (previousLegacy === undefined) delete process.env.AGENELF_LEGACY_API_URL; else process.env.AGENELF_LEGACY_API_URL = previousLegacy;
  }
});
