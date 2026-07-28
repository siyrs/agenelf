import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { createAgenelfServer } from "../apps/api/src/main.ts";

async function setup(validationText = "") {
  const root = await mkdtemp(join(tmpdir(), "agenelf-api-test-"));
  await mkdir(join(root, "web"), { recursive: true });
  await writeFile(join(root, "web", "index.html"), "<html>node-ui</html>");
  if (validationText) {
    await mkdir(join(root, "local"), { recursive: true });
    await writeFile(join(root, "local", "validation.yaml"), validationText, "utf8");
  }
  const server = await createAgenelfServer({ root });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing address");
  return { root, server, base: `http://127.0.0.1:${address.port}` };
}

const validationConfig = `
checks:
  local-health:
    type: http
    url: http://127.0.0.1:1/health
    expected_status: [200]
suites:
  smoke:
    checks:
      - local-health
`;

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

test("Node-native Validation API exposes aliases and immutable queue requests", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  process.env.AGENELF_API_TOKEN = "validation-token";
  const { server, base } = await setup(validationConfig);
  const headers = { "x-agenelf-token": "validation-token", "content-type": "application/json" };
  try {
    const catalogResponse = await fetch(`${base}/validation/catalog`, { headers });
    assert.equal(catalogResponse.status, 200);
    const catalog = await catalogResponse.json();
    assert.equal(catalog.checks[0].name, "local-health");
    assert.equal(catalog.suites[0].name, "smoke");

    const submit = await fetch(`${base}/validation/checks/local-health`, { method: "POST", headers, body: "{}" });
    assert.equal(submit.status, 202);
    const request = await submit.json();
    assert.match(request.id, /^val-[0-9a-f]{16}$/);
    assert.equal(request.capability, "software.validation");
    assert.equal(request.parameters && Object.keys(request.parameters).length, 0);

    const state = await fetch(`${base}/validation/results/${request.id}`, { headers });
    assert.equal(state.status, 200);
    assert.equal((await state.json()).status, "queued");
  } finally {
    server.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
  }
});

test("Validation API fails closed and never contacts a configured legacy service", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  const previousLegacy = process.env.AGENELF_LEGACY_API_URL;
  process.env.AGENELF_API_TOKEN = "validation-closed-token";
  let legacyCalls = 0;
  const legacy = (await import("node:http")).createServer((_request, response) => {
    legacyCalls += 1;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ checks: ["unsafe-fallback"] }));
  });
  legacy.listen(0, "127.0.0.1");
  await once(legacy, "listening");
  const address = legacy.address();
  if (!address || typeof address === "string") throw new Error("missing legacy address");
  process.env.AGENELF_LEGACY_API_URL = `http://127.0.0.1:${address.port}`;
  const { server, base } = await setup();
  try {
    const response = await fetch(`${base}/validation/catalog`, { headers: { "x-agenelf-token": "validation-closed-token" } });
    assert.equal(response.status, 503);
    assert.match((await response.json()).error, /fail-closed/);
    assert.equal(legacyCalls, 0);
  } finally {
    server.close(); legacy.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
    if (previousLegacy === undefined) delete process.env.AGENELF_LEGACY_API_URL; else process.env.AGENELF_LEGACY_API_URL = previousLegacy;
  }
});

test("optimization and autonomy remain Node-native even when a legacy URL is configured", async () => {
  const previousToken = process.env.AGENELF_API_TOKEN;
  const previousLegacy = process.env.AGENELF_LEGACY_API_URL;
  process.env.AGENELF_API_TOKEN = "native-only-token";
  let calls = 0;
  const legacy = (await import("node:http")).createServer((_request, response) => {
    calls += 1;
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
    const headers = { "x-agenelf-token": "native-only-token" };
    const optimization = await fetch(`${base}/self/optimization`, { headers });
    assert.equal(optimization.status, 200);
    assert.equal((await optimization.json()).consciousness_claim, false);
    const autonomy = await fetch(`${base}/autonomy/cycles`, { headers });
    assert.equal(autonomy.status, 200);
    assert.deepEqual((await autonomy.json()).cycles, []);
    const unknown = await fetch(`${base}/legacy-unknown`, { headers });
    assert.equal(unknown.status, 404);
    assert.equal(calls, 0);
    assert.equal(optimization.headers.get("x-agenelf-compatibility"), null);
  } finally {
    server.close(); legacy.close();
    if (previousToken === undefined) delete process.env.AGENELF_API_TOKEN; else process.env.AGENELF_API_TOKEN = previousToken;
    if (previousLegacy === undefined) delete process.env.AGENELF_LEGACY_API_URL; else process.env.AGENELF_LEGACY_API_URL = previousLegacy;
  }
});
