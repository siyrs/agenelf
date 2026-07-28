import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ApprovalRunner,
  bindingFingerprint,
  bindingFromRequest,
  OwnerApprovalStore
} from "../packages/core/src/owner-approval.ts";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "agenelf-approval-boundary-"));
  const store = new OwnerApprovalStore(root);
  await store.initialize();
  return { root, store, key: Buffer.from("k".repeat(64), "utf8") };
}

async function request(store: OwnerApprovalStore, id: string, target = "demo-api") {
  const value = {
    schema_version: 1,
    id,
    capability: "server.operations",
    operation: "restart_service",
    target,
    parameters: { service: target },
    risk: "change",
    summary: `Restart ${target}`,
    created_at: new Date().toISOString(),
    fingerprint: ""
  };
  value.fingerprint = bindingFingerprint(bindingFromRequest(value));
  await writeFile(join(store.opsRequests, `${id}.json`), `${JSON.stringify(value, null, 2)}\n`);
  return value;
}

test("mismatched duplicate is rejected before any decision is written", async () => {
  const { store } = await fixture();
  const primary = await request(store, "op-1111111111111111", "demo-api");
  const mismatch = await request(store, "op-2222222222222222", "other-api");
  await assert.rejects(
    () => store.applyDecision(primary.id, { action: "approve", duplicates: [mismatch.id], decidedBy: "owner-cli" }),
    /指纹不同/
  );
  await assert.rejects(readFile(join(store.authDecisions, `${primary.id}.json`), "utf8"));
  await assert.rejects(readFile(join(store.authDecisions, `${mismatch.id}.json`), "utf8"));
});

test("unknown command fields and future timestamps fail closed", async () => {
  const { store, key } = await fixture();
  const pending = await request(store, "op-3333333333333333");
  const command = await store.submitCommand(pending.id, key, { action: "approve" });
  const unknown = { ...command, shell: "rm -rf /" };
  await assert.rejects(() => store.verifyCommand(unknown, key), /未知字段/);

  const future = { ...command };
  const futureCreated = new Date(Date.now() + 10 * 60 * 1000);
  future.created_at = futureCreated.toISOString();
  future.expires_at = new Date(futureCreated.getTime() + 60_000).toISOString();
  future.signature = OwnerApprovalStore.signCommand(future, key);
  await assert.rejects(() => store.verifyCommand(future, key), /时钟偏差/);
});

test("short keys and conflicting decisions are rejected", async () => {
  const { store, key } = await fixture();
  const pending = await request(store, "op-4444444444444444");
  await assert.rejects(() => store.submitCommand(pending.id, Buffer.from("short")), /太短/);
  await store.applyDecision(pending.id, { action: "approve", reason: "first", decidedBy: "owner-cli" });
  await assert.rejects(
    () => store.applyDecision(pending.id, { action: "deny", reason: "different", decidedBy: "owner-cli" }),
    /不同裁决/
  );

  const command = await store.submitCommand(
    (await request(store, "op-5555555555555555")).id,
    key,
    { action: "approve" }
  );
  command.request_fingerprint = "0".repeat(64);
  command.signature = OwnerApprovalStore.signCommand(command, key);
  const runner = new ApprovalRunner(store.root, key);
  await runner.initialize();
  await writeFile(join(store.commands, `${String(command.id)}.json`), `${JSON.stringify(command, null, 2)}\n`);
  assert.deepEqual(await runner.processOnce(), { failed: 1 });
  const result = JSON.parse(await readFile(join(store.commandResults, `${String(command.id)}.json`), "utf8"));
  assert.match(result.error, /未绑定当前请求指纹/);
});
