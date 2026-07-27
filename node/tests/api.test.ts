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
  await writeFile(join(root, "web", "index.html"), "<html>node-ui</html>");
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
