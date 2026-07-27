import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AgenelfAgent } from "../packages/core/src/agent.ts";
import { SessionLedgerStore } from "../packages/core/src/session-ledger.ts";

async function root() {
  const value = await mkdtemp(join(tmpdir(), "agenelf-agent-test-"));
  await mkdir(join(value, "node", "resources"), { recursive: true });
  return value;
}

test("mock agent completes chat and persists messages/events", async () => {
  const value = await root();
  const agent = new AgenelfAgent(value);
  await agent.initialize();
  const reply = await agent.chat("你好", { sessionId: "chat", subject: "api" });
  assert.match(reply, /Node Runtime/);
  const ledger = new SessionLedgerStore(value);
  const messages = await ledger.entries("chat", { type: "message", limit: 10 });
  assert.equal(messages.length, 2);
  assert.equal((await ledger.verify("chat")).integrity, "ok");
});

test("mock agent executes status tool through policy registry", async () => {
  const value = await root();
  const agent = new AgenelfAgent(value);
  const reply = await agent.chat("请查看 status", { sessionId: "status", subject: "api" });
  assert.match(reply, /已完成工具调用/);
  assert.match(reply, /node-typescript/);
});

test("same session runs are serialized", async () => {
  const value = await root();
  const agent = new AgenelfAgent(value);
  const a = agent.startChat("first", { sessionId: "serial" });
  const b = agent.startChat("second", { sessionId: "serial" });
  await Promise.all([a.completion, b.completion]);
  const messages = await new SessionLedgerStore(value).entries("serial", { type: "message", limit: 10 });
  assert.deepEqual(messages.map((entry) => entry.payload.content), ["first", "Agenelf Node Runtime 已接收：first", "second", "Agenelf Node Runtime 已接收：second"]);
});
