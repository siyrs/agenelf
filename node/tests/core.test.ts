import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sanitizeObject } from "../packages/core/src/privacy.ts";
import { SessionLedgerStore } from "../packages/core/src/session-ledger.ts";
import { RunEventStream, EventCursorExpired, RunAlreadyTerminal } from "../packages/core/src/agent-events.ts";
import { PolicyEngine } from "../packages/core/src/policy.ts";

async function tempRoot() { return mkdtemp(join(tmpdir(), "agenelf-node-test-")); }

test("privacy recursively redacts secrets", () => {
  const value = sanitizeObject({ password: "secret", nested: { token: "abc", text: "Bearer abcdefghijklmnop" } });
  assert.equal(value.password, "[REDACTED]");
  assert.equal((value.nested as Record<string, unknown>).token, "[REDACTED]");
  assert.match(String((value.nested as Record<string, unknown>).text), /Bearer \[REDACTED\]/);
});

test("session ledger branches, verifies and detects tampering", async () => {
  const root = await tempRoot();
  const ledger = new SessionLedgerStore(root);
  const first = await ledger.append({ sessionId: "demo", type: "message", origin: "owner", payload: { role: "user", content: "hello" } });
  const second = await ledger.append({ sessionId: "demo", type: "message", payload: { role: "assistant", content: "hi" } });
  const branch = await ledger.createBranch("demo", first.id, "alternative");
  assert.equal(second.parent_id, first.id);
  assert.match(branch.branch_id, /^br-/);
  assert.equal((await ledger.verify("demo")).integrity, "ok");
  const path = join(root, "local", "memory", "session-ledger", "demo.jsonl");
  const rows = (await readFile(path, "utf8")).trim().split("\n").map(JSON.parse);
  rows[0].payload.content = "changed";
  await writeFile(path, rows.map(JSON.stringify).join("\n") + "\n");
  assert.equal((await ledger.verify("demo")).integrity, "failed");
});

test("event stream persists durable events and enforces terminal", async () => {
  const root = await tempRoot();
  const stream = new RunEventStream(root, "events", "run-0000000000000001", 3);
  await stream.emit("run.started");
  await stream.emit("message.delta", { delta: "a" });
  await stream.emit("message.completed", { text: "a" });
  await stream.emit("run.settled");
  assert.equal(stream.snapshot().last_seq, 4);
  await assert.rejects(() => stream.emit("run.failed"), RunAlreadyTerminal);
  const ledger = new SessionLedgerStore(root);
  assert.equal((await ledger.entries("events", { limit: 20 })).length, 3);
});

test("event cursor expiry is explicit", async () => {
  const root = await tempRoot();
  const stream = new RunEventStream(root, "cursor", "run-0000000000000002", 2);
  await stream.emit("run.started", {}, { transient: true });
  await stream.emit("message.delta", { delta: "1" });
  await stream.emit("message.delta", { delta: "2" });
  assert.throws(() => stream.eventsAfter(0), EventCursorExpired);
});

test("policy is fail closed and preserves trust domains", async () => {
  const root = await tempRoot();
  const policy = new PolicyEngine(root);
  assert.equal(policy.evaluate(null, "agent").allowed, false);
  assert.equal(policy.evaluate({ capability: "x", operation: "read", risk: "read", executionMode: "pure" }, "web").allowed, true);
  assert.equal(policy.evaluate({ capability: "x", operation: "host", risk: "change", executionMode: "host_controlled" }, "web").allowed, false);
  assert.equal(policy.evaluate({ capability: "x", operation: "host", risk: "change", executionMode: "host_controlled" }, "cli").allowed, true);
});
