import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { RunEventStream } from "../packages/core/src/agent-events.ts";
import { SessionLedgerStore } from "../packages/core/src/session-ledger.ts";

test("ordinary messages remain redacted by the event core", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-event-redaction-"));
  const stream = new RunEventStream(root, "default");
  const event = await stream.emit("message.delta", { delta: "sk-super-secret-owner-value" });
  assert.equal(event.payload.delta, "sk-[REDACTED]");
  assert.equal(event.transient, true);
});

test("only deterministic owner secret messages can carry transient plaintext", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-direct-secret-event-"));
  const stream = new RunEventStream(root, "default");
  const plaintext = "sk-owner-visible-direct-value";
  const delta = await stream.emit("message.delta", {
    delta: plaintext,
    sensitive: true,
    direct_route: "reveal"
  }, { allowSensitivePayload: true });
  const completed = await stream.emit("message.completed", {
    text: plaintext,
    sensitive: true,
    direct_route: "reveal"
  }, { allowSensitivePayload: true });

  assert.equal(delta.payload.delta, plaintext);
  assert.equal(completed.payload.text, plaintext);
  assert.equal(delta.transient, true);
  assert.equal(completed.transient, true);

  const ledger = new SessionLedgerStore(root);
  assert.deepEqual(await ledger.entries("default", { type: "custom", limit: 20 }), []);
});

test("sensitive event bypass fails closed outside the exact route", async () => {
  const root = await mkdtemp(join(tmpdir(), "agenelf-direct-secret-event-deny-"));
  const stream = new RunEventStream(root, "default");
  await assert.rejects(
    () => stream.emit("tool.completed", {
      result_preview: "sk-must-not-leak",
      sensitive: true,
      direct_route: "reveal"
    }, { allowSensitivePayload: true }),
    /只允许确定性主人 Secret Chat 消息/
  );
  await assert.rejects(
    () => stream.emit("message.completed", {
      text: "sk-must-not-leak",
      sensitive: false,
      direct_route: "reveal"
    }, { allowSensitivePayload: true }),
    /只允许确定性主人 Secret Chat 消息/
  );
  await assert.rejects(
    () => stream.emit("message.completed", {
      text: "sk-must-not-leak",
      sensitive: true,
      direct_route: "reveal"
    }, { allowSensitivePayload: true, origin: "agent_skill" }),
    /只允许确定性主人 Secret Chat 消息/
  );
});
